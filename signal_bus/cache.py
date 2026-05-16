"""In-memory ring buffers for klines / liq / markPrice / funding, with SQLite flush.

Ring buffers are thread-safe via per-key locks. Writers (WS thread) and readers
(HTTP threads) share these directly. SQLite is durable warm-state.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Optional


KLINE_CAP = 1000   # bars per (coin, tf)
LIQ_CAP   = 50000  # liq events total
MARK_CAP  = 300    # last 5min @ 1s


_DB_LOCK = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    coin       TEXT NOT NULL,
    tf         TEXT NOT NULL,
    open_ts    INTEGER NOT NULL,
    open_px    REAL NOT NULL,
    high_px    REAL NOT NULL,
    low_px     REAL NOT NULL,
    close_px   REAL NOT NULL,
    volume     REAL NOT NULL,
    PRIMARY KEY (coin, tf, open_ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_klines_coin_tf_ts ON klines(coin, tf, open_ts DESC);

CREATE TABLE IF NOT EXISTS liq_events (
    ts         INTEGER NOT NULL,
    coin       TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        REAL NOT NULL,
    price      REAL NOT NULL,
    usd        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liq_ts ON liq_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_liq_coin_ts ON liq_events(coin, ts DESC);

CREATE TABLE IF NOT EXISTS funding (
    coin       TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    rate       REAL NOT NULL,
    venue      TEXT NOT NULL DEFAULT 'binance',
    PRIMARY KEY (coin, ts, venue)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_funding_coin_ts ON funding(coin, ts DESC);

CREATE TABLE IF NOT EXISTS hl_fills (
    fill_id    TEXT PRIMARY KEY,
    ts         INTEGER NOT NULL,
    coin       TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        REAL NOT NULL,
    price      REAL NOT NULL,
    cloid      TEXT,
    raw_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_hl_fills_ts ON hl_fills(ts DESC);
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    return conn


class Cache:
    """All state for the signal-bus. Single instance per process."""

    def __init__(self, db_path: str):
        self.db = init_db(db_path)
        self.klines: dict[tuple[str, str], Deque[dict]] = defaultdict(lambda: deque(maxlen=KLINE_CAP))
        self.liqs: Deque[dict] = deque(maxlen=LIQ_CAP)
        self.marks: dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=MARK_CAP))
        self.funding_latest: dict[str, dict] = {}
        # HL state
        self.hl_account: dict = {"value": 0.0, "margin_used": 0.0, "positions": []}
        self.hl_positions: list[dict] = []
        self.hl_fills: Deque[dict] = deque(maxlen=10000)
        self.last_update: dict[str, float] = {
            "binance_ws": 0.0, "hl_ws": 0.0, "binance_flush": 0.0, "liq_flush": 0.0,
            "okx_ws": 0.0, "bybit_ws": 0.0,
        }
        self.ws_alive: dict[str, bool] = {"binance": False, "hl": False, "okx": False, "bybit": False}
        self._lock = threading.RLock()

    # -------- writers --------

    def push_kline(self, coin: str, tf: str, k: dict) -> None:
        with self._lock:
            dq = self.klines[(coin, tf)]
            # dedupe on open_ts: replace if same bucket
            if dq and dq[-1]["open_ts"] == k["open_ts"]:
                dq[-1] = k
            else:
                dq.append(k)
            self.last_update["binance_ws"] = time.time()

    def push_liq(self, ev: dict) -> None:
        with self._lock:
            self.liqs.append(ev)
            self.last_update["binance_ws"] = time.time()

    def push_mark(self, coin: str, m: dict) -> None:
        with self._lock:
            self.marks[coin].append(m)
            self.last_update["binance_ws"] = time.time()

    def push_funding(self, coin: str, ts_ms: int, rate: float, venue: str = "binance") -> None:
        with self._lock:
            self.funding_latest[coin] = {"ts": ts_ms, "rate": rate, "venue": venue}
            with _DB_LOCK:
                self.db.execute(
                    "INSERT OR REPLACE INTO funding(coin,ts,rate,venue) VALUES(?,?,?,?)",
                    (coin, ts_ms, rate, venue),
                )

    # -------- readers --------

    def get_klines(self, coin: str, tf: str, n: int) -> list[dict]:
        with self._lock:
            dq = self.klines.get((coin, tf))
            if dq:
                return list(dq)[-n:]
        # cold-load from SQLite
        rows = self.db.execute(
            "SELECT open_ts,open_px,high_px,low_px,close_px,volume FROM klines "
            "WHERE coin=? AND tf=? ORDER BY open_ts DESC LIMIT ?",
            (coin, tf, n),
        ).fetchall()
        return [
            {"open_ts": r["open_ts"], "open": r["open_px"], "high": r["high_px"],
             "low": r["low_px"], "close": r["close_px"], "volume": r["volume"]}
            for r in reversed(rows)
        ]

    def get_liqs(self, since_ms: int = 0, coin: Optional[str] = None) -> list[dict]:
        with self._lock:
            out = [e for e in self.liqs if e["ts"] >= since_ms and (coin is None or e["coin"] == coin)]
        return out

    def get_mark(self, coin: str) -> dict:
        with self._lock:
            dq = self.marks.get(coin)
            latest = dq[-1] if dq else None
        return latest or {"coin": coin, "ts": 0, "binance_mid": None, "hl_mid": None}

    def get_funding(self, coin: str, hours: int) -> list[dict]:
        since = int((time.time() - hours * 3600) * 1000)
        rows = self.db.execute(
            "SELECT ts, rate, venue FROM funding WHERE coin=? AND ts>=? ORDER BY ts ASC",
            (coin, since),
        ).fetchall()
        return [dict(r) for r in rows]

    # -------- flushers --------

    def flush_klines(self) -> int:
        """Write all in-memory klines to SQLite. Called hourly by scheduler."""
        with self._lock:
            snapshot = {k: list(v) for k, v in self.klines.items()}
        n = 0
        with _DB_LOCK:
            for (coin, tf), bars in snapshot.items():
                for b in bars:
                    self.db.execute(
                        "INSERT OR REPLACE INTO klines(coin,tf,open_ts,open_px,high_px,low_px,close_px,volume) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (coin, tf, b["open_ts"], b["open"], b["high"], b["low"], b["close"], b["volume"]),
                    )
                    n += 1
        self.last_update["binance_flush"] = time.time()
        return n

    def flush_liqs(self) -> int:
        """Write liq events newer than last flush to SQLite. Every 5min."""
        with self._lock:
            snapshot = list(self.liqs)
        n = 0
        with _DB_LOCK:
            for e in snapshot:
                self.db.execute(
                    "INSERT INTO liq_events(ts,coin,side,qty,price,usd) VALUES(?,?,?,?,?,?)",
                    (e["ts"], e["coin"], e["side"], e["qty"], e["price"], e["usd"]),
                )
                n += 1
            # prune SQLite to last 7d
            cutoff = int((time.time() - 7 * 86400) * 1000)
            self.db.execute("DELETE FROM liq_events WHERE ts < ?", (cutoff,))
        self.last_update["liq_flush"] = time.time()
        return n

    def cold_load(self, hours_klines: int = 24, hours_liqs: int = 24) -> None:
        """On boot: rehydrate ring buffers from SQLite."""
        since_kline = int((time.time() - hours_klines * 3600) * 1000)
        kline_rows = self.db.execute(
            "SELECT coin,tf,open_ts,open_px,high_px,low_px,close_px,volume FROM klines "
            "WHERE open_ts >= ? ORDER BY coin, tf, open_ts ASC",
            (since_kline,),
        ).fetchall()
        with self._lock:
            for r in kline_rows:
                self.klines[(r["coin"], r["tf"])].append({
                    "open_ts": r["open_ts"], "open": r["open_px"], "high": r["high_px"],
                    "low": r["low_px"], "close": r["close_px"], "volume": r["volume"],
                })
        since_liq = int((time.time() - hours_liqs * 3600) * 1000)
        liq_rows = self.db.execute(
            "SELECT ts,coin,side,qty,price,usd FROM liq_events WHERE ts>=? ORDER BY ts ASC",
            (since_liq,),
        ).fetchall()
        with self._lock:
            for r in liq_rows:
                self.liqs.append({"ts": r["ts"], "coin": r["coin"], "side": r["side"],
                                  "qty": r["qty"], "price": r["price"], "usd": r["usd"]})

    def stats(self) -> dict:
        with self._lock:
            return {
                "ws_alive": dict(self.ws_alive),
                "last_update": dict(self.last_update),
                "kline_keys": len(self.klines),
                "kline_bars_total": sum(len(v) for v in self.klines.values()),
                "liq_events": len(self.liqs),
                "mark_coins": len(self.marks),
                "funding_coins": len(self.funding_latest),
                "hl_positions": len(self.hl_positions),
                "hl_fills_buffered": len(self.hl_fills),
            }
