"""
Entrypoint — starts the Playwright session, then runs the web dashboard.
The scheduler (Phase 2) will be wired in here.
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
from db.database import init_db

console = Console()

# ---------------------------------------------------------------------------
# Logging — rich console output
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
    session_path = Path(settings.session_file)
    if not session_path.exists():
        console.print()
        console.print("[bold red]  No saved Facebook session found.[/bold red]")
        console.print()
        console.print("  Run the setup script first:")
        console.print()
        console.print("  [bold cyan]  python scripts/setup_session.py[/bold cyan]")
        console.print()
        console.print("  A browser window will open — log in to Facebook (solve any")
        console.print("  CAPTCHA), then the script saves your session automatically.")
        console.print()
        sys.exit(1)


def free_port(port: int) -> None:
    """Kill any processes currently listening on the given port."""
    killed: list[int] = []
    for proc in psutil.process_iter(["pid", "connections"]):
        try:
            for conn in proc.connections(kind="inet"):
                if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                    if proc.pid == psutil.Process().pid:
                        continue  # Don’t kill ourselves
                    proc.kill()
                    killed.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if killed:
        log.info("Freed port %d by killing PID(s): %s", port, killed)


# ---------------------------------------------------------------------------
# FastAPI lifespan — startup / shutdown hooks
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting MarketplaceScraper...")
    await init_db()
    log.info("Database initialised.")
    await session_manager.start()
    log.info("Browser session ready.")
    yield
    log.info("Shutting down...")
    await session_manager.stop()


app = FastAPI(title="MarketplaceScraper", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes — dashboard routes added in Phase 4
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "interval_minutes": settings.scan_interval_minutes}


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
