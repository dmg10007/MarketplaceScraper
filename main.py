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
from fastapi import FastAPI, HTTPException, Request
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
async def run_log_route(limit: int = 50):
    from db.database import get_run_log
    return await get_run_log(limit=limit)


# ---------------------------------------------------------------------------
# HTMX partial fragments
# ---------------------------------------------------------------------------

def _listing_card(l: dict) -> str:
    price = f"${l['price']:.0f}" if l.get("price") else "Free"
    location = l.get("location") or ""
    condition = l.get("condition") or ""
    img_url = l.get("image_url") or ""
    title = l.get("title") or "(no title)"
    listing_url = l.get("listing_url") or "#"
    lid = l["id"]

    img_html = (
        f'<img src="{img_url}" alt="" loading="lazy" onerror="this.style.display=\'none\'" />'
        if img_url else
        '<div class="img-placeholder"><span>No image</span></div>'
    )

    cond_badge = f'<span class="cond-badge">{condition}</span>' if condition else ""

    return f"""
    <div class="listing-card" id="card-{lid}">
      <div class="card-img">
        {img_html}
        <span class="price-tag">{price}</span>
      </div>
      <div class="card-body">
        <a href="{listing_url}" target="_blank" rel="noopener" class="card-title">{title}</a>
        <div class="card-meta">
          {'<span>&#128205; ' + location + '</span>' if location else ''}
          {cond_badge}
        </div>
        <button class="btn-dismiss"
          hx-post="/listings/{lid}/dismiss"
          hx-target="#card-{lid}"
          hx-swap="outerHTML swap:0.4s">
          &#10005; Not interested
        </button>
      </div>
    </div>"""


@app.get("/partials/listings", response_class=HTMLResponse)
async def partial_listings(search_id: Optional[int] = None, limit: int = 48):
    from db.database import get_listings
    listings = await get_listings(search_id=search_id, limit=limit)
    if not listings:
        return HTMLResponse('<div class="empty-state">&#128722; No listings found yet. Run a scan to get started.</div>')
    return HTMLResponse("".join(_listing_card(l) for l in listings))


@app.get("/partials/runlog", response_class=HTMLResponse)
async def partial_runlog(limit: int = 20):
    from db.database import get_run_log
    entries = await get_run_log(limit=limit)
    if not entries:
        return HTMLResponse('<tr><td colspan="4" style="color:var(--muted);text-align:center">No runs yet</td></tr>')
    rows = ""
    for e in entries:
        cls = "green" if e["status"] == "success" else "red"
        rows += f"""
        <tr>
          <td>{e.get('search_name') or '&mdash;'}</td>
          <td><span class="badge {cls}">{e['status']}</span></td>
          <td>{e['new_listings']}</td>
          <td style="color:var(--muted);font-size:.78rem">{e['finished_at']}</td>
        </tr>"""
    return HTMLResponse(rows)


@app.get("/partials/searches", response_class=HTMLResponse)
async def partial_searches():
    from db.database import get_all_searches
    searches = await get_all_searches()
    if not searches:
        return HTMLResponse('<tr><td colspan="7" style="color:var(--muted);text-align:center">No searches configured</td></tr>')
    rows = ""
    for s in searches:
        badge = (
            '<span class="badge green">enabled</span>' if s["enabled"]
            else '<span class="badge red">disabled</span>'
        )
        pmin = f"${s['price_min']:.0f}" if s.get('price_min') else '$0'
        pmax = f"${s['price_max']:.0f}" if s.get('price_max') else '&infin;'
        rows += f"""
        <tr id="search-row-{s['id']}">
          <td style="color:var(--muted)">{s['id']}</td>
          <td><b>{s['name']}</b></td>
          <td><code style="font-size:.78rem">{s['keywords']}</code></td>
          <td>{pmin}&ndash;{pmax}</td>
          <td>{s['distance_mi']} mi</td>
          <td>{badge}</td>
          <td>
            <button class="btn-dismiss"
              hx-delete="/searches/{s['id']}"
              hx-confirm="Delete search '{s['name']}'?"
              hx-target="#search-row-{s['id']}"
              hx-swap="outerHTML swap:0.3s"
            >Delete</button>
          </td>
        </tr>"""
    return HTMLResponse(rows)


