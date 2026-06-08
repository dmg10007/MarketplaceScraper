"""
Scheduler — runs periodic scans and health watchdog.

Phase 5:
  - Health alert if no successful run in >1 hour
  - Session expiry detection via marketplace.reset_location_warm()
  - mark_seen only after alert succeeds (no lost listings)
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self._session_manager = None
        self._notifier = None
        self.paused: bool = False
        self._scan_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._running: bool = False

    def wire(self, session_manager, notifier) -> None:
        self._session_manager = session_manager
        self._notifier = notifier

    def pause(self) -> None:
        self.paused = True
        log.info("Scheduler paused.")

    def resume(self) -> None:
        self.paused = False
        log.info("Scheduler resumed.")

    async def start(self) -> None:
        self._running = True
        self._scan_task = asyncio.create_task(self._scan_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        log.info("Scheduler started (interval=%dm).", settings.scan_interval_minutes)

    async def stop(self) -> None:
        self._running = False
        for task in (self._scan_task, self._watchdog_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        log.info("Scheduler stopped.")

    async def _scan_loop(self) -> None:
        await asyncio.sleep(5)
        while self._running:
            if not self.paused:
                try:
                    await self.run_all_searches()
                except Exception as exc:
                    log.error("Scan loop unhandled error: %r", exc)
            await asyncio.sleep(settings.scan_interval_minutes * 60)

    async def _watchdog_loop(self) -> None:
        CHECK_INTERVAL = 600
        ALERT_THRESHOLD = 3600
        await asyncio.sleep(CHECK_INTERVAL)
        while self._running:
            try:
                from db.database import get_last_successful_run
                last = await get_last_successful_run()
                if last and last.get("finished_at"):
                    finished = datetime.fromisoformat(last["finished_at"])
                    if finished.tzinfo is None:
                        finished = finished.replace(tzinfo=timezone.utc)
                    gap = (datetime.now(tz=timezone.utc) - finished).total_seconds()
                    if gap > ALERT_THRESHOLD and self._notifier:
                        mins = int(gap / 60)
                        await self._notifier.send_health_alert(
                            f"No successful scan in {mins} minutes.\n"
                            f"Last run: {last['finished_at']}\n\n"
                            f"Check logs or send /scan to retry."
                        )
            except Exception as exc:
                log.warning("Watchdog check error: %r", exc)
            await asyncio.sleep(CHECK_INTERVAL)

    async def run_all_searches(self) -> None:
        from db.database import get_all_searches, log_run, mark_seen
        from core.marketplace import scrape_search

        if self._session_manager is None or self._notifier is None:
            log.error("Scheduler not wired — call scheduler.wire() before starting.")
            return

        searches = await get_all_searches(enabled_only=True)
        if not searches:
            log.warning("[Scheduler] No enabled searches found in DB — add one via /addsearch or the dashboard.")
            return

        log.info("[Scheduler] Starting scan for %d search(es).", len(searches))

        page = await self._session_manager.get_page()
        if page is None:
            log.error("[Scheduler] No browser page available — skipping scan.")
            return

        for search in searches:
            if self.paused:
                break
            log.info("[Scheduler] ► Running search [%d] '%s'", search["id"], search["name"])
            new_count = 0
            status = "success"
            try:
                listings = await scrape_search(page, search)
                log.info("[Scheduler] Search [%d] returned %d new listing(s).", search["id"], len(listings))

                if not listings:
                    log.info("[Scheduler] No new listings for '%s' — all already seen or none found.", search["name"])

                for listing in listings:
                    log.info(
                        "[Scheduler] Sending alert: '%s' $%s  %s",
                        listing.title,
                        listing.price,
                        listing.listing_url,
                    )
                    sent = await self._notifier.send_alert(listing, search["name"])
                    if sent:
                        await mark_seen(listing.id, search["id"])
                        new_count += 1
                        log.info("[Scheduler] ✅ Alert sent + marked seen: %s", listing.id)
                    else:
                        log.warning("[Scheduler] ❌ Alert FAILED for %s — NOT marking seen.", listing.id)

            except Exception as exc:
                log.exception("[Scheduler] Search [%d] crashed: %r", search["id"], exc)
                status = "error"

            await log_run(
                search_id=search["id"],
                search_name=search["name"],
                status=status,
                new_listings=new_count,
            )
            log.info("[Scheduler] ■ Done search [%d] — status=%s new=%d", search["id"], status, new_count)

        log.info("[Scheduler] All searches complete.")


scheduler = Scheduler()
