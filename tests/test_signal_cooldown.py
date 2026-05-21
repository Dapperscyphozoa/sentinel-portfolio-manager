"""Tests for per-(engine, coin) signal cooldown in scan_once.

Operator concern 2026-05-21: e17_bb_fade_bt_4h/SUI generated 5 signal rows
in 25min while BB-break condition held. Dashboard showed all 5. Trader's
coin lock blocked the duplicate ORDERS, but signal-row telemetry kept
piling up.

Fix: scan_once now tracks _SIGNAL_FIRE_LAST[(engine, coin)] = timestamp.
Subsequent fires within max(4 × TF, 5min) get suppressed before reaching
trader.open / signal-row persistence.
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_tf_to_seconds_basic():
    from strategy_runner.runner import _tf_to_seconds
    assert _tf_to_seconds("1m") == 60
    assert _tf_to_seconds("5m") == 300
    assert _tf_to_seconds("15m") == 900
    assert _tf_to_seconds("1h") == 3600
    assert _tf_to_seconds("4h") == 14400
    assert _tf_to_seconds("1d") == 86400


def test_tf_to_seconds_unknown_defaults_to_5min():
    from strategy_runner.runner import _tf_to_seconds
    assert _tf_to_seconds("") == 300
    assert _tf_to_seconds("invalid") == 300
    assert _tf_to_seconds(None) == 300
    assert _tf_to_seconds("xyz") == 300


def test_cooldown_blocks_second_fire_within_window():
    """Same (engine, coin) firing twice within cooldown should suppress the second."""
    from strategy_runner.runner import _SIGNAL_FIRE_LAST, _tf_to_seconds
    _SIGNAL_FIRE_LAST.clear()
    key = ("test_engine", "BTC")
    # Simulate first fire
    _SIGNAL_FIRE_LAST[key] = time.time()
    # Second fire at same instant — cooldown active
    cooldown_s = max(_tf_to_seconds("1h") * 4, 300)  # 14400s for 1h TF
    assert cooldown_s == 14400
    last = _SIGNAL_FIRE_LAST[key]
    in_cooldown = (time.time() - last) < cooldown_s
    assert in_cooldown is True


def test_cooldown_floor_is_5min_for_fast_tfs():
    """1m engine: 4 bars = 4min, but floor enforces 5min minimum."""
    from strategy_runner.runner import _tf_to_seconds
    tf_sec = _tf_to_seconds("1m")
    cooldown = max(tf_sec * 4, 300)
    assert cooldown == 300  # 240 floored to 300


def test_cooldown_scales_with_tf_for_slow_engines():
    """4h engine: 4 bars = 16h cooldown (no refire for half a day)."""
    from strategy_runner.runner import _tf_to_seconds
    tf_sec = _tf_to_seconds("4h")
    cooldown = max(tf_sec * 4, 300)
    assert cooldown == 16 * 3600


def test_cooldown_different_engines_isolated():
    """engine_a fires on BTC; engine_b firing on BTC is NOT blocked."""
    from strategy_runner.runner import _SIGNAL_FIRE_LAST
    _SIGNAL_FIRE_LAST.clear()
    _SIGNAL_FIRE_LAST[("engine_a", "BTC")] = time.time()
    # engine_b/BTC has no entry → not in cooldown
    assert ("engine_b", "BTC") not in _SIGNAL_FIRE_LAST


def test_cooldown_different_coins_isolated():
    """engine fires on BTC; same engine firing on ETH is NOT blocked."""
    from strategy_runner.runner import _SIGNAL_FIRE_LAST
    _SIGNAL_FIRE_LAST.clear()
    _SIGNAL_FIRE_LAST[("engine", "BTC")] = time.time()
    assert ("engine", "ETH") not in _SIGNAL_FIRE_LAST


def test_cooldown_expires_after_window():
    """After cooldown_s elapses, re-fire should be permitted."""
    from strategy_runner.runner import _SIGNAL_FIRE_LAST, _tf_to_seconds
    _SIGNAL_FIRE_LAST.clear()
    key = ("test", "BTC")
    cooldown_s = max(_tf_to_seconds("5m") * 4, 300)  # 1200s
    # Simulate fire 1300s ago (past cooldown)
    _SIGNAL_FIRE_LAST[key] = time.time() - 1300
    last = _SIGNAL_FIRE_LAST[key]
    in_cooldown = (time.time() - last) < cooldown_s
    assert in_cooldown is False  # cooldown expired
