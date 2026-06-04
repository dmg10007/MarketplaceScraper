"""
Database layer — SQLite via aiosqlite.
Tables: searches, listings, seen_ids, run_log
"""

import aiosqlite
from pathlib import Path
from datetime import datetime
from typing import Optional

DB_PATH = Path("data/marketplace.db")


CREATE_SEARCHES = """
CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    keywords    TEXT    NOT NULL,
    neg_keywords TEXT   NOT NULL DEFAULT '',
    price_min   REAL,
    price_max   REAL,
    distance_mi INTEGER NOT NULL DEFAULT 40,
    zip_code    TEXT    NOT NULL,
    condition   TEXT    NOT NULL DEFAULT 'any',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_LISTINGS = """
CREATE TABLE IF NOT EXISTS listings (
    id          TEXT    PRIMARY KEY,
    search_id   INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    price       REAL,
    location    TEXT,
    image_url   TEXT,
    listing_url TEXT    NOT NULL,
    condition   TEXT,
    alerted     INTEGER NOT NULL DEFAULT 0,
    dismissed   INTEGER NOT NULL DEFAULT 0,
    found_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_SEEN_IDS = """
CREATE TABLE IF NOT EXISTS seen_ids (
    listing_id  TEXT    NOT NULL,
    search_id   INTEGER NOT NULL,
    seen_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (listing_id, search_id)
);
"""

CREATE_RUN_LOG = """
CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id   INTEGER REFERENCES searches(id) ON DELETE SET NULL,
    status      TEXT    NOT NULL,  -- 'success' | 'error' | 'skipped'
    listings_found INTEGER NOT NULL DEFAULT 0,
    new_listings   INTEGER NOT NULL DEFAULT 0,
    error_msg   TEXT,
    started_at  TEXT    NOT NULL,
    finished_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


async def init_db() -> None:
    """Create tables and ensure data directory exists."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute(CREATE_SEARCHES)
        await db.execute(CREATE_LISTINGS)
        await db.execute(CREATE_SEEN_IDS)
        await db.execute(CREATE_RUN_LOG)
        await db.commit()


def get_db():
    """Async context manager — yields an open aiosqlite connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return aiosqlite.connect(DB_PATH)


# ---------------------------------------------------------------------------
# Search CRUD
# ---------------------------------------------------------------------------

async def create_search(
    name: str,
    keywords: str,
    zip_code: str,
    neg_keywords: str = "",
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    distance_mi: int = 40,
    condition: str = "any",
) -> int:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON;")
        cursor = await db.execute(
            """
            INSERT INTO searches (name, keywords, neg_keywords, price_min, price_max,
                                  distance_mi, zip_code, condition)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, keywords, neg_keywords, price_min, price_max, distance_mi, zip_code, condition),
        )
        await db.commit()
        return cursor.lastrowid


async def get_all_searches(enabled_only: bool = False) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM searches"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id"
        cursor = await db.execute(query)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_search(search_id: int) -> Optional[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM searches WHERE id = ?", (search_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_search(search_id: int, **fields) -> None:
    allowed = {"name", "keywords", "neg_keywords", "price_min", "price_max",
               "distance_mi", "zip_code", "condition", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    async with get_db() as db:
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute(
            f"UPDATE searches SET {set_clause} WHERE id = ?",
            (*updates.values(), search_id),
        )
        await db.commit()


async def delete_search(search_id: int) -> None:
    async with get_db() as db:
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute("DELETE FROM searches WHERE id = ?", (search_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Listing CRUD
# ---------------------------------------------------------------------------

async def upsert_listing(
    listing_id: str,
    search_id: int,
    title: str,
    listing_url: str,
    price: Optional[float] = None,
    location: Optional[str] = None,
    image_url: Optional[str] = None,
    condition: Optional[str] = None,
) -> None:
    async with get_db() as db:
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute(
            """
            INSERT OR IGNORE INTO listings
                (id, search_id, title, price, location, image_url, listing_url, condition)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (listing_id, search_id, title, price, location, image_url, listing_url, condition),
        )
        await db.commit()


async def is_seen(listing_id: str, search_id: int) -> bool:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT 1 FROM seen_ids WHERE listing_id = ? AND search_id = ?",
            (listing_id, search_id),
        )
        return await cursor.fetchone() is not None


async def mark_seen(listing_id: str, search_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_ids (listing_id, search_id) VALUES (?, ?)",
            (listing_id, search_id),
        )
        await db.commit()


async def mark_alerted(listing_id: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE listings SET alerted = 1 WHERE id = ?", (listing_id,))
        await db.commit()


async def dismiss_listing(listing_id: str) -> None:
    """Mark dismissed — suppresses future duplicate alerts for this listing."""
    async with get_db() as db:
        await db.execute("UPDATE listings SET dismissed = 1 WHERE id = ?", (listing_id,))
        await db.commit()


async def get_listings(
    search_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    dismissed: bool = False,
) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        where_clauses = [f"dismissed = {1 if dismissed else 0}"]
        params: list = []
        if search_id is not None:
            where_clauses.append("search_id = ?")
            params.append(search_id)
        where = " AND ".join(where_clauses)
        cursor = await db.execute(
            f"SELECT * FROM listings WHERE {where} ORDER BY found_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Run Log
# ---------------------------------------------------------------------------

async def log_run(
    search_id: Optional[int],
    status: str,
    listings_found: int = 0,
    new_listings: int = 0,
    error_msg: Optional[str] = None,
    started_at: Optional[str] = None,
) -> None:
    started = started_at or datetime.utcnow().isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO run_log (search_id, status, listings_found, new_listings, error_msg, started_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (search_id, status, listings_found, new_listings, error_msg, started),
        )
        await db.commit()


async def get_run_log(limit: int = 100) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT rl.*, s.name as search_name FROM run_log rl "
            "LEFT JOIN searches s ON rl.search_id = s.id "
            "ORDER BY rl.finished_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_last_successful_run() -> Optional[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM run_log WHERE status = 'success' ORDER BY finished_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
