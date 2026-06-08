"""
Scheduler — runs searches on a fixed interval.

Key fix: send_alert is called BEFORE mark_seen so a failed alert
never silently suppresses a listing. mark_seen only runs after a
successful alert (or if the listing was filtered out).

Phase 5: health alert, SessionExpiredError handling.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from config.settings import settings
from core.marketplace import search_marketplace, SessionExpiredError
from db.database import (
    get_all_searches,
    is_seen, mark_seen, upsert_listing,
    log_run, get_last_successful_run,
)

log = logging.getLogger(__name__)

_HEALTH_CHECK_INTERVAL = 3600
_MAX_STALE_SECONDS = 3600


class Scheduler:
    def __init__(self, interval_minutes: int = 15):
        self.interval = interval_minutes * 60
        self.paused = False
        self._task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._session_manager = None
        self._notifier = None

    def wire(self, session_manager, notifier):
        self._session_manager = session_manager
        self._notifier = notifier

    async def start(self):
        self._task = asyncio.create_task(self._scan_loop())
        self._health_task = asyncio.create_task(self._health_loop())
        log.info("Scheduler started (interval=%dm).", settings.scan_interval_minutes)

    async def stop(self):
        for t in (self._task, self._health_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        log.info("Scheduler stopped.")

    # ----------------------------------------------------------------
    # Main scan loop
    # ----------------------------------------------------------------

    async def _scan_loop(self):
        while True:
            try:
                await self.run_all_searches()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Scan loop unhandled error: %s", exc, exc_info=True)
            await asyncio.sleep(self.interval)

    async def run_all_searches(self):
        if self.paused:
            log.info("[Scheduler] Paused — skipping scan.")
            return
        if not self._session_manager or not self._notifier:
            log.warning("[Scheduler] Not wired — skipping scan.")
            return

        searches = await get_all_searches(enabled_only=True)
        if not searches:
            log.info("[Scheduler] No enabled searches.")
            return

        log.info("[Scheduler] Starting scan for %d search(es).", len(searches))
        page = await self._session_manager.new_page()

        try:
            for s in searches:
                log.info("[Scheduler] ► Running search [%d] '%s'", s["id"], s["name"])
                neg_kws = [
                    k.strip().lower()
                    for k in (s.get("neg_keywords") or "").split(",")
                    if k.strip()
                ]
                try:
                    new_listings = await search_marketplace(
                        page=page,
                        search_id=s["id"],
                        search_name=s["name"],
                        keywords=s["keywords"],
                        zip_code=s["zip_code"] or settings.default_zip,
                        price_min=s.get("price_min"),
                        price_max=s.get("price_max"),
                        distance_mi=s.get("distance_mi") or 40,
                        neg_keywords=neg_kws,
                        condition=s.get("condition") or "any",
                        is_seen_fn=is_seen,
                        mark_seen_fn=mark_seen,
                        upsert_listing_fn=upsert_listing,
                    )

                    alerted = 0
                    for listing in new_listings:
                        try:
                            # CRITICAL ORDER: alert FIRST, mark seen only on success
                            await self._notifier.send_alert(s, listing)
                            await mark_seen(listing["id"], s["id"])
                            log.info(
                                "[Scheduler] ✅ Alert sent + marked seen: %s (%s)",
                                listing.get("title") or listing["id"],
                                listing.get("price") or "no price",
                            )
                            alerted += 1
                        except Exception as alert_exc:
                            # Alert failed — do NOT mark seen so it retries next scan
                            log.error(
                                "[Scheduler] Alert failed for %s (will retry next scan): %s",
                                listing["id"], alert_exc,
                            )

                    await log_run(
                        search_id=s["id"],
                        search_name=s["name"],
                        status="success",
                        new_listings=alerted,
                    )
                    if alerted:
                        log.info("[Scheduler] Search [%d] sent %d alert(s).", s["id"], alerted)
                    else:
                        log.info(
                            "[Scheduler] No new listings for '%s' — all already seen.",
                            s["name"],
                        )

                except SessionExpiredError:
                    log.warning(
                        "[Scheduler] Session expired during search '%s'. Attempting re-login...",
                        s["name"],
                    )
                    await log_run(s["id"], s["name"], "session_expired", 0)
                    relogged = await self._session_manager.attempt_relogin()
                    if relogged:
                        log.info("[Scheduler] Re-login successful.")
                    else:
                        log.error("[Scheduler] Re-login failed. Manual intervention required.")
                        await self._notifier.send_raw(
                            "\u26a0\ufe0f *MarketplaceScraper* — Facebook session expired and "
                            "re-login failed. Run `python scripts/setup_session.py` to restore."
                        )
                    break

                except Exception as exc:
                    log.error(
                        "[Scheduler] Error in search '%s': %s", s["name"], exc, exc_info=True
                    )
                    await log_run(s["id"], s["name"], f"error: {exc}", 0)

        finally:
            await page.close()

    # ----------------------------------------------------------------
    # Health check loop
    # ----------------------------------------------------------------

    async def _health_loop(self):
        await asyncio.sleep(3600)
        while True:
            try:
                await self._check_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Health check error: %s", exc)
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL)

    async def _check_health(self):
        last = await get_last_successful_run()
        if not last:
            await self._send_health_alert("No successful scan has ever completed.")
            return
        try:
            finished = datetime.fromisoformat(last["finished_at"].replace("Z", "+00:00"))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            age = datetime.now(tz=timezone.utc) - finished
            if age > timedelta(seconds=_MAX_STALE_SECONDS):
                mins = int(age.total_seconds() / 60)
                await self._send_health_alert(
                    f"Last successful scan was {mins} minutes ago. Bot may be stuck."
                )
        except Exception as exc:
            log.warning("Health check datetime parse error: %s", exc)

    async def _send_health_alert(self, reason: str):
        if self._notifier:
            msg = f"\u26a0\ufe0f *MarketplaceScraper Health Alert*\n{reason}"
            try:
                await self._notifier.send_raw(msg)
                log.warning("Health alert sent: %s", reason)
            except Exception as exc:
                log.error("Failed to send health alert: %s", exc)


scheduler = Scheduler(interval_minutes=settings.scan_interval_minutes)
