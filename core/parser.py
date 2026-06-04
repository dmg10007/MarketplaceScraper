"""
DOM parser — extracts structured Listing objects from a Marketplace search page.

Facebook uses minified, randomised class names that change frequently.
This parser deliberately avoids class-name selectors. Instead it targets:
  1. ARIA roles and labels
  2. Data attributes
  3. Structural position (nth-child relationships)
  4. URL patterns for listing IDs

If Facebook updates its layout and this parser breaks, the fix is always
in the selector constants at the top of this file — no logic changes needed.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, urlencode, quote

from playwright.async_api import Page

from core.scraper import session_manager

log = logging.getLogger(__name__)

FB_BASE = "https://www.facebook.com"

# ---------------------------------------------------------------------------
# Selector constants — update these if FB changes its DOM
# ---------------------------------------------------------------------------

# Each Marketplace listing card in a search result
# Facebook wraps cards in <a> tags whose href contains /marketplace/item/
SEL_LISTING_LINK = "a[href*='/marketplace/item/']"

# Inside a card: price text is typically in an element with aria or structural role
SEL_PRICE = "[aria-label*='$'], span:has-text('$')"

# Listing title — the first meaningful text span inside the card
SEL_TITLE = "span[dir='auto']"

# Listing image
SEL_IMAGE = "img[referrerpolicy='origin-when-cross-origin']"

# Location text — appears as a secondary span below the price
SEL_LOCATION = "span[dir='auto'] ~ span"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    id: str
    title: str
    listing_url: str
    price: Optional[float] = None
    price_raw: Optional[str] = None
    location: Optional[str] = None
    image_url: Optional[str] = None
    condition: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "listing_url": self.listing_url,
            "price": self.price,
            "price_raw": self.price_raw,
            "location": self.location,
            "image_url": self.image_url,
            "condition": self.condition,
        }


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

def build_search_url(
    keywords: str,
    zip_code: str,
    distance_mi: int = 40,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    condition: str = "any",
) -> str:
    """
    Build a Facebook Marketplace search URL from search parameters.

    Example output:
    https://www.facebook.com/marketplace/
        ?query=road+bike&deliveryMethod=local
        &minPrice=50&maxPrice=300&radius=64
    """
    params: dict = {
        "query": keywords,
        "deliveryMethod": "local",
        # FB uses km internally; 1 mile ≈ 1.60934 km
        "radius": str(round(distance_mi * 1.60934)),
    }
    if price_min is not None:
        params["minPrice"] = str(int(price_min))
    if price_max is not None:
        params["maxPrice"] = str(int(price_max))
    if condition and condition != "any":
        # FB accepted values: 'new', 'used_like_new', 'used_good', 'used_fair'
        params["itemCondition"] = condition

    # FB Marketplace uses /marketplace/<zip>/ prefix for location context
    base = f"{FB_BASE}/marketplace/{zip_code}/"
    return base + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

async def scrape_search(search: dict) -> list[Listing]:
    """
    Navigate to the search URL and extract all visible listing cards.

    Args:
        search: A searches row dict from the database.

    Returns:
        List of Listing objects found on the page.
    """
    url = build_search_url(
        keywords=search["keywords"],
        zip_code=search["zip_code"],
        distance_mi=search["distance_mi"],
        price_min=search.get("price_min"),
        price_max=search.get("price_max"),
        condition=search.get("condition", "any"),
    )

    log.info("[Search %d] Scraping: %s", search["id"], url)

    try:
        page = await session_manager.navigate(url)
        await _scroll_to_load(page)
        listings = await _extract_listings(page)
        log.info("[Search %d] Found %d listing(s)", search["id"], len(listings))
        return listings
    except Exception as exc:
        log.error("[Search %d] Scrape failed: %s", search["id"], exc, exc_info=True)
        return []


async def _scroll_to_load(page: Page, scrolls: int = 3) -> None:
    """
    Scroll down incrementally to trigger lazy-loaded listing cards.
    Facebook loads more items as you scroll.
    """
    import asyncio, random
    for _ in range(scrolls):
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight * 0.6)")
        await asyncio.sleep(random.uniform(1.5, 3.0))


async def _extract_listings(page: Page) -> list[Listing]:
    """Pull all listing cards from the current page state."""
    listings: list[Listing] = []

    # Get all <a> elements that point to a marketplace item
    card_elements = await page.query_selector_all(SEL_LISTING_LINK)

    seen_ids: set[str] = set()

    for el in card_elements:
        try:
            href = await el.get_attribute("href") or ""
            listing_id = _extract_id_from_url(href)
            if not listing_id or listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)

            full_url = href if href.startswith("http") else FB_BASE + href

            # Strip tracking params from URL
            full_url = full_url.split("?")[0]

            title = await _extract_text(el, SEL_TITLE)
            price_raw = await _extract_price_raw(el)
            price = _parse_price(price_raw)
            location = await _extract_location(el)
            image_url = await _extract_image(el)

            if not title:
                continue  # Skip cards that didn't parse properly

            listings.append(
                Listing(
                    id=listing_id,
                    title=title.strip(),
                    listing_url=full_url,
                    price=price,
                    price_raw=price_raw,
                    location=location,
                    image_url=image_url,
                )
            )
        except Exception as exc:
            log.debug("Failed to parse listing card: %s", exc)
            continue

    return listings


# ---------------------------------------------------------------------------
# Element-level extractors
# ---------------------------------------------------------------------------

def _extract_id_from_url(href: str) -> Optional[str]:
    """Pull the numeric listing ID from a Marketplace item URL."""
    # URLs look like: /marketplace/item/1234567890/
    match = re.search(r"/marketplace/item/(\d+)", href)
    return match.group(1) if match else None


async def _extract_text(el, selector: str) -> Optional[str]:
    child = await el.query_selector(selector)
    if child:
        return await child.inner_text()
    return None


async def _extract_price_raw(el) -> Optional[str]:
    """Try multiple strategies to get price text from a card."""
    # Strategy 1: aria-label containing '$'
    child = await el.query_selector("[aria-label*='$']")
    if child:
        label = await child.get_attribute("aria-label")
        if label:
            return label

    # Strategy 2: any span containing '$'
    spans = await el.query_selector_all("span")
    for span in spans:
        text = await span.inner_text()
        if text and "$" in text and len(text) < 20:
            return text.strip()

    return None


def _parse_price(raw: Optional[str]) -> Optional[float]:
    """Convert raw price string like '$1,250' or 'C$450' to a float."""
    if not raw:
        return None
    digits = re.sub(r"[^\d.]", "", raw)
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


async def _extract_location(el) -> Optional[str]:
    """Location is typically the second span[dir='auto'] inside the card."""
    spans = await el.query_selector_all("span[dir='auto']")
    # First span = title, second span = location
    if len(spans) >= 2:
        text = await spans[1].inner_text()
        return text.strip() if text else None
    return None


async def _extract_image(el) -> Optional[str]:
    img = await el.query_selector(SEL_IMAGE)
    if img:
        return await img.get_attribute("src")
    # Fallback: any img inside the card
    img = await el.query_selector("img")
    if img:
        return await img.get_attribute("src")
    return None
