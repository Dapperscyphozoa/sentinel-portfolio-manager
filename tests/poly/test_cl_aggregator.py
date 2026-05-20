"""Unit tests for poly_signal_bus.cl_aggregator."""
from __future__ import annotations

import math
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest

from poly_signal_bus.cl_aggregator import (
    DEFAULT_VENUES,
    aggregate,
    aggregate_with_diagnostics,
    diff_bps,
)


def test_aggregate_returns_none_when_insufficient_venues():
    prices = {"binance": 100.0, "coinbase": 100.5}
    assert aggregate(prices, min_venues=5) is None


def test_aggregate_drops_outliers():
    # 6 venues clustered at 100, 1 outlier at 200; outlier must be trimmed
    prices = {
        "binance": 100.0, "coinbase": 100.1, "kraken": 99.9,
        "bitstamp": 100.05, "bitfinex": 100.02, "okx": 99.95,
        "huobi": 200.0,
    }
    result = aggregate(prices, trim_outlier_bps=50, drop_extremes=False)
    assert result is not None
    assert 99 <= result <= 101


def test_aggregate_diagnostics_identifies_trimmed_venue():
    prices = {
        "binance": 67830, "coinbase": 67831, "kraken": 67829,
        "bitstamp": 67830, "bitfinex": 67830, "okx": 67830,
        "huobi": 68500,   # outlier
    }
    diag = aggregate_with_diagnostics(prices)
    assert "huobi" in diag["trimmed_venues"]
    assert diag["predicted"] is not None
    # the predicted price should be near 67830 once huobi is trimmed
    assert abs(diag["predicted"] - 67830) < 5


def test_diff_bps_signed():
    assert diff_bps(100.5, 100.0) == pytest.approx(50.0)
    assert diff_bps(99.5, 100.0) == pytest.approx(-50.0)


def test_aggregate_handles_zero_prices():
    prices = {"binance": 0, "coinbase": 100.0, "kraken": 100.0,
              "bitstamp": 100.0, "bitfinex": 100.0, "okx": 100.0,
              "huobi": 100.0}
    # zero gets filtered out; 6 valid venues
    result = aggregate(prices)
    assert result is not None
    assert 99 < result < 101


def test_aggregate_min_venues_after_trim():
    # Start with 6, trim drops 2 → only 4 survivors → below min_venues=5
    prices = {
        "binance": 100.0, "coinbase": 100.0, "kraken": 100.0,
        "bitstamp": 100.0, "bitfinex": 200.0, "okx": 200.0,
    }
    result = aggregate(prices, trim_outlier_bps=50, min_venues=5)
    assert result is None
