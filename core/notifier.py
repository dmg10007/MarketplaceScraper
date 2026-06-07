"""
Telegram notifier + command handler.

Alert format:
  📦 <Title>
  💰 $Price  |  📍 Location  |  ⭐ Condition
  [Open listing]

Commands (polling-based, runs in background task):
  /status   — show last run time, search count, listing count
  /pause    — pause the scheduler
  /resume   — resume the scheduler
  /searches — list all active searches
"""

import asyncio
import logging
from typing import Optional

import httpx

from config.settings import settings
from core.listing import Listing

log = logging.getLogger(__name__)

TG_BASE = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


async def _tg(method: str, **kwargs) -> Optional[dict]:
    """Make a Telegram Bot API call. Returns parsed JSON or None on error."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{TG_BASE}/{method}", json=kwargs)
            data = r.json()
            if not data.get("ok"):
                log.warning("Telegram API error [%s]: %s", method, data.get("description"))
                return None
            return data
    except Exception as exc:
        log.error("Telegram request failed [%s]: %s", method, exc)
        return None


async def delete_webhook() -> None:
    """
    Remove any registered webhook so long-polling works.
    Telegram does not allow getUpdates while a webhook is set.
    Safe to call even if no webhook is registered.
    """
    result = await _tg("deleteWebhook", drop_pending_updates=False)
    if result:
        log.info("Telegram webhook cleared — polling mode active.")
    else:
        log.warning("Could not clear Telegram webhook (may already be clear).")


async def send_alert(listing: Listing, search_name: str) -> bool:
    """
    Send a rich Telegram alert for a new listing.
    Uses sendPhoto when image_url is available, sendMessage otherwise.
    Returns True on success.
    """
    price_str = listing.price_display()
    location_str = listing.location or "Location unknown"
    condition_str = listing.condition or "Not specified"

    caption = (
        f"📦 <b>{_esc(listing.title)}</b>\n"
        f"💰 <b>{_esc(price_str)}</b>\n"
        f"📍 {_esc(location_str)}\n"
        f"⭐ {_esc(condition_str)}\n"
        f"🔍 Search: <i>{_esc(search_name)}</i>\n\n"
        f'<a href="{listing.listing_url}">Open on Facebook Marketplace →</a>'
    )

    if listing.image_url:
        result = await _tg(
            "sendPhoto",
            chat_id=settings.telegram_chat_id,
            photo=listing.image_url,
            caption=caption,
            parse_mode="HTML",
        )
    else:
        result = await _tg(
            "sendMessage",
            chat_id=settings.telegram_chat_id,
            text=caption,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )

    return result is not None


async def send_message(text: str) -> bool:
    """Send a plain message to the configured chat."""
    result = await _tg(
        "sendMessage",
        chat_id=settings.telegram_chat_id,
        text=text,
        parse_mode="HTML",
    )
    return result is not None


async def send_health_alert(message: str) -> bool:
    """Send a health/error alert with a warning emoji prefix."""
    return await send_message(f"⚠️ <b>MarketplaceScraper Alert</b>\n\n{message}")


def _esc(text: str) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Command polling
# ---------------------------------------------------------------------------

class TelegramPoller:
    """
    Long-poll for Telegram updates and handle bot commands.
    Runs as a background asyncio task alongside the scheduler.
    """

    def __init__(self, scheduler) -> None:
        self._scheduler = scheduler
        self._offset: int = 0
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        # Must delete any registered webhook before polling will work
        await delete_webhook()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info("Telegram command poller started.")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Telegram command poller stopped.")

    async def _poll_loop(self) -> None:
        backoff = 1
        while self._running:
            try:
                data = await _tg(
                    "getUpdates",
                    offset=self._offset,
                    timeout=30,
                    allowed_updates=["message"],
                )
                if data is None:
                    # API error — back off before retrying
                    await asyncio.sleep(min(backoff, 60))
                    backoff = min(backoff * 2, 60)
                    continue

                backoff = 1  # reset on success
                if data.get("result"):
                    for update in data["result"]:
                        self._offset = update["update_id"] + 1
                        await self._handle_update(update)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("Telegram poll error: %s", exc)
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)

    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if chat_id != str(settings.telegram_chat_id):
            return

        if text == "/status":
            await self._cmd_status()
        elif text == "/pause":
            await self._cmd_pause()
        elif text == "/resume":
            await self._cmd_resume()
        elif text == "/searches":
            await self._cmd_searches()
        elif text == "/help":
            await self._cmd_help()

    async def _cmd_status(self) -> None:
        from db.database import get_run_log, get_all_searches
        searches = await get_all_searches(enabled_only=True)
        log_entries = await get_run_log(limit=1)

        last_run = "Never"
        if log_entries:
            last_run = log_entries[0].get("finished_at", "Unknown")

        paused = self._scheduler.paused
        status_icon = "⏸️ Paused" if paused else "▶️ Running"

        await send_message(
            f"🤖 <b>MarketplaceScraper Status</b>\n\n"
            f"Status: {status_icon}\n"
            f"Active searches: {len(searches)}\n"
            f"Scan interval: {settings.scan_interval_minutes} min\n"
            f"Last run: {last_run}"
        )

    async def _cmd_pause(self) -> None:
        self._scheduler.pause()
        await send_message("⏸️ Scheduler paused. Send /resume to restart scans.")

    async def _cmd_resume(self) -> None:
        self._scheduler.resume()
        await send_message("▶️ Scheduler resumed. Next scan starting shortly.")

    async def _cmd_searches(self) -> None:
        from db.database import get_all_searches
        searches = await get_all_searches()
        if not searches:
            await send_message("No searches configured yet.")
            return
        lines = ["<b>Configured Searches</b>\n"]
        for s in searches:
            icon = "✅" if s["enabled"] else "❌"
            lines.append(
                f"{icon} [{s['id']}] <b>{_esc(s['name'])}</b>\n"
                f"   Keywords: {_esc(s['keywords'])}\n"
                f"   Price: ${s['price_min'] or 0}–${s['price_max'] or '∞'}  "
                f"Distance: {s['distance_mi']}mi"
            )
        await send_message("\n".join(lines))

    async def _cmd_help(self) -> None:
        await send_message(
            "🤖 <b>Available Commands</b>\n\n"
            "/status — bot status and last run time\n"
            "/pause — pause all scans\n"
            "/resume — resume scans\n"
            "/searches — list all configured searches\n"
            "/help — show this message"
        )
