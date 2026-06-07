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
        if method == "getUpdates":
            return {"ok": True, "result": []}
        log.warning("Telegram timeout [%s]", method)
        return None
    except Exception as exc:
        log.error("Telegram request failed [%s]: %r", method, exc)
        return None


async def delete_webhook() -> None:
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
        # Tracks pending /delete confirmations: {search_id: search_name}
        self._pending_delete: dict[int, str] = {}

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
                    timeout=25,
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
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if chat_id != str(settings.telegram_chat_id):
            return

        lower = text.lower()

        # ── Confirmation replies for pending /delete ──
        if lower in ("yes", "y") and self._pending_delete:
            await self._confirm_delete()
            return
        if lower in ("no", "n", "cancel") and self._pending_delete:
            self._pending_delete.clear()
            await send_message("❌ Delete cancelled.")
            return

        # ── Routed commands ──
        if lower in ("/status", "/status@" + lower.split("@")[-1]):
            await self._cmd_status()
        elif lower == "/pause":
            await self._cmd_pause()
        elif lower == "/resume":
            await self._cmd_resume()
        elif lower == "/searches":
            await self._cmd_searches()
        elif lower == "/help":
            await self._cmd_help()
        elif lower.startswith("/delete"):
            await self._cmd_delete(text)
        elif lower.startswith("/addsearch"):
            await self._cmd_addsearch(text)

    # ------------------------------------------------------------------ #
    #  Commands
    # ------------------------------------------------------------------ #

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
                f"{icon} [<code>{s['id']}</code>] <b>{_esc(s['name'])}</b>\n"
                f"   Keywords: {_esc(s['keywords'])}\n"
                f"   Price: ${s['price_min'] or 0}–${s['price_max'] or '∞'}  "
                f"Distance: {s['distance_mi']}mi\n"
                f"   To delete: /delete {s['id']}"
            )
        await send_message("\n".join(lines))

    async def _cmd_delete(self, text: str) -> None:
        from db.database import get_search
        parts = text.strip().split()
        if len(parts) < 2 or not parts[1].isdigit():
            await send_message(
                "ℹ️ Usage: /delete &lt;id&gt;\n"
                "Send /searches to see search IDs."
            )
            return

        search_id = int(parts[1])
        search = await get_search(search_id)
        if not search:
            await send_message(f"❌ No search found with ID {search_id}.")
            return

        # Stage the delete and ask for confirmation
        self._pending_delete = {search_id: search["name"]}
        await send_message(
            f"⚠️ Are you sure you want to delete search [{search_id}] "
            f"<b>{_esc(search['name'])}</b>?\n\n"
            f"This will also remove all its listings and seen history.\n\n"
            f"Reply <b>yes</b> to confirm or <b>no</b> to cancel."
        )

    async def _confirm_delete(self) -> None:
        from db.database import delete_search
        search_id, search_name = next(iter(self._pending_delete.items()))
        self._pending_delete.clear()
        try:
            await delete_search(search_id)
            await send_message(
                f"✅ Search [{search_id}] <b>{_esc(search_name)}</b> deleted successfully.\n"
                f"All associated listings and seen history have been removed."
            )
            log.info("Search %d (%s) deleted via Telegram command.", search_id, search_name)
        except Exception as exc:
            await send_message(f"❌ Failed to delete search: {_esc(str(exc))}")
            log.error("Failed to delete search %d: %s", search_id, exc)

    async def _cmd_addsearch(self, text: str) -> None:
        """
        Quick add a search from Telegram.
        Usage: /addsearch <name> | <keywords> | <zip> | <max_price>
        Example: /addsearch Road Bike | bike,road bike | 27330 | 400
        """
        from db.database import create_search
        try:
            _, args = text.split(None, 1)
            parts = [p.strip() for p in args.split("|")]
            if len(parts) < 3:
                raise ValueError("not enough parts")
            name = parts[0]
            keywords = parts[1]
            zip_code = parts[2]
            price_max = float(parts[3]) if len(parts) > 3 else None
            search_id = await create_search(
                name=name,
                keywords=keywords,
                zip_code=zip_code,
                price_max=price_max,
            )
            await send_message(
                f"✅ Search created! ID: <code>{search_id}</code>\n"
                f"Name: <b>{_esc(name)}</b>\n"
                f"Keywords: {_esc(keywords)}\n"
                f"Zip: {zip_code}  Max price: ${price_max or '∞'}\n\n"
                f"Next scan will include this search."
            )
        except (ValueError, IndexError):
            await send_message(
                "ℹ️ <b>Usage:</b> /addsearch name | keywords | zip | max_price\n\n"
                "<b>Example:</b>\n"
                "/addsearch Road Bike | bike,road bike | 27330 | 400\n\n"
                "max_price is optional."
            )

    async def _cmd_help(self) -> None:
        await send_message(
            "🤖 <b>Available Commands</b>\n\n"
            "/status — bot status and last run time\n"
            "/searches — list all searches with their IDs\n"
            "/delete &lt;id&gt; — delete a search (asks for confirmation)\n"
            "/addsearch name | keywords | zip | max_price — add a new search\n"
            "/pause — pause all scans\n"
            "/resume — resume scans\n"
            "/help — show this message"
        )
