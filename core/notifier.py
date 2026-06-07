"""
Telegram notifier + command handler.
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
        async with httpx.AsyncClient(timeout=35) as client:
            r = await client.post(f"{TG_BASE}/{method}", json=kwargs)
            data = r.json()
            if not data.get("ok"):
                log.warning(
                    "Telegram API error [%s] %d: %s",
                    method,
                    r.status_code,
                    data.get("description", "no description"),
                )
                return None
            return data
    except httpx.TimeoutException:
        # Long-poll timeout is normal — not an error
        if method == "getUpdates":
            return {"ok": True, "result": []}
        log.warning("Telegram timeout [%s]", method)
        return None
    except Exception as exc:
        log.error("Telegram request failed [%s]: %r", method, exc)
        return None


async def delete_webhook() -> None:
    """
    Remove any registered webhook so long-polling works.
    Telegram blocks getUpdates while a webhook is active.
    """
    result = await _tg("deleteWebhook", drop_pending_updates=False)
    if result:
        log.info("Telegram webhook cleared — polling mode active.")
    else:
        log.warning("Could not clear Telegram webhook (may already be clear).")


async def send_alert(listing: Listing, search_name: str) -> bool:
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
    result = await _tg(
        "sendMessage",
        chat_id=settings.telegram_chat_id,
        text=text,
        parse_mode="HTML",
    )
    return result is not None


async def send_health_alert(message: str) -> bool:
    return await send_message(f"⚠️ <b>MarketplaceScraper Alert</b>\n\n{message}")


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Command polling
# ---------------------------------------------------------------------------

class TelegramPoller:
    def __init__(self, scheduler) -> None:
        self._scheduler = scheduler
        self._offset: int = 0
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
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
                    timeout=25,           # seconds Telegram holds the connection
                    allowed_updates=["message"],
                )
                if data is None:
                    await asyncio.sleep(min(backoff, 60))
                    backoff = min(backoff * 2, 60)
                    continue

                backoff = 1
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    await self._handle_update(update)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("Telegram poll loop error: %r", exc)
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)

    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if chat_id != str(settings.telegram_chat_id):
            return

        dispatch = {
            "/status":   self._cmd_status,
            "/pause":    self._cmd_pause,
            "/resume":   self._cmd_resume,
            "/searches": self._cmd_searches,
            "/help":     self._cmd_help,
        }
        handler = dispatch.get(text)
        if handler:
            await handler()

    async def _cmd_status(self) -> None:
        from db.database import get_run_log, get_all_searches
        searches = await get_all_searches(enabled_only=True)
        log_entries = await get_run_log(limit=1)
        last_run = log_entries[0].get("finished_at", "Unknown") if log_entries else "Never"
        status_icon = "⏸️ Paused" if self._scheduler.paused else "▶️ Running"
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
