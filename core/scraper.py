"""
Playwright session manager — Phase 5 proxy layer added.

Proxy is injected at browser launch from settings.proxy_url.
Leave PROXY_URL blank in .env to disable.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config.settings import settings

log = logging.getLogger(__name__)


class SessionManager:
    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def start(self):
        self._playwright = await async_playwright().start()

        launch_kwargs = dict(
            headless=settings.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        # Phase 5: inject proxy if configured
        if settings.proxy_url:
            launch_kwargs["proxy"] = {"server": settings.proxy_url}
            log.info("Proxy enabled: %s", settings.proxy_url)

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)

        context_kwargs = dict(
            user_agent=settings.user_agent,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

        session_path = Path(settings.session_file)
        if session_path.exists():
            log.info("Restoring session from %s", settings.session_file)
            storage_state = json.loads(session_path.read_text())
            context_kwargs["storage_state"] = storage_state

        self._context = await self._browser.new_context(**context_kwargs)

        # Stealth: mask webdriver flag
        await self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        log.info("SessionManager started.")

    async def new_page(self) -> Page:
        if not self._context:
            raise RuntimeError("SessionManager not started")
        return await self._context.new_page()

    async def attempt_relogin(self) -> bool:
        """
        Try to restore session from disk (user may have re-run setup_session.py).
        Returns True if new context loaded successfully.
        """
        session_path = Path(settings.session_file)
        if not session_path.exists():
            log.error("Re-login failed: session file not found.")
            return False
        try:
            if self._context:
                await self._context.close()
            storage_state = json.loads(session_path.read_text())
            self._context = await self._browser.new_context(
                user_agent=settings.user_agent,
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                timezone_id="America/New_York",
                storage_state=storage_state,
            )
            await self._context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            log.info("Session restored from disk.")
            return True
        except Exception as exc:
            log.error("Re-login attempt failed: %s", exc)
            return False

    async def stop(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log.info("SessionManager stopped.")


session_manager = SessionManager()
