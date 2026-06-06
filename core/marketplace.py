"""
Facebook Marketplace scraper.

Responsibilities:
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

# Miles → km conversion (FB radius param is in km)
_MI_TO_KM = 1.60934

# Condition map: our internal names → FB URL param values
_CONDITION_MAP = {
    "any": None,
    "new": "new",
    "used_like_new": "used_like_new",
    "used_good": "used_good",
    "used_fair": "used_fair",
    "used": "used_good,used_fair,used_like_new",  # any used
}

# Selectors — FB uses minified/dynamic class names so we target
# structural/ARIA attributes that are stable across deploys.
_LISTING_CONTAINER = "div[data-testid='marketplace_search_feed']"
_LISTING_CARDS     = "div[data-testid='marketplace_search_feed'] > div > div > div > a[href*='/marketplace/item/']"
_CARD_TITLE        = "span[dir='auto']"
_CARD_PRICE        = "span[dir='auto']"

# Fallback: grab all anchor tags pointing to marketplace items
_ITEM_LINK_RE = re.compile(r"/marketplace/item/(\d+)")


def _build_url(search: dict) -> str:
    """Construct the FB Marketplace search URL from a search config dict."""
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
    """Extract first numeric price from a string like '$1,200' or 'Free'."""
    text = text.replace(",", "")
    m = re.search(r"\$(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1))
    if "free" in text.lower():
        return 0.0
    return None


def _passes_filter(listing: Listing, search: dict) -> bool:
    """Return True if listing passes all search criteria."""
    title_lower = listing.title.lower()

    # Keyword match (all keywords must appear in title)
    keywords = [k.strip().lower() for k in search["keywords"].split(",") if k.strip()]
    for kw in keywords:
        # Allow multi-word keywords as phrase match
        if not any(word in title_lower for word in kw.split()):
            log.debug("Filter FAIL keyword '%s' not in '%s'", kw, listing.title)
            return False

    # Negative keywords (any match = reject)
    neg_raw = search.get("neg_keywords", "")
    if neg_raw:
        neg_keywords = [n.strip().lower() for n in neg_raw.split(",") if n.strip()]
        for neg in neg_keywords:
            if neg in title_lower:
                log.debug("Filter FAIL neg keyword '%s' in '%s'", neg, listing.title)
                return False

    # Price range
    if listing.price is not None:
        if search.get("price_min") and listing.price < search["price_min"]:
            return False
        if search.get("price_max") and listing.price > search["price_max"]:
            return False

    return True


async def _scroll_for_listings(page: Page, scrolls: int = 3) -> None:
    """Scroll down to load more cards — simulates human browsing."""
    for i in range(scrolls):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        await asyncio.sleep(1.5 + (i * 0.3))  # increasing jitter


async def scrape_search(page: Page, search: dict) -> list[Listing]:
    """
    Run one search and return NEW listings that pass all filters.

    Args:
        page:   Active Playwright page (from session_manager)
        search: Row dict from searches table

    Returns:
        List of new Listing objects (not previously seen, passes filters)
    """
    url = _build_url(search)
    search_id = search["id"]
    log.info("[Search %d] Scanning: %s", search_id, url)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except PWTimeoutError:
        log.warning("[Search %d] Page load timed out — continuing with partial content.", search_id)

    # Brief pause then scroll to trigger lazy-loaded cards
    await asyncio.sleep(2.5)
    await _scroll_for_listings(page, scrolls=3)
    await asyncio.sleep(1.0)

    # Extract all listing anchor tags
    anchors = await page.query_selector_all("a[href*='/marketplace/item/']") 
    log.info("[Search %d] Found %d raw listing links.", search_id, len(anchors))

    new_listings: list[Listing] = []
    seen_count = 0

    for anchor in anchors:
        try:
            href = await anchor.get_attribute("href") or ""
            listing_id = _extract_id_from_url(href)
            if not listing_id:
                continue

            # Deduplication check
            if await is_seen(listing_id, search_id):
                seen_count += 1
                continue

            # Build full URL
            listing_url = (
                href if href.startswith("http")
                else f"https://www.facebook.com{href.split('?')[0]}"
            )

            # Extract title — first non-empty span with dir='auto' inside the card
            title = ""
            spans = await anchor.query_selector_all("span[dir='auto']")
            for span in spans:
                text = (await span.inner_text()).strip()
                if text and len(text) > 2:
                    title = text
                    break

            if not title:
                title = "(no title)"

            # Extract price — look for spans containing '$'
            price: Optional[float] = None
            for span in spans:
                text = (await span.inner_text()).strip()
                if "$" in text or "free" in text.lower():
                    price = _parse_price(text)
                    break

            # Extract image
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

            # Apply filter engine
            if not _passes_filter(listing, search):
                await mark_seen(listing_id, search_id)  # mark so we don't re-check
                continue

            # Persist to DB
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
