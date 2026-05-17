"""range_fade — mean-reversion in range (SPEC §3.3).

Blocked when PM regime ∈ {trend_up, trend_down} at conf > 0.7.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase
from ._indicators import rsi, bollinger


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


class RangeFade(StrategyBase):
    NAME = "range_fade"
    CLOID_PREFIX = "rngfd_"
    AFFINITY = ["range", "chop"]
    TF = "15m"
    UNIVERSE = [
        "SOL", "AVAX", "LINK", "DOGE", "NEAR", "SUI", "APT", "ARB", "OP",
        "INJ", "SEI", "TIA", "DOT", "LTC", "ATOM", "STG", "FET", "JUP",
    ]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        rsi_n = int(_f("RF_RSI_PERIOD", 14))
        rsi_lo = _f("RF_RSI_LOW", 25)
        rsi_hi = _f("RF_RSI_HIGH", 75)
        bb_n = int(_f("RF_BB_PERIOD", 20))
        bb_k = _f("RF_BB_STD", 2.0)
        sl_pct = _f("RF_SL_PCT", 0.012)
        tp_pct = _f("RF_TP_PCT", 0.020)
        max_hold = int(_f("RF_MAX_HOLD_BARS", 12))
        use_regime = os.environ.get("RF_REGIME_FILTER", "1") in ("1", "true", "yes")

        bars = bus.candles(coin, cls.TF, n=max(rsi_n, bb_n) + 5)
        if not bars or len(bars) < max(rsi_n, bb_n) + 1:
            return None
        closes = [float(b["close"]) for b in bars]
        rsi_vals = rsi(closes, rsi_n)
        upper, _, lower = bollinger(closes, bb_n, bb_k)
        r = rsi_vals[-1]
        u, l = upper[-1], lower[-1]
        c = closes[-1]
        if r is None or u is None or l is None:
            return None

        if use_regime:
            try:
                reg = bus._client.get(f"{bus.base_url}/regime").json() if hasattr(bus, "_client") else None
            except Exception:
                reg = None
            # fall through: PM gate is authoritative. evaluate() only uses bus.

        fire_long = (r < rsi_lo) and (c <= l * 1.001)
        fire_short = (r > rsi_hi) and (c >= u * 0.999)
        if not (fire_long or fire_short):
            return None

        if fire_long:
            return Signal(
                coin=coin, side="B", is_long=True, ref_price=c,
                sl_px=c * (1 - sl_pct), tp_px=c * (1 + tp_pct),
                max_hold_bars=max_hold, fire_ts=time.time() * 1000,
                fire_reason="rsi_oversold_bb_lower",
                extras={"rsi": r, "bb_lower": l, "close": c},
            )
        return Signal(
            coin=coin, side="A", is_long=False, ref_price=c,
            sl_px=c * (1 + sl_pct), tp_px=c * (1 - tp_pct),
            max_hold_bars=max_hold, fire_ts=time.time() * 1000,
            fire_reason="rsi_overbought_bb_upper",
            extras={"rsi": r, "bb_upper": u, "close": c},
        )
