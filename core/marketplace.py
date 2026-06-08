"""
Facebook Marketplace scraper.

Strategy:
  1. Try desktop FB (www.facebook.com/marketplace/search) first.
  2. If zero results, fall back to mobile FB (m.facebook.com).

Phase 5 hardening:
  - Random jitter (2-8s) between navigations
  - Human-like scroll simulation before parsing
  - Session expiry detection via page title
  - Proxy support via PROXY_URL in .env
"""

import asyncio
import logging
import random
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, quote_plus

from playwright.async_api import Page, TimeoutError as PWTimeoutError

from core.listing import Listing
from db.database import is_seen, mark_seen, upsert_listing

log = logging.getLogger(__name__)

_CONDITION_MAP = {
    "any": None,
    "new": "new",
    "used_like_new": "used_like_new",
    "used_good": "used_good",
    "used_fair": "used_fair",
    "used": "used_good,used_fair,used_like_new",
}

_ITEM_LINK_RE = re.compile(r"/marketplace/item/(\d+)")
_SCREENSHOT_PATH = Path("data/debug_screenshot.png")
_location_warmed: bool = False


def _build_desktop_url(search: dict) -> str:
    """Desktop FB Marketplace search URL."""
    zip_code = search.get("zip_code", "")
    keywords = search["keywords"]
    radius = int(search.get("distance_mi", 40))

    params: dict = {}
    if search.get("price_min") is not None:
        params["minPrice"] = int(search["price_min"])
    if search.get("price_max") is not None:
        params["maxPrice"] = int(search["price_max"])
    params["deliveryMethod"] = "local"
    condition_val = _CONDITION_MAP.get(search.get("condition", "any"))
    if condition_val:
        params["itemCondition"] = condition_val

    qs = urlencode(params)
    location_segment = zip_code if zip_code else "109550"
    return (
        f"https://www.facebook.com/marketplace/{location_segment}/search"
        f"?query={quote_plus(keywords)}&radius={radius}&{qs}"
    ).rstrip("&")


def _build_mobile_url(search: dict) -> str:
    """Mobile FB Marketplace search URL (fallback)."""
    params: dict = {"query": search["keywords"]}
    if search.get("price_min") is not None:
        params["minPrice"] = int(search["price_min"])
    if search.get("price_max") is not None:
        params["maxPrice"] = int(search["price_max"])
    params["radius"] = int(search.get("distance_mi", 40))
    condition_val = _CONDITION_MAP.get(search.get("condition", "any"))
    if condition_val:
        params["itemCondition"] = condition_val
    if search.get("zip_code"):
        params["location"] = search["zip_code"]
    return f"https://m.facebook.com/marketplace/search/?{urlencode(params)}"


def _extract_id_from_url(href: str) -> Optional[str]:
    m = _ITEM_LINK_RE.search(href)
    return m.group(1) if m else None


