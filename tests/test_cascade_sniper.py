"""Tests for cascade sniper bot."""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock


class MockBus:
    """Simulates signal-bus client for tests."""
    def __init__(self):
        self.liq_data: dict = {}        # coin -> list of liq events
        self.markprice_data: dict = {}  # coin -> {hl_mid, binance_mid}
        self.candles_data: dict = {}    # (coin, tf) -> list of bars

    def liq(self, since_ms=None, coin=None):
        return list(self.liq_data.get(coin, []))

    def markprice(self, coin):
        return self.markprice_data.get(coin, {})

    def candles(self, coin, tf, n=200):
        return list(self.candles_data.get((coin, tf), []))


def make_liq(side, usd, ts_ms=None):
    return {
        "side": side,
        "usd": usd,
        "ts": ts_ms or int(time.time() * 1000),
    }


def setup_bus_with_trend(coin="BTC", price=100_000.0, trend="up"):
    """Build a bus with a coin in a trending regime."""
    bus = MockBus()
    bus.markprice_data[coin] = {"hl_mid": price, "binance_mid": price * 1.0001}
    # 14 bars at 1h with SMA below current price → trend_up
    if trend == "up":
        bars = [{"open": price * 0.95, "high": price * 0.96, "low": price * 0.94,
                 "close": price * 0.95, "volume": 1000, "open_ts": 0} for _ in range(14)]
    elif trend == "down":
        bars = [{"open": price * 1.05, "high": price * 1.06, "low": price * 1.04,
                 "close": price * 1.05, "volume": 1000, "open_ts": 0} for _ in range(14)]
    else:  # range
        bars = [{"open": price, "high": price * 1.005, "low": price * 0.995,
                 "close": price, "volume": 1000, "open_ts": 0} for _ in range(14)]
    bus.candles_data[(coin, "1h")] = bars
    return bus


def test_no_liqs_returns_none():
    from strategy_runner.strategies.cascade_sniper import CascadeSniperHL, reset_cooldowns
    reset_cooldowns()
    bus = MockBus()
    bus.markprice_data["BTC"] = {"hl_mid": 100_000.0}
    sig = CascadeSniperHL.evaluate("BTC", bus)
    assert sig is None


def test_below_threshold_returns_none():
    from strategy_runner.strategies.cascade_sniper import CascadeSniperHL, reset_cooldowns
    reset_cooldowns()
    bus = setup_bus_with_trend("BTC", trend="up")
    # Total $100k of long liqs — below $500k threshold
    bus.liq_data["BTC"] = [make_liq("SELL", 100_000)]
    sig = CascadeSniperHL.evaluate("BTC", bus)
    assert sig is None


def test_long_cascade_in_uptrend_rides_short():
    """Longs getting liq'd in an uptrend → ride means SHORT (more sell pressure)."""
    from strategy_runner.strategies.cascade_sniper import CascadeSniperHL, reset_cooldowns
    reset_cooldowns()
    bus = setup_bus_with_trend("BTC", price=100_000.0, trend="up")
    # $600k of long liqs (SELL pressure)
    bus.liq_data["BTC"] = [make_liq("SELL", 300_000), make_liq("SELL", 350_000)]
    sig = CascadeSniperHL.evaluate("BTC", bus)
    assert sig is not None
    # Ride: dominant=long → SHORT
    assert not sig.is_long, "Expected SHORT in trend+long_liq cascade"
    assert sig.side == "A"
    # Verify SL/TP are RIDE config (0.4% SL, 0.8% TP)
    assert abs(sig.sl_px - 100_000 * 1.004) < 1
    assert abs(sig.tp_px - 100_000 * 0.992) < 1
    assert "ride" in sig.fire_reason
    assert "trend" in sig.fire_reason