# ---------------------------------------------------------------------------
# Main dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    from db.database import get_all_searches, get_last_successful_run
    searches = await get_all_searches()
    last = await get_last_successful_run()
    last_run_str = last["finished_at"] if last else "Never"
    status_str = "&#9646;&#9646; Paused" if scheduler.paused else "&#9654; Running"
    status_color = "#f59e0b" if scheduler.paused else "#22c55e"
    active_count = len([s for s in searches if s['enabled']])

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MarketplaceScraper</title>
  <script src="https://unpkg.com/htmx.org@1.9.12" defer></script>
  <style>
    :root {{
      --bg: #0d0f14;
      --surface: #161920;
      --surface-2: #1c1f2a;
      --border: #252837;
      --text: #e2e8f0;
      --muted: #64748b;
      --faint: #334155;
      --primary: #6366f1;
      --primary-hover: #4f46e5;
      --green: #22c55e;
      --red: #ef4444;
      --amber: #f59e0b;
      --radius: .5rem;
      --transition: 180ms cubic-bezier(.16,1,.3,1);
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
            font-size: 14px; line-height: 1.6; }}

    /* ---- Header ---- */
    header {{
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: .85rem 1.5rem;
      display: flex; align-items: center; gap: .75rem;
      position: sticky; top: 0; z-index: 50;
    }}
    .logo {{ display: flex; align-items: center; gap: .5rem; }}
    .logo svg {{ width: 22px; height: 22px; color: var(--primary); }}
    .logo h1 {{ font-size: 1rem; font-weight: 700; letter-spacing: -.01em; }}
    .status-dot {{ width: 8px; height: 8px; border-radius: 50%;
                   background: {status_color}; flex-shrink: 0; }}
    .pill {{ padding: .2rem .6rem; border-radius: 999px; font-size: .7rem;
             background: var(--surface-2); border: 1px solid var(--border); color: var(--muted); }}
    .header-actions {{ margin-left: auto; display: flex; gap: .5rem; align-items: center; }}
    .btn {{
      padding: .4rem .9rem; border-radius: var(--radius); border: none;
      cursor: pointer; font-size: .8rem; font-weight: 600; transition: background var(--transition);
    }}
    .btn-primary {{ background: var(--primary); color: #fff; }}
    .btn-primary:hover {{ background: var(--primary-hover); }}
    .btn-secondary {{
      background: transparent; color: var(--text);
      border: 1px solid var(--border);
    }}
    .btn-secondary:hover {{ background: var(--surface-2); }}

    /* ---- Layout ---- */
    .page {{ display: grid; grid-template-columns: 260px 1fr; min-height: calc(100vh - 53px); }}
    .sidebar {{
      background: var(--surface); border-right: 1px solid var(--border);
      padding: 1.25rem; overflow-y: auto;
    }}
    .main-content {{ padding: 1.5rem; overflow-y: auto; }}

    /* ---- Sidebar ---- */
    .sidebar-section {{ margin-bottom: 1.75rem; }}
    .sidebar-label {{
      font-size: .65rem; text-transform: uppercase; letter-spacing: .07em;
      color: var(--muted); margin-bottom: .6rem; display: block;
    }}
    .kpi-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; }}
    .kpi {{
      background: var(--surface-2); border: 1px solid var(--border);
      border-radius: var(--radius); padding: .75rem;
    }}
    .kpi .kv {{ font-size: 1.35rem; font-weight: 700; line-height: 1; }}
    .kpi .kl {{ font-size: .65rem; color: var(--muted); margin-top: .2rem; }}

    /* ---- Add search form ---- */
    .add-form {{
      background: var(--surface-2); border: 1px solid var(--border);
      border-radius: var(--radius); padding: .9rem;
    }}
    .add-form .form-grid {{
      display: grid; grid-template-columns: 1fr 1fr; gap: .4rem;
      margin-bottom: .5rem;
    }}
    .add-form .full {{ grid-column: 1/-1; }}
    .add-form input, .add-form select {{
      width: 100%; background: var(--bg); border: 1px solid var(--border);
      color: var(--text); padding: .35rem .55rem; border-radius: .35rem;
      font-size: .78rem;
    }}
    .add-form input::placeholder {{ color: var(--muted); }}
    .add-form input:focus, .add-form select:focus {{
      outline: 2px solid var(--primary); outline-offset: 1px;
    }}

    /* ---- Tabs ---- */
    .tabs {{ display: flex; gap: .25rem; margin-bottom: 1.25rem; border-bottom: 1px solid var(--border); }}
    .tab-btn {{
      padding: .5rem 1rem; font-size: .82rem; font-weight: 500;
      color: var(--muted); background: none; border: none; cursor: pointer;
      border-bottom: 2px solid transparent; margin-bottom: -1px;
      transition: color var(--transition), border-color var(--transition);
    }}
    .tab-btn.active {{ color: var(--text); border-bottom-color: var(--primary); }}
    .tab-btn:hover:not(.active) {{ color: var(--text); }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}

    /* ---- Listing cards grid ---- */
    .listings-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      gap: .9rem;
    }}
    .listing-card {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); overflow: hidden;
      transition: box-shadow var(--transition), transform var(--transition);
    }}
    .listing-card:hover {{
      box-shadow: 0 8px 24px rgba(0,0,0,.4);
      transform: translateY(-2px);
    }}
    .card-img {{
      position: relative; aspect-ratio: 4/3;
      background: var(--surface-2); overflow: hidden;
    }}
    .card-img img {{ width: 100%; height: 100%; object-fit: cover; }}
    .img-placeholder {{
      width: 100%; height: 100%; display: flex; align-items: center;
      justify-content: center; color: var(--faint); font-size: .75rem;
    }}
    .price-tag {{
      position: absolute; bottom: .5rem; left: .5rem;
      background: rgba(0,0,0,.75); backdrop-filter: blur(4px);
      color: #fff; font-weight: 700; font-size: .85rem;
      padding: .15rem .45rem; border-radius: .3rem;
    }}
    .card-body {{ padding: .65rem; }}
    .card-title {{
      color: var(--text); text-decoration: none; font-size: .82rem;
      font-weight: 600; display: block; margin-bottom: .35rem;
      display: -webkit-box; -webkit-line-clamp: 2;
      -webkit-box-orient: vertical; overflow: hidden;
    }}
    .card-title:hover {{ color: var(--primary); }}
    .card-meta {{
      font-size: .7rem; color: var(--muted); display: flex;
      gap: .4rem; flex-wrap: wrap; margin-bottom: .5rem;
    }}
    .cond-badge {{
      background: var(--surface-2); border: 1px solid var(--border);
      border-radius: .2rem; padding: .05rem .35rem; font-size: .65rem;
    }}
    .btn-dismiss {{
      width: 100%; padding: .3rem; background: transparent;
      border: 1px solid var(--faint); border-radius: .3rem;
      color: var(--muted); font-size: .72rem; cursor: pointer;
      transition: all var(--transition);
    }}
    .btn-dismiss:hover {{
      border-color: var(--red); color: var(--red);
      background: rgba(239,68,68,.08);
    }}
    .htmx-swapping {{ opacity: 0 !important; transform: scale(.95); transition: all .4s ease; }}

    /* ---- Table ---- */
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: .5rem .75rem; border-bottom: 1px solid var(--border); }}
    th {{ font-size: .65rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: var(--surface-2); }}
    code {{ background: var(--surface-2); border: 1px solid var(--border); border-radius: .2rem;
             padding: .05rem .3rem; font-size: .78rem; }}

    .badge {{ padding: .15rem .45rem; border-radius: .25rem; font-size: .68rem; font-weight: 600; }}
    .badge.green {{ background: #14532d; color: #86efac; }}
    .badge.red   {{ background: #7f1d1d; color: #fca5a5; }}
    .badge.amber {{ background: #78350f; color: #fcd34d; }}

    .empty-state {{
      grid-column: 1/-1; text-align: center; padding: 3rem 1rem;
      color: var(--muted); font-size: .9rem;
    }}

    /* ---- Scan progress indicator ---- */
    #scan-indicator {{
      display: none; align-items: center; gap: .4rem;
      font-size: .75rem; color: var(--amber);
    }}
    #scan-indicator.visible {{ display: flex; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .spinner {{
      width: 12px; height: 12px; border: 2px solid var(--faint);
      border-top-color: var(--amber); border-radius: 50%;
      animation: spin .7s linear infinite;
    }}

    /* ---- Responsive ---- */
    @media (max-width: 768px) {{
      .page {{ grid-template-columns: 1fr; }}
      .sidebar {{ border-right: none; border-bottom: 1px solid var(--border); }}
      .listings-grid {{ grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }}
    }}
  </style>
</head>
<body>

<header>
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M6 2 3 6v14a2 2 0 002 2h14a2 2 0 002-2V6l-3-4z"/>
      <line x1="3" y1="6" x2="21" y2="6"/>
      <path d="M16 10a4 4 0 01-8 0"/>
    </svg>
    <h1>MarketplaceScraper</h1>
  </div>
  <div class="status-dot"></div>
  <span class="pill">{status_str}</span>
  <span class="pill" id="last-run-pill">Last: {last_run_str}</span>
  <div id="scan-indicator">
    <div class="spinner"></div>
    Scanning&hellip;
  </div>
  <div class="header-actions">
    <button class="btn btn-secondary"
      hx-get="/partials/listings"
      hx-target="#listings-grid"
      hx-swap="innerHTML"
      hx-on::before-request="document.getElementById('scan-indicator').classList.add('visible')"
      hx-on::after-request="document.getElementById('scan-indicator').classList.remove('visible')">
      &#8635; Refresh
    </button>
    <button class="btn btn-primary" id="scan-btn"
      hx-post="/scan/trigger"
      hx-swap="none"
      hx-on::before-request="this.disabled=true; this.textContent='Scanning…'; document.getElementById('scan-indicator').classList.add('visible')"
      hx-on::after-request="setTimeout(()=>{{this.disabled=false; this.textContent='&#9654; Scan Now'; document.getElementById('scan-indicator').classList.remove('visible'); htmx.trigger('#listings-grid','refresh');}}, 8000)">
      &#9654; Scan Now
    </button>
  </div>
</header>

<div class="page">

  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-section">
      <span class="sidebar-label">Overview</span>
      <div class="kpi-grid">
        <div class="kpi"><div class="kv">{active_count}</div><div class="kl">Searches</div></div>
        <div class="kpi"><div class="kv">{settings.scan_interval_minutes}m</div><div class="kl">Interval</div></div>
      </div>
    </div>

    <div class="sidebar-section">
      <span class="sidebar-label">Add Search</span>
      <form class="add-form"
        hx-post="/searches"
        hx-target="#searches-tbody"
        hx-swap="innerHTML"
        hx-on::after-request="this.reset(); htmx.ajax('GET','/partials/searches',{{target:'#searches-tbody',swap:'innerHTML'}})">
        <div class="form-grid">
          <input class="full" name="name" placeholder="Name *" required>
          <input class="full" name="keywords" placeholder="Keywords *" required>
          <input class="full" name="zip_code" placeholder="Zip code *" required>
          <input name="price_min" placeholder="Min $" type="number">
          <input name="price_max" placeholder="Max $" type="number">
          <input name="distance_mi" placeholder="Radius mi" type="number" value="40">
          <input class="full" name="neg_keywords" placeholder="Exclude keywords">
          <select class="full" name="condition">
            <option value="any">Any condition</option>
            <option value="new">New</option>
            <option value="used_like_new">Like new</option>
            <option value="used_good">Good</option>
            <option value="used_fair">Fair</option>
          </select>
        </div>
        <button type="submit" class="btn btn-primary" style="width:100%">+ Add Search</button>
      </form>
    </div>

    <div class="sidebar-section">
      <span class="sidebar-label">Scan Log</span>
      <table>
        <thead><tr><th>Search</th><th>Status</th><th>New</th></tr></thead>
        <tbody id="runlog-tbody"
          hx-get="/partials/runlog"
          hx-trigger="load, every 60s"
          hx-swap="innerHTML">
        </tbody>
      </table>
    </div>
  </aside>

  <!-- Main content -->
  <main class="main-content">
    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab(this,'tab-listings')">&#128722; Listings</button>
      <button class="tab-btn" onclick="switchTab(this,'tab-searches')">&#128269; Searches</button>
    </div>

    <!-- Listings tab -->
    <div class="tab-panel active" id="tab-listings">
      <div class="listings-grid" id="listings-grid"
        hx-get="/partials/listings"
        hx-trigger="load, refresh"
        hx-swap="innerHTML">
        <div class="empty-state">Loading listings&hellip;</div>
      </div>
    </div>

    <!-- Searches tab -->
    <div class="tab-panel" id="tab-searches">
      <table>
        <thead><tr>
          <th>ID</th><th>Name</th><th>Keywords</th>
          <th>Price</th><th>Radius</th><th>Status</th><th></th>
        </tr></thead>
        <tbody id="searches-tbody"
          hx-get="/partials/searches"
          hx-trigger="load"
          hx-swap="innerHTML">
        </tbody>
      </table>
    </div>
  </main>
</div>

<script>
function switchTab(btn, panelId) {{
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(panelId).classList.add('active');
  if (panelId === 'tab-searches') {{
    htmx.ajax('GET', '/partials/searches', {{target:'#searches-tbody', swap:'innerHTML'}});
  }}
}}
</script>
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
