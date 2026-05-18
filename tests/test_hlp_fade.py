"""Unit tests for hlp_fade strategy."""
from collections import deque

import pytest

from signal_bus.hlp_poller import compute_zscore
from strategy_runner.strategies.hlp_fade import HLPFade


# ---------- compute_zscore tests ----------

def test_zscore_insufficient_history_returns_none():
    # Less than 100 samples → None
    hist = deque([(i, 1000.0 + i) for i in range(50)])
    assert compute_zscore(hist, 1500.0) is None


def test_zscore_zero_variance_returns_none():
    hist = deque([(i, 1000.0) for i in range(200)])
    assert compute_zscore(hist, 1000.0) is None


def test_zscore_positive_extreme():
    # 200 samples around 1000, current value 1500 → z >> 0
    import random
    random.seed(42)
    hist = deque([(i, 1000.0 + random.gauss(0, 10)) for i in range(200)])
    z = compute_zscore(hist, 1500.0)
    assert z is not None
    assert z > 30   # 500-unit deviation on 10-stddev = z>30


def test_zscore_negative_extreme():
    import random
    random.seed(42)
    hist = deque([(i, 1000.0 + random.gauss(0, 10)) for i in range(200)])
    z = compute_zscore(hist, 500.0)
    assert z is not None
    assert z < -30


def test_zscore_at_mean_near_zero():
    import random
    random.seed(42)
    hist = deque([(i, 1000.0 + random.gauss(0, 10)) for i in range(200)])
    z = compute_zscore(hist, 1000.0)
    assert z is not None
    assert abs(z) < 1.0


# ---------- HLPFade.evaluate tests ----------

class FakeBus:
    """Minimal bus stub for strategy unit tests."""
    def __init__(self, hlp_position_data=None, markprice_data=None):
        self._hlp = hlp_position_data
        self._mp = markprice_data or {"hl_mid": 100.0, "binance_mid": 100.0}

    def hlp_position(self, coin):
        return self._hlp

    def markprice(self, coin):
        return self._mp


def test_evaluate_returns_none_when_no_hlp_position():
    sig = HLPFade.evaluate("BTC", FakeBus(hlp_position_data=None))
    assert sig is None


def test_evaluate_returns_none_when_below_min_usd():
    hlp = {"net_size": 100, "net_usd": 10_000, "vault_count": 2,
           "zscore_7d": 3.0, "history_n": 300, "ts": 0}
    sig = HLPFade.evaluate("BTC", FakeBus(hlp_position_data=hlp))
    assert sig is None  # net_usd 10k below 50k minimum


def test_evaluate_returns_none_when_single_vault():
    hlp = {"net_size": 1000, "net_usd": 100_000, "vault_count": 1,
           "zscore_7d": 3.0, "history_n": 300, "ts": 0}
    sig = HLPFade.evaluate("BTC", FakeBus(hlp_position_data=hlp))
    assert sig is None  # vault_count below 2 minimum


def test_evaluate_returns_none_when_insufficient_history():
    hlp = {"net_size": 1000, "net_usd": 100_000, "vault_count": 2,
           "zscore_7d": 3.0, "history_n": 100, "ts": 0}  # below 200 min
    sig = HLPFade.evaluate("BTC", FakeBus(hlp_position_data=hlp))
    assert sig is None


def test_evaluate_returns_none_when_z_below_threshold():
    hlp = {"net_size": 1000, "net_usd": 100_000, "vault_count": 2,
           "zscore_7d": 1.5, "history_n": 300, "ts": 0}
    sig = HLPFade.evaluate("BTC", FakeBus(hlp_position_data=hlp))
    assert sig is None


def test_evaluate_long_signal_when_z_above_2():
    hlp = {"net_size": 1000, "net_usd": 100_000, "vault_count": 2,
           "zscore_7d": 2.5, "history_n": 300, "ts": 0}
    sig = HLPFade.evaluate("BTC", FakeBus(hlp_position_data=hlp))
    assert sig is not None
    assert sig.is_long is True
    assert sig.side == "B"
    assert sig.sl_px < sig.ref_price < sig.tp_px
    assert sig.extras["hlp_z"] == 2.5
    assert sig.extras["hlp_vault_count"] == 2


def test_evaluate_short_signal_when_z_below_neg2():
    hlp = {"net_size": -1000, "net_usd": -100_000, "vault_count": 2,
           "zscore_7d": -2.7, "history_n": 300, "ts": 0}
    sig = HLPFade.evaluate("BTC", FakeBus(hlp_position_data=hlp))
    assert sig is not None
    assert sig.is_long is False
    assert sig.side == "A"
    assert sig.sl_px > sig.ref_price > sig.tp_px


def test_evaluate_returns_none_when_markprice_unavailable():
    hlp = {"net_size": 1000, "net_usd": 100_000, "vault_count": 2,
           "zscore_7d": 2.5, "history_n": 300, "ts": 0}
    sig = HLPFade.evaluate("BTC", FakeBus(hlp_position_data=hlp,
                                          markprice_data={"hl_mid": 0, "binance_mid": 0}))
    assert sig is None


# ---------- should_close tests ----------

def test_should_close_when_z_near_neutral():
    hlp = {"zscore_7d": 0.3}
    row = {"coin": "BTC", "extras_json": '{"hlp_z": 2.5}'}
    should_close, reason = HLPFade.should_close(row, FakeBus(hlp_position_data=hlp))
    assert should_close is True
    assert "neutral" in reason


def test_should_close_when_z_flipped():
    hlp = {"zscore_7d": -1.5}    # was +2.5 originally
    row = {"coin": "BTC", "extras_json": '{"hlp_z": 2.5}'}
    should_close, reason = HLPFade.should_close(row, FakeBus(hlp_position_data=hlp))
    assert should_close is True
    assert "flipped" in reason


def test_should_not_close_when_z_still_extreme():
    hlp = {"zscore_7d": 2.8}     # still extreme
    row = {"coin": "BTC", "extras_json": '{"hlp_z": 2.5}'}
    should_close, reason = HLPFade.should_close(row, FakeBus(hlp_position_data=hlp))
    assert should_close is False
