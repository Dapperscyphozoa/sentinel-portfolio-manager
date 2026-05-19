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
    monkeypatch.setenv("STRATEGY_RANGE_FADE_ENABLED", "1")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        # range_fade affinity is range/chop; in strong trend_up regime → block
        r = pretrade.check(conn, "range_fade", _sig(), {"regime": "trend_up", "confidence": 0.9},
                           500.0, [])
        assert r.allow is False
        assert "regime_mismatch" in r.reason


def test_pretrade_regime_low_confidence_passes(monkeypatch):
    monkeypatch.setenv("STRATEGY_RANGE_FADE_ENABLED", "1")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        # low confidence trend → don't block even if affinity mismatch
        r = pretrade.check(conn, "range_fade", _sig(), {"regime": "trend_up", "confidence": 0.5},
                           500.0, [])
        assert r.allow is True


def test_pretrade_coin_concentration_blocks(monkeypatch):
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    monkeypatch.setenv("PRETRADE_COIN_CONC_MAX", "1.0")  # no further BTC allowed
    monkeypatch.setenv("RISK_PCT_PER_TRADE", "0.02")
    monkeypatch.setenv("LEVERAGE", "5")
    monkeypatch.setenv("MIN_TRADE_USD", "10")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        # already have BTC notional; add proposal also for BTC → blocked
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [{"coin": "BTC", "strategy": "fsp", "notional": 200}])
        assert r.allow is False
        assert "concentration" in r.reason


# -------- ADJUSTMENTS_REPORT.md #3: by-coin pruning gate --------

def _seed_closures(conn, strategy: str, coin: str, wins: int, losses: int,
                   win_pnl: float = 5.0, loss_pnl: float = 10.0,
                   close_reason: str = "tp_hit") -> None:
    """Insert synthetic closure rows for testing the by-coin prune gate.

    wins fires of +win_pnl and losses fires of -loss_pnl. close_reason
    controls clean-vs-noisy classification.
    """
    import time as _t
    base_ts = _t.time() - 100
    for i in range(wins + losses):
        pnl = win_pnl if i < wins else -loss_pnl
        conn.execute(
            "INSERT INTO closures(cloid,strategy,coin,is_long,open_ts,close_ts,"
            "open_px,close_px,size_coin,pnl_usd,fees_usd,close_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"c_{strategy}_{coin}_{i}", strategy, coin, 1, base_ts, base_ts + 1,
             100, 101, 1.0, pnl, 0.0, close_reason),
        )


def test_by_coin_prune_default_off(monkeypatch):
    """Default OFF: even with a dead history, the gate must not reject."""
    monkeypatch.delenv("ENABLE_BY_COIN_PRUNE", raising=False)
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        _seed_closures(conn, "fsp", "BTC", wins=2, losses=20)  # PF 1/20 = 0.05
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [])
        assert r.allow is True


def test_by_coin_prune_blocks_dead_pair(monkeypatch):
    """Enabled + dead pair (n>=15, PF<1.0) → reject with by_coin_prune reason."""
    monkeypatch.setenv("ENABLE_BY_COIN_PRUNE", "1")
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        _seed_closures(conn, "fsp", "BTC", wins=2, losses=20)  # PF = 10/200 = 0.05
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [])
        assert r.allow is False
        assert r.reason.startswith("by_coin_prune:")


def test_by_coin_prune_allows_below_min_n(monkeypatch):
    """Below sample-size floor (n<15) — must not prune yet."""
    monkeypatch.setenv("ENABLE_BY_COIN_PRUNE", "1")
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        _seed_closures(conn, "fsp", "BTC", wins=0, losses=10)  # PF 0 but n=10
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [])
        assert r.allow is True


def test_by_coin_prune_allows_pf_above_threshold(monkeypatch):
    """Engine/coin pair with PF>1.0 — must not prune."""
    monkeypatch.setenv("ENABLE_BY_COIN_PRUNE", "1")
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        _seed_closures(conn, "fsp", "BTC", wins=15, losses=2)  # PF = 75/20 = 3.75
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [])
        assert r.allow is True


def test_by_coin_prune_ignores_noisy_closures(monkeypatch):
    """Noisy closures (force_close*) must not count toward the dead-pair test —
    otherwise an operator force-close binge can poison an otherwise-healthy pair.
    """
    monkeypatch.setenv("ENABLE_BY_COIN_PRUNE", "1")
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        # 20 noisy losses — should be ignored. Only 5 clean wins counted.
        _seed_closures(conn, "fsp", "BTC", wins=0, losses=20,
                       close_reason="force_close:audit_red")
        _seed_closures(conn, "fsp", "BTC", wins=5, losses=0,
                       close_reason="tp_hit")
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [])
        # n=5 (below min_n 15) AND no losses → allow
        assert r.allow is True


def test_by_coin_prune_engine_allowlist(monkeypatch):
    """BY_COIN_PRUNE_ENGINES allowlist restricts the rule to named engines."""
    monkeypatch.setenv("ENABLE_BY_COIN_PRUNE", "1")
    monkeypatch.setenv("BY_COIN_PRUNE_ENGINES", "vsq,lh1")  # fsp not in list
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        _seed_closures(conn, "fsp", "BTC", wins=2, losses=20)  # dead pair
        r = pretrade.check(conn, "fsp", _sig("BTC"), {"regime": "range", "confidence": 0.6},
                           500.0, [])
        assert r.allow is True  # fsp not in allowlist → bypassed


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
