"""Regression tests for the 5 sentinel-audit fixes.

1. drawdown peak persists across process restart (kv_state)
2. liq flush cursor — events written once, not on every flush
3. pretrade coin concentration math — exact arithmetic verified
4. HL funding extraction from activeAssetCtx
5. backtest load_strategy handles range_bo NAME / range_breakout module split
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

import pytest

from common import persistence
from signal_bus.cache import Cache
from signal_bus import hl_ws
from pm import pretrade

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# -------- Fix 1: drawdown peak persistence --------

def test_kv_state_persists_value():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "kv.db")
        conn = persistence.init_db(path)
        persistence.kv_set(conn, "drawdown_peak_v1", json.dumps({"value": 500.0, "ts": 1.0}))
        conn.close()
        # reopen (simulates process restart)
        conn2 = persistence.init_db(path)
        raw = persistence.kv_get(conn2, "drawdown_peak_v1")
        d2 = json.loads(raw)
        assert d2["value"] == 500.0
        assert d2["ts"] == 1.0


def test_drawdown_load_peak_handles_missing():
    from monitor.routines import drawdown_check
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "k.db"))
        peak = drawdown_check._load_peak(conn)
        assert peak == {"value": 0.0, "ts": 0.0}


def test_drawdown_load_peak_handles_corruption():
    from monitor.routines import drawdown_check
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "k.db"))
        persistence.kv_set(conn, drawdown_check._KV_KEY, "not json")
        peak = drawdown_check._load_peak(conn)
        assert peak == {"value": 0.0, "ts": 0.0}


def test_drawdown_save_then_load_roundtrip():
    from monitor.routines import drawdown_check
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "k.db"))
        drawdown_check._save_peak(conn, {"value": 1234.5, "ts": 999.0})
        peak = drawdown_check._load_peak(conn)
        assert peak["value"] == 1234.5
        assert peak["ts"] == 999.0


# -------- Fix 2: liq flush cursor --------

def test_flush_liqs_does_not_double_write():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        now = int(time.time() * 1000)
        for i in range(5):
            c.push_liq({"ts": now + i, "coin": "BTC", "side": "SELL",
                        "qty": 1, "price": 100, "usd": 100})
        n1 = c.flush_liqs()
        assert n1 == 5
        # second flush with no new events → writes 0
        n2 = c.flush_liqs()
        assert n2 == 0
        # SQLite must have exactly 5 rows for these events
        rows = c.db.execute("SELECT COUNT(*) AS n FROM liq_events").fetchone()
        assert rows["n"] == 5


def test_flush_liqs_only_writes_new_events():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        base = int(time.time() * 1000)
        for i in range(3):
            c.push_liq({"ts": base + i, "coin": "BTC", "side": "SELL",
                        "qty": 1, "price": 100, "usd": 100})
        c.flush_liqs()
        # new events with newer ts
        for i in range(2):
            c.push_liq({"ts": base + 100 + i, "coin": "BTC", "side": "BUY",
                        "qty": 1, "price": 100, "usd": 100})
        n = c.flush_liqs()
        assert n == 2
        rows = c.db.execute("SELECT COUNT(*) AS n FROM liq_events").fetchone()
        assert rows["n"] == 5


def test_cold_load_seeds_flush_cursor():
    """Restart: events already in SQLite must not be re-flushed back into SQLite."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        c = Cache(path)
        now = int(time.time() * 1000)
        for i in range(3):
            c.push_liq({"ts": now + i, "coin": "BTC", "side": "SELL",
                        "qty": 1, "price": 100, "usd": 100})
        c.flush_liqs()  # rows are now in SQLite

        # simulate restart
        c2 = Cache(path)
        c2.cold_load(hours_liqs=24)
        # ring buffer rehydrated from SQLite (3 entries)
        assert len(c2.liqs) == 3
        n_extra = c2.flush_liqs()
        assert n_extra == 0  # cursor seeded → no double-write
        rows = c2.db.execute("SELECT COUNT(*) AS n FROM liq_events").fetchone()
        assert rows["n"] == 3


