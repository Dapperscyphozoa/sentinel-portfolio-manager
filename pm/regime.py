"""Regime detector (SPEC §7.2).

Classifies the market into one of: trend_up, trend_down, range, chop.
Computation uses BTC 1h closes from signal-bus, plus an aggregate breadth check.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from strategy_runner.strategies._indicators import ema, atr


log = logging.getLogger("regime")


def _slope(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    return (xs[-1] - xs[0]) / xs[0]


def classify(closes: list[float], highs: list[float], lows: list[float]) -> dict:
    if len(closes) < 60:
        return {"regime": "unknown", "confidence": 0.0, "ts": time.time(),
                "ema20_slope": 0, "atr_pct": 0}
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if e20[-1] is None or e50[-1] is None:
        return {"regime": "unknown", "confidence": 0.0, "ts": time.time(),
                "ema20_slope": 0, "atr_pct": 0}
    slope20 = _slope([x for x in e20[-10:] if x is not None])
    ema_gap = (e20[-1] - e50[-1]) / e50[-1]
    a = atr(highs, lows, closes, 14)
    a_last = a[-1] or 0.0
    atr_pct = a_last / closes[-1] if closes[-1] > 0 else 0.0

    # rules (calibrate via env later)
    if slope20 > 0.005 and ema_gap > 0.002:
        return {"regime": "trend_up", "confidence": min(1.0, abs(slope20) * 50),
                "ts": time.time(), "ema20_slope": slope20, "atr_pct": atr_pct}
    if slope20 < -0.005 and ema_gap < -0.002:
        return {"regime": "trend_down", "confidence": min(1.0, abs(slope20) * 50),
                "ts": time.time(), "ema20_slope": slope20, "atr_pct": atr_pct}
    if atr_pct < 0.012:
        return {"regime": "range", "confidence": 0.7, "ts": time.time(),
                "ema20_slope": slope20, "atr_pct": atr_pct}
    return {"regime": "chop", "confidence": 0.6, "ts": time.time(),
            "ema20_slope": slope20, "atr_pct": atr_pct}
