# MarketplaceScraper

Automated Facebook Marketplace scraper with Telegram alerts and a local web dashboard.

## Architecture

```
Ingestion (Playwright) → Parsing → Filtering/Dedup → Notification (Telegram)
                                         ↓
                               Web Dashboard (FastAPI)
```

## Setup

### 1. Prerequisites

- Python 3.11+
- Windows 11 (headful browser mode is the default)

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure

```bash
copy .env.example .env
# Edit .env with your Facebook credentials and Telegram bot token
```

### 4. Get your Telegram credentials

1. Message **@BotFather** on Telegram → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`
2. Message **@userinfobot** on Telegram → copy the number into `TELEGRAM_CHAT_ID`

### 5. Run

```bash
python main.py
```

The dashboard will be available at `http://127.0.0.1:8080`.

On first run, a browser window will open and log in to Facebook.
The session is saved to `data/fb_session.json` so subsequent runs skip the login step.

## Project Structure

```
MarketplaceScraper/
├── core/
│   ├── scraper.py        # Playwright session manager
│   └── parser.py         # DOM extraction → Listing objects
├── db/
│   └── database.py       # SQLite schema + async CRUD helpers
├── config/
│   └── settings.py       # pydantic-settings config loader
├── main.py               # Entrypoint: FastAPI + lifespan hooks
├── requirements.txt
├── .env.example
└── README.md
```

## Development Phases

- [x] **Phase 1** — Foundation: DB schema, config, session manager, parser
- [ ] **Phase 2** — Scraping loop: scheduler, filters, deduplication
- [ ] **Phase 3** — Telegram notifications
- [ ] **Phase 4** — Web dashboard
- [ ] **Phase 5** — Hardening: health monitoring, proxy layer, re-login
