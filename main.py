"""
Entrypoint — Playwright session, scheduler, Telegram poller, web dashboard.
"""

import asyncio
import sys
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from rich.logging import RichHandler
from rich.console import Console

from config.settings import settings
from core.scraper import session_manager
from core.scheduler import scheduler
from core.notifier import Notifier, TelegramPoller
from db.database import init_db

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)],
)
log = logging.getLogger("main")


def check_session_exists() -> None:
    if not Path(settings.session_file).exists():
        console.print()
        console.print("[bold red]  No saved Facebook session found.[/bold red]")
        console.print("  Run: [bold cyan]python scripts/setup_session.py[/bold cyan]")
        console.print()
        sys.exit(1)


def free_port(port: int) -> None:
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return
    for c in conns:
        if (
            c.laddr.port == port
            and c.status == psutil.CONN_LISTEN
            and c.pid
            and c.pid != psutil.Process().pid
        ):
            try:
                psutil.Process(c.pid).kill()
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting MarketplaceScraper...")
    await init_db()
    log.info("Database initialised.")

    await session_manager.start()
    log.info("Browser session ready.")

    # KEY FIX: wire Notifier (has send_alert), not TelegramPoller
    notifier = Notifier()
    poller = TelegramPoller(scheduler)
    scheduler.wire(session_manager, notifier)
    await scheduler.start()
    log.info("Scheduler started.")

    await poller.start()
    log.info("Telegram poller ready. Send /help to your bot to test.")

    yield

    log.info("Shutting down...")
    await poller.stop()
    await scheduler.stop()
    await session_manager.stop()


