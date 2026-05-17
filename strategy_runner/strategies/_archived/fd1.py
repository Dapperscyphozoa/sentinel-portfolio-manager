"""fd1 — Funding Divergence (SPEC §3.6).

Detect divergence between funding rate trend and price trend over FD_DIVERGENCE_BARS.
Fire when divergence is fresh (i.e. the prior window did NOT show divergence).
Direction: fade the price move (price up + funding falling → SHORT; price down +
funding rising → LONG).
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _slope(xs: list[float]) -> float:
    """Simple end-vs-start slope; positive = rising."""
    if len(xs) < 2:
        return 0.0
    return xs[-1] - xs[0]


class FD1(StrategyBase):
    NAME = "fd1"
    CLOID_PREFIX = "fdivg_"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = "1h"
    UNIVERSE = [
        "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "NEAR",
        "SUI", "APT", "ARB", "OP", "INJ", "SEI", "TIA", "DOT", "ATOM",
    ]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        thr_hi = _f("FD_FUNDING_THRESHOLD_HI", 1.5e-5)
        thr_lo = _f("FD_FUNDING_THRESHOLD_LO", -5e-5)
        div_bars = int(_f("FD_DIVERGENCE_BARS", 4))
        sl_pct = _f("FD_SL_PCT", 0.015)
        tp_pct = _f("FD_TP_PCT", 0.030)
        max_hold = int(_f("FD_MAX_HOLD_BARS", 24))

        bars = bus.candles(coin, cls.TF, n=div_bars + 2)
        if not bars or len(bars) < div_bars + 1:
            return None
        funding_rows = bus.funding(coin, hours=div_bars + 2)
        if not funding_rows or len(funding_rows) < div_bars + 1:
            return None

        closes = [float(b["close"]) for b in bars]
        rates = [float(r["rate"]) for r in funding_rows]

        # current window
        win_close = closes[-div_bars:]
        win_rate = rates[-div_bars:]
        # prior window (one bar earlier)
        prior_close = closes[-(div_bars + 1):-1]
        prior_rate = rates[-(div_bars + 1):-1]

        ps = _slope(win_close)
        rs = _slope(win_rate)
        prior_ps = _slope(prior_close)
        prior_rs = _slope(prior_rate)

        # Divergence definitions:
        # price up + funding down (and one of them past threshold) → SHORT
        # price down + funding up → LONG
        div_short = ps > 0 and rs < 0 and (max(win_rate) > thr_hi or min(win_rate) < thr_lo)
        div_long = ps < 0 and rs > 0 and (max(win_rate) > thr_hi or min(win_rate) < thr_lo)

        # freshness: prior window did NOT already show divergence
        prior_div_short = prior_ps > 0 and prior_rs < 0
        prior_div_long = prior_ps < 0 and prior_rs > 0

        fire_short = div_short and not prior_div_short
        fire_long = div_long and not prior_div_long
        if not (fire_short or fire_long):
            return None

        ref = closes[-1]
        if fire_long:
            return Signal(
                coin=coin, side="B", is_long=True, ref_price=ref,
                sl_px=ref * (1 - sl_pct), tp_px=ref * (1 + tp_pct),
                max_hold_bars=max_hold, fire_ts=time.time() * 1000,
                fire_reason="funding_price_divergence_long",
                extras={"price_slope": ps, "rate_slope": rs, "div_bars": div_bars},
            )
        return Signal(
            coin=coin, side="A", is_long=False, ref_price=ref,
            sl_px=ref * (1 + sl_pct), tp_px=ref * (1 - tp_pct),
            max_hold_bars=max_hold, fire_ts=time.time() * 1000,
            fire_reason="funding_price_divergence_short",
            extras={"price_slope": ps, "rate_slope": rs, "div_bars": div_bars},
        )
