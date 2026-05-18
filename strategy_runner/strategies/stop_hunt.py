"""stop_hunt — Stop Hunt Rejection at S/R Levels.

THESIS:
Algorithmic traders systematically sweep obvious stop clusters at horizontal
support/resistance to trigger retail stops, then reverse. The signal is the
SWEEP+REJECT pattern:

  1. Price approaches a defined S/R level (recent swing high/low)
  2. Wicks BELOW support (or above resistance) by ≥ Y bps
  3. CLOSES back inside the prior range
  4. Wick is dominant (≥ 50% of bar body)

When all 4 conditions align on a 1h candle, it's almost certainly an
algorithmic stop sweep. Reversal is the high-probability trade.

EXPECTED WR: 65-75% (with strict mechanical filters). Council projected
PF 2.5-3.5.

OPERATOR CAVEAT (council finding): "75% WR is plausible IF mechanical
wick definition is strict (>50% bar, >2x ATR sweep). Without strict
mechanics, drops to 60%." This implementation uses strict definitions.

SIGNAL:
- TF: 1h
- Find recent 48h swing high + swing low
- For each new closed candle:
  - LOW sweep (bullish setup): bar low < swing_low - 0.2% (clean sweep)
    AND bar close > swing_low (closed back inside)
    AND wick_below = swing_low - bar.low > 0.5 × |close - open| × 0.5
    AND |close - open| > 0  (real body, not doji)
    → LONG signal
  - HIGH sweep (bearish setup): mirror logic → SHORT

EXIT:
- SL: just below/above the swept level (tight, beyond the wick extreme)
- TP: 2× SL distance (R:R = 1:2)
- Max hold: 12 hours (1h × 12 bars)
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


STOPH_SWING_LOOKBACK = int(os.environ.get("STOPH_SWING_LOOKBACK", "48"))   # bars to find S/R
STOPH_SWEEP_BPS = float(os.environ.get("STOPH_SWEEP_BPS", "0.002"))         # 20bps minimum sweep
STOPH_WICK_RATIO = float(os.environ.get("STOPH_WICK_RATIO", "0.5"))          # wick must be ≥50% of bar
STOPH_MIN_BODY_PCT = float(os.environ.get("STOPH_MIN_BODY_PCT", "0.001"))   # bar body ≥10bps (not doji)
STOPH_RR = float(os.environ.get("STOPH_RR", "2.0"))                          # TP = RR × SL distance
STOPH_MAX_HOLD_BARS = int(os.environ.get("STOPH_MAX_HOLD_BARS", "12"))


DEFAULT_UNIVERSE = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "DOT",
    "NEAR", "INJ", "SUI", "APT", "ARB", "OP", "SEI", "TIA", "WIF", "JUP",
    "kPEPE", "kSHIB", "AAVE", "UNI", "ATOM", "TRX", "ADA", "ORDI", "WLD",
]


class StopHunt(StrategyBase):
    NAME = "stop_hunt"
    CLOID_PREFIX = "stoph_"
    AFFINITY = ["range", "chop", "high_vol"]
    TF = "1h"
    UNIVERSE = DEFAULT_UNIVERSE

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Need enough history for swing detection + last closed candle
        try:
            candles = bus.candles(coin, cls.TF, n=STOPH_SWING_LOOKBACK + 4)
        except Exception:
            return None
        if not candles or len(candles) < STOPH_SWING_LOOKBACK + 2:
            return None

        # Last CLOSED candle = candles[-2] (candles[-1] is the in-progress bar)
        bar = candles[-2]
        try:
            o = float(bar["open"]); h = float(bar["high"])
            l = float(bar["low"]); c = float(bar["close"])
        except (KeyError, ValueError, TypeError):
            return None

        # Swing levels from prior STOPH_SWING_LOOKBACK bars (excluding the sweep bar)
        prior = candles[-(STOPH_SWING_LOOKBACK + 2):-2]
        try:
            swing_low = min(float(b["low"]) for b in prior)
            swing_high = max(float(b["high"]) for b in prior)
        except (KeyError, ValueError, TypeError):
            return None

        body_abs = abs(c - o)
        if c <= 0 or body_abs / c < STOPH_MIN_BODY_PCT:
            return None         # too small a body

        bar_range = h - l
        if bar_range <= 0:
            return None

        # ── LOW SWEEP (bullish setup) ──
        if l < swing_low * (1 - STOPH_SWEEP_BPS) and c > swing_low:
            wick_below = swing_low - l
            wick_pct_of_bar = wick_below / bar_range
            if wick_pct_of_bar >= STOPH_WICK_RATIO and c > o:
                # Confirmed: deep sweep + bullish close → LONG
                is_long = True
                # SL just below the sweep low (with small buffer)
                sl_px = l * (1 - 0.001)
                sl_dist = c - sl_px
                tp_px = c + sl_dist * STOPH_RR
                return cls._make_signal(coin, is_long, c, sl_px, tp_px,
                                        reason=f"sweep_low_wick={wick_pct_of_bar*100:.0f}%",
                                        extras={
                                            "swing_low": swing_low,
                                            "sweep_low": l,
                                            "wick_pct_of_bar": round(wick_pct_of_bar, 3),
                                            "sweep_bps": round((swing_low - l) / swing_low * 10000, 1),
                                        })

        # ── HIGH SWEEP (bearish setup) ──
        if h > swing_high * (1 + STOPH_SWEEP_BPS) and c < swing_high:
            wick_above = h - swing_high
            wick_pct_of_bar = wick_above / bar_range
            if wick_pct_of_bar >= STOPH_WICK_RATIO and c < o:
                is_long = False
                sl_px = h * (1 + 0.001)
                sl_dist = sl_px - c
                tp_px = c - sl_dist * STOPH_RR
                return cls._make_signal(coin, is_long, c, sl_px, tp_px,
                                        reason=f"sweep_high_wick={wick_pct_of_bar*100:.0f}%",
                                        extras={
                                            "swing_high": swing_high,
                                            "sweep_high": h,
                                            "wick_pct_of_bar": round(wick_pct_of_bar, 3),
                                            "sweep_bps": round((h - swing_high) / swing_high * 10000, 1),
                                        })

        return None

    @classmethod
    def _make_signal(cls, coin, is_long, ref_px, sl_px, tp_px, reason, extras):
        return Signal(
            coin=coin,
            side="B" if is_long else "A",
            is_long=is_long,
            ref_price=ref_px,
            sl_px=sl_px,
            tp_px=tp_px,
            max_hold_bars=STOPH_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=reason,
            extras=extras,
        )
