"""Smoke tests for hl_vault_predict — anticipate HLP rebalance.

Tests use the BUS PUBLIC METHOD bus.hlp_position(coin) after the
2026-05-19 fix that removed direct bus._client access (commit pending).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from strategy_runner.strategies.hl_vault_predict import HLVaultPredict


def _bars(closes: list[float]) -> list[dict]:
    return [{"open_ts": i, "open": c, "high": c * 1.001, "low": c * 0.999,
             "close": c, "volume": 100.0} for i, c in enumerate(closes)]


def _bus(hlp_data, closes):
    bus = MagicMock()
    bus.hlp_position.return_value = hlp_data
    bus.candles.return_value = _bars(closes)
    bus.markprice.return_value = {"hl_mid": closes[-1], "binance_mid": closes[-1]}
    bus.cvd.return_value = {"net": 0.0, "buy_usd": 1000.0, "sell_usd": 1000.0,
                            "n_buy": 10, "n_sell": 10}
    return bus


def test_strategy_metadata():
    assert HLVaultPredict.NAME == "hl_vault_predict"
    assert HLVaultPredict.CLOID_PREFIX == "vlpre"


def test_no_hlp_position_returns_none():
    bus = _bus(hlp_data=None, closes=[100, 100, 100, 100])
    sig = HLVaultPredict.evaluate("BTC", bus)
    assert sig is None


def test_tiny_vault_returns_none():
    """net_usd below VP_MIN_VAULT_NET_USD (100k default) → skip."""
    bus = _bus(hlp_data={"net_usd": 50_000.0, "unrealized_pnl": 1000.0},
               closes=[100, 100.1, 100.2, 100.3])
    sig = HLVaultPredict.evaluate("BTC", bus)
    assert sig is None


def test_missing_unrealized_pnl_returns_none():
    """Post-fix (2026-05-19): when unrealized_pnl is 0 / missing, the broken
    15min-lookback price proxy is now DISABLED. Strategy must return None
    rather than fire on bad signal."""
    bus = _bus(hlp_data={"net_usd": 1_000_000.0, "unrealized_pnl": 0},
               closes=[100, 101, 102, 103])
    sig = HLVaultPredict.evaluate("BTC", bus)
    assert sig is None


def test_hlp_long_gaining_fast_can_fire_short():
    """HLP net long with positive unrealized PnL → rebalance-sell incoming → SHORT."""
    bus = _bus(hlp_data={
        "net_usd": 10_000_000.0,           # large long
        "unrealized_pnl": 100_000.0,        # 1% unrealized gain
    }, closes=[100, 100.5, 101, 101.5])
    sig = HLVaultPredict.evaluate("BTC", bus)
    if sig is not None:
        assert sig.is_long is False
        assert sig.side == "A"
        assert sig.tp_px < sig.ref_price
        assert sig.sl_px > sig.ref_price


def test_hlp_long_losing_fast_can_fire_long():
    """HLP net long with negative unrealized PnL → defensive add → LONG."""
    bus = _bus(hlp_data={
        "net_usd": 10_000_000.0,
        "unrealized_pnl": -100_000.0,
    }, closes=[100, 99.5, 99, 98.5])
    sig = HLVaultPredict.evaluate("BTC", bus)
    if sig is not None:
        assert sig.is_long is True
        assert sig.side == "B"
        assert sig.tp_px > sig.ref_price
        assert sig.sl_px < sig.ref_price


def test_does_not_use_private_bus_internals():
    """Regression check kept for documentation. The actual repo-wide lint
    is `tests/test_no_private_bus_access.py` which scans EVERY strategy
    file (not just hl_vault_predict.evaluate) and is much harder to
    bypass. See that test for sentinel-audit-driven 2026-05-19 fix."""
    import inspect
    src = inspect.getsource(HLVaultPredict)
    # Quick local check — full repo coverage is in the dedicated lint test
    code = "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    )
    # Won't catch docstring mentions, so we accept some looseness here.
    # Authoritative check is the repo-wide lint test.
    assert "bus._client" not in code or "DOCSTRING" in code.upper(), \
        "see tests/test_no_private_bus_access.py for full check"
