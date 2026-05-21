"""Tests for signal_bus cache cold_load — historical kline rehydration.

Bug fixed 2026-05-21: cold_load(hours_klines=24) only loaded last 24h from
SQLite, capping 1d cache at 1 bar, 4h at 6, 1h at 24. This broke any engine
needing multi-week history (vpoc_retest 850×1h, ict_confluence_1d 60×1d).

Fix: load up to KLINE_CAP=1000 most-recent bars per (coin, tf) regardless
of age, using a window-function partition.
"""
import os
import time
import tempfile
import sys

# Add repo to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def _make_cache_with_history():
    """Create a Cache with KLINE_CAP bars persisted in SQLite for BTC/1h
    and ETH/1d, simulating a service that had previously accumulated weeks
    of data."""
    from signal_bus.cache import Cache, KLINE_CAP
    import tempfile
    tmp = tempfile.mkdtemp()
    cache = Cache(db_path=os.path.join(tmp, "cache.db"))
    # Insert 1500 BTC 1h bars (older than 24h)
    now_ms = int(time.time() * 1000)
    for i in range(1500):
        ts = now_ms - i * 3600 * 1000
        cache.db.execute(
            "INSERT INTO klines VALUES('BTC','1h',?,?,?,?,?,?)",
            (ts, 100.0 + i * 0.01, 105.0, 95.0, 102.0, 1000.0)
        )
    # Insert 800 ETH 1d bars (most older than 24h)
    for i in range(800):
        ts = now_ms - i * 86400 * 1000
        cache.db.execute(
            "INSERT INTO klines VALUES('ETH','1d',?,?,?,?,?,?)",
            (ts, 2000.0 + i * 0.5, 2050.0, 1950.0, 2020.0, 500.0)
        )
    # Cache memory should be empty until cold_load runs
    return cache, tmp


def test_cold_load_rehydrates_full_history_not_just_24h():
    """Cold load should fill the ring buffer up to KLINE_CAP per (coin, tf),
    not just last 24h. Previous bug: 1d/4h/1h caches were 1/6/24 bars on boot."""
    from signal_bus.cache import KLINE_CAP
    cache, _ = _make_cache_with_history()
    # Memory empty pre-load
    assert len(cache.klines.get(("BTC", "1h"), [])) == 0
    assert len(cache.klines.get(("ETH", "1d"), [])) == 0

    cache.cold_load()

    # BTC 1h: should be full 1000 (KLINE_CAP)
    btc_1h = list(cache.klines[("BTC", "1h")])
    assert len(btc_1h) == KLINE_CAP, f"expected {KLINE_CAP}, got {len(btc_1h)}"
    # Should be the MOST RECENT 1000 (chronological asc)
    assert btc_1h[0]["open_ts"] < btc_1h[-1]["open_ts"], "must be ascending"

    # ETH 1d: 800 bars exist in DB (less than KLINE_CAP), all should load
    eth_1d = list(cache.klines[("ETH", "1d")])
    assert len(eth_1d) == 800, f"expected 800, got {len(eth_1d)}"
    assert eth_1d[0]["open_ts"] < eth_1d[-1]["open_ts"]


def test_cold_load_respects_klines_per_key_override():
    """Caller can request fewer bars via klines_per_key for memory-constrained runs."""
    cache, _ = _make_cache_with_history()
    cache.cold_load(klines_per_key=100)
    # Both coin/tf combos capped at 100
    assert len(cache.klines[("BTC", "1h")]) == 100
    assert len(cache.klines[("ETH", "1d")]) == 100


def test_cold_load_preserves_chronological_order_per_key():
    """Bars within each (coin, tf) ring buffer must be oldest-first."""
    cache, _ = _make_cache_with_history()
    cache.cold_load()
    btc = list(cache.klines[("BTC", "1h")])
    timestamps = [b["open_ts"] for b in btc]
    assert timestamps == sorted(timestamps), "BTC 1h bars not in ascending order"
    eth = list(cache.klines[("ETH", "1d")])
    timestamps_eth = [b["open_ts"] for b in eth]
    assert timestamps_eth == sorted(timestamps_eth), "ETH 1d bars not in ascending order"


def test_cold_load_handles_empty_db():
    """Empty SQLite should not crash cold_load."""
    from signal_bus.cache import Cache
    cache = Cache(db_path=os.path.join(tempfile.mkdtemp(), "cache.db"))
    cache.cold_load()  # no rows to load — must not crash
    assert len(cache.klines) == 0


def test_cold_load_partition_isolation():
    """Per-(coin, tf) ROW_NUMBER must not bleed across keys.
    If BTC has 1500 bars and ETH has 1500 bars, the LIMIT 1000 partition should
    return 1000+1000, not 1000 total."""
    cache, _ = _make_cache_with_history()
    # Add another big partition to verify partition isolation
    now_ms = int(time.time() * 1000)
    for i in range(1200):
        ts = now_ms - i * 60 * 1000  # 1m bars
        cache.db.execute(
            "INSERT INTO klines VALUES('SOL','1m',?,?,?,?,?,?)",
            (ts, 50.0, 51.0, 49.0, 50.5, 100.0)
        )
    cache.cold_load()
    # Each partition independently caps at 1000
    assert len(cache.klines[("BTC", "1h")]) == 1000
    assert len(cache.klines[("ETH", "1d")]) == 800   # < cap, all rows
    assert len(cache.klines[("SOL", "1m")]) == 1000
