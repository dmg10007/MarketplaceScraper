"""
Facebook Marketplace scraper — Phase 5 hardening.

Phase 5 additions:
  - Random jitter sleep (2-8s) between requests
  - Human-like scroll simulation before extracting links
  - Proxy support via settings.proxy_url
  - Session expiry detection (redirected to login)
"""

import asyncio
import logging
import random
import re
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import Page, BrowserContext

from config.settings import settings

log = logging.getLogger(__name__)

# ---- Jitter helpers --------------------------------------------------------

async def _jitter(min_s: float = 2.0, max_s: float = 8.0) -> None:
    """Random sleep to mimic human browsing pace."""
    delay = random.uniform(min_s, max_s)
    log.debug("Jitter sleep %.1fs", delay)
    await asyncio.sleep(delay)


async def _human_scroll(page: Page) -> None:
    """Simulate human scroll: slow incremental scrolls then back to top."""
    try:
        scroll_height = await page.evaluate("document.body.scrollHeight")
        viewport_height = await page.evaluate("window.innerHeight")
        current = 0
        while current < min(scroll_height, 3000):
            step = random.randint(300, 700)
            current = min(current + step, scroll_height)
            await page.evaluate(f"window.scrollTo({{top: {current}, behavior: 'smooth'}})")
            await asyncio.sleep(random.uniform(0.3, 0.9))
        # Scroll back to top so links at top are in viewport
        await page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        await asyncio.sleep(0.3)
    except Exception as exc:
        log.debug("Scroll simulation error (non-fatal): %s", exc)


def _is_login_page(url: str) -> bool:
    """Detect if FB redirected us to login."""
    return "login" in url or "checkpoint" in url


# ---- URL builders ----------------------------------------------------------

def _desktop_url(query: str, zip_code: str, max_price: Optional[float], distance_mi: int) -> str:
    q = quote_plus(query)
    url = f"https://www.facebook.com/marketplace/{zip_code}/search?query={q}&radius={distance_mi}"
    if max_price:
        url += f"&maxPrice={int(max_price)}"
    url += "&deliveryMethod=local"
    return url


def _mobile_url(query: str, zip_code: str, max_price: Optional[float], distance_mi: int) -> str:
    q = quote_plus(query)
    url = f"https://m.facebook.com/marketplace/search/?query={q}&radius={distance_mi}&location={zip_code}"
    if max_price:
        url += f"&maxPrice={int(max_price)}"
    return url


# ---- Link extraction -------------------------------------------------------

def _extract_listing_ids(links: list[str]) -> list[str]:
    """Extract unique listing IDs from raw href list."""
    seen: set[str] = set()
    ids: list[str] = []
    for href in links:
        m = re.search(r"/marketplace/item/(\d+)", href)
        if m:
            lid = m.group(1)
            if lid not in seen:
                seen.add(lid)
                ids.append(lid)
    return ids


async def _get_all_links(page: Page) -> list[str]:
    """Wait for marketplace items and return all hrefs."""
    try:
        await page.wait_for_selector('[href*="/marketplace/item/"]', timeout=12_000)
    except Exception:
        pass
    elements = await page.query_selector_all('[href*="/marketplace/item/"]')
    links = []
    for el in elements:
        href = await el.get_attribute("href")
        if href:
            links.append(href)
    return links


# ---- Detail scraping -------------------------------------------------------

async def _scrape_listing_detail(
    page: Page, listing_id: str
) -> dict:
    """Visit listing page and extract structured data."""
    url = f"https://www.facebook.com/marketplace/item/{listing_id}/"
    detail: dict = {
        "id": listing_id,
        "listing_url": url,
        "title": None,
        "price": None,
        "location": None,
        "image_url": None,
        "condition": None,
    }

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await _jitter(1.5, 4.0)

        if _is_login_page(page.url):
            log.warning("[Detail] Session expired — redirected to login for listing %s", listing_id)
            raise SessionExpiredError()

        # Title
        for sel in [
            'h1[data-testid="marketplace-listing-title"]',
            'h1',
            'span[class*="x193iq5w"]',
        ]:
            el = await page.query_selector(sel)
            if el:
                detail["title"] = (await el.inner_text()).strip()
                if detail["title"]:
                    break

        # Price
        for sel in [
            '[data-testid="marketplace-listing-price"]',
            'span[class*="x193iq5w"]:has-text("$")',
        ]:
            el = await page.query_selector(sel)
            if el:
                txt = await el.inner_text()
                m = re.search(r'[\d,]+', txt.replace(',', ''))
                if m:
                    detail["price"] = float(m.group().replace(',', ''))
                    break

        # Image — OG tag is most reliable
        el = await page.query_selector('meta[property="og:image"]')
        if el:
            detail["image_url"] = await el.get_attribute("content")
        else:
            img = await page.query_selector('img[src*="scontent"]')
            if img:
                detail["image_url"] = await img.get_attribute("src")

        # Location
        for sel in [
            '[aria-label*="Location"]',
            'span[class*="x193iq5w"]:has-text(", ")',
        ]:
            el = await page.query_selector(sel)
            if el:
                detail["location"] = (await el.inner_text()).strip()
                break

    except SessionExpiredError:
        raise
    except Exception as exc:
        log.debug("[Detail] Error scraping %s: %s", listing_id, exc)

    return detail


