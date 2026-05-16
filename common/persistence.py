"""Shared SQLite schema. One DB file per service; this module defines the canonical tables.

Tables:
  signals     — every evaluate() that returned a Signal
  trades      — open orders + their lifecycle
  closures    — closed trades with realized pnl
  halts       — strategy-level halt history
  spend       — per-call API spend ledger (monitor service)
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    strategy        TEXT NOT NULL,
    coin            TEXT NOT NULL,
    side            TEXT NOT NULL,
    is_long         INTEGER NOT NULL,
    ref_price       REAL NOT NULL,
    sl_px           REAL NOT NULL,
    tp_px           REAL NOT NULL,
    max_hold_bars   INTEGER NOT NULL,
    fire_reason     TEXT,
    extras_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_strategy_ts ON signals(strategy, ts);
CREATE INDEX IF NOT EXISTS idx_signals_coin_ts ON signals(coin, ts);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cloid           TEXT UNIQUE NOT NULL,
    strategy        TEXT NOT NULL,
    coin            TEXT NOT NULL,
    side            TEXT NOT NULL,
    is_long         INTEGER NOT NULL,
    open_ts         REAL NOT NULL,
    open_px         REAL,
    size_usd        REAL,
    size_coin       REAL,
    sl_px           REAL,
    tp_px           REAL,
    max_hold_bars   INTEGER,
    status          TEXT NOT NULL,
    extras_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_status ON trades(strategy, status);
CREATE INDEX IF NOT EXISTS idx_trades_cloid ON trades(cloid);

CREATE TABLE IF NOT EXISTS closures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cloid           TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    coin            TEXT NOT NULL,
    is_long         INTEGER NOT NULL,
    open_ts         REAL NOT NULL,
    close_ts        REAL NOT NULL,
    open_px         REAL NOT NULL,
    close_px        REAL NOT NULL,
    size_coin       REAL NOT NULL,
    pnl_usd         REAL NOT NULL,
    fees_usd        REAL NOT NULL DEFAULT 0,
    close_reason    TEXT,
    extras_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_closures_strategy_ts ON closures(strategy, close_ts);

CREATE TABLE IF NOT EXISTS halts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    strategy        TEXT NOT NULL,
    halted          INTEGER NOT NULL,
    reason          TEXT,
    actor           TEXT
);
CREATE INDEX IF NOT EXISTS idx_halts_strategy_ts ON halts(strategy, ts);

CREATE TABLE IF NOT EXISTS spend (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    routine         TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cost_usd        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_ts ON spend(ts);
CREATE INDEX IF NOT EXISTS idx_spend_routine_ts ON spend(routine, ts);

CREATE TABLE IF NOT EXISTS kv_state (
    k     TEXT PRIMARY KEY,
    v     TEXT NOT NULL,
    ts    REAL NOT NULL
) WITHOUT ROWID;
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def kv_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT v FROM kv_state WHERE k=?", (key,)).fetchone()
    return row["v"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    import time as _t
    conn.execute(
        "INSERT OR REPLACE INTO kv_state(k, v, ts) VALUES(?, ?, ?)",
        (key, value, _t.time()),
    )
