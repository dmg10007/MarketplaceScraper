"""
Playwright session manager.

Responsibilities:
  - Launch / reuse a persistent browser context
  - Login to Facebook and persist the session to disk
  - Detect session expiry and re-login automatically
  - Provide a thin navigate() helper with human-like delay

Anti-detection strategy:
  - headless=False by default (real window on Windows desktop)
  - playwright-stealth v2 patches applied per-page (once only)
  - Random sleep jitter between every navigation
  - Cookies + localStorage persisted to data/fb_session.json
"""

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeoutError,
)

# playwright-stealth v2.x API
try:
    from playwright_stealth import Stealth
    _STEALTH_V2 = True
except ImportError:
    from playwright_stealth import stealth_async as _stealth_async_v1  # type: ignore
    _STEALTH_V2 = False

from config.settings import settings

log = logging.getLogger(__name__)

FB_BASE = "https://www.facebook.com"
FB_LOGIN = f"{FB_BASE}/login"
SESSION_PATH = Path(settings.session_file)

_stealth = Stealth() if _STEALTH_V2 else None
_stealth_applied: set[int] = set()


async def _apply_stealth(page: Page) -> None:
    """Apply stealth patches once per page instance."""
    page_id = id(page)
    if page_id in _stealth_applied:
        return
    _stealth_applied.add(page_id)
    if _STEALTH_V2:
        await _stealth.apply_stealth_async(page)  # type: ignore
    else:
        await _stealth_async_v1(page)  # type: ignore


# ---------------------------------------------------------------------------
# Dialog dismissal helpers
# ---------------------------------------------------------------------------

_PRE_LOGIN_DIALOGS = [
    "[data-testid='cookie-policy-manage-dialog-accept-button']",
    "button[title='Accept all']",
    "button[title='Allow all cookies']",
    "div[role='dialog'] button:has-text('Allow')",
    "div[role='dialog'] button:has-text('Accept')",
    "div[role='dialog'] button:has-text('OK')",
]

_POST_LOGIN_DIALOGS = [
    "div[role='dialog'] div[aria-label='Not Now']",
    "div[role='dialog'] button:has-text('Not Now')",
    "div[role='dialog'] button:has-text('Not now')",
    "div[role='dialog'] button:has-text('Close')",
    "div[role='dialog'] div[aria-label='Close']",
    "div[role='dialog'] [aria-label='Close']",
]


async def _dismiss_dialogs(page: Page, selectors: list[str], label: str) -> None:
    """Try each selector and click the first one found. Silent if none match."""
    for sel in selectors:
        try:
            btn = await page.wait_for_selector(sel, timeout=3_000, state="visible")
            if btn:
                log.info("Dismissing %s dialog: %s", label, sel)
                await btn.click()
                await asyncio.sleep(1.0)
                for sel2 in selectors:
                    try:
                        btn2 = await page.wait_for_selector(sel2, timeout=2_000, state="visible")
                        if btn2:
                            await btn2.click()
                            await asyncio.sleep(0.8)
                    except PWTimeoutError:
                        continue
                return
        except PWTimeoutError:
            continue


