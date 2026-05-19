"""Historical OHLCV store backed by SQLite, lazily backfilled from OKX history-candles.

Serves /ohlcv for sentinel-pm bench validation. Symbol format: BTCUSDT (Binance-style),
internally strips USDT and queries OKX SWAP. Bars are bar-open-time UTC ms.

Schema (one row per bar):
  ohlcv(symbol TEXT, interval TEXT, open_ts INTEGER, open REAL, high REAL,
        low REAL, close REAL, volume REAL, PRIMARY KEY(symbol, interval, open_ts))
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Iterable

import httpx


log = logging.getLogger("ohlcv_store")

_OKX_BAR = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}
_BAR_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
           "4h": 14_400_000, "1d": 86_400_000}


class OhlcvStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        # Per-(symbol, interval) backfill locks so a slow OKX pull doesn't
        # block concurrent reads of unrelated symbols.
        self._bf_locks: dict[tuple[str, str], threading.Lock] = {}
        self._bf_locks_guard = threading.Lock()

    def _init_db(self) -> None:
        with self._connect() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS ohlcv (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_ts INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY (symbol, interval, open_ts)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_ohlcv_range ON ohlcv(symbol, interval, open_ts)")
            c.commit()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _bf_lock(self, symbol: str, interval: str) -> threading.Lock:
        key = (symbol, interval)
        with self._bf_locks_guard:
            lk = self._bf_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._bf_locks[key] = lk
            return lk

    @staticmethod
    def _to_okx_inst(symbol: str) -> str:
        s = symbol.upper()
        for sfx in ("USDT", "USDC", "BUSD"):
            if s.endswith(sfx):
                return f"{s[:-len(sfx)]}-USDT-SWAP"
        return f"{s}-USDT-SWAP"

    def _fetch_okx_range(self, symbol: str, interval: str,
                         start_ms: int, end_ms: int) -> int:
        """Fetch [start_ms, end_ms] from OKX history-candles, paginating backward
        from end_ms. Returns number of new bars inserted."""
        bar = _OKX_BAR.get(interval)
        if not bar:
            return 0
        inst = self._to_okx_inst(symbol)
        url = "https://www.okx.com/api/v5/market/history-candles"
        inserted = 0
        # OKX `history-candles` returns newest→oldest; we page using `after`
        # which means "older than this ts". Start with end_ms.
        after = end_ms
        rows_buf: list[tuple] = []
        try:
            with httpx.Client(timeout=30) as cl:
                while True:
                    params = {"instId": inst, "bar": bar, "limit": "100",
                              "after": str(after)}
                    try:
                        r = cl.get(url, params=params)
                        if r.status_code == 429:
                            time.sleep(0.5)
                            continue
                        r.raise_for_status()
                    except Exception as e:
                        log.warning("okx fetch %s %s after=%s: %s", symbol, interval, after, e)
                        break
                    data = (r.json() or {}).get("data") or []
                    if not data:
                        break
                    oldest_in_batch = None
                    for row in data:
                        try:
                            ts = int(row[0])
                        except (ValueError, IndexError):
                            continue
                        if ts < start_ms:
                            oldest_in_batch = ts
                            continue
                        try:
                            rows_buf.append((
                                symbol, interval, ts,
                                float(row[1]), float(row[2]), float(row[3]),
                                float(row[4]), float(row[5]),
                            ))
                        except (ValueError, IndexError):
                            continue
                        oldest_in_batch = ts
                    if oldest_in_batch is None or oldest_in_batch <= start_ms:
                        break
                    after = oldest_in_batch
                    time.sleep(0.12)
        finally:
            if rows_buf:
                with self._lock, self._connect() as c:
                    c.executemany(
                        "INSERT OR IGNORE INTO ohlcv VALUES (?,?,?,?,?,?,?,?)",
                        rows_buf,
                    )
                    inserted = c.total_changes
                    c.commit()
        return inserted

    def _coverage_gaps(self, symbol: str, interval: str,
                       start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        """Identify spans in [start_ms, end_ms] not yet covered by DB. Conservative:
        we just check first/last bar presence; full gap-detection would be
        expensive. If the DB lacks the start or end bar, we backfill the whole
        range. Subsequent calls hit cache."""
        step = _BAR_MS.get(interval, 14_400_000)
        first_expected = (start_ms // step) * step
        last_expected = (end_ms // step) * step
        with self._connect() as c:
            row = c.execute(
                "SELECT MIN(open_ts), MAX(open_ts), COUNT(*) FROM ohlcv "
                "WHERE symbol=? AND interval=? AND open_ts>=? AND open_ts<=?",
                (symbol, interval, first_expected, last_expected),
            ).fetchone()
        mn, mx, cnt = row or (None, None, 0)
        if cnt == 0:
            return [(first_expected, last_expected)]
        if mn > first_expected:
            return [(first_expected, last_expected)]
        if mx < last_expected - step:  # allow up to 1-bar lag at the tail
            return [(mx + step, last_expected)]
        return []

    def ensure_range(self, symbol: str, interval: str,
                     start_ms: int, end_ms: int) -> None:
        """Block until [start_ms, end_ms] is covered (or OKX has been polled to
        exhaustion)."""
        with self._bf_lock(symbol, interval):
            gaps = self._coverage_gaps(symbol, interval, start_ms, end_ms)
            for g_start, g_end in gaps:
                log.info("backfilling %s %s [%d, %d]", symbol, interval, g_start, g_end)
                self._fetch_okx_range(symbol, interval, g_start, g_end)

    def query(self, symbol: str, interval: str,
              start_ms: int, end_ms: int, limit: int) -> list[dict]:
        with self._connect() as c:
            cur = c.execute(
                "SELECT open_ts, open, high, low, close, volume FROM ohlcv "
                "WHERE symbol=? AND interval=? AND open_ts>=? AND open_ts<=? "
                "ORDER BY open_ts ASC LIMIT ?",
                (symbol, interval, start_ms, end_ms, limit),
            )
            return [
                {"open_ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                 "close": r[4], "volume": r[5]}
                for r in cur
            ]


def fetch_bars(store: OhlcvStore, symbol: str, interval: str,
               start_ms: int, end_ms: int, page_limit: int = 10_000) -> tuple[list[dict], int | None]:
    """Returns (bars, next_cursor_ms). next_cursor is the open_ts of the next
    page's first bar (=last_returned_ts + 1ms) if we hit page_limit, else None."""
    store.ensure_range(symbol, interval, start_ms, end_ms)
    bars = store.query(symbol, interval, start_ms, end_ms, page_limit + 1)
    if len(bars) > page_limit:
        bars = bars[:page_limit]
        next_cursor = bars[-1]["open_ts"] + 1
        return bars, next_cursor
    return bars, None
