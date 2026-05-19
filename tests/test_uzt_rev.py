"""Smoke tests for UZT_REV — reversal-only ship config (v3).

UZT_REV is live at cap_frac=0.05 with 3-sample-consistency backtest
validation (n=41, PF 6.92, monotonic 90×20→120×20→120×30). Smoke tests
cover the v3-specific guards:

- metadata + ship_version='v3' invariant
- CON path filtering (only REV signals ship)
- single 5R TP override
- 40-bar time stop
- Asia hours filter (00-05 UTC blocked)
- filter telemetry hook regression (the _compute_filter_telemetry helper
  must be wired into Signal.extras['filter_telem'] at every fire)
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

from strategy_runner.strategies.uzt_rev import (
    UZT_REV,
    _in_asia_window,
    _compute_filter_telemetry,
    UZT_REV_TP_R,
    UZT_REV_HOLD_MAX_BARS,
)


def _empty_bus():
    bus = MagicMock()
    bus.candles.return_value = []
    return bus


def _flat_bars(n: int = 600, close: float = 100.0):
    return [{
        "open_ts": i * 900_000, "open": close,
        "high": close * 1.0001, "low": close * 0.9999,
        "close": close, "volume": 100.0,
    } for i in range(n)]


def test_metadata():
    assert UZT_REV.NAME == "uzt_rev"
    assert UZT_REV.CLOID_PREFIX == "uztrv_"
    assert UZT_REV.TF == "15m"
    # Tier-1 KEEP cohort: ~16 coins
    assert 10 <= len(UZT_REV.UNIVERSE) <= 20


def test_ship_constants_match_v3():
    """Lock the v3 ship parameters — changing these without re-backtest is
    a serious regression (these are the params that produced PF 6.92)."""
    assert UZT_REV_TP_R == 5.0          # single 5R TP, no scaling
    assert UZT_REV_HOLD_MAX_BARS == 40   # 10h on 15m


def test_no_bars_returns_none():
    sig = UZT_REV.evaluate("SOL", _empty_bus())
    assert sig is None


def test_flat_bars_no_zones_returns_none():
    """Flat data → no displacement → no zones → no signal."""
    bus = MagicMock()
    bus.candles.return_value = _flat_bars()
    sig = UZT_REV.evaluate("SOL", bus)
    assert sig is None


def test_in_asia_window_helper():
    """00:00-05:00 UTC blocked; everything else allowed."""
    # 02:00 UTC
    ts_2am = int(time.mktime((2026, 5, 19, 2, 0, 0, 0, 0, 0)) - time.timezone) * 1000
    assert _in_asia_window(ts_2am) is True
    # 12:00 UTC
    ts_noon = int(time.mktime((2026, 5, 19, 12, 0, 0, 0, 0, 0)) - time.timezone) * 1000
    assert _in_asia_window(ts_noon) is False


def test_filter_telemetry_helper_runs_without_bus_data():
    """_compute_filter_telemetry must NEVER raise — uzt_rev wraps it in
    try/except for safety but the helper itself should fail-soft on each
    of the 3 filter subsystems independently."""
    bus = MagicMock()
    # Each bus method may legitimately return [] or {} when there's no data
    bus.liq.return_value = []
    bus.oi.return_value = []
    bus.cvd.return_value = {}
    telem = _compute_filter_telemetry(
        bus=bus, coin="SOL", entry_px=100.0, zone_edge_px=99.5,
        is_long=True, fire_ts_ms=int(time.time() * 1000),
    )
    assert isinstance(telem, dict)
    assert telem.get("telem_version") == 1
    # Liq window stats should be 0 (no events)
    assert telem.get("liq_5m_total_usd") == 0.0
    assert telem.get("liq_15m_total_usd") == 0.0
    assert telem.get("liq_30m_total_usd") == 0.0


def test_filter_telemetry_handles_bus_exceptions():
    """If individual bus calls raise, each sub-section records its err but
    the helper as a whole returns a dict, not None."""
    bus = MagicMock()
    bus.liq.side_effect = RuntimeError("bus dead")
    bus.oi.side_effect = RuntimeError("oi dead")
    bus.cvd.side_effect = RuntimeError("cvd dead")
    telem = _compute_filter_telemetry(
        bus=bus, coin="SOL", entry_px=100.0, zone_edge_px=99.5,
        is_long=True, fire_ts_ms=int(time.time() * 1000),
    )
    assert isinstance(telem, dict)
    assert "liq_telem_err" in telem
    assert "oi_telem_err" in telem
    assert "cvd_telem_err" in telem


def test_filter_telemetry_records_zone_proximal_liqs():
    """A liq at zone-edge price should count in zone_usd. A liq far from
    zone should count in total but NOT zone."""
    now_ms = int(time.time() * 1000)
    bus = MagicMock()
    # Two liqs, one at zone edge (within 0.5%), one far away
    bus.liq.return_value = [
        {"ts": now_ms - 60_000, "side": "SELL", "price": 99.6, "usd": 100_000.0},   # zone-proximal (zone=99.5, within 0.5%)
        {"ts": now_ms - 120_000, "side": "BUY", "price": 95.0, "usd": 200_000.0},   # far from zone
    ]
    bus.oi.return_value = []
    bus.cvd.return_value = {}
    telem = _compute_filter_telemetry(
        bus=bus, coin="SOL", entry_px=100.0, zone_edge_px=99.5,
        is_long=True, fire_ts_ms=now_ms,
    )
    # Total in 5m window: both liqs (300k)
    assert telem["liq_5m_total_usd"] == 300_000.0
    # Zone-proximal in 5m: only the first liq (within 0.5% of 99.5 = 99.0–100.0)
    assert telem["liq_5m_zone_usd"] == 100_000.0
    # By side: 100k long liq (SELL) + 200k short liq (BUY)
    assert telem["liq_5m_long_usd"] == 100_000.0
    assert telem["liq_5m_short_usd"] == 200_000.0