def test_short_cascade_in_downtrend_rides_long():
    """Shorts getting liq'd in downtrend → ride means LONG (more buy pressure)."""
    from strategy_runner.strategies.cascade_sniper import CascadeSniperHL, reset_cooldowns
    reset_cooldowns()
    bus = setup_bus_with_trend("ETH", price=3000.0, trend="down")
    bus.liq_data["ETH"] = [make_liq("BUY", 600_000)]
    sig = CascadeSniperHL.evaluate("ETH", bus)
    assert sig is not None
    assert sig.is_long, "Expected LONG in downtrend+short_liq cascade ride"
    assert "ride" in sig.fire_reason


def test_long_cascade_in_range_fades_long():
    """Longs getting liq'd in range → fade means LONG (buy the dip)."""
    from strategy_runner.strategies.cascade_sniper import CascadeSniperHL, reset_cooldowns
    reset_cooldowns()
    bus = setup_bus_with_trend("SOL", price=200.0, trend="range")
    bus.liq_data["SOL"] = [make_liq("SELL", 700_000)]
    sig = CascadeSniperHL.evaluate("SOL", bus)
    assert sig is not None
    assert sig.is_long, "Expected LONG fade in range+long_liq cascade"
    # FADE config: 0.6% SL, 1.2% TP
    assert abs(sig.sl_px - 200 * 0.994) < 0.01
    assert abs(sig.tp_px - 200 * 1.012) < 0.01
    assert "fade" in sig.fire_reason
    assert "range" in sig.fire_reason


def test_anti_spam_cooldown():
    """Two cascades on same coin within cooldown → only first fires."""
    from strategy_runner.strategies.cascade_sniper import CascadeSniperHL, reset_cooldowns
    reset_cooldowns()
    bus = setup_bus_with_trend("BTC", trend="up")
    bus.liq_data["BTC"] = [make_liq("SELL", 600_000)]
    sig1 = CascadeSniperHL.evaluate("BTC", bus)
    assert sig1 is not None
    # Immediate re-fire attempt
    sig2 = CascadeSniperHL.evaluate("BTC", bus)
    assert sig2 is None, "Cooldown should suppress immediate re-fire"


def test_cooldown_does_not_block_other_coins():
    from strategy_runner.strategies.cascade_sniper import CascadeSniperHL, reset_cooldowns
    reset_cooldowns()
    bus = setup_bus_with_trend("BTC", trend="up")
    bus.markprice_data["ETH"] = {"hl_mid": 3000.0}
    bus.candles_data[("ETH", "1h")] = bus.candles_data[("BTC", "1h")]
    bus.liq_data["BTC"] = [make_liq("SELL", 600_000)]
    bus.liq_data["ETH"] = [make_liq("BUY", 700_000)]
    sig_btc = CascadeSniperHL.evaluate("BTC", bus)
    sig_eth = CascadeSniperHL.evaluate("ETH", bus)
    assert sig_btc is not None
    assert sig_eth is not None, "ETH cascade should fire independently of BTC cooldown"


def test_env_override_threshold():
    """LIQ_USD_MIN can be tuned via CASC_LIQ_USD_MIN env."""
    from strategy_runner.strategies.cascade_sniper import CascadeSniperHL, reset_cooldowns
    reset_cooldowns()
    os.environ["CASC_LIQ_USD_MIN"] = "200000"
    bus = setup_bus_with_trend("BTC", trend="up")
    bus.liq_data["BTC"] = [make_liq("SELL", 300_000)]   # below default 500k, above 200k
    sig = CascadeSniperHL.evaluate("BTC", bus)
    assert sig is not None
    del os.environ["CASC_LIQ_USD_MIN"]


def test_strategy_metadata():
    from strategy_runner.strategies.cascade_sniper import CascadeSniperHL
    assert CascadeSniperHL.NAME == "cascade_sniper_hl"
    assert CascadeSniperHL.CLOID_PREFIX == "casc_"
    assert "BTC" in CascadeSniperHL.UNIVERSE
    assert len(CascadeSniperHL.UNIVERSE) >= 20
