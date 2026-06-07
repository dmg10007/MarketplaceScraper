"""
Facebook Marketplace scraper.

URL strategy:
  Uses /marketplace/category/search with location= zip param.
  Waits for networkidle, then scrolls the RIGHT-SIDE results pane
  (not window) to trigger lazy-loading of listing cards.
  Saves a debug screenshot to data/debug_screenshot.png on zero results.
"""

import asyncio
import logging
import re
from pathlib import Path
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
_CARD_SELECTOR = "a[href*='/marketplace/item/']"
_SCREENSHOT_PATH = Path("data/debug_screenshot.png")
_location_warmed: bool = False

# JS that finds the scrollable results pane and scrolls it.
# FB puts listings in a div with overflow:auto/scroll to the RIGHT of the
# filter sidebar. We find the first deeply-scrollable div that isn't the
# sidebar itself (scrollWidth roughly equal to clientWidth, but scrollHeight
# larger than clientHeight) and scroll it. Falls back to window.scrollBy.
_SCROLL_JS = """
(async (scrolls, delay) => {
    // Find the scrollable results container.
    // It will be a div with overflow scroll/auto whose scrollHeight > clientHeight.
    const allDivs = Array.from(document.querySelectorAll('div'));
    let pane = null;
    for (const d of allDivs) {
        const st = window.getComputedStyle(d);
        const overflow = st.overflowY;
        if ((overflow === 'auto' || overflow === 'scroll') &&
            d.scrollHeight > d.clientHeight + 100 &&
            d.clientHeight > 300) {   // tall enough to be the main results pane
            pane = d;
            break;
        }
    }
    const target = pane || window;
    for (let i = 0; i < scrolls; i++) {
        if (target === window) {
            window.scrollBy(0, window.innerHeight * 0.8);
        } else {
            target.scrollBy(0, target.clientHeight * 0.8);
        }
        await new Promise(r => setTimeout(r, delay + i * 200));
    }
    return pane ? 'pane' : 'window';
})
"""


def _build_url(search: dict) -> str:
    params: dict = {
        "query":    search["keywords"],
        "exact":    "false",
        "location": search.get("zip_code", ""),
    }
    if search.get("price_min") is not None:
        params["minPrice"] = int(search["price_min"])
    if search.get("price_max") is not None:
        params["maxPrice"] = int(search["price_max"])

    distance_km = round(search.get("distance_mi", 40) * _MI_TO_KM)
    params["radius"] = distance_km

    condition_val = _CONDITION_MAP.get(search.get("condition", "any"))
    if condition_val:
        params["itemCondition"] = condition_val

    return f"https://www.facebook.com/marketplace/category/search?{urlencode(params)}"


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
            log.debug("Filter FAIL keyword '%s' not in '%s'", kw, listing.title)
            return False

    neg_raw = search.get("neg_keywords", "")
    if neg_raw:
        for neg in [n.strip().lower() for n in neg_raw.split(",") if n.strip()]:
            if neg in title_lower:
                log.debug("Filter FAIL neg keyword '%s' in '%s'", neg, listing.title)
                return False

    if listing.price is not None:
        if search.get("price_min") and listing.price < search["price_min"]:
            return False
        if search.get("price_max") and listing.price > search["price_max"]:
            return False

    return True


async def _warm_location(page: Page) -> None:
    global _location_warmed
    if _location_warmed:
        return
    log.info("Warming Marketplace session...")
    try:
        await page.goto(
            "https://www.facebook.com/marketplace/",
            wait_until="networkidle",
            timeout=60_000,
        )
        await asyncio.sleep(2.0)
        _location_warmed = True
        log.info("Marketplace session warmed.")
    except PWTimeoutError:
        log.warning("Location warmup timed out — proceeding anyway.")
        _location_warmed = True


async def reset_location_warm() -> None:
    global _location_warmed
    _location_warmed = False


async def _scroll_results_pane(page: Page, search_id: int, scrolls: int = 6) -> None:
    """
    Scroll the right-side results pane to trigger lazy-loading.
    FB Marketplace renders listings inside an overflow:auto div, not on
    the window — scrolling window does nothing to load more cards.
    """
    try:
        scroll_target = await page.evaluate(f"{_SCROLL_JS}(6, 1200)")
        log.info("[Search %d] Scrolled via: %s", search_id, scroll_target)
    except Exception as exc:
        log.warning("[Search %d] Scroll JS error: %s", search_id, exc)
        # Fallback: plain window scroll
        for i in range(scrolls):
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            await asyncio.sleep(1.2 + i * 0.2)


async def _save_debug_screenshot(page: Page, search_id: int) -> None:
    try:
        _SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(_SCREENSHOT_PATH), full_page=False)
        log.warning(
            "[Search %d] Screenshot saved to %s — open it to see what FB is showing.",
            search_id, _SCREENSHOT_PATH,
        )
    except Exception as exc:
        log.warning("[Search %d] Could not save screenshot: %s", search_id, exc)


async def scrape_search(page: Page, search: dict) -> list[Listing]:
    search_id = search["id"]

    await _warm_location(page)

    url = _build_url(search)
    log.info("[Search %d] Scanning: %s", search_id, url)

    try:
        await page.goto(url, wait_until="networkidle", timeout=60_000)
    except PWTimeoutError:
        log.warning("[Search %d] networkidle timed out — continuing.", search_id)

    # Initial render buffer
    await asyncio.sleep(2.0)

    # Scroll the results pane to trigger lazy-load of listing cards
    await _scroll_results_pane(page, search_id)
    await asyncio.sleep(1.5)

    anchors = await page.query_selector_all(_CARD_SELECTOR)
    log.info("[Search %d] Found %d raw listing links.", search_id, len(anchors))

    if len(anchors) == 0:
        title = await page.title()
        log.warning("[Search %d] Zero listings — page title: '%s'", search_id, title)
        try:
            body_text = await page.inner_text("body")
            log.warning(
                "[Search %d] Page snippet: %s",
                search_id,
                body_text[:800].replace("\n", " "),
            )
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

            spans = await anchor.query_selector_all("span[dir='auto']")

            title = ""
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
