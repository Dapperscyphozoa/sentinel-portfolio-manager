"""Tests for poly_runner.strategies._base helpers."""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest

from poly_runner.strategies._base import (
    dynamic_fee,
    kelly_fraction,
    true_prob_from_cl,
)


def test_dynamic_fee_peaks_at_half():
    assert dynamic_fee(0.5) == pytest.approx(0.0156, rel=1e-6)


def test_dynamic_fee_zero_at_extremes():
    assert dynamic_fee(0.01) < 0.001
    assert dynamic_fee(0.99) < 0.001


def test_dynamic_fee_outside_range_zero():
    assert dynamic_fee(0.0) == 0.0
    assert dynamic_fee(1.0) == 0.0
    assert dynamic_fee(-0.1) == 0.0
    assert dynamic_fee(1.1) == 0.0


def test_kelly_zero_for_zero_edge():
    assert kelly_fraction(0, 0.5) == 0.0


def test_kelly_capped():
    f = kelly_fraction(edge=1.0, win_prob=0.99, cap=0.05)
    assert f <= 0.05


def test_true_prob_from_cl_above_start_is_higher_prob():
    # If CL prediction is above start_price, probability of ending up
    # (at zero vol) is 1.0, regardless of time remaining.
    p = true_prob_from_cl(predicted_cl=101, start_price=100,
                           time_remaining_s=10, sigma_per_sec=1e-9)
    assert p > 0.99


def test_true_prob_from_cl_at_start_is_half():
    # CL prediction == start_price → 0.5
    p = true_prob_from_cl(predicted_cl=100, start_price=100,
                           time_remaining_s=60, sigma_per_sec=0.001)
    assert p == pytest.approx(0.5, abs=1e-6)


def test_true_prob_from_cl_falls_as_time_elapses_with_drift():
    # Predicted = start: at any time, prob = 0.5
    # If predicted < start: prob should rise toward "win" only with negative drift
    p_short = true_prob_from_cl(predicted_cl=99, start_price=100,
                                 time_remaining_s=5, sigma_per_sec=0.001)
    p_long = true_prob_from_cl(predicted_cl=99, start_price=100,
                                time_remaining_s=300, sigma_per_sec=0.001)
    # short time → stronger conviction that we won't recover to 100
    assert p_short < p_long
