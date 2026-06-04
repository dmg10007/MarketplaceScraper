"""
Playwright session manager.

Responsibilities:
  - Launch / reuse a persistent browser context
  - Login to Facebook and persist the session to disk
  - Detect session expiry and re-login automatically
  - Provide a thin navigate() helper with human-like delay

Anti-detection strategy:
  - headless=False by default (real window on Windows desktop)
  - playwright-stealth patches applied on every new context
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
)
from playwright_stealth import stealth_async

from config.settings import settings

log = logging.getLogger(__name__)

FB_BASE = "https://www.facebook.com"
FB_LOGIN = f"{FB_BASE}/login"
FB_MARKETPLACE = f"{FB_BASE}/marketplace"
SESSION_PATH = Path(settings.session_file)


class SessionManager:
    """Long-lived Playwright session — one instance per application lifetime."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch browser and restore or create a Facebook session."""
        self._playwright = await async_playwright().start()

        launch_kwargs = dict(
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
        """Gracefully shut down — save session before closing."""
        await self._save_session()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log.info("SessionManager stopped.")

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def get_page(self) -> Page:
        """Return the active page, re-logging in if session expired."""
        if not self._page or self._page.is_closed():
            self._page = await self._context.new_page()
            await stealth_async(self._page)

        if await self._session_expired():
            log.warning("Session expired — re-logging in.")
            await self._login()

        return self._page

    async def navigate(self, url: str) -> Page:
        """Navigate to url with random delay applied before and after."""
        page = await self.get_page()
        await self._jitter()
        log.debug("Navigating to %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await self._jitter()
        return page

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    async def _restore_or_create_context(self) -> None:
        if SESSION_PATH.exists():
            log.info("Restoring session from %s", SESSION_PATH)
            storage_state = json.loads(SESSION_PATH.read_text())
            self._context = await self._browser.new_context(
                storage_state=storage_state,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
        else:
            log.info("No saved session found — creating fresh context.")
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            self._page = await self._context.new_page()
            await stealth_async(self._page)
            await self._login()

    async def _save_session(self) -> None:
        if self._context:
            SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            state = await self._context.storage_state()
            SESSION_PATH.write_text(json.dumps(state))
            log.info("Session saved to %s", SESSION_PATH)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _login(self) -> None:
        """Perform Facebook login and persist session on success."""
        log.info("Logging in to Facebook as %s", settings.fb_email)
        page = self._page or await self._context.new_page()
        await stealth_async(page)
        self._page = page

        await page.goto(FB_LOGIN, wait_until="domcontentloaded")
        await self._jitter()

        await page.fill("#email", settings.fb_email)
        await self._jitter(short=True)
        await page.fill("#pass", settings.fb_password)
        await self._jitter(short=True)
        await page.click("[name='login']")  # Login button
        await page.wait_for_load_state("domcontentloaded")
        await self._jitter()

        if await self._session_expired():
            raise RuntimeError(
                "Login failed — check FB_EMAIL / FB_PASSWORD in .env, "
                "or the account may require 2FA / CAPTCHA resolution."
            )

        await self._save_session()
        log.info("Login successful.")

    async def _session_expired(self) -> bool:
        """Return True if the current page indicates the user is logged out."""
        if not self._page:
            return True
        try:
            url = self._page.url
            # Facebook redirects to /login or /checkpoint when session is dead
            if "login" in url or "checkpoint" in url:
                return True
            # Check for login form presence as fallback
            login_btn = await self._page.query_selector("[name='login']")
            return login_btn is not None
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def _jitter(self, short: bool = False) -> None:
        """Random sleep to simulate human pacing."""
        if short:
            await asyncio.sleep(random.uniform(0.3, 1.2))
        else:
            await asyncio.sleep(
                random.uniform(settings.request_delay_min, settings.request_delay_max)
            )


# Module-level singleton
session_manager = SessionManager()