class SessionExpiredError(Exception):
    pass


# ---- Main search function --------------------------------------------------

async def search_marketplace(
    page: Page,
    search_id: int,
    search_name: str,
    keywords: str,
    zip_code: str,
    price_min: Optional[float],
    price_max: Optional[float],
    distance_mi: int,
    neg_keywords: list[str],
    condition: str,
    is_seen_fn,
    mark_seen_fn,
    upsert_listing_fn,
) -> list[dict]:
    """
    Run one marketplace search. Returns list of new unseen listings.
    Raises SessionExpiredError if FB redirects to login.
    """
    desktop_url = _desktop_url(keywords, zip_code, price_max, distance_mi)
    mobile_url  = _mobile_url(keywords, zip_code, price_max, distance_mi)
    new_listings: list[dict] = []

    for attempt, (label, url) in enumerate([("desktop", desktop_url), ("mobile", mobile_url)]):
        if attempt > 0:
            await _jitter(3.0, 7.0)  # extra pause before mobile fallback

        log.info("[Search %d] Trying %s URL: %s", search_id, label, url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:
            log.warning("[Search %d] Navigation error (%s): %s", search_id, label, exc)
            continue

        await _jitter(2.0, 5.0)

        if _is_login_page(page.url):
            log.warning("[Search %d] Session expired — redirected to login.", search_id)
            raise SessionExpiredError()

        # Human-like scroll before extracting
        await _human_scroll(page)

        raw_links = await _get_all_links(page)
        listing_ids = _extract_listing_ids(raw_links)
        log.info("[Search %d] Found %d raw item links on page.", search_id, len(raw_links))

        # Screenshot on zero results
        if not listing_ids:
            try:
                path = f"data/debug_{search_id}_{label}.png"
                await page.screenshot(path=path, full_page=False)
                log.warning("Screenshot saved → %s  (open this to see what FB showed)", path)
            except Exception:
                pass
            continue

        # Filter unseen
        new_ids = []
        for lid in listing_ids:
            if not await is_seen_fn(lid, search_id):
                new_ids.append(lid)

        already_seen = len(listing_ids) - len(new_ids)
        log.info(
            "[Search %d] Results — new=%d  already_seen=%d",
            search_id, len(new_ids), already_seen,
        )

        if not new_ids:
            log.info("[Search %d] No new listings on %s.", search_id, label)
            break  # results are cached; mobile won't differ

        # Scrape details for each new listing
        for lid in new_ids:
            await _jitter(1.5, 4.5)
            try:
                detail = await _scrape_listing_detail(page, lid)
            except SessionExpiredError:
                raise
            except Exception as exc:
                log.warning("[Search %d] Could not scrape detail for %s: %s", search_id, lid, exc)
                detail = {"id": lid, "listing_url": f"https://www.facebook.com/marketplace/item/{lid}/",
                          "title": None, "price": None, "location": None,
                          "image_url": None, "condition": None}

            # Apply filters
            title_lower = (detail.get("title") or "").lower()
            if neg_keywords and any(nk in title_lower for nk in neg_keywords):
                log.info("[Search %d] Filtered out %s (neg keyword match)", search_id, lid)
                await mark_seen_fn(lid, search_id)
                continue
            if price_min and detail.get("price") and detail["price"] < price_min:
                log.info("[Search %d] Filtered out %s (price below min)", search_id, lid)
                await mark_seen_fn(lid, search_id)
                continue

            await upsert_listing_fn(
                lid, search_id,
                detail.get("title"), detail.get("listing_url"),
                detail.get("price"), detail.get("location"),
                detail.get("image_url"), detail.get("condition"),
            )
            new_listings.append(detail)

        break  # success — don't try mobile if desktop worked

    return new_listings
