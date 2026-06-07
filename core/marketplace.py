"""
Facebook Marketplace scraper.

Responsibilities:
  - Warm location context by visiting /marketplace before each search
  - Build search URLs from Search config
  - Navigate to Marketplace search results via Playwright
  - Extract listing cards (id, title, price, location, image, condition)
  - Apply filter engine: price range, keywords, negative keywords, condition
  - Return list[Listing] of NEW (unseen) items only

FB Marketplace URL format:
  https://www.facebook.com/marketplace/{zip}/search
    ?query={keywords}
    &minPrice={min}
    &maxPrice={max}
    &exact=false
    &radius={miles_as_km}   <- FB uses km internally even for US searches
    &itemCondition={new|used_like_new|used_good|used_fair|used}
"""

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlencode

from playwright.async_api import Page, TimeoutError as PWTimeoutError

from core.listing import Listing
from db.database import is_seen, mark_seen, upsert_listing

log = logging.getLogger(__name__)

_MI_TO_KM = 1.60934

_CONDITION_MAP = {
    "any": None,
    "new": "new",
    "used_like_new": "used_like_new",
    "used_good": "used_good",
    "used_fair": "used_fair",
    "used": "used_good,used_fair,used_like_new",
}

_ITEM_LINK_RE = re.compile(r"/marketplace/item/(\d+)")

# Track whether we've warmed location this session
_location_warmed: bool = False


def _build_url(search: dict) -> str:
    params: dict = {
        "query": search["keywords"],
        "exact": "false",
    }
    if search.get("price_min") is not None:
        params["minPrice"] = int(search["price_min"])
    if search.get("price_max") is not None:
        params["maxPrice"] = int(search["price_max"])

    distance_km = round(search.get("distance_mi", 40) * _MI_TO_KM)
    params["radius"] = distance_km

    condition_key = search.get("condition", "any")
    condition_val = _CONDITION_MAP.get(condition_key)
    if condition_val:
        params["itemCondition"] = condition_val

    zip_code = search.get("zip_code", "")
    base = f"https://www.facebook.com/marketplace/{zip_code}/search"
    return f"{base}?{urlencode(params)}"


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

    keywords = [k.strip().lower() for k in search["keywords"].split(",") if k.strip()]
    for kw in keywords:
        if not any(word in title_lower for word in kw.split()):
            log.debug("Filter FAIL keyword '%s' not in '%s'", kw, listing.title)
            return False

    neg_raw = search.get("neg_keywords", "")
    if neg_raw:
        neg_keywords = [n.strip().lower() for n in neg_raw.split(",") if n.strip()]
        for neg in neg_keywords:
            if neg in title_lower:
                log.debug("Filter FAIL neg keyword '%s' in '%s'", neg, listing.title)
                return False

    if listing.price is not None:
        if search.get("price_min") and listing.price < search["price_min"]:
            return False
        if search.get("price_max") and listing.price > search["price_max"]:
            return False

    return True


async def _warm_location(page: Page, zip_code: str) -> None:
    """
    Navigate to the Marketplace homepage for this zip code first.
    FB uses this visit to establish the user's location context.
    Without it, the subsequent search URL returns 0 results because
    FB doesn't know where to centre the radius.
    Only runs once per session; subsequent searches skip this step.
    """
    global _location_warmed
    if _location_warmed:
        return

    warmup_url = f"https://www.facebook.com/marketplace/{zip_code}"
    log.info("Warming location context: %s", warmup_url)
    try:
        await page.goto(warmup_url, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(3.0)  # let FB register the location
        _location_warmed = True
        log.info("Location context established for zip %s.", zip_code)
    except PWTimeoutError:
        log.warning("Location warmup timed out — proceeding anyway.")


async def reset_location_warm() -> None:
    """Call this if the session is refreshed so location is re-warmed."""
    global _location_warmed
    _location_warmed = False


async def _scroll_for_listings(page: Page, scrolls: int = 4) -> None:
    """Scroll down incrementally to trigger lazy-loaded cards."""
    for i in range(scrolls):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        await asyncio.sleep(1.5 + (i * 0.25))


async def scrape_search(page: Page, search: dict) -> list[Listing]:
    """
    Run one search and return NEW listings that pass all filters.

    Args:
        page:   Active Playwright page (from session_manager)
        search: Row dict from searches table

    Returns:
        List of new Listing objects (not previously seen, passes filters)
    """
    search_id = search["id"]
    zip_code  = search.get("zip_code", "")

    # Step 1: warm location context (once per session)
    await _warm_location(page, zip_code)

    # Step 2: navigate to the search results
    url = _build_url(search)
    log.info("[Search %d] Scanning: %s", search_id, url)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except PWTimeoutError:
        log.warning("[Search %d] Page load timed out — continuing with partial content.", search_id)

    # Wait for feed + scroll to load lazy cards
    await asyncio.sleep(3.0)
    await _scroll_for_listings(page, scrolls=4)
    await asyncio.sleep(1.5)

    # Log current URL in case FB redirected us (helps diagnose empty results)
    current_url = page.url
    if current_url != url:
        log.info("[Search %d] Redirected to: %s", search_id, current_url)

    # Extract all listing anchor tags
    anchors = await page.query_selector_all("a[href*='/marketplace/item/']")
    log.info("[Search %d] Found %d raw listing links.", search_id, len(anchors))

    if len(anchors) == 0:
        # Log page title to diagnose login expiry or captcha
        title = await page.title()
        log.warning("[Search %d] Zero listings — page title: '%s'", search_id, title)

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

            title = ""
            spans = await anchor.query_selector_all("span[dir='auto']")
            for span in spans:
                text = (await span.inner_text()).strip()
                if text and len(text) > 2:
                    title = text
                    break

            if not title:
                title = "(no title)"

            price: Optional[float] = None
            for span in spans:
                text = (await span.inner_text()).strip()
                if "$" in text or "free" in text.lower():
                    price = _parse_price(text)
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
            await mark_seen(listing_id, search_id)
            new_listings.append(listing)

        except Exception as exc:
            log.warning("[Search %d] Error parsing card: %s", search_id, exc)
            continue

    log.info(
        "[Search %d] Done — %d new, %d already seen.",
        search_id, len(new_listings), seen_count,
    )
    return new_listings
