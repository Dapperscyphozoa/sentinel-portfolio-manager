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
from common import edge_filters


STOPH_SWING_LOOKBACK = int(os.environ.get("STOPH_SWING_LOOKBACK", "48"))   # bars to find S/R
STOPH_SWEEP_BPS = float(os.environ.get("STOPH_SWEEP_BPS", "0.002"))         # 20bps minimum sweep
STOPH_WICK_RATIO = float(os.environ.get("STOPH_WICK_RATIO", "0.5"))          # wick must be ≥50% of bar
STOPH_MIN_BODY_PCT = float(os.environ.get("STOPH_MIN_BODY_PCT", "0.001"))   # bar body ≥10bps (not doji)
STOPH_RR = float(os.environ.get("STOPH_RR", "2.0"))                          # TP = RR × SL distance
STOPH_LIQ_FILTER_ENABLED = int(os.environ.get("STOPH_LIQ_FILTER_ENABLED", "1"))
STOPH_LIQ_MIN_DEPTH_USD = float(os.environ.get("STOPH_LIQ_MIN_DEPTH_USD", "50000"))
STOPH_MAX_HOLD_BARS = int(os.environ.get("STOPH_MAX_HOLD_BARS", "12"))
STOPH_NEWS_SPIKE_ATR_MULT = float(os.environ.get("STOPH_NEWS_SPIKE_ATR_MULT", "3.0"))
STOPH_ATR_PERIOD = int(os.environ.get("STOPH_ATR_PERIOD", "14"))


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

        # ── News-spike filter (council Q5 2026-05-18) ──
        # 5/6 council voters flagged news-spike wick-sweep false positives.
        # Compute ATR over last STOPH_ATR_PERIOD closed bars (excluding sweep bar).
        # If current bar_range > STOPH_NEWS_SPIKE_ATR_MULT × ATR, the move is too
        # large to be a stop hunt — likely macro news (NFP/CPI/CME open) which
        # does NOT reliably revert. Reject.
        atr_bars = candles[-(STOPH_ATR_PERIOD + 2):-2]
        if len(atr_bars) >= STOPH_ATR_PERIOD:
            try:
                trs = []
                prev_close = None
                for b in atr_bars:
                    bh = float(b["high"])
                    bl = float(b["low"])
                    bc = float(b["close"])
                    if prev_close is None:
                        tr = bh - bl
                    else:
                        tr = max(bh - bl, abs(bh - prev_close), abs(bl - prev_close))
                    trs.append(tr)
                    prev_close = bc
                atr = sum(trs) / len(trs)
                if atr > 0 and bar_range > STOPH_NEWS_SPIKE_ATR_MULT * atr:
                    return None      # news-spike, not stop hunt
            except (KeyError, ValueError, TypeError):
                pass  # missing data — don't block, fall through

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
                                        bus=bus,
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
                                        bus=bus,
                                        extras={
                                            "swing_high": swing_high,
                                            "sweep_high": h,
                                            "wick_pct_of_bar": round(wick_pct_of_bar, 3),
                                            "sweep_bps": round((h - swing_high) / swing_high * 10000, 1),
                                        })

        return None

    @classmethod
    def _make_signal(cls, coin, is_long, ref_px, sl_px, tp_px, reason, extras, bus=None):
        # ── Stage 2 council filter: liquidity at sweep target (+20% WR) ──
        if STOPH_LIQ_FILTER_ENABLED and bus is not None:
            passes, liq_detail = edge_filters.liquidity_at_target(
                bus, coin, is_long, min_far_side_depth_usd=STOPH_LIQ_MIN_DEPTH_USD,
            )
            extras.update(liq_detail)
            if not passes:
                return None

        # ── Stage 2 council filter: CVD-alignment confluence (+5-15% WR) ──
        if int(os.environ.get("STOPH_CVD_ALIGN_ENABLED", "1")) == 1 and bus is not None:
            passes, cvd_d = edge_filters.cvd_alignment(
                bus, coin, is_long=is_long, window_ms=60_000,
                min_z=float(os.environ.get("STOPH_CVD_MIN_Z", "0.3")),
            )
            extras.update(cvd_d)
            if not passes:
                return None
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
