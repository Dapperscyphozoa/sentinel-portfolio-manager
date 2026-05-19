"""pm tests: pretrade gate + regime classifier + attribution."""
from __future__ import annotations

import os
import tempfile

import pytest

from common import persistence
from pm import attribution, pretrade, regime as regime_mod


# -------- pretrade --------

def _sig(coin="BTC", is_long=True, ref=60000.0):
    return {"coin": coin, "is_long": is_long, "side": "B" if is_long else "A",
            "ref_price": ref, "sl_px": ref * 0.99, "tp_px": ref * 1.03,
            "max_hold_bars": 24, "fire_reason": "test"}


def test_pretrade_allows_basic(monkeypatch):
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    monkeypatch.setenv("RISK_PCT_PER_TRADE", "0.02")
    monkeypatch.setenv("LEVERAGE", "5")
    monkeypatch.setenv("MIN_TRADE_USD", "10")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        r = pretrade.check(conn, "fsp", _sig(), {"regime": "range", "confidence": 0.6},
                           account_value_usd=500.0, open_positions=[])
        assert r.allow is True
        assert r.size_usd > 0


def test_pretrade_blocks_when_disabled(monkeypatch):
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "0")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        r = pretrade.check(conn, "fsp", _sig(), {"regime": "range", "confidence": 0.6},
                           500.0, [])
        assert r.allow is False
        assert "disabled" in r.reason


def test_pretrade_blocks_when_max_open_global(monkeypatch):
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "2")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        positions = [{"coin": "ETH", "strategy": "vsq", "notional": 100},
                     {"coin": "SOL", "strategy": "fsp", "notional": 100}]
        r = pretrade.check(conn, "fsp", _sig(), {"regime": "range", "confidence": 0.6},
                           500.0, positions)
        assert r.allow is False
        assert r.reason == "max_open_global"


def test_pretrade_regime_mismatch_blocks(monkeypatch):
    # oi_concentration affinity is ["high_vol", "range", "chop"] (no trend_up);
    # in strong trend_up regime the affinity gate should block.
    monkeypatch.setenv("STRATEGY_OI_CONCENTRATION_ENABLED", "1")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        r = pretrade.check(conn, "oi_concentration", _sig(),
                           {"regime": "trend_up", "confidence": 0.9}, 500.0, [])
        assert r.allow is False
        assert "regime_mismatch" in r.reason


def test_pretrade_regime_low_confidence_passes(monkeypatch):
    monkeypatch.setenv("STRATEGY_OI_CONCENTRATION_ENABLED", "1")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        # low confidence trend → don't block even if affinity mismatch
        r = pretrade.check(conn, "oi_concentration", _sig(),
                           {"regime": "trend_up", "confidence": 0.5}, 500.0, [])
        assert r.allow is True


def test_pretrade_coin_lock_blocks_same_coin(monkeypatch):
    # 1_GLOBAL coin lock — one position per coin across all engines.
    # Replaces the v1 PRETRADE_COIN_CONC_MAX gate (deleted from pm.pretrade).
    monkeypatch.setenv("STRATEGY_STOP_HUNT_ENABLED", "1")
    monkeypatch.setenv("LEVERAGE", "5")
    monkeypatch.setenv("MIN_TRADE_USD", "10")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        # An engine already holds BTC; another engine attempting BTC is blocked.
        r = pretrade.check(conn, "stop_hunt", _sig("BTC"),
                           {"regime": "range", "confidence": 0.6}, 500.0,
                           [{"coin": "BTC", "strategy": "hl_settle_5m", "notional": 200}])
        assert r.allow is False
        assert r.reason == "coin_locked"


# -------- regime --------

def test_regime_unknown_with_few_bars():
    r = regime_mod.classify([100.0] * 10, [101.0] * 10, [99.0] * 10)
    assert r["regime"] == "unknown"


def test_regime_trend_up():
    # steady uptrend
    closes = [100.0 + i * 0.5 for i in range(80)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    r = regime_mod.classify(closes, highs, lows)
    assert r["regime"] == "trend_up"


def test_regime_trend_down():
    closes = [200.0 - i * 0.5 for i in range(80)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    r = regime_mod.classify(closes, highs, lows)
    assert r["regime"] == "trend_down"


def test_regime_range_when_quiet():
    closes = [100.0 + (i % 2) * 0.05 for i in range(80)]
    highs = [c + 0.06 for c in closes]
    lows = [c - 0.06 for c in closes]
    r = regime_mod.classify(closes, highs, lows)
    assert r["regime"] == "range"


# -------- attribution --------

def test_attribution_register_and_lookup():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        attribution.init(conn)
        attribution.register(conn, "0xab" + "00" * 15, "fsp", "BTC", "B")
        assert attribution.strategy_for(conn, "0xab" + "00" * 15) == "fsp"
        assert attribution.strategy_for(conn, "0xnonexistent") is None


def test_attribution_by_strategy_pnl():
    import time as t
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        attribution.init(conn)
        now = t.time()
        for strat, pnl in [("fsp", 10.0), ("fsp", 5.0), ("vsq", -3.0)]:
            conn.execute(
                "INSERT INTO closures(cloid,strategy,coin,is_long,open_ts,close_ts,open_px,close_px,size_coin,pnl_usd,fees_usd) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("c" + str(pnl), strat, "BTC", 1, now - 100, now, 60000, 60100, 0.001, pnl, 0),
            )
        rows = attribution.by_strategy(conn, since_ms=0)
        by = {r["strategy"]: r for r in rows}
        assert by["fsp"]["n"] == 2
        assert abs(by["fsp"]["pnl_usd"] - 15.0) < 1e-6
        assert by["vsq"]["pnl_usd"] == -3.0
