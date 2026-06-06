"""
Entrypoint — starts the Playwright session, then runs the web dashboard.
The scheduler (Phase 2) will be wired in here.
"""

import sys
import logging
from contextlib import asynccontextmanager
from pathlib import Path

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
# Pre-flight check — require a saved session before starting
# ---------------------------------------------------------------------------

def check_session_exists() -> None:
    session_path = Path(settings.session_file)
    if not session_path.exists():
        console.print()
        console.print("[bold red]  No saved Facebook session found.[/bold red]")
        console.print()
        console.print("  Before running main.py, you need to log in once manually so")
        console.print("  the bot can save your session cookies.")
        console.print()
        console.print("  Run the setup script first:")
        console.print()
        console.print("  [bold cyan]  python scripts/setup_session.py[/bold cyan]")
        console.print()
        console.print("  A browser window will open. Log in to Facebook (solve any")
        console.print("  CAPTCHA), then the script will save your session automatically.")
        console.print()
        sys.exit(1)


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
    uvicorn.run(
        "main:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
        log_config=None,
    )
