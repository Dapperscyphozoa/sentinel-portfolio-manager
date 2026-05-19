"""Coin-lock invariants across the unified engine registry.

Rewritten 2026-05-19: the old shape of this file (13/20 engine counts,
CUT_ENGINES set, fsp/range_fade KEPT lists) was obsolete after the
2026-05-18/19 cuts. Surviving assertions exercise the OOS_ENGINE_REGISTRY
backward-compat alias and the 1_GLOBAL coin lock that supersedes the
former concentration gate.
"""
from __future__ import annotations


def test_oos_alias_still_works():
    """OOS_ENGINE_REGISTRY must remain an alias to ENGINE_REGISTRY."""
    from pm.pretrade import OOS_ENGINE_REGISTRY, ENGINE_REGISTRY
    assert OOS_ENGINE_REGISTRY is ENGINE_REGISTRY


def test_coin_lock_blocks_second_engine_on_same_coin():
    """1_GLOBAL coin lock: an OOS engine holding BTC blocks any other engine
    from also opening BTC, regardless of which engine fires first."""
    from pm.pretrade import check
    open_pos = [{"coin": "BTC", "strategy": "e08_dip3d10_td_1d",
                 "notional": 100, "margin": 20}]
    signal = {"coin": "BTC", "side": "B"}
    regime = {"regime": "trend_down", "confidence": 0.8}
    result = check(None, "stop_hunt", signal, regime, 491.24, open_pos)
    assert not result.allow
    assert result.reason == "coin_locked"


def test_coin_lock_blocks_in_either_direction():
    """Symmetric: engine A holding ETH must block engine B regardless of
    which side of the registry they live on."""
    from pm.pretrade import check
    open_pos = [{"coin": "ETH", "strategy": "vpoc_retest",
                 "notional": 100, "margin": 20}]
    signal = {"coin": "ETH", "side": "B"}
    regime = {"regime": "trend_up", "confidence": 0.8}
    result = check(None, "e07_zfade2s_tu_1d", signal, regime, 491.24, open_pos)
    assert not result.allow
    assert result.reason == "coin_locked"
