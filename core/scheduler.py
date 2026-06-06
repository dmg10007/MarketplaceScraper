"""
Scheduler — runs all enabled searches on a fixed interval.

Design:
  - APScheduler AsyncIOScheduler triggers run_all_searches() every N minutes
  - run_all_searches() fans out to up to MAX_CONCURRENT searches via asyncio.Semaphore
  - Each search gets its own Playwright page (isolated navigation state)
  - New listings are alerted via Telegram immediately after each search completes
  - Health check: if no successful run in HEALTH_THRESHOLD minutes, sends Telegram alert
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config.settings import settings
from core.listing import Listing
from core.marketplace import scrape_search
from db.database import (
    get_all_searches,
    get_search,
    log_run,
    get_last_successful_run,
    mark_alerted,
)

log = logging.getLogger(__name__)


class Scheduler:
    """Wraps APScheduler and owns the scan loop lifecycle."""

    def __init__(self) -> None:
        self._aps = AsyncIOScheduler()
        self._sem: Optional[asyncio.Semaphore] = None
        self._paused: bool = False
        self._notifier = None   # set after construction to avoid circular import
        self._session = None    # set after construction

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def wire(self, session_manager, notifier) -> None:
        """Inject dependencies post-construction."""
        self._session = session_manager
        self._notifier = notifier

    async def start(self) -> None:
        self._sem = asyncio.Semaphore(settings.max_concurrent_searches)
        self._aps.add_job(
            self.run_all_searches,
            trigger="interval",
            minutes=settings.scan_interval_minutes,
            id="scan_loop",
            next_run_time=datetime.now(),  # run immediately on startup
        )
        self._aps.add_job(
            self._health_check,
            trigger="interval",
            minutes=15,
            id="health_check",
        )
        self._aps.start()
        log.info(
            "Scheduler started — scanning every %d minutes.",
            settings.scan_interval_minutes,
        )

    async def stop(self) -> None:
        self._aps.shutdown(wait=False)
        log.info("Scheduler stopped.")

    def pause(self) -> None:
        self._paused = True
        log.info("Scheduler paused.")

    def resume(self) -> None:
        self._paused = False
        log.info("Scheduler resumed.")

    @property
    def paused(self) -> bool:
        return self._paused

    # ------------------------------------------------------------------
    # Main scan loop
    # ------------------------------------------------------------------

    async def run_all_searches(self) -> None:
        """Fan out all enabled searches concurrently (up to max_concurrent)."""
        if self._paused:
            log.info("Scheduler paused — skipping scan.")
            return

        searches = await get_all_searches(enabled_only=True)
        if not searches:
            log.info("No enabled searches — nothing to do.")
            return

        log.info("Starting scan for %d search(es)...", len(searches))
        tasks = [self._run_one_search(s) for s in searches]
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Scan cycle complete.")

    async def _run_one_search(self, search: dict) -> None:
        """Run a single search inside the concurrency semaphore."""
        async with self._sem:
            search_id = search["id"]
            started_at = datetime.utcnow().isoformat()
            try:
                page = await self._session.get_page()
                new_listings = await scrape_search(page, search)

                # Send alerts for each new listing
                for listing in new_listings:
                    sent = await self._notifier.send_alert(listing, search["name"])
                    if sent:
                        await mark_alerted(listing.id)

                await log_run(
                    search_id=search_id,
                    status="success",
                    listings_found=len(new_listings),
                    new_listings=len(new_listings),
                    started_at=started_at,
                )

            except Exception as exc:
                log.error("[Search %d] Failed: %s", search_id, exc, exc_info=True)
                await log_run(
                    search_id=search_id,
                    status="error",
                    error_msg=str(exc),
                    started_at=started_at,
                )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def _health_check(self) -> None:
        """Alert via Telegram if no successful run in threshold window."""
        last = await get_last_successful_run()
        if not last:
            return  # no runs yet — bot just started

        last_time = datetime.fromisoformat(last["finished_at"])
        threshold = timedelta(minutes=settings.health_alert_threshold_minutes)
        if datetime.utcnow() - last_time > threshold:
            msg = (
                f"No successful scan in over {settings.health_alert_threshold_minutes} minutes.\n"
                f"Last success: {last['finished_at']}\n"
                "Check the bot — session may have expired."
            )
            log.warning("Health alert: %s", msg)
            await self._notifier.send_health_alert(msg)


# Module-level singleton
scheduler = Scheduler()