# -------- Fix 3: pretrade concentration math --------

def _sig(coin="BTC", is_long=True, ref=60000.0):
    return {"coin": coin, "is_long": is_long, "side": "B" if is_long else "A",
            "ref_price": ref, "sl_px": ref * 0.99, "tp_px": ref * 1.03,
            "max_hold_bars": 24, "fire_reason": "test"}


# Concentration tests removed 2026-05-19: the PRETRADE_COIN_CONC_MAX gate
# was deleted from pm.pretrade and replaced by a strict 1_GLOBAL coin lock.
# Coin-lock behavior is covered by tests/test_pm.py::test_pretrade_coin_lock_blocks_same_coin.


# -------- Fix 4: HL funding from activeAssetCtx --------

def test_hl_ws_writes_funding_when_present():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        hl_ws._on_active_asset_ctx(c, {
            "coin": "BTC",
            "ctx": {"markPx": "60000.0", "funding": "0.000125"},
        })
        rows = c.db.execute(
            "SELECT venue, rate FROM funding WHERE coin='BTC' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        assert rows is not None
        assert rows["venue"] == "hyperliquid"
        assert abs(rows["rate"] - 0.000125) < 1e-9


def test_hl_ws_skips_funding_when_missing():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        hl_ws._on_active_asset_ctx(c, {
            "coin": "BTC",
            "ctx": {"markPx": "60000.0"},  # no funding
        })
        rows = c.db.execute(
            "SELECT COUNT(*) AS n FROM funding WHERE coin='BTC' AND venue='hyperliquid'"
        ).fetchone()
        assert rows["n"] == 0


def test_hl_ws_funding_does_not_clobber_binance_mark():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        c.push_mark("BTC", {"ts": 1, "binance_mid": 60000.0, "hl_mid": None})
        hl_ws._on_active_asset_ctx(c, {
            "coin": "BTC",
            "ctx": {"markPx": "60010.0", "funding": "0.0001"},
        })
        m = c.get_mark("BTC")
        assert m["binance_mid"] == 60000.0
        assert m["hl_mid"] == 60010.0


# -------- Fix 5: backtest load_strategy --------

# load_strategy tests for archived modules (range_bo, range_breakout, fsp,
# vsq, range_fade, fd1, lh1, precog, cex_dex_arb) removed 2026-05-19 — those
# modules live in strategy_runner/strategies/_archived/. test_load_strategy_unknown_raises
# is preserved below as it exercises the negative path.


def test_load_strategy_unknown_raises():
    from scripts.backtest_harness import load_strategy
    with pytest.raises(SystemExit):
        load_strategy("definitely_not_a_strategy")


# fd1 red-gate test removed 2026-05-19: fd1 was archived from the registry,
# so pm.check no longer hard-blocks it with reason "audit_red_gated" — it
# falls through as an unknown engine. test_pretrade_other_strategies_not_red_gated
# remains valid since it only checks the *absence* of audit_red_gated.


def test_pretrade_other_strategies_not_red_gated(monkeypatch):
    # Archived strategies should not be hard-blocked with audit_red_gated;
    # they may be blocked for other reasons (e.g. no registry entry), but
    # the specific red-gate path is gone.
    monkeypatch.setenv("STRATEGY_STOP_HUNT_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_VPOC_RETEST_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_OI_CONCENTRATION_ENABLED", "1")
    monkeypatch.setenv("MIN_TRADE_USD", "10")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        for s in ("stop_hunt", "vpoc_retest", "oi_concentration"):
            r = pretrade.check(conn, s, _sig(), {"regime": "range", "confidence": 0.6},
                               500.0, [])
            assert r.reason != "audit_red_gated", f"{s} incorrectly red-gated"
