"""range_breakout — breakout of compressed range with volume confirmation (SPEC §3.4)."""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


class RangeBreakout(StrategyBase):
    NAME = "range_bo"
    CLOID_PREFIX = "rngbo_"
    AFFINITY = ["trend_up", "trend_down"]
    TF = "15m"
    UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        lookback = int(_f("RB_RANGE_LOOKBACK", 48))
        max_range = _f("RB_RANGE_MAX_PCT", 0.04)
        buf = _f("RB_BREAK_BUFFER", 0.001)
        vol_mult = _f("RB_VOL_MULT", 2.0)
        sl_pct = _f("RB_SL_PCT", 0.015)
        tp_pct = _f("RB_TP_PCT", 0.045)
        max_hold = int(_f("RB_MAX_HOLD_BARS", 24))

        bars = bus.candles(coin, cls.TF, n=lookback + 25)
        if not bars or len(bars) < lookback + 21:
            return None

        # exclude current bar from range definition (it's the breaker)
        ref_bars = bars[-(lookback + 1):-1]
        highs = [float(b["high"]) for b in ref_bars]
        lows = [float(b["low"]) for b in ref_bars]
        range_high = max(highs)
        range_low = min(lows)
        if range_low <= 0:
            return None
        range_pct = (range_high - range_low) / range_low
        if range_pct > max_range:
            return None

        cur = bars[-1]
        c = float(cur["close"])
        v = float(cur["volume"])
        vols = [float(b["volume"]) for b in bars[-21:-1]]
        avg_vol = sum(vols) / max(1, len(vols))
        if v < vol_mult * avg_vol or avg_vol <= 0:
            return None

        fire_long = c > range_high * (1 + buf)
        fire_short = c < range_low * (1 - buf)
        if not (fire_long or fire_short):
            return None

        if fire_long:
            return Signal(
                coin=coin, side="B", is_long=True, ref_price=c,
                sl_px=c * (1 - sl_pct), tp_px=c * (1 + tp_pct),
                max_hold_bars=max_hold, fire_ts=time.time() * 1000,
                fire_reason="range_break_up",
                extras={"range_high": range_high, "range_low": range_low,
                        "range_pct": range_pct, "vol_ratio": v / avg_vol},
            )
        return Signal(
            coin=coin, side="A", is_long=False, ref_price=c,
            sl_px=c * (1 + sl_pct), tp_px=c * (1 - tp_pct),
            max_hold_bars=max_hold, fire_ts=time.time() * 1000,
            fire_reason="range_break_down",
            extras={"range_high": range_high, "range_low": range_low,
                    "range_pct": range_pct, "vol_ratio": v / avg_vol},
        )
