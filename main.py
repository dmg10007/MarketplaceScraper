"""
Entrypoint — starts the Playwright session, then runs the web dashboard.
The scheduler (Phase 2) will be wired in here.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from rich.logging import RichHandler

from config.settings import settings
from core.scraper import session_manager
from db.database import init_db

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
# Placeholder route — dashboard routes added in Phase 4
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "interval_minutes": settings.scan_interval_minutes}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
        log_config=None,  # Use our Rich logger instead
    )
