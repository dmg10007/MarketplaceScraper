"""
Entrypoint — starts the Playwright session, scheduler, and Telegram poller,
then serves the web dashboard.
"""

import sys
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
import uvicorn
from fastapi import FastAPI
from rich.logging import RichHandler
from rich.console import Console

from config.settings import settings
from core.scraper import session_manager
from core.scheduler import scheduler
from core.notifier import TelegramPoller
from db.database import init_db

console = Console()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)],
)
log = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def check_session_exists() -> None:
    if not Path(settings.session_file).exists():
        console.print()
        console.print("[bold red]  No saved Facebook session found.[/bold red]")
        console.print()
        console.print("  Run the setup script first:")
        console.print("  [bold cyan]  python scripts/setup_session.py[/bold cyan]")
        console.print()
        sys.exit(1)


def free_port(port: int) -> None:
    """Kill any PIDs currently listening on the given port."""
    killed: list[int] = []
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        log.warning("Cannot check port %d — run as Administrator to auto-free ports.", port)
        return

    pids_on_port = {
        c.pid for c in conns
        if c.laddr.port == port
        and c.status == psutil.CONN_LISTEN
        and c.pid is not None
        and c.pid != psutil.Process().pid
    }
    for pid in pids_on_port:
        try:
            psutil.Process(pid).kill()
            killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if killed:
        log.info("Freed port %d by killing PID(s): %s", port, killed)


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting MarketplaceScraper...")
    await init_db()
    log.info("Database initialised.")

    # Browser session
    await session_manager.start()
    log.info("Browser session ready.")

    # Wire scheduler dependencies and start
    poller = TelegramPoller(scheduler)
    scheduler.wire(session_manager, poller)
    await scheduler.start()
    log.info("Scheduler started.")

    # Telegram command poller
    await poller.start()
    log.info("Telegram poller ready. Send /help to your bot to test.")

    yield

    # Shutdown
    log.info("Shutting down...")
    await poller.stop()
    await scheduler.stop()
    await session_manager.stop()


app = FastAPI(title="MarketplaceScraper", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes — Phase 4 dashboard routes added next
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    from db.database import get_last_successful_run
    last = await get_last_successful_run()
    return {
        "status": "ok",
        "paused": scheduler.paused,
        "interval_minutes": settings.scan_interval_minutes,
        "last_successful_run": last["finished_at"] if last else None,
    }


@app.post("/scan/trigger")
async def trigger_scan():
    """Manually kick off a full scan cycle."""
    import asyncio
    asyncio.create_task(scheduler.run_all_searches())
    return {"status": "scan triggered"}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    check_session_exists()
    free_port(settings.dashboard_port)
    uvicorn.run(
        "main:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
        log_config=None,
    )
