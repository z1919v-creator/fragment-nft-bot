# db.py — SQLite-хранилище (пользователи, подписки, лоты, кеш кошельков)
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiosqlite

from config import DB_PATH

MSK = timezone(timedelta(hours=3))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    price_max  REAL                          -- лимит цены уведомлений (NULL = нет)
);
CREATE TABLE IF NOT EXISTS subs (
    user_id    INTEGER NOT NULL,
    collection TEXT    NOT NULL,
    PRIMARY KEY (user_id, collection)
);
CREATE TABLE IF NOT EXISTS listings (
    listing_id      TEXT PRIMARY KEY,          -- 'plushpepe-69'
    collection      TEXT NOT NULL,
    name            TEXT NOT NULL,
    num             TEXT,
    price_ton       REAL,
    wallet          TEXT,
    domain          TEXT,
    username        TEXT,
    url             TEXT,
    first_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    is_backfill     INTEGER NOT NULL DEFAULT 0, -- 1 = каталожный лот до подписки
    last_drop       TEXT                       -- время последнего уведомления о снижении
);
CREATE TABLE IF NOT EXISTS wallets (
    wallet     TEXT PRIMARY KEY,
    username   TEXT,
    domain     TEXT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_subs_coll      ON subs(collection);
CREATE INDEX IF NOT EXISTS idx_listings_coll  ON listings(collection, first_seen);
"""


@asynccontextmanager
async def connect():
    """Правильный aiosqlite-контекст: один await на соединение."""
    conn = await aiosqlite.connect(DB_PATH)
    try:
        await conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = aiosqlite.Row
        yield conn
    finally:
        await conn.close()


async def init():
    async with connect() as conn:
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.executescript(SCHEMA)
        # миграции для баз старых версий
        for ddl in (
            "ALTER TABLE users ADD COLUMN price_max REAL",
            "ALTER TABLE listings ADD COLUMN is_backfill INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE listings ADD COLUMN last_drop TEXT",
        ):
            try:
                await conn.execute(ddl)
            except aiosqlite.OperationalError:
                pass  # колонка уже есть
        await conn.commit()


# ── пользователи и подписки ─────────────────────────────────────────
async def add_user(user_id: int):
    async with connect() as conn:
        await conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                           (user_id,))
        await conn.commit()


async def has_users() -> bool:
    """Есть ли хоть один пользователь (для проверки «база свежая/сброшенная»)."""
    async with connect() as conn:
        async with conn.execute("SELECT 1 FROM users LIMIT 1") as cur:
            return await cur.fetchone() is not None


async def remove_user(user_id: int):
    async with connect() as conn:
        await conn.execute("DELETE FROM subs WHERE user_id = ?", (user_id,))
        await conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await conn.commit()


async def get_subs(user_id: int) -> list[str]:
    async with connect() as conn:
        async with conn.execute(
                "SELECT collection FROM subs WHERE user_id = ?", (user_id,)) as cur:
            return [r[0] for r in await cur.fetchall()]


async def set_sub(user_id: int, collection: str, on: bool):
    async with connect() as conn:
        if on:
            await conn.execute(
                "INSERT OR IGNORE INTO subs (user_id, collection) VALUES (?, ?)",
                (user_id, collection))
        else:
            await conn.execute(
                "DELETE FROM subs WHERE user_id = ? AND collection = ?",
                (user_id, collection))
        await conn.commit()


async def unsub_all(user_id: int) -> int:
    async with connect() as conn:
        cur = await conn.execute("DELETE FROM subs WHERE user_id = ?", (user_id,))
        await conn.commit()
        return cur.rowcount


async def sub_users(collection: str) -> list[int]:
    async with connect() as conn:
        async with conn.execute(
                "SELECT user_id FROM subs WHERE collection = ?",
                (collection,)) as cur:
            return [r[0] for r in await cur.fetchall()]


async def subscribed_collections() -> list[str]:
    async with connect() as conn:
        async with conn.execute(
                "SELECT DISTINCT collection FROM subs") as cur:
            return [r[0] for r in await cur.fetchall()]


# ── цена-максимум ───────────────────────────────────────────────────
async def get_price_max(user_id: int) -> float | None:
    async with connect() as conn:
        async with conn.execute(
                "SELECT price_max FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row and row[0] is not None else None


async def set_price_max(user_id: int, value: float | None):
    async with connect() as conn:
        await conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                           (user_id,))
        await conn.execute("UPDATE users SET price_max = ? WHERE user_id = ?",
                           (value, user_id))
        await conn.commit()


# ── лоты ────────────────────────────────────────────────────────────
async def count_listings(collection: str) -> int:
    async with connect() as conn:
        async with conn.execute(
                "SELECT COUNT(*) FROM listings WHERE collection = ?",
                (collection,)) as cur:
            return (await cur.fetchone())[0]


async def get_listing(listing_id: str):
    """Строка лота (aiosqlite.Row) или None."""
    async with connect() as conn:
        async with conn.execute(
                "SELECT * FROM listings WHERE listing_id = ?", (listing_id,)) as cur:
            return await cur.fetchone()


async def listing_exists(listing_id: str) -> bool:
    return await get_listing(listing_id) is not None


async def insert_listing(listing_id: str, collection: str, name: str, num: str,
                         price_ton: float, url: str,
                         wallet: str | None = None, domain: str | None = None,
                         username: str | None = None, is_backfill: bool = False):
    async with connect() as conn:
        await conn.execute(
            """INSERT OR IGNORE INTO listings
               (listing_id, collection, name, num, price_ton,
                wallet, domain, username, url, first_seen, is_backfill)
               VALUES (?,?,?,?,?,?,?,?,?, datetime('now'), ?)""",
            (listing_id, collection, name, num, price_ton,
             wallet, domain, username, url, 1 if is_backfill else 0))
        await conn.commit()


async def update_listing_price(listing_id: str, price: float,
                               set_drop_time: bool = False):
    """Обновить цену; при set_drop_time запомнить время уведомления о снижении."""
    ts = (datetime.now(timezone.utc).isoformat(timespec="seconds")
          if set_drop_time else None)
    async with connect() as conn:
        await conn.execute(
            """UPDATE listings
               SET price_ton = ?, last_drop = COALESCE(?, last_drop)
               WHERE listing_id = ?""",
            (price, ts, listing_id))
        await conn.commit()


async def update_listing_seller(listing_id: str, wallet: str | None,
                                domain: str | None, username: str | None):
    async with connect() as conn:
        await conn.execute(
            """UPDATE listings
               SET wallet = ?, domain = COALESCE(?, domain), username = ?
               WHERE listing_id = ?""",
            (wallet, domain, username, listing_id))
        await conn.commit()


# ── статистика ──────────────────────────────────────────────────────
async def stats_today(collection: str) -> int:
    """Новых лотов сегодня (по МСК), не считая каталожной «истории» при подписке."""
    async with connect() as conn:
        async with conn.execute(
                """SELECT COUNT(*) FROM listings
                   WHERE collection = ? AND is_backfill = 0
                     AND date(first_seen, '+3 hours') = date('now', '+3 hours')""",
                (collection,)) as cur:
            return (await cur.fetchone())[0]


async def last_new_time(collection: str) -> str | None:
    """Время последнего нового лота «HH:MM» по МСК (или None)."""
    async with connect() as conn:
        async with conn.execute(
                """SELECT MAX(first_seen) FROM listings
                   WHERE collection = ? AND is_backfill = 0""",
                (collection,)) as cur:
            row = await cur.fetchone()
    val = row[0] if row else None
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val).replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK).strftime("%H:%M")
    except ValueError:
        return None


# ── кеш кошелёк → username ─────────────────────────────────────────
async def get_wallet(wallet: str):
    """(username, domain, checked_at) или None."""
    async with connect() as conn:
        async with conn.execute(
                "SELECT username, domain, checked_at FROM wallets WHERE wallet = ?",
                (wallet,)) as cur:
            row = await cur.fetchone()
            return (row[0], row[1], row[2]) if row else None


async def set_wallet(wallet: str, username: str | None, domain: str | None):
    async with connect() as conn:
        await conn.execute(
            """INSERT INTO wallets (wallet, username, domain, checked_at)
               VALUES (?,?,?, datetime('now'))
               ON CONFLICT(wallet) DO UPDATE SET
                   username = excluded.username,
                   domain   = COALESCE(excluded.domain, wallets.domain),
                   checked_at = excluded.checked_at""",
            (wallet, username, domain))
        await conn.commit()
