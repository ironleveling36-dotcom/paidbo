"""
database.py — PostgreSQL persistence layer
Uses Railway's free built-in Postgres plugin via DATABASE_URL env var.
Falls back to SQLite for local development.
"""

import os
import sqlite3
import threading
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    logger.info("Using PostgreSQL database")
else:
    logger.info("Using SQLite database (local dev)")

# ── Connection helpers ─────────────────────────────────────────────────────────

_sqlite_local = threading.local()


def _get_conn():
    if USE_POSTGRES:
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        if not hasattr(_sqlite_local, "conn") or _sqlite_local.conn is None:
            Path("data").mkdir(exist_ok=True)
            conn = sqlite3.connect("data/swiggy_bot.db", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            _sqlite_local.conn = conn
        return _sqlite_local.conn


def _placeholder():
    """Return %s for postgres, ? for sqlite."""
    return "%s" if USE_POSTGRES else "?"


P = property(lambda self: _placeholder())


def _execute(sql: str, params=(), fetch="none"):
    """
    Run a query. fetch = 'none' | 'one' | 'all'
    Handles connection lifecycle for Postgres (new conn per call).
    """
    # Normalise placeholders
    if not USE_POSTGRES:
        sql = sql.replace("%s", "?")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        result = None
        if fetch == "one":
            result = cur.fetchone()
        elif fetch == "all":
            result = cur.fetchall()
        conn.commit()
        return result
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        if USE_POSTGRES:
            conn.close()


def _lastrowid(sql: str, params=()):
    """Insert and return last inserted ID."""
    if USE_POSTGRES:
        sql = sql.rstrip(";") + " RETURNING id;"
        row = _execute(sql, params, fetch="one")
        return row["id"] if row else None
    else:
        if not USE_POSTGRES:
            sql = sql.replace("%s", "?")
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur.lastrowid


# ── Schema ─────────────────────────────────────────────────────────────────────

def init_db():
    """Create tables if they don't exist. Call once on startup."""
    if USE_POSTGRES:
        _execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     BIGINT PRIMARY KEY,
            username    TEXT,
            full_name   TEXT,
            credits     INTEGER NOT NULL DEFAULT 0,
            free_given  INTEGER NOT NULL DEFAULT 0,
            joined_at   TIMESTAMP NOT NULL DEFAULT NOW(),
            last_seen   TIMESTAMP NOT NULL DEFAULT NOW()
        )""")
        _execute("""
        CREATE TABLE IF NOT EXISTS recharges (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            username    TEXT,
            utr         TEXT NOT NULL,
            amount      TEXT NOT NULL DEFAULT '₹20',
            credits_req INTEGER NOT NULL DEFAULT 40,
            status      TEXT NOT NULL DEFAULT 'pending',
            created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMP
        )""")
        _execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id           SERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL,
            credits_used INTEGER NOT NULL DEFAULT 2,
            status       TEXT NOT NULL DEFAULT 'started',
            success_cnt  INTEGER DEFAULT 0,
            failed_cnt   INTEGER DEFAULT 0,
            total_earned TEXT,
            started_at   TIMESTAMP NOT NULL DEFAULT NOW(),
            finished_at  TIMESTAMP
        )""")
    else:
        conn = _get_conn()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            full_name   TEXT,
            credits     INTEGER NOT NULL DEFAULT 0,
            free_given  INTEGER NOT NULL DEFAULT 0,
            joined_at   TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS recharges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            username    TEXT,
            utr         TEXT NOT NULL,
            amount      TEXT NOT NULL DEFAULT '₹20',
            credits_req INTEGER NOT NULL DEFAULT 40,
            status      TEXT NOT NULL DEFAULT 'pending',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            credits_used INTEGER NOT NULL DEFAULT 2,
            status       TEXT NOT NULL DEFAULT 'started',
            success_cnt  INTEGER DEFAULT 0,
            failed_cnt   INTEGER DEFAULT 0,
            total_earned TEXT,
            started_at   TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at  TEXT
        );
        """)
        conn.commit()
    logger.info("Database tables ready ✅")


# ── User helpers ───────────────────────────────────────────────────────────────

def upsert_user(user_id: int, username: str | None, full_name: str):
    if USE_POSTGRES:
        _execute("""
            INSERT INTO users(user_id, username, full_name)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                username  = EXCLUDED.username,
                full_name = EXCLUDED.full_name,
                last_seen = NOW()
        """, (user_id, username or "", full_name))
    else:
        _execute("""
            INSERT INTO users(user_id, username, full_name)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                username  = excluded.username,
                full_name = excluded.full_name,
                last_seen = datetime('now')
        """, (user_id, username or "", full_name))
    return get_user(user_id)


def get_user(user_id: int):
    return _execute(
        "SELECT * FROM users WHERE user_id=%s", (user_id,), fetch="one"
    )


def give_free_credits(user_id: int, amount: int = 2) -> bool:
    row = _execute(
        "SELECT free_given FROM users WHERE user_id=%s", (user_id,), fetch="one"
    )
    if not row or row["free_given"]:
        return False
    _execute(
        "UPDATE users SET credits=credits+%s, free_given=1 WHERE user_id=%s",
        (amount, user_id),
    )
    return True


def get_credits(user_id: int) -> int:
    row = get_user(user_id)
    return row["credits"] if row else 0


def deduct_credits(user_id: int, amount: int = 2) -> bool:
    row = _execute(
        "SELECT credits FROM users WHERE user_id=%s", (user_id,), fetch="one"
    )
    if not row or row["credits"] < amount:
        return False
    _execute(
        "UPDATE users SET credits=credits-%s WHERE user_id=%s",
        (amount, user_id),
    )
    return True


def add_credits(user_id: int, amount: int) -> int:
    _execute(
        "UPDATE users SET credits=credits+%s WHERE user_id=%s",
        (amount, user_id),
    )
    row = get_user(user_id)
    return row["credits"] if row else 0


# ── Recharge helpers ───────────────────────────────────────────────────────────

def create_recharge(user_id: int, username: str | None, utr: str,
                    credits_req: int = 40) -> int:
    return _lastrowid("""
        INSERT INTO recharges(user_id, username, utr, credits_req)
        VALUES (%s, %s, %s, %s)
    """, (user_id, username or "", utr, credits_req))


def get_pending_recharges():
    return _execute(
        "SELECT * FROM recharges WHERE status='pending' ORDER BY created_at",
        fetch="all",
    ) or []


def resolve_recharge(recharge_id: int, action: str):
    row = _execute(
        "SELECT * FROM recharges WHERE id=%s", (recharge_id,), fetch="one"
    )
    if not row:
        return None
    if USE_POSTGRES:
        _execute("""
            UPDATE recharges SET status=%s, resolved_at=NOW() WHERE id=%s
        """, (action, recharge_id))
    else:
        _execute("""
            UPDATE recharges SET status=%s, resolved_at=datetime('now') WHERE id=%s
        """, (action, recharge_id))
    if action == "approved":
        _execute(
            "UPDATE users SET credits=credits+%s WHERE user_id=%s",
            (row["credits_req"], row["user_id"]),
        )
    return row


def get_recharge(recharge_id: int):
    return _execute(
        "SELECT * FROM recharges WHERE id=%s", (recharge_id,), fetch="one"
    )


# ── Run helpers ────────────────────────────────────────────────────────────────

def start_run(user_id: int) -> int:
    return _lastrowid(
        "INSERT INTO runs(user_id) VALUES (%s)", (user_id,)
    )


def finish_run(run_id: int, status: str, success: int, failed: int, earned: str):
    if USE_POSTGRES:
        _execute("""
            UPDATE runs SET status=%s, success_cnt=%s, failed_cnt=%s,
                            total_earned=%s, finished_at=NOW()
            WHERE id=%s
        """, (status, success, failed, earned, run_id))
    else:
        _execute("""
            UPDATE runs SET status=%s, success_cnt=%s, failed_cnt=%s,
                            total_earned=%s, finished_at=datetime('now')
            WHERE id=%s
        """, (status, success, failed, earned, run_id))


def user_run_count(user_id: int) -> int:
    row = _execute(
        "SELECT COUNT(*) AS c FROM runs WHERE user_id=%s AND status='done'",
        (user_id,), fetch="one",
    )
    return row["c"] if row else 0
