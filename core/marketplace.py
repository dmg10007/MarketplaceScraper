"""
Facebook Marketplace scraper — mobile site strategy.

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
from urllib.parse import urlencode

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


def _build_url(search: dict) -> str:
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
    for kw in [k.strip().lower() for k in search["keywords"].split(",") if k.strip()]:
        if not any(word in title_lower for word in kw.split()):
            return False
    neg_raw = search.get("neg_keywords", "")
    if neg_raw:
        for neg in [n.strip().lower() for n in neg_raw.split(",") if n.strip()]:
            if neg in title_lower:
                return False
    if listing.price is not None:
        if search.get("price_min") and listing.price < search["price_min"]:
            return False
        if search.get("price_max") and listing.price > search["price_max"]:
            return False
    return True


async def _jitter(short: bool = False) -> None:
    """Phase 5: random delay to mimic human browsing pace."""
    lo, hi = (0.5, 2.0) if short else (2.0, 8.0)
    await asyncio.sleep(random.uniform(lo, hi))


async def _human_scroll(page: Page) -> None:
    """Phase 5: scroll down in steps to trigger lazy-loaded content."""
    try:
        for _ in range(random.randint(3, 6)):
            dist = random.randint(300, 700)
            await page.evaluate(f"window.scrollBy(0, {dist})")
            await asyncio.sleep(random.uniform(0.4, 1.2))
        await page.evaluate("window.scrollBy(0, -200)")
    except Exception:
        pass


async def _session_still_valid(page: Page) -> bool:
    """Phase 5: detect FB logout / checkpoint via page title."""
    try:
        title = (await page.title()).lower()
        return not any(b in title for b in ("log in", "log into", "checkpoint"))
    except Exception:
        return True


async def _warm_location(page: Page) -> None:
    global _location_warmed
    if _location_warmed:
        return
    log.info("Warming mobile Marketplace session...")
    try:
        await page.goto(
            "https://m.facebook.com/marketplace/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await _jitter()
        _location_warmed = True
        log.info("Mobile Marketplace session warmed.")
    except PWTimeoutError:
        log.warning("Warmup timed out — proceeding anyway.")
        _location_warmed = True


async def reset_location_warm() -> None:
    global _location_warmed
    _location_warmed = False


async def _save_debug_screenshot(page: Page, search_id: int) -> None:
    try:
        _SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(_SCREENSHOT_PATH), full_page=True)
        log.warning("[Search %d] Screenshot -> %s", search_id, _SCREENSHOT_PATH)
    except Exception as exc:
        log.warning("[Search %d] Screenshot failed: %s", search_id, exc)


async def scrape_search(page: Page, search: dict) -> list[Listing]:
    """
    Scrape and return NEW listings that passed filtering.
    mark_seen() is NOT called for new listings here — scheduler calls it
    after a successful alert. Filtered-out listings are marked seen immediately.
    """
    search_id = search["id"]
    await _warm_location(page)

    if not await _session_still_valid(page):
        log.warning("[Search %d] Session appears expired — skipping scan.", search_id)
        return []

    url = _build_url(search)
    log.info("[Search %d] Scanning (mobile): %s", search_id, url)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except PWTimeoutError:
        log.warning("[Search %d] Page load timed out — continuing.", search_id)

    await _human_scroll(page)

    anchors = await page.query_selector_all("a[href*='/marketplace/item/']")
    log.info("[Search %d] Found %d raw listing links.", search_id, len(anchors))

    if len(anchors) == 0:
        title = await page.title()
        log.warning("[Search %d] Zero listings — page title: '%s'", search_id, title)
        try:
            body_text = await page.inner_text("body")
            log.warning("[Search %d] Snippet: %s", search_id, body_text[:500].replace("\n", " "))
        except Exception:
            pass
        await _save_debug_screenshot(page, search_id)
        return []

    new_listings: list[Listing] = []
    seen_count = 0

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
            title = anchor_text.strip().split("\n")[0]
            if not title or len(title) < 2:
                title = "(no title)"

            price: Optional[float] = None
            price_match = re.search(r"\$(\d[\d,]*(?:\.\d+)?)", anchor_text)
            if price_match:
                price = _parse_price(price_match.group(0))
            elif "free" in anchor_text.lower():
                price = 0.0

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

            if not _passes_filter(listing, search):
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
            # DO NOT mark_seen here — scheduler does it after alert succeeds
            new_listings.append(listing)
            await _jitter(short=True)

        except Exception as exc:
            log.warning("[Search %d] Error parsing card: %s", search_id, exc)
            continue

    log.info("[Search %d] Done — %d new, %d already seen.", search_id, len(new_listings), seen_count)
    return new_listings