app = FastAPI(title="MarketplaceScraper", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SearchCreate(BaseModel):
    name: str
    keywords: str
    zip_code: str
    neg_keywords: str = ""
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    distance_mi: int = 40
    condition: str = "any"


# ---------------------------------------------------------------------------
# API routes
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
    asyncio.create_task(scheduler.run_all_searches())
    return {"status": "scan triggered"}


@app.get("/searches")
async def list_searches():
    from db.database import get_all_searches
    return await get_all_searches()


@app.post("/searches", status_code=201)
async def create_search_route(body: SearchCreate):
    from db.database import create_search
    search_id = await create_search(
        name=body.name, keywords=body.keywords, zip_code=body.zip_code,
        neg_keywords=body.neg_keywords, price_min=body.price_min,
        price_max=body.price_max, distance_mi=body.distance_mi,
        condition=body.condition,
    )
    return {"id": search_id, "name": body.name}


@app.delete("/searches/{search_id}")
async def delete_search_route(search_id: int):
    from db.database import delete_search, get_search
    if not await get_search(search_id):
        raise HTTPException(status_code=404, detail="Search not found")
    await delete_search(search_id)
    return {"deleted": search_id}


@app.get("/listings")
async def list_listings(search_id: Optional[int] = None, limit: int = 50):
    from db.database import get_listings
    return await get_listings(search_id=search_id, limit=limit)


@app.post("/listings/{listing_id}/dismiss")
async def dismiss_listing_route(listing_id: str):
    from db.database import dismiss_listing
    await dismiss_listing(listing_id)
    return {"dismissed": listing_id}


@app.get("/runlog")
async def run_log(limit: int = 50):
    from db.database import get_run_log
    return await get_run_log(limit=limit)


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    from db.database import get_all_searches, get_run_log, get_last_successful_run
    searches = await get_all_searches()
    log_entries = await get_run_log(limit=15)
    last = await get_last_successful_run()

    last_run_str = last["finished_at"] if last else "Never"
    status_str = "\u23f8 Paused" if scheduler.paused else "\u25b6 Running"

    search_rows = ""
    for s in searches:
        badge = (
            '<span class="badge green">enabled</span>' if s["enabled"]
            else '<span class="badge red">disabled</span>'
        )
        search_rows += f"""
        <tr>
          <td>{s['id']}</td>
          <td><b>{s['name']}</b></td>
          <td>{s['keywords']}</td>
          <td>${s['price_min'] or 0}&ndash;${s['price_max'] or '&infin;'}</td>
          <td>{s['distance_mi']} mi</td>
          <td>{badge}</td>
          <td>
            <button class="btn-danger"
              hx-delete="/searches/{s['id']}"
              hx-confirm="Delete search '{s['name']}'?"
              hx-target="closest tr"
              hx-swap="outerHTML swap:0.3s"
            >Delete</button>
          </td>
        </tr>"""

    log_rows = ""
    for e in log_entries:
        cls = "green" if e["status"] == "success" else "red"
        log_rows += f"""
        <tr>
          <td>{e.get('search_name') or '&mdash;'}</td>
          <td><span class="badge {cls}">{e['status']}</span></td>
          <td>{e['new_listings']}</td>
          <td>{e['finished_at']}</td>
        </tr>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MarketplaceScraper</title>
  <script src="https://unpkg.com/htmx.org@1.9.12"></script>
  <style>
    :root {{
      --bg:#0f1117;--surface:#1a1d27;--border:#2a2d3e;
      --text:#e2e8f0;--muted:#94a3b8;--primary:#6366f1;
    }}
    *{{box-sizing:border-box;margin:0;padding:0;}}
    body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.6;}}
    header{{background:var(--surface);border-bottom:1px solid var(--border);padding:1rem 2rem;display:flex;align-items:center;gap:.75rem;}}
    header h1{{font-size:1.15rem;font-weight:700;}}
    .pill{{padding:.2rem .65rem;border-radius:999px;font-size:.72rem;background:#1e293b;border:1px solid var(--border);color:var(--muted);}}
    main{{max-width:1100px;margin:2rem auto;padding:0 1.5rem;}}
    .kpis{{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin-bottom:2rem;}}
    .kpi{{background:var(--surface);border:1px solid var(--border);border-radius:.5rem;padding:1.25rem;}}
    .kpi .label{{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:.25rem;}}
    .kpi .value{{font-size:1.4rem;font-weight:700;}}
    section{{margin-bottom:2.5rem;}}
    section h2{{font-size:.95rem;font-weight:600;margin-bottom:1rem;padding-bottom:.5rem;border-bottom:1px solid var(--border);}}
    table{{width:100%;border-collapse:collapse;}}
    th,td{{text-align:left;padding:.55rem .75rem;border-bottom:1px solid var(--border);}}
    th{{font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);}}
    tr:last-child td{{border-bottom:none;}}
    .badge{{padding:.15rem .45rem;border-radius:.25rem;font-size:.68rem;font-weight:600;}}
    .badge.green{{background:#166534;color:#86efac;}}
    .badge.red{{background:#7f1d1d;color:#fca5a5;}}
    .btn{{padding:.4rem .9rem;border-radius:.375rem;border:none;cursor:pointer;font-size:.8rem;font-weight:600;}}
    .btn-primary{{background:var(--primary);color:#fff;}}
    .btn-primary:hover{{background:#4f46e5;}}
    .btn-danger{{background:transparent;color:#ef4444;border:1px solid #ef4444;padding:.22rem .55rem;border-radius:.25rem;cursor:pointer;font-size:.72rem;}}
    .btn-danger:hover{{background:#7f1d1d33;}}
    form.add{{background:var(--surface);border:1px solid var(--border);border-radius:.5rem;padding:1.25rem;
              display:grid;grid-template-columns:repeat(auto-fill,minmax(175px,1fr));gap:.65rem;margin-bottom:1rem;}}
    form.add input,form.add select{{background:var(--bg);border:1px solid var(--border);color:var(--text);
      padding:.38rem .6rem;border-radius:.35rem;font-size:.83rem;width:100%;}}
    form.add input::placeholder{{color:var(--muted);}}
    form.add .full{{grid-column:1/-1;}}
    .htmx-swapping{{opacity:0;transition:opacity .3s;}}
    @media(max-width:640px){{.kpis{{grid-template-columns:1fr;}}form.add{{grid-template-columns:1fr;}}}}
  </style>
</head>
<body>
  <header>
    <h1>&#x1F6D2; MarketplaceScraper</h1>
    <span class="pill">{status_str}</span>
    <span class="pill">Last scan: {last_run_str}</span>
    <button class="btn btn-primary" style="margin-left:auto"
      hx-post="/scan/trigger" hx-swap="none">&#9654; Scan Now</button>
  </header>

  <main>
    <div class="kpis">
      <div class="kpi"><div class="label">Active Searches</div>
        <div class="value">{len([s for s in searches if s['enabled']])}</div></div>
      <div class="kpi"><div class="label">Scan Interval</div>
        <div class="value">{settings.scan_interval_minutes} min</div></div>
      <div class="kpi"><div class="label">Status</div>
        <div class="value" style="font-size:1rem">{status_str}</div></div>
    </div>

    <section>
      <h2>Searches</h2>
      <form class="add"
        hx-post="/searches"
        hx-target="body" hx-swap="outerHTML"
        hx-on::after-request="this.reset()">
        <input name="name"         placeholder="Name *" required>
        <input name="keywords"     placeholder="Keywords * (comma-sep)" required>
        <input name="zip_code"     placeholder="Zip code *" required>
        <input name="price_min"    placeholder="Min price" type="number">
        <input name="price_max"    placeholder="Max price" type="number">
        <input name="distance_mi" placeholder="Radius (mi)" type="number" value="40">
        <input name="neg_keywords" placeholder="Negative keywords">
        <select name="condition">
          <option value="any">Any condition</option>
          <option value="new">New</option>
          <option value="used_like_new">Used &ndash; like new</option>
          <option value="used_good">Used &ndash; good</option>
          <option value="used_fair">Used &ndash; fair</option>
        </select>
        <div class="full"><button type="submit" class="btn btn-primary">+ Add Search</button></div>
      </form>

      <table>
        <thead><tr>
          <th>ID</th><th>Name</th><th>Keywords</th>
          <th>Price</th><th>Radius</th><th>Status</th><th></th>
        </tr></thead>
        <tbody>{search_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>Recent Scan Log</h2>
      <table>
        <thead><tr>
          <th>Search</th><th>Status</th><th>New Listings</th><th>Finished</th>
        </tr></thead>
        <tbody>{log_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>""")


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
