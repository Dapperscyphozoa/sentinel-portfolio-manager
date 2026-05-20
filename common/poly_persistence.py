"""Poly-specific SQLite schema. Tables for binary-market trading.

Lives in a separate DB file from SPM to keep the two stacks fully isolated
on the filesystem. Default path: $STATE_DIR/poly.db.

Tables:
  poly_signals      - every evaluate() that returned a Signal
  poly_orders       - submitted orders (live + cancelled)
  poly_fills        - fills as reported by PM CLOB
  poly_positions    - current outcome-token inventory per market
  poly_market_state - per-market metadata snapshot at signal time
  poly_quotes       - maker_quote refresh history
  poly_resolutions  - final settlement outcome per market
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional


POLY_SCHEMA = """
CREATE TABLE IF NOT EXISTS poly_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    strategy        TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    asset           TEXT NOT NULL,
    token           TEXT NOT NULL,           -- "YES" or "NO"
    side            TEXT NOT NULL,           -- "BUY" or "SELL"
    price           REAL NOT NULL,
    size_usdc       REAL NOT NULL,
    edge_bps        REAL,
    cl_predicted    REAL,
    pm_implied      REAL,
    fire_reason     TEXT,
    extras_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_poly_signals_strategy_ts ON poly_signals(strategy, ts);
CREATE INDEX IF NOT EXISTS idx_poly_signals_market_ts ON poly_signals(market_id, ts);

CREATE TABLE IF NOT EXISTS poly_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cloid           TEXT UNIQUE NOT NULL,
    order_hash      TEXT,
    pm_order_id     TEXT,
    strategy        TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    token           TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           REAL NOT NULL,
    size_usdc       REAL NOT NULL,
    submit_ts       REAL NOT NULL,
    status          TEXT NOT NULL,           -- POSTED, FILLED, PARTIAL, REJECTED, CANCELLED
    fill_amount     REAL DEFAULT 0,
    fill_price      REAL,
    signing_ms      INTEGER,
    total_ms        INTEGER,
    error           TEXT,
    extras_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_poly_orders_strategy ON poly_orders(strategy, submit_ts);
CREATE INDEX IF NOT EXISTS idx_poly_orders_market ON poly_orders(market_id);

CREATE TABLE IF NOT EXISTS poly_fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pm_trade_id     TEXT UNIQUE NOT NULL,
    cloid           TEXT,
    market_id       TEXT NOT NULL,
    token           TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           REAL NOT NULL,
    size            REAL NOT NULL,
    fee_usdc        REAL,
    rebate_usdc     REAL,
    ts              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_poly_fills_market ON poly_fills(market_id, ts);

CREATE TABLE IF NOT EXISTS poly_positions (
    market_id       TEXT NOT NULL,
    token           TEXT NOT NULL,
    qty             REAL NOT NULL DEFAULT 0,
    avg_cost        REAL,
    last_update_ts  REAL NOT NULL,
    PRIMARY KEY (market_id, token)
);

CREATE TABLE IF NOT EXISTS poly_resolutions (
    market_id       TEXT PRIMARY KEY,
    asset           TEXT NOT NULL,
    start_price     REAL,
    final_price     REAL,
    resolution      TEXT NOT NULL,           -- "YES", "NO", "INVALID"
    resolve_ts      REAL NOT NULL,
    chainlink_round_id TEXT
);

CREATE TABLE IF NOT EXISTS poly_quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    market_id       TEXT NOT NULL,
    bid_yes         REAL,
    ask_yes         REAL,
    inventory       REAL,
    fair_prob       REAL,
    action          TEXT NOT NULL            -- POST, CANCEL, REPLACE
);
CREATE INDEX IF NOT EXISTS idx_poly_quotes_market ON poly_quotes(market_id, ts);

CREATE TABLE IF NOT EXISTS poly_cl_validation (
    ts              REAL PRIMARY KEY,
    asset           TEXT NOT NULL,
    cl_actual       REAL NOT NULL,
    cl_predicted    REAL NOT NULL,
    diff_bps        REAL NOT NULL,
    n_venues        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_poly_cl_validation_asset_ts ON poly_cl_validation(asset, ts);
"""


def get_poly_db_path(state_dir: Optional[str] = None) -> str:
    sd = state_dir or os.environ.get("STATE_DIR", "/var/data")
    os.makedirs(sd, exist_ok=True)
    return os.path.join(sd, "poly.db")


def init_poly_db(state_dir: Optional[str] = None) -> str:
    path = get_poly_db_path(state_dir)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(POLY_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return path


def connect_poly(state_dir: Optional[str] = None) -> sqlite3.Connection:
    path = get_poly_db_path(state_dir)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


if __name__ == "__main__":
    p = init_poly_db()
    print(f"poly db initialized at {p}")