def _parse_price(text: str) -> Optional[float]:
    text = text.replace(",", "")
    m = re.search(r"\$(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1))
    if "free" in text.lower():
        return 0.0
    return None


def _passes_filter(listing: Listing, search: dict) -> bool:
    title_lower = listing.title.lower()
    # Keyword check: every comma-separated keyword must appear somewhere in title
    for kw in [k.strip().lower() for k in search["keywords"].split(",") if k.strip()]:
        if not any(word in title_lower for word in kw.split()):
            return False
    # Negative keyword check
    neg_raw = search.get("neg_keywords", "")
    if neg_raw:
        for neg in [n.strip().lower() for n in neg_raw.split(",") if n.strip()]:
            if neg in title_lower:
                log.debug("Filtered out '%s' — matched neg keyword '%s'", listing.title, neg)
                return False
    # Price check
    if listing.price is not None:
        if search.get("price_min") and listing.price < search["price_min"]:
            return False
        if search.get("price_max") and listing.price > search["price_max"]:
            return False
    return True


async def _jitter(short: bool = False) -> None:
    lo, hi = (0.5, 2.0) if short else (2.0, 8.0)
    await asyncio.sleep(random.uniform(lo, hi))


async def _human_scroll(page: Page) -> None:
    try:
        for _ in range(random.randint(3, 6)):
            dist = random.randint(300, 700)
            await page.evaluate(f"window.scrollBy(0, {dist})")
            await asyncio.sleep(random.uniform(0.4, 1.2))
        await page.evaluate("window.scrollBy(0, -200)")
    except Exception:
        pass


async def _session_still_valid(page: Page) -> bool:
    try:
        title = (await page.title()).lower()
        return not any(b in title for b in ("log in", "log into", "checkpoint"))
    except Exception:
        return True


async def _save_debug_screenshot(page: Page, label: str) -> None:
    try:
        _SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        path = Path(f"data/debug_{label}.png")
        await page.screenshot(path=str(path), full_page=True)
        log.warning("Screenshot saved → %s  (open this to see what FB showed)", path)
    except Exception as exc:
        log.warning("Screenshot failed: %s", exc)


async def _try_load_page(page: Page, url: str, search_id: int) -> bool:
    """Navigate and return True if the page loaded without hard timeout."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        return True
    except PWTimeoutError:
        log.warning("[Search %d] Page load timed out: %s", search_id, url)
        return False


async def _extract_listings(page: Page, search: dict) -> list[Listing]:
    """
    Parse anchor tags from the current page.
    Returns only NEW listings that pass the filter.
    Already-seen listings are silently skipped.
    Filtered-out (neg kw / price) listings are marked seen immediately.
    """
    search_id = search["id"]
    anchors = await page.query_selector_all("a[href*='/marketplace/item/']")
    log.info("[Search %d] Found %d raw item links on page.", search_id, len(anchors))

    if len(anchors) == 0:
        title = await page.title()
        log.warning(
            "[Search %d] Zero item links — page title: '%s'",
            search_id, title,
        )
        try:
            snippet = await page.inner_text("body")
            log.warning("[Search %d] Body snippet: %s", search_id, snippet[:600].replace("\n", " "))
        except Exception:
            pass
        return []

    new_listings: list[Listing] = []
    seen_count = 0
    filtered_count = 0

    for anchor in anchors:
        try:
            href = await anchor.get_attribute("href") or ""
            listing_id = _extract_id_from_url(href)
            if not listing_id:
                continue

            if await is_seen(listing_id, search_id):
                seen_count += 1
                continue

            listing_url = (
                href if href.startswith("http")
                else f"https://www.facebook.com{href.split('?')[0]}"
            )

            anchor_text = await anchor.inner_text()
            lines = [l.strip() for l in anchor_text.strip().splitlines() if l.strip()]
            title = lines[0] if lines else "(no title)"

            price: Optional[float] = None
            for line in lines:
                p = _parse_price(line)
                if p is not None:
                    price = p
                    break

            image_url: Optional[str] = None
            img = await anchor.query_selector("img")
            if img:
                image_url = await img.get_attribute("src")

            listing = Listing(
                id=listing_id,
                title=title,
                listing_url=listing_url,
                search_id=search_id,
                price=price,
                image_url=image_url,
            )

            log.debug(
                "[Search %d] Candidate: id=%s title='%s' price=%s",
                search_id, listing_id, title, price,
            )

            if not _passes_filter(listing, search):
                filtered_count += 1
                await mark_seen(listing_id, search_id)
                continue

            await upsert_listing(
                listing_id=listing.id,
                search_id=search_id,
                title=listing.title,
                listing_url=listing.listing_url,
                price=listing.price,
                location=listing.location,
                image_url=listing.image_url,
                condition=listing.condition,
            )
            new_listings.append(listing)
            await _jitter(short=True)

        except Exception as exc:
            log.warning("[Search %d] Error parsing card: %s", search_id, exc)
            continue

    log.info(
        "[Search %d] Results — new=%d  already_seen=%d  filtered_out=%d",
        search_id, len(new_listings), seen_count, filtered_count,
    )
    return new_listings


async def reset_location_warm() -> None:
    global _location_warmed
    _location_warmed = False


async def scrape_search(page: Page, search: dict) -> list[Listing]:
    search_id = search["id"]

    if not await _session_still_valid(page):
        log.warning("[Search %d] Session appears expired — skipping scan.", search_id)
        return []

    # --- Strategy 1: Desktop FB ---
    desktop_url = _build_desktop_url(search)
    log.info("[Search %d] Trying desktop URL: %s", search_id, desktop_url)
    loaded = await _try_load_page(page, desktop_url, search_id)
    if loaded:
        await _human_scroll(page)
        listings = await _extract_listings(page, search)
        if listings:
            return listings
        # If 0 results from desktop, check if it's a session/load issue
        title = await page.title()
        log.info("[Search %d] Desktop returned 0 new listings (title='%s'). Trying mobile...", search_id, title)
        await _save_debug_screenshot(page, f"{search_id}_desktop")

    # --- Strategy 2: Mobile FB fallback ---
    mobile_url = _build_mobile_url(search)
    log.info("[Search %d] Trying mobile fallback URL: %s", search_id, mobile_url)
    loaded = await _try_load_page(page, mobile_url, search_id)
    if loaded:
        await _jitter()
        await _human_scroll(page)
        listings = await _extract_listings(page, search)
        if listings:
            return listings
        await _save_debug_screenshot(page, f"{search_id}_mobile")

    log.warning(
        "[Search %d] Both desktop and mobile returned 0 new listings. "
        "Check data/debug_%d_desktop.png and data/debug_%d_mobile.png",
        search_id, search_id, search_id,
    )
    return []
