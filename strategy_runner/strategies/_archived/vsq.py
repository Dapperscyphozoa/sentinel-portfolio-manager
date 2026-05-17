"""vsq — Volatility Squeeze Breakout (SPEC §3.2).

Bollinger inside Keltner for N consecutive bars = squeeze; volume expansion +
close outside bands = breakout. Trade direction of breakout.
WARNING: PF 3.04 claim predates look-ahead-bias purge. Run honest backtest
(scripts/honest_backtest.py) before promoting beyond paper.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase
from ._indicators import bollinger, keltner, atr


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


class VSQ(StrategyBase):
    NAME = "vsq"
    CLOID_PREFIX = "vsqzr_"
    AFFINITY = ["trend_up", "trend_down"]
    TF = "1h"
    UNIVERSE = [
        "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "NEAR",
        "SUI", "APT", "ARB", "OP", "INJ", "SEI", "TIA", "WIF", "JUP", "DOT",
        "ATOM", "FET", "STG", "POLYX",
    ]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bb_n = int(_f("VSQ_BB_PERIOD", 20))
        bb_k = _f("VSQ_BB_STD", 2.0)
        kc_n = int(_f("VSQ_KC_PERIOD", 14))
        kc_mult = _f("VSQ_KC_ATR_MULT", 1.5)
        squeeze_bars = int(_f("VSQ_SQUEEZE_BARS", 6))
        vol_mult = _f("VSQ_VOL_MULT", 1.8)
        sl_mult = _f("VSQ_SL_ATR_MULT", 2.0)
        tp_mult = _f("VSQ_TP_ATR_MULT", 6.0)
        max_hold = int(_f("VSQ_MAX_HOLD_BARS", 24))

        need = max(bb_n, kc_n) + squeeze_bars + 5
        bars = bus.candles(coin, cls.TF, n=need + 5)
        if not bars or len(bars) < need:
            return None
        closes = [float(b["close"]) for b in bars]
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        vols = [float(b["volume"]) for b in bars]

        bb_u, _, bb_l = bollinger(closes, bb_n, bb_k)
        kc_u, _, kc_l = keltner(highs, lows, closes, kc_n, kc_mult)
        atrs = atr(highs, lows, closes, kc_n)

        # check last squeeze_bars windows for squeeze (BB inside KC)
        for i in range(-squeeze_bars - 1, -1):
            if any(x is None for x in (bb_u[i], bb_l[i], kc_u[i], kc_l[i])):
                return None
            if not (bb_u[i] < kc_u[i] and bb_l[i] > kc_l[i]):
                return None

        # last bar conditions
        c = closes[-1]
        bu, bl = bb_u[-1], bb_l[-1]
        if bu is None or bl is None:
            return None
        avg_vol = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else (sum(vols[:-1]) / max(1, len(vols) - 1))
        v = vols[-1]
        vol_ok = v > vol_mult * avg_vol and avg_vol > 0

        a = atrs[-1]
        if a is None or a <= 0:
            return None

        breakout_up = c > bu and vol_ok
        breakout_down = c < bl and vol_ok

        if not (breakout_up or breakout_down):
            return None

        if breakout_up:
            return Signal(
                coin=coin, side="B", is_long=True, ref_price=c,
                sl_px=c - sl_mult * a, tp_px=c + tp_mult * a,
                max_hold_bars=max_hold, fire_ts=time.time() * 1000,
                fire_reason="squeeze_break_up",
                extras={"squeeze_bars": squeeze_bars, "atr": a, "vol_ratio": v / avg_vol},
            )
        return Signal(
            coin=coin, side="A", is_long=False, ref_price=c,
            sl_px=c + sl_mult * a, tp_px=c - tp_mult * a,
            max_hold_bars=max_hold, fire_ts=time.time() * 1000,
            fire_reason="squeeze_break_down",
            extras={"squeeze_bars": squeeze_bars, "atr": a, "vol_ratio": v / avg_vol},
        )
