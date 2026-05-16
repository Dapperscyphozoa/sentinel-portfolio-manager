"""Tests for Phase 1 live-safety controls + ICT confluence integration."""
from __future__ import annotations

import os
import tempfile
import time


def setup_safety_db():
    """Fresh safety DB for each test."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.unlink(path)
    os.environ["LIVE_SAFETY_DB"] = path
    # reset singleton
    import common.live_safety as ls
    ls._safety = None
    return path


def teardown_safety_db(path: str):
    if os.path.exists(path):
        os.unlink(path)


# ─────────────────────── Live Safety Controller ───────────────────────
def test_safety_kill_switch_blocks_all():
    p = setup_safety_db()
    try:
        from common.live_safety import LiveSafetyController
        os.environ["PM_FORCE_KILL_ALL"] = "1"
        ctl = LiveSafetyController(p)
        sig = {"ref_price": 100.0, "sl_px": 95.0}
        r = ctl.check(sig, 491.24, [])
        assert not r.allow
        assert r.reason == "kill_switch_active"
        del os.environ["PM_FORCE_KILL_ALL"]
    finally:
        teardown_safety_db(p)


def test_safety_atr_sizing_at_council_spec():
    """0.25% risk × 3x lev with 2% SL → margin = $0.41 per $491 (correct)."""
    p = setup_safety_db()
    try:
        from common.live_safety import LiveSafetyController
        os.environ.pop("PM_FORCE_KILL_ALL", None)
        ctl = LiveSafetyController(p)
        sig = {"ref_price": 100.0, "sl_px": 98.0}   # 2% SL distance
        r = ctl.check(sig, 491.24, [])
        assert r.allow, f"expected allow, got {r.reason}"
        # risk = 0.25% × 491.24 = $1.228
        # notional = $1.228 / 0.02 = $61.4
        # margin = $61.4 / 3 = $20.47
        # But max_margin_cap = 3% × 491.24 = $14.74 → capped
        assert 10 < r.margin_usd < 25, f"margin {r.margin_usd} outside expected range"
        assert r.risk_pct == 0.0025
        assert r.leverage == 3.0
    finally:
        teardown_safety_db(p)


def test_safety_max_concurrent_1():
    p = setup_safety_db()
    try:
        from common.live_safety import LiveSafetyController
        os.environ.pop("PM_FORCE_KILL_ALL", None)
        ctl = LiveSafetyController(p)
        sig = {"ref_price": 100.0, "sl_px": 98.0}
        open_pos = [{"coin": "BTC", "margin": 10}]
        r = ctl.check(sig, 491.24, open_pos)
        assert not r.allow
        assert r.reason.startswith("max_concurrent")
    finally:
        teardown_safety_db(p)


def test_safety_consec_losses_circuit_breaker():
    p = setup_safety_db()
    try:
        from common.live_safety import LiveSafetyController
        os.environ.pop("PM_FORCE_KILL_ALL", None)
        ctl = LiveSafetyController(p)
        # Record 3 consecutive losses
        for i in range(3):
            ctl.record_close(-5.0, 491.24 - (i+1)*5)
            time.sleep(0.01)
        sig = {"ref_price": 100.0, "sl_px": 98.0}
        r = ctl.check(sig, 491.24, [])
        assert not r.allow
        assert "cb_consec" in r.reason or "daily_halt" in r.reason
    finally:
        teardown_safety_db(p)


def test_safety_win_resets_consec_count():
    p = setup_safety_db()
    try:
        from common.live_safety import LiveSafetyController
        os.environ.pop("PM_FORCE_KILL_ALL", None)
        ctl = LiveSafetyController(p)
        ctl.record_close(-5.0, 486.0)
        time.sleep(0.01)
        ctl.record_close(-5.0, 481.0)
        time.sleep(0.01)
        ctl.record_close(+10.0, 491.0)   # WIN — resets counter
        time.sleep(0.01)
        ctl.record_close(-5.0, 486.0)
        # only 1 consec loss now (after the win)
        assert ctl.consecutive_losses(10) == 1
    finally:
        teardown_safety_db(p)


def test_safety_daily_halt_after_breaker():
    p = setup_safety_db()
    try:
        from common.live_safety import LiveSafetyController
        os.environ.pop("PM_FORCE_KILL_ALL", None)
        ctl = LiveSafetyController(p)
        # Trigger consec-loss breaker
        for _ in range(3):
            ctl.record_close(-5.0, 491.24)
        sig = {"ref_price": 100.0, "sl_px": 98.0}
        ctl.check(sig, 491.24, [])   # triggers halt
        halted, reason = ctl.is_daily_halted()
        assert halted
        assert "consec" in reason
    finally:
        teardown_safety_db(p)


# ─────────────────────── ICT Confluence integration ───────────────────────
def test_ict_in_engine_registry():
    from pm.pretrade import ENGINE_REGISTRY
    assert "ict_confluence_4h" in ENGINE_REGISTRY
    assert "ict_confluence_1d" in ENGINE_REGISTRY
    # ICT cap_frac is 0 because live_safety controls sizing
    assert ENGINE_REGISTRY["ict_confluence_4h"]["cap_frac"] == 0.00


def test_ict_engine_classes_load():
    from strategy_runner.strategies.ict_confluence import (
        ICT_Confluence_4h, ICT_Confluence_1d,
        find_swings, detect_bos, find_ob, find_fvg, find_wick_sweep, zones_align,
    )
    # Class attributes
    assert ICT_Confluence_4h.NAME == "ict_confluence_4h"
    assert ICT_Confluence_4h.TF == "4h"
    assert ICT_Confluence_4h.RISK_PCT == 0.005   # backtest-spec stays at 0.5%
    assert ICT_Confluence_1d.NAME == "ict_confluence_1d"
    assert ICT_Confluence_1d.TF == "1d"


def test_ict_pretrade_routes_through_live_safety():
    """ICT signals must go through live_safety gate."""
    p = setup_safety_db()
    try:
        from pm.pretrade import check
        os.environ.pop("PM_FORCE_KILL_ALL", None)
        os.environ["STRATEGY_ICT_CONFLUENCE_4H_ENABLED"] = "1"
        signal = {
            "coin": "BTC", "side": "B", "is_long": True,
            "ref_price": 100.0, "sl_px": 98.0, "tp_px": 105.0,
        }
        regime = {"regime": "trend_up", "confidence": 0.8}
        # Empty positions
        result = check(None, "ict_confluence_4h", signal, regime, 491.24, [])
        assert result.allow, f"expected allow, got {result.reason}"
        # Live safety should have produced smaller margin than 5% (0.25% × 3x w/2% SL ≈ $14)
        assert result.size_usd < 25
    finally:
        teardown_safety_db(p)


def test_ict_pretrade_blocks_when_max_concurrent_1():
    p = setup_safety_db()
    try:
        from pm.pretrade import check
        os.environ.pop("PM_FORCE_KILL_ALL", None)
        os.environ["STRATEGY_ICT_CONFLUENCE_4H_ENABLED"] = "1"
        signal = {
            "coin": "BTC", "side": "B", "is_long": True,
            "ref_price": 100.0, "sl_px": 98.0, "tp_px": 105.0,
        }
        regime = {"regime": "trend_up", "confidence": 0.8}
        # One open position via different coin
        open_pos = [{"coin": "ETH", "strategy": "ict_confluence_4h", "margin": 10}]
        result = check(None, "ict_confluence_4h", signal, regime, 491.24, open_pos)
        assert not result.allow
        assert "live_safety:max_concurrent" in result.reason
    finally:
        teardown_safety_db(p)


def test_ict_pretrade_kill_switch_blocks():
    p = setup_safety_db()
    try:
        from pm.pretrade import check
        os.environ["PM_FORCE_KILL_ALL"] = "1"
        os.environ["STRATEGY_ICT_CONFLUENCE_4H_ENABLED"] = "1"
        signal = {"coin": "BTC", "side": "B", "is_long": True,
                  "ref_price": 100.0, "sl_px": 98.0, "tp_px": 105.0}
        regime = {"regime": "trend_up", "confidence": 0.8}
        result = check(None, "ict_confluence_4h", signal, regime, 491.24, [])
        assert not result.allow
        assert "kill_switch_active" in result.reason
        del os.environ["PM_FORCE_KILL_ALL"]
    finally:
        teardown_safety_db(p)


def test_non_ict_engines_unaffected_by_live_safety():
    """OOS engines still use flat 5%/5x — only ICT routes through live_safety."""
    p = setup_safety_db()
    try:
        from pm.pretrade import check
        os.environ.pop("PM_FORCE_KILL_ALL", None)
        os.environ["STRATEGY_E07_ZFADE2S_TU_1D_ENABLED"] = "1"
        os.environ.pop("MAX_OPEN_POSITIONS", None)
        signal = {"coin": "BTC", "side": "B", "is_long": True,
                  "ref_price": 100.0, "sl_px": 90.0, "tp_px": 105.0}
        regime = {"regime": "trend_up", "confidence": 0.8}
        result = check(None, "e07_zfade2s_tu_1d", signal, regime, 491.24, [])
        # Non-ICT: should pass with flat 5% margin
        assert result.allow, f"expected allow, got {result.reason}"
        # Margin should be ~ $24 (5% of $491)
        assert 20 < result.size_usd < 30
    finally:
        teardown_safety_db(p)
