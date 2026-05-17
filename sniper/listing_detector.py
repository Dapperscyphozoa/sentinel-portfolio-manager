"""HL new-listing detector.

Polls /info {type:"meta"} every N seconds. When a NEW coin symbol appears
in the universe that wasn't there last poll, fires a listing event.

This is the entry point for the sniper bot. Listings on HL are rare (council
estimate: 10-80 per year) so polling cadence can be conservative (5-15s).

Persistent state: SQLite tracks last-seen universe so the detector survives
service restarts without spurious "new" detections.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("listing_detector")


HL_INFO = "https://api.hyperliquid.xyz/info"


@dataclass
class ListingEvent:
    coin: str
    detected_ts: int        # ms epoch
    hl_universe_index: int  # position in HL universe list


class ListingDetector:
    """Polls HL meta endpoint and emits new-listing events."""

    def __init__(self, state_path: Optional[str] = None,
                 poll_interval_s: float = 10.0,
                 http_timeout_s: float = 10.0) -> None:
        self.state_path = state_path or os.environ.get(
            "SNIPER_LISTING_DB", "/var/data/sniper_listings.sqlite"
        )
        self.poll_interval_s = poll_interval_s
        self.http_timeout_s = http_timeout_s
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.state_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def _init_db(self) -> None:
        c = self._conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS known_universe (
            coin TEXT PRIMARY KEY,
            first_seen_ts INTEGER NOT NULL,
            universe_index INTEGER
        );
        CREATE TABLE IF NOT EXISTS listing_events (
            ts INTEGER NOT NULL,
            coin TEXT NOT NULL,
            universe_index INTEGER,
            handled INTEGER DEFAULT 0,
            PRIMARY KEY (ts, coin)
        );
        """)
        c.commit()
        c.close()

    def fetch_universe(self) -> list[str]:
        """Returns the current list of HL perp symbols."""
        from sniper.oracle_lag import _client
        cli = _client(self.http_timeout_s)
        r = cli.post(HL_INFO, json={"type": "meta"})
        r.raise_for_status()
        data = r.json()
        # `meta` returns { universe: [ {name: "BTC", ...}, ... ] }
        return [u["name"] for u in data.get("universe", [])]

    def known_coins(self) -> set[str]:
        c = self._conn()
        rows = c.execute("SELECT coin FROM known_universe").fetchall()
        c.close()
        return {r["coin"] for r in rows}

    def record_listing(self, coin: str, universe_index: int, ts: int) -> None:
        c = self._conn()
        c.execute(
            "INSERT OR IGNORE INTO known_universe VALUES (?, ?, ?)",
            (coin, ts, universe_index),
        )
        c.execute(
            "INSERT OR IGNORE INTO listing_events (ts, coin, universe_index) VALUES (?, ?, ?)",
            (ts, coin, universe_index),
        )
        c.commit()
        c.close()

    def check_for_new(self) -> list[ListingEvent]:
        """Single poll cycle. Returns any new listings found."""
        try:
            current_universe = self.fetch_universe()
        except Exception as e:
            log.warning("fetch_universe failed: %s", e)
            return []
        now_ms = int(time.time() * 1000)
        known = self.known_coins()
        new_events: list[ListingEvent] = []
        for idx, coin in enumerate(current_universe):
            if coin not in known:
                event = ListingEvent(coin=coin, detected_ts=now_ms, hl_universe_index=idx)
                new_events.append(event)
                self.record_listing(coin, idx, now_ms)
                log.info("NEW LISTING DETECTED: %s (universe_index=%d)", coin, idx)
        return new_events

    def bootstrap_known_universe(self) -> int:
        """First-run: populate known_universe so we don't fire events for
        every existing coin. Returns count populated."""
        c = self._conn()
        existing = c.execute("SELECT COUNT(*) FROM known_universe").fetchone()[0]
        c.close()
        if existing > 0:
            return 0   # already bootstrapped
        try:
            current_universe = self.fetch_universe()
        except Exception as e:
            log.error("bootstrap failed: %s", e)
            return 0
        now_ms = int(time.time() * 1000)
        c = self._conn()
        for idx, coin in enumerate(current_universe):
            c.execute(
                "INSERT OR IGNORE INTO known_universe VALUES (?, ?, ?)",
                (coin, now_ms, idx),
            )
        c.commit()
        c.close()
        log.info("bootstrapped %d coins as known", len(current_universe))
        return len(current_universe)

    def recent_listings(self, since_ms: int) -> list[dict]:
        """Return listing events in [since_ms, now]."""
        c = self._conn()
        rows = c.execute(
            "SELECT ts, coin, universe_index, handled FROM listing_events WHERE ts >= ? ORDER BY ts",
            (since_ms,),
        ).fetchall()
        c.close()
        return [dict(r) for r in rows]

    def mark_handled(self, ts: int, coin: str) -> None:
        c = self._conn()
        c.execute(
            "UPDATE listing_events SET handled=1 WHERE ts=? AND coin=?",
            (ts, coin),
        )
        c.commit()
        c.close()
