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


def test_concentration_allows_room_inside_cap(monkeypatch):
    """Already have $100 BTC notional, cap=2.0 → allow up to $100 more."""
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    monkeypatch.setenv("PRETRADE_COIN_CONC_MAX", "2.0")
    monkeypatch.setenv("RISK_PCT_PER_TRADE", "0.02")
    monkeypatch.setenv("LEVERAGE", "5")
    monkeypatch.setenv("MIN_TRADE_USD", "10")
    monkeypatch.setenv("PER_STRATEGY_CAP", "1.0")  # don't let per-strategy cap interfere
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        # account $500, risk 2% * lev 5 → proposed $50; existing $100 BTC notional;
        # max_additional = 100 * (2-1) = $100. Proposed $50 ≤ $100 → allow at $50.
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [{"coin": "BTC", "strategy": "fsp", "notional": 100}])
        assert r.allow is True
        assert r.size_usd == 50.0


def test_concentration_caps_size_at_max_additional(monkeypatch):
    """Already have $100 BTC notional, cap=1.5 → max_additional = $50; if proposal would be larger, clip to $50."""
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    monkeypatch.setenv("PRETRADE_COIN_CONC_MAX", "1.5")
    monkeypatch.setenv("RISK_PCT_PER_TRADE", "0.10")  # large proposed
    monkeypatch.setenv("LEVERAGE", "5")
    monkeypatch.setenv("MIN_TRADE_USD", "10")
    monkeypatch.setenv("PER_STRATEGY_CAP", "1.0")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        # max_additional = 100 * 0.5 = $50 ; proposed pre-cap would be $250
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [{"coin": "BTC", "strategy": "fsp", "notional": 100}])
        assert r.allow is True
        assert r.size_usd == 50.0


def test_concentration_blocks_when_cap_equals_one(monkeypatch):
    """cap=1.0 → no further concentration allowed."""
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    monkeypatch.setenv("PRETRADE_COIN_CONC_MAX", "1.0")
    monkeypatch.setenv("PER_STRATEGY_CAP", "1.0")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [{"coin": "BTC", "strategy": "fsp", "notional": 100}])
        assert r.allow is False
        assert r.reason == "coin_concentration_full"


def test_concentration_no_existing_position_is_open(monkeypatch):
    """No existing this-coin notional → concentration check is no-op."""
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    monkeypatch.setenv("PRETRADE_COIN_CONC_MAX", "1.0")  # would block if applied
    monkeypatch.setenv("MIN_TRADE_USD", "10")
    monkeypatch.setenv("PER_STRATEGY_CAP", "1.0")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [])  # no BTC position
        assert r.allow is True


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

def test_load_strategy_range_bo_via_range_breakout_module():
    from scripts.backtest_harness import load_strategy
    cls = load_strategy("range_bo")
    assert cls.NAME == "range_bo"


def test_load_strategy_explicit_module_name():
    from scripts.backtest_harness import load_strategy
    cls = load_strategy("range_breakout")
    assert cls.NAME == "range_bo"


def test_load_strategy_direct_name_match():
    from scripts.backtest_harness import load_strategy
    for n in ("fsp", "vsq", "range_fade", "fd1", "lh1", "precog", "liq_cascade", "cex_dex_arb"):
        cls = load_strategy(n)
        assert cls.NAME == n, f"{n} → {cls.NAME}"


def test_load_strategy_unknown_raises():
    from scripts.backtest_harness import load_strategy
    with pytest.raises(SystemExit):
        load_strategy("definitely_not_a_strategy")
