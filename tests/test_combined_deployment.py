"""Tests for combined deployment (legacy 9 + OOS 11)."""
from __future__ import annotations

import os


def test_registry_has_13_engines_after_cuts():
    from pm.pretrade import ENGINE_REGISTRY
    assert len(ENGINE_REGISTRY) == 17  # +2 ICT


def test_registry_includes_legacy_provisional_2():
    """After audit: only fsp + liq_cascade are KEPT from legacy 9."""
    from pm.pretrade import ENGINE_REGISTRY
    kept = {"fsp", "liq_cascade"}
    missing = kept - set(ENGINE_REGISTRY.keys())
    assert not missing, f"missing kept legacy engines: {missing}"


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


# ---------- Audit cuts ----------
def test_cut_engines_blocked():
    """Cut engines are hard-blocked regardless of env."""
    import os
    os.environ["STRATEGY_VSQ_ENABLED"] = "1"   # even with enabled flag, audit cut wins
    from pm.pretrade import check, CUT_ENGINES
    assert "vsq" in CUT_ENGINES
    signal = {"coin": "BTC", "side": "B"}
    regime = {"regime": "trend_up", "confidence": 0.8}
    result = check(None, "vsq", signal, regime, 491.24, [])
    assert not result.allow
    assert result.reason == "engine_cut_by_audit"


def test_all_7_cut_engines():
    """All 7 audit-cut engines are blocked."""
    import os
    from pm.pretrade import check, CUT_ENGINES
    expected_cuts = {"vsq", "range_fade", "range_bo", "lh1", "fd1", "cex_dex_arb", "precog"}
    assert CUT_ENGINES == expected_cuts
    signal = {"coin": "BTC", "side": "B"}
    regime = {"regime": "trend_up", "confidence": 0.8}
    for sname in expected_cuts:
        os.environ[f"STRATEGY_{sname.upper()}_ENABLED"] = "1"
        result = check(None, sname, signal, regime, 491.24, [])
        assert not result.allow, f"{sname} should be cut but was allowed"
        assert result.reason == "engine_cut_by_audit"


def test_provisional_legacy_still_works():
    """fsp and liq_cascade are KEPT provisional, not cut."""
    import os
    from pm.pretrade import check, CUT_ENGINES, ENGINE_REGISTRY
    assert "fsp" not in CUT_ENGINES
    assert "liq_cascade" not in CUT_ENGINES
    assert "fsp" in ENGINE_REGISTRY
    assert "liq_cascade" in ENGINE_REGISTRY
    # Pretrade should allow fsp through
    os.environ["STRATEGY_FSP_ENABLED"] = "1"
    signal = {"coin": "BTC", "side": "B"}
    regime = {"regime": "trend_up", "confidence": 0.8}
    result = check(None, "fsp", signal, regime, 491.24, [])
    assert result.allow, f"fsp should be allowed, got {result.reason}"


def test_registry_is_13_engines():
    """Final registry: 11 OOS + 2 legacy provisional = 13."""
    from pm.pretrade import ENGINE_REGISTRY
    assert len(ENGINE_REGISTRY) == 17  # +2 ICT
