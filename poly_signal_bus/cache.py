"""In-memory + SQLite cache for poly-signal-bus.

Owns the canonical state for:
  - Last N CEX ticks per (venue, asset)  →  for cl_aggregator
  - Last N Chainlink reports per asset    →  for validation logging
  - PM market dict (active markets, books) →  served to runner

Memory budget: ~20MB for default sizes (600 ticks * 7 venues * 2 assets *
~80 bytes; plus PM markets * ~500 bytes; plus CL reports).

Persistence: flushes to SQLite every PERSIST_INTERVAL_S so a restart
loses no more than that.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import sqlite3
import time
from typing import Optional

from common.poly_persistence import connect_poly, init_poly_db

log = logging.getLogger("poly_cache")

CEX_RING_SIZE = 600        # ~10 min at 1s resolution
CL_RING_SIZE = 1000        # historical CL ticks
PERSIST_INTERVAL_S = 60.0
CL_VALIDATION_KEEP_DAYS = 30


class PolyCache:
    """Thread-safe-ish (single asyncio loop) state holder."""

    def __init__(self) -> None:
        # CEX ticks: { (venue, asset) -> deque[(ts_ms, mid, bid, ask)] }
        self.cex: dict[tuple[str, str], collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=CEX_RING_SIZE))

        # CL reports: { asset -> deque[(ts_ms, price)] }
        self.cl_actual: dict[str, collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=CL_RING_SIZE))

        # PM markets: { market_id -> dict }
        self.markets: dict[str, dict] = {}

        # Last predicted CL value: { asset -> (ts_ms, predicted, diag) }
        self.cl_predicted: dict[str, tuple[int, float, dict]] = {}

        # Per-venue last update timestamp (for /health)
        self.venue_last_seen: dict[str, int] = {}

        self._db_path = init_poly_db()
        self._stop = asyncio.Event()

    # ─── CEX ───────────────────────────────────────────────────────────
    def add_cex(self, venue: str, asset: str, ts_ms: int,
                mid: float, bid: float, ask: float) -> None:
        self.cex[(venue, asset)].append((ts_ms, mid, bid, ask))
        self.venue_last_seen[venue] = ts_ms

    def latest_cex_mids(self, asset: str, max_age_ms: int = 3000) -> dict[str, float]:
        """Return {venue: mid_price} for all venues with a recent tick."""
        now = int(time.time() * 1000)
        out: dict[str, float] = {}
        for (venue, a), ring in self.cex.items():
            if a != asset or not ring:
                continue
            ts, mid, *_ = ring[-1]
            if now - ts <= max_age_ms:
                out[venue] = mid
        return out

    def cex_history(self, venue: str, asset: str, n: int = 60) -> list[tuple]:
        ring = self.cex.get((venue, asset))
        if not ring:
            return []
        return list(ring)[-n:]

    # ─── CL ───────────────────────────────────────────────────────────
    def add_cl_actual(self, asset: str, ts_ms: int, price: float) -> None:
        self.cl_actual[asset].append((ts_ms, price))

    def latest_cl_actual(self, asset: str) -> Optional[tuple[int, float]]:
        ring = self.cl_actual.get(asset)
        if not ring:
            return None
        return ring[-1]

    def set_cl_predicted(self, asset: str, ts_ms: int, predicted: float,
                          diag: dict) -> None:
        self.cl_predicted[asset] = (ts_ms, predicted, diag)

    def latest_cl_predicted(self, asset: str) -> Optional[tuple[int, float, dict]]:
        return self.cl_predicted.get(asset)

    # ─── PM markets ────────────────────────────────────────────────────
    def upsert_market(self, m: dict) -> None:
        mid = m["market_id"]
        if mid in self.markets:
            self.markets[mid].update(m)
        else:
            self.markets[mid] = m

    def update_book(self, market_id: str, token_id: str, side_data: dict) -> None:
        m = self.markets.get(market_id)
        if not m:
            return
        if token_id == m.get("token_id_yes"):
            m["yes_bid"] = side_data.get("best_bid")
            m["yes_ask"] = side_data.get("best_ask")
        elif token_id == m.get("token_id_no"):
            m["no_bid"] = side_data.get("best_bid")
            m["no_ask"] = side_data.get("best_ask")
        m["last_update_ts"] = side_data.get("ts", time.time())

    def active_markets(self) -> list[dict]:
        now = time.time()
        return [m for m in self.markets.values()
                if m.get("end_ts") and m["end_ts"] > now]

    def expire_stale_markets(self) -> int:
        now = time.time()
        stale = [mid for mid, m in self.markets.items()
                 if m.get("end_ts") and m["end_ts"] < now - 60]
        for mid in stale:
            del self.markets[mid]
        return len(stale)

    # ─── Persistence loop ──────────────────────────────────────────────
    async def persist_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(PERSIST_INTERVAL_S)
                self._persist_now()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"persist failed: {e}")

    def _persist_now(self) -> None:
        """Flush CL validation samples to SQLite for the gate script."""
        conn = connect_poly()
        try:
            now = int(time.time() * 1000)
            for asset, (ts_ms, predicted, diag) in self.cl_predicted.items():
                actual = self.latest_cl_actual(asset)
                if not actual:
                    continue
                actual_ts, actual_price = actual
                if abs(actual_ts - ts_ms) > 5000:
                    continue  # too far apart to compare
                from .cl_aggregator import diff_bps
                d = diff_bps(predicted, actual_price)
                n_v = diag.get("n_after_trim", 0)
                conn.execute(
                    "INSERT OR IGNORE INTO poly_cl_validation"
                    "(ts, asset, cl_actual, cl_predicted, diff_bps, n_venues)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (ts_ms / 1000.0, asset, actual_price, predicted, d, n_v),
                )
            # GC validation table
            cutoff = (time.time() - CL_VALIDATION_KEEP_DAYS * 86400)
            conn.execute("DELETE FROM poly_cl_validation WHERE ts < ?", (cutoff,))
        finally:
            conn.close()

    def stop(self) -> None:
        self._stop.set()


# Singleton accessor
_singleton: Optional[PolyCache] = None


def get_cache() -> PolyCache:
    global _singleton
    if _singleton is None:
        _singleton = PolyCache()
    return _singleton
