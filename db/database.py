"""
SQLite async database layer (aiosqlite).

Tables:
  searches     — user-configured search criteria
  seen         — dedup: (listing_id, search_id) pairs
  listings     — scraped listing records
  run_log      — scan history for health monitoring + dashboard

init_db() is safe to run on existing DBs — uses ALTER TABLE IF NOT EXISTS
pattern to add missing columns without destroying existing data.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from config.settings import settings

log = logging.getLogger(__name__)

DB_PATH = settings.db_path


async def _add_column_if_missing(db: aiosqlite.Connection, table: str, column: str, col_def: str) -> None:
    """Safely add a column to a table if it doesn't already exist."""
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        cols = [row[1] for row in await cur.fetchall()]
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        log.info("Migrated: added column '%s' to table '%s'", column, table)


async def init_db() -> None:
    import os
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        # Create tables
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS searches (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL,
                keywords      TEXT    NOT NULL,
                neg_keywords  TEXT    DEFAULT '',
                zip_code      TEXT    NOT NULL DEFAULT '',
                price_min     REAL,
                price_max     REAL,
                distance_mi   INTEGER DEFAULT 40,
                condition     TEXT    DEFAULT 'any',
                enabled       INTEGER DEFAULT 1,
                created_at    TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS seen (
                listing_id    TEXT    NOT NULL,
                search_id     INTEGER NOT NULL,
                dismissed     INTEGER DEFAULT 0,
                first_seen_at TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (listing_id, search_id)
            );

            CREATE TABLE IF NOT EXISTS listings (
                id          TEXT    PRIMARY KEY,
                search_id   INTEGER NOT NULL,
                title       TEXT,
                listing_url TEXT,
                price       REAL,
                location    TEXT,
                image_url   TEXT,
                condition   TEXT,
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS run_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id    INTEGER,
                status       TEXT,
                new_listings INTEGER DEFAULT 0,
                finished_at  TEXT    DEFAULT (datetime('now'))
            );
        """)

        # Safe migrations — add missing columns to existing tables
        await _add_column_if_missing(db, "run_log", "search_name", "TEXT")
        await _add_column_if_missing(db, "seen", "dismissed", "INTEGER DEFAULT 0")
        await _add_column_if_missing(db, "seen", "first_seen_at", "TEXT DEFAULT (datetime('now'))")
        await _add_column_if_missing(db, "searches", "neg_keywords", "TEXT DEFAULT ''")
        await _add_column_if_missing(db, "searches", "zip_code", "TEXT NOT NULL DEFAULT ''")
        await _add_column_if_missing(db, "searches", "condition", "TEXT DEFAULT 'any'")
        await _add_column_if_missing(db, "listings", "location", "TEXT")
        await _add_column_if_missing(db, "listings", "image_url", "TEXT")
        await _add_column_if_missing(db, "listings", "condition", "TEXT")

        await db.commit()
        log.info("Database ready at %s", DB_PATH)


async def get_all_searches(enabled_only: bool = False) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = "SELECT * FROM searches"
        if enabled_only:
            q += " WHERE enabled = 1"
        q += " ORDER BY id"
        async with db.execute(q) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def get_search(search_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM searches WHERE id = ?", (search_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


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
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO searches (name, keywords, zip_code, neg_keywords,
                                  price_min, price_max, distance_mi, condition)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (name, keywords, zip_code, neg_keywords, price_min, price_max, distance_mi, condition),
        )
        await db.commit()
        log.info("Search created: [%d] %s", cur.lastrowid, name)
        return cur.lastrowid


async def delete_search(search_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM searches WHERE id = ?", (search_id,))
        await db.execute("DELETE FROM seen WHERE search_id = ?", (search_id,))
        await db.execute("DELETE FROM listings WHERE search_id = ?", (search_id,))
        await db.commit()
        log.info("Search %d and all related data deleted.", search_id)


async def is_seen(listing_id: str, search_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen WHERE listing_id = ? AND search_id = ? AND dismissed = 0",
            (listing_id, search_id),
        ) as cur:
            return await cur.fetchone() is not None


async def mark_seen(listing_id: str, search_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen (listing_id, search_id) VALUES (?,?)",
            (listing_id, search_id),
        )
        await db.commit()


async def reset_seen_for_search(search_id: int) -> int:
    """Clear all seen entries for a search so all listings fire as new on next run."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM seen WHERE search_id = ?", (search_id,))
        await db.commit()
        return cur.rowcount


async def dismiss_listing(listing_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE seen SET dismissed = 1 WHERE listing_id = ?",
            (listing_id,),
        )
        async with db.execute("SELECT id FROM searches") as cur:
            rows = await cur.fetchall()
        for (sid,) in rows:
            await db.execute(
                "INSERT OR IGNORE INTO seen (listing_id, search_id, dismissed) VALUES (?,?,1)",
                (listing_id, sid),
            )
        await db.commit()
        log.info("Listing %s dismissed across all searches.", listing_id)


async def upsert_listing(
    listing_id: str,
    search_id: int,
    title: str,
    listing_url: str,
    price: Optional[float],
    location: Optional[str],
    image_url: Optional[str],
    condition: Optional[str],
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO listings (id, search_id, title, listing_url, price, location, image_url, condition)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, price=excluded.price,
                location=excluded.location, image_url=excluded.image_url
            """,
            (listing_id, search_id, title, listing_url, price, location, image_url, condition),
        )
        await db.commit()


async def get_listings(search_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if search_id:
            async with db.execute(
                "SELECT * FROM listings WHERE search_id=? ORDER BY created_at DESC LIMIT ?",
                (search_id, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT * FROM listings ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def log_run(
    search_id: int,
    search_name: str,
    status: str,
    new_listings: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO run_log (search_id, search_name, status, new_listings, finished_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (search_id, search_name, status, new_listings,
             datetime.now(tz=timezone.utc).isoformat()),
        )
        await db.commit()


async def get_run_log(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_last_successful_run() -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM run_log WHERE status='success' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None