class SessionManager:
    """Long-lived Playwright session — one instance per application lifetime."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._just_logged_in: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._playwright = await async_playwright().start()

        launch_kwargs: dict = dict(
            headless=settings.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        if settings.proxy_url:
            launch_kwargs["proxy"] = {"server": settings.proxy_url}

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        await self._restore_or_create_context()
        log.info("SessionManager started.")

    async def stop(self) -> None:
        """Gracefully shut down — best-effort session save before closing."""
        await self._save_session()
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        log.info("SessionManager stopped.")

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def get_page(self) -> Page:
        """Return the active page, re-logging in if session expired."""
        if not self._page or self._page.is_closed():
            self._page = await self._context.new_page()
            await _apply_stealth(self._page)
            self._just_logged_in = False

        if not self._just_logged_in and await self._session_expired():
            log.warning("Session expired — re-logging in.")
            await self._login()

        self._just_logged_in = False
        return self._page

    async def navigate(self, url: str) -> Page:
        """Navigate to url with random delay applied before and after."""
        page = await self.get_page()
        await self._jitter()
        log.debug("Navigating to %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await self._jitter()
        return page

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    async def _restore_or_create_context(self) -> None:
        context_kwargs: dict = dict(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        if SESSION_PATH.exists():
            log.info("Restoring session from %s", SESSION_PATH)
            context_kwargs["storage_state"] = json.loads(SESSION_PATH.read_text())
            self._context = await self._browser.new_context(**context_kwargs)
            self._page = await self._context.new_page()
            await _apply_stealth(self._page)
            self._just_logged_in = True
        else:
            log.info("No saved session found — creating fresh context.")
            self._context = await self._browser.new_context(**context_kwargs)
            self._page = await self._context.new_page()
            await _apply_stealth(self._page)
            await self._login()

    async def _save_session(self) -> None:
        """Persist cookies + localStorage. No-op if context is already closed."""
        if not self._context:
            return
        try:
            state = await self._context.storage_state()
            SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            SESSION_PATH.write_text(json.dumps(state))
            log.info("Session saved to %s", SESSION_PATH)
        except Exception as exc:
            # Browser was already closed (e.g. CTRL+C) — session was saved
            # at login time so this is safe to skip.
            log.debug("Session save skipped (context closed): %s", exc)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _login(self) -> None:
        log.info("Logging in to Facebook as %s ...", settings.fb_email)
        page = self._page
        if page is None or page.is_closed():
            page = await self._context.new_page()
            await _apply_stealth(page)
            self._page = page

        await page.goto(FB_LOGIN, wait_until="networkidle", timeout=60_000)
        await self._jitter()

        await _dismiss_dialogs(page, _PRE_LOGIN_DIALOGS, "consent")

        log.info("Waiting for login form...")
        try:
            await page.wait_for_selector("#email", state="visible", timeout=30_000)
        except PWTimeoutError:
            shot_path = Path("data/login_debug.png")
            shot_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot_path))
            raise RuntimeError(
                f"Login form (#email) not found after 30s. "
                f"Screenshot saved to {shot_path} — open it to see what Facebook showed."
            )

        await page.fill("#email", settings.fb_email)
        await self._jitter(short=True)
        await page.fill("#pass", settings.fb_password)
        await self._jitter(short=True)
        await page.click("[name='login']")

        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except PWTimeoutError:
            pass
        await self._jitter()

        current_url = page.url
        if "login" in current_url or "checkpoint" in current_url:
            shot_path = Path("data/login_debug.png")
            await page.screenshot(path=str(shot_path))
            raise RuntimeError(
                f"Login did not succeed. URL: {current_url}\n"
                f"Screenshot: {shot_path}\n"
                "Check FB_EMAIL / FB_PASSWORD in .env, or resolve any security checkpoint."
            )

        log.info("Dismissing post-login dialogs...")
        await _dismiss_dialogs(page, _POST_LOGIN_DIALOGS, "post-login")

        await self._save_session()
        self._just_logged_in = True
        log.info("Login successful. Session saved.")

    async def _session_expired(self) -> bool:
        if not self._page or self._page.is_closed():
            return True
        try:
            url = self._page.url
            if not url or url == "about:blank":
                return False
            if "/login" in url or "/checkpoint" in url:
                return True
            login_btn = await self._page.query_selector("[name='login']", timeout=2_000)
            return login_btn is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def _jitter(self, short: bool = False) -> None:
        if short:
            await asyncio.sleep(random.uniform(0.3, 1.2))
        else:
            await asyncio.sleep(
                random.uniform(settings.request_delay_min, settings.request_delay_max)
            )


session_manager = SessionManager()
