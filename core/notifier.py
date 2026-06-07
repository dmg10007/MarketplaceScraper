"""
Telegram notifier + command handler.

Public surface:
  Notifier       — injectable class with send_alert / send_message / send_health_alert
  TelegramPoller — long-poll loop + bot command dispatch
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
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            r = await client.post(f"{TG_BASE}/{method}", json=kwargs)
            data = r.json()
            if not data.get("ok"):
                log.warning(
                    "Telegram API error [%s] %d: %s",
                    method, r.status_code,
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
        log.warning("Could not clear Telegram webhook.")


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Notifier — injectable class used by Scheduler
# ---------------------------------------------------------------------------

class Notifier:
    """
    Injected into Scheduler via scheduler.wire().
    Exposes send_alert / send_message / send_health_alert.
    """

    async def send_alert(self, listing: Listing, search_name: str) -> bool:
        price_str = listing.price_display()
        location_str = listing.location or "Location unknown"
        condition_str = listing.condition or "Not specified"

        caption = (
            f"\U0001f3f7\ufe0f <b>{_esc(listing.title)}</b>\n"
            f"\U0001f4b0 <b>{_esc(price_str)}</b>\n"
            f"\U0001f4cd {_esc(location_str)}\n"
            f"\u2b50 Condition: {_esc(condition_str)}\n"
            f"\U0001f50d Search: <i>{_esc(search_name)}</i>\n\n"
            f"<a href=\"{listing.listing_url}\">\U0001f4f2 Open on Facebook Marketplace \u2192</a>"
        )

        reply_markup = {
            "inline_keyboard": [[
                {"text": "Open in FB Marketplace", "url": listing.listing_url}
            ]]
        }

        if listing.image_url:
            result = await _tg(
                "sendPhoto",
                chat_id=settings.telegram_chat_id,
                photo=listing.image_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            if result is None:
                result = await _tg(
                    "sendMessage",
                    chat_id=settings.telegram_chat_id,
                    text=caption,
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                    reply_markup=reply_markup,
                )
        else:
            result = await _tg(
                "sendMessage",
                chat_id=settings.telegram_chat_id,
                text=caption,
                parse_mode="HTML",
                disable_web_page_preview=False,
                reply_markup=reply_markup,
            )

        if result:
            log.info("Alert sent: %s (%s)", listing.title, price_str)
        else:
            log.warning("Alert FAILED for listing %s", listing.id)
        return result is not None

    async def send_message(self, text: str) -> bool:
        result = await _tg(
            "sendMessage",
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode="HTML",
        )
        return result is not None

    async def send_health_alert(self, message: str) -> bool:
        return await self.send_message(
            f"\u26a0\ufe0f <b>MarketplaceScraper Alert</b>\n\n{message}"
        )


# Module-level singleton used by TelegramPoller command handlers
_notifier = Notifier()


async def send_message(text: str) -> bool:
    return await _notifier.send_message(text)


async def send_health_alert(message: str) -> bool:
    return await _notifier.send_health_alert(message)


# ---------------------------------------------------------------------------
# Command polling
# ---------------------------------------------------------------------------

class TelegramPoller:
    def __init__(self, scheduler) -> None:
        self._scheduler = scheduler
        self._offset: int = 0
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
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

        if lower in ("yes", "y") and self._pending_delete:
            await self._confirm_delete()
            return
        if lower in ("no", "n", "cancel") and self._pending_delete:
            self._pending_delete.clear()
            await _notifier.send_message("\u274c Delete cancelled.")
            return

        if lower == "/status":           await self._cmd_status()
        elif lower == "/pause":           await self._cmd_pause()
        elif lower == "/resume":          await self._cmd_resume()
        elif lower == "/searches":        await self._cmd_searches()
        elif lower == "/scan":            await self._cmd_scan()
        elif lower == "/help":            await self._cmd_help()
        elif lower.startswith("/delete"): await self._cmd_delete(text)
        elif lower.startswith("/addsearch"): await self._cmd_addsearch(text)

    async def _cmd_status(self) -> None:
        from db.database import get_run_log, get_all_searches
        searches = await get_all_searches(enabled_only=True)
        log_entries = await get_run_log(limit=1)
        last_run = log_entries[0].get("finished_at", "Unknown") if log_entries else "Never"
        status_icon = "\u23f8\ufe0f Paused" if self._scheduler.paused else "\u25b6\ufe0f Running"
        await _notifier.send_message(
            f"\U0001f916 <b>MarketplaceScraper Status</b>\n\n"
            f"Status: {status_icon}\n"
            f"Active searches: {len(searches)}\n"
            f"Scan interval: {settings.scan_interval_minutes} min\n"
            f"Last run: {last_run}"
        )

    async def _cmd_pause(self) -> None:
        self._scheduler.pause()
        await _notifier.send_message("\u23f8\ufe0f Scheduler paused. Send /resume to restart.")

    async def _cmd_resume(self) -> None:
        self._scheduler.resume()
        await _notifier.send_message("\u25b6\ufe0f Scheduler resumed.")

    async def _cmd_scan(self) -> None:
        await _notifier.send_message("\U0001f50d Manual scan triggered...")
        asyncio.create_task(self._scheduler.run_all_searches())

    async def _cmd_searches(self) -> None:
        from db.database import get_all_searches
        searches = await get_all_searches()
        if not searches:
            await _notifier.send_message("No searches configured yet.")
            return
        lines = ["<b>Configured Searches</b>\n"]
        for s in searches:
            icon = "\u2705" if s["enabled"] else "\u274c"
            lines.append(
                f"{icon} [<code>{s['id']}</code>] <b>{_esc(s['name'])}</b>\n"
                f"   Keywords: {_esc(s['keywords'])}\n"
                f"   Price: ${s['price_min'] or 0}\u2013${s['price_max'] or '\u221e'}  "
                f"Radius: {s['distance_mi']}mi\n"
                f"   /delete {s['id']} to remove"
            )
        await _notifier.send_message("\n".join(lines))

    async def _cmd_delete(self, text: str) -> None:
        from db.database import get_search
        parts = text.strip().split()
        if len(parts) < 2 or not parts[1].isdigit():
            await _notifier.send_message(
                "\u2139\ufe0f Usage: /delete &lt;id&gt;\nSend /searches to see IDs."
            )
            return
        search_id = int(parts[1])
        search = await get_search(search_id)
        if not search:
            await _notifier.send_message(f"\u274c No search found with ID {search_id}.")
            return
        self._pending_delete = {search_id: search["name"]}
        await _notifier.send_message(
            f"\u26a0\ufe0f Delete search [{search_id}] <b>{_esc(search['name'])}</b>?\n\n"
            f"Removes all listings + seen history.\n"
            f"Reply <b>yes</b> or <b>no</b>."
        )

    async def _confirm_delete(self) -> None:
        from db.database import delete_search
        search_id, search_name = next(iter(self._pending_delete.items()))
        self._pending_delete.clear()
        try:
            await delete_search(search_id)
            await _notifier.send_message(
                f"\u2705 Search [{search_id}] <b>{_esc(search_name)}</b> deleted.\n"
                f"All listings and seen history removed."
            )
        except Exception as exc:
            await _notifier.send_message(f"\u274c Delete failed: {_esc(str(exc))}")

    async def _cmd_addsearch(self, text: str) -> None:
        from db.database import create_search
        try:
            _, args = text.split(None, 1)
            parts = [p.strip() for p in args.split("|")]
            if len(parts) < 3:
                raise ValueError
            name, keywords, zip_code = parts[0], parts[1], parts[2]
            price_max = float(parts[3]) if len(parts) > 3 else None
            search_id = await create_search(
                name=name, keywords=keywords,
                zip_code=zip_code, price_max=price_max,
            )
            await _notifier.send_message(
                f"\u2705 Search created! ID: <code>{search_id}</code>\n"
                f"Name: <b>{_esc(name)}</b>  Keywords: {_esc(keywords)}\n"
                f"Zip: {zip_code}  Max: ${price_max or '\u221e'}\n"
                f"Next scan will include this search."
            )
        except (ValueError, IndexError):
            await _notifier.send_message(
                "\u2139\ufe0f <b>Usage:</b> /addsearch name | keywords | zip | max_price\n\n"
                "<b>Example:</b>\n/addsearch Road Bike | bike | 27330 | 400"
            )

    async def _cmd_help(self) -> None:
        await _notifier.send_message(
            "\U0001f916 <b>Available Commands</b>\n\n"
            "/status — bot status and last run\n"
            "/searches — list searches with IDs\n"
            "/scan — trigger an immediate scan\n"
            "/delete &lt;id&gt; — delete a search (asks confirmation)\n"
            "/addsearch name | keywords | zip | max — add a search\n"
            "/pause — pause scanning\n"
            "/resume — resume scanning\n"
            "/help — this message"
        )
