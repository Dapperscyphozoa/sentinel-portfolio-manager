"""Attribution: cloid → strategy lookup, P&L by strategy.

A SQLite registry maps each cloid issued by the runner to its strategy + coin.
Closure rows joined onto the registry let us answer /attribution.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional


log = logging.getLogger("attribution")


REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloid_registry (
    cloid       TEXT PRIMARY KEY,
    strategy    TEXT NOT NULL,
    coin        TEXT NOT NULL,
    side        TEXT NOT NULL,
    registered_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cloid_reg_strat ON cloid_registry(strategy);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(REGISTRY_SCHEMA)


def register(conn: sqlite3.Connection, cloid: str, strategy: str, coin: str, side: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cloid_registry(cloid,strategy,coin,side,registered_ts) "
        "VALUES(?,?,?,?,?)",
        (cloid, strategy, coin.upper(), side, time.time()),
    )


def strategy_for(conn: sqlite3.Connection, cloid: str) -> Optional[str]:
    row = conn.execute("SELECT strategy FROM cloid_registry WHERE cloid=?", (cloid,)).fetchone()
    return row["strategy"] if row else None


def by_strategy(conn: sqlite3.Connection, since_ms: int = 0) -> list[dict]:
    rows = conn.execute(
        "SELECT strategy, COUNT(*) AS n, SUM(pnl_usd) AS pnl_usd "
        "FROM closures WHERE close_ts >= ? GROUP BY strategy ORDER BY pnl_usd DESC",
        (since_ms / 1000.0,),
    ).fetchall()
    return [dict(r) for r in rows]
