"""Tests for combined deployment (legacy 9 + OOS 11)."""
from __future__ import annotations

import os


def test_registry_has_20_engines():
    from pm.pretrade import ENGINE_REGISTRY
    assert len(ENGINE_REGISTRY) == 20


def test_registry_includes_all_legacy_9():
    from pm.pretrade import ENGINE_REGISTRY
    legacy_9 = {"fsp", "vsq", "range_fade", "range_bo", "lh1", "fd1",
                "precog", "liq_cascade", "cex_dex_arb"}
    missing = legacy_9 - set(ENGINE_REGISTRY.keys())
    assert not missing, f"missing legacy engines: {missing}"


def test_registry_includes_all_oos_11():
    from pm.pretrade import ENGINE_REGISTRY
    oos_11 = {"e01_zfade3s_tu_1d", "e07_zfade2s_tu_1d", "e08_dip3d10_td_1d",
              "e09_pump3d10_td_1d", "e16_bb_fade_hv_1d", "e17_bb_fade_bt_1d",
              "e01_zfade3s_tu_4h", "e07_zfade2s_tu_4h", "e08_dip3d7_td_4h",
              "e16_bb_fade_hv_4h", "e17_bb_fade_bt_4h"}
    missing = oos_11 - set(ENGINE_REGISTRY.keys())
    assert not missing, f"missing OOS engines: {missing}"


def test_combined_registry_cap_fracs_sum_to_1():
    from pm.pretrade import ENGINE_REGISTRY
    total = sum(e["cap_frac"] for e in ENGINE_REGISTRY.values())
    assert abs(total - 1.0) < 0.02, f"cap_fracs sum to {total}"


def test_oos_alias_still_works():
    """Backward compat: OOS_ENGINE_REGISTRY should alias to ENGINE_REGISTRY."""
    from pm.pretrade import OOS_ENGINE_REGISTRY, ENGINE_REGISTRY
    assert OOS_ENGINE_REGISTRY is ENGINE_REGISTRY


def test_legacy_engine_pretrade_allows():
    """Legacy engine fsp should be allowed through PM gate."""
    os.environ["STRATEGY_FSP_ENABLED"] = "1"
    os.environ["COOLDOWN_DB"] = "/tmp/test_combined_cooldown.sqlite"
    if os.path.exists("/tmp/test_combined_cooldown.sqlite"):
        os.unlink("/tmp/test_combined_cooldown.sqlite")
    from pm.pretrade import check
    signal = {"coin": "BTC", "side": "B"}
    regime = {"regime": "trend_up", "confidence": 0.8}
    result = check(None, "fsp", signal, regime, 491.24, [])
    assert result.allow, f"fsp should be allowed, got reason={result.reason}"
    assert result.bt_pf == 2.65   # SPEC.md FSP backtest PF


def test_oos_engine_locks_out_legacy_on_same_coin():
    """If an OOS engine holds BTC, a legacy engine cannot also open BTC."""
    os.environ["STRATEGY_FSP_ENABLED"] = "1"
    from pm.pretrade import check
    open_pos = [{"coin": "BTC", "strategy": "e08_dip3d10_td_1d", "notional": 100, "margin": 20}]
    signal = {"coin": "BTC", "side": "B"}
    regime = {"regime": "trend_down", "confidence": 0.8}
    result = check(None, "fsp", signal, regime, 491.24, open_pos)
    assert not result.allow
    assert result.reason == "coin_locked"


def test_legacy_engine_locks_out_oos_on_same_coin():
    """Reverse: legacy engine holding ETH blocks OOS engine on ETH."""
    from pm.pretrade import check
    open_pos = [{"coin": "ETH", "strategy": "fsp", "notional": 100, "margin": 20}]
    signal = {"coin": "ETH", "side": "B"}
    regime = {"regime": "trend_up", "confidence": 0.8}
    result = check(None, "e07_zfade2s_tu_1d", signal, regime, 491.24, open_pos)
    assert not result.allow
    assert result.reason == "coin_locked"


def test_runner_loads_combined_by_default():
    """Default _load_registered loads both OOS (11) and legacy strategies."""
    # Clear any previous registry state
    from strategy_runner import runner
    runner.REGISTRY.clear()
    runner._load_registered()
    names = {c.NAME for c in runner.REGISTRY}
    # Should include OOS
    oos_present = sum(1 for n in names if n.startswith(("e01_", "e07_", "e08_", "e09_", "e16_", "e17_")))
    assert oos_present >= 11, f"expected 11+ OOS, got {oos_present}: {names}"
    # And at least some legacy (depends on which modules import cleanly)
    legacy_known = {"fsp", "range_fade", "range_bo", "vsq", "lh1", "precog",
                    "liq_cascade", "cex_dex_arb", "donchian"}
    legacy_present = names & legacy_known
    assert legacy_present, f"no legacy strategies loaded: {names}"
    print(f"Total registry: {len(names)} strategies")
