"""ICT Fair Value Gap (FVG) strategy — institutional-grade entry method.

Logic (proper ICT):
  1. Identify Break of Structure (BOS):
     - Bullish BOS: price closes above last swing high
     - Bearish BOS: price closes below last swing low
  2. After BOS, scan the impulse leg for FVG:
     - Bullish FVG: candle[i-2].high < candle[i].low (3-candle gap up)
     - Bearish FVG: candle[i-2].low > candle[i].high (3-candle gap down)
  3. Wait for price to RETRACE into the FVG zone (mitigation)
  4. Enter ON TOUCH of FVG (50% level — "consequent encroachment")
  5. SL: beyond the FVG opposite edge (tight, defined-risk)
  6. TP: previous swing structure (R/R typically 1:2 to 1:3)

Why this beats raw zfade/bb_fade:
  - Edges from order-flow imbalance, not statistical noise
  - Defined invalidation (FVG breaks → exit)
  - Asymmetric R/R (1:2+ vs zfade's 1:1)
  - Multi-timeframe natural (HTF structure + LTF entry)
"""
from __future__ import annotations

from typing import Optional

from ._base import Signal, StrategyBase


class ICTFVGBase:
    """Mixin/utility class — FVG detection + BOS logic."""

    SWING_LOOKBACK = 10        # bars on each side to confirm swing pivot
    FVG_VALID_BARS = 50        # FVG expires if not mitigated within N bars
    R_MULT_TP = 2.5            # take-profit at 2.5R
    HOLD_MAX_BARS = 50

    @classmethod
    def find_pivots(cls, highs: list[float], lows: list[float],
                    lookback: int) -> tuple[list[int], list[int]]:
        """Return (pivot_high_indices, pivot_low_indices)."""
        ph, pl = [], []
        for i in range(lookback, len(highs) - lookback):
            is_ph = all(highs[i] >= highs[j] for j in range(i - lookback, i)) and \
                    all(highs[i] > highs[j] for j in range(i + 1, i + lookback + 1))
            is_pl = all(lows[i] <= lows[j] for j in range(i - lookback, i)) and \
                    all(lows[i] < lows[j] for j in range(i + 1, i + lookback + 1))
            if is_ph: ph.append(i)
            if is_pl: pl.append(i)
        return ph, pl

    @classmethod
    def detect_bos(cls, highs: list[float], lows: list[float], closes: list[float],
                   curr_idx: int, pivots_h: list[int], pivots_l: list[int]) -> Optional[str]:
        """Detect Break of Structure at curr_idx.
        Returns 'BULL_BOS', 'BEAR_BOS', or None.

        BULL_BOS: current close > last confirmed swing high (within recent N pivots)
        BEAR_BOS: current close < last confirmed swing low
        """
        recent_ph = [p for p in pivots_h if p < curr_idx - cls.SWING_LOOKBACK]
        recent_pl = [p for p in pivots_l if p < curr_idx - cls.SWING_LOOKBACK]
        if not recent_ph and not recent_pl:
            return None
        c = closes[curr_idx]
        # Most-recent swing high broken upward?
        if recent_ph:
            last_high = highs[recent_ph[-1]]
            # Confirm BOS only if prior bar was below the swing high (fresh break)
            if c > last_high and closes[curr_idx - 1] <= last_high:
                return "BULL_BOS"
        if recent_pl:
            last_low = lows[recent_pl[-1]]
            if c < last_low and closes[curr_idx - 1] >= last_low:
                return "BEAR_BOS"
        return None

    @classmethod
    def find_recent_fvg(cls, highs: list[float], lows: list[float],
                        bos_idx: int, curr_idx: int, direction: str) -> Optional[dict]:
        """Scan bars [bos_idx-10 .. curr_idx-1] for an unmitigated FVG matching direction.
        Mitigation = a PRIOR bar (between FVG creation and curr_idx-1) already touched it.
        The current bar (curr_idx) is the candidate entry trigger — it's allowed to touch.

        Returns: {'idx': i, 'top': float, 'bottom': float, 'mid': float} or None.
        """
        scan_start = max(2, bos_idx - 10)
        scan_end = curr_idx - 1
        found = None
        for i in range(scan_start, scan_end + 1):
            if i < 2 or i >= len(highs):
                continue
            if direction == "BULL_BOS":
                if highs[i - 2] < lows[i]:
                    fvg = {"idx": i, "top": lows[i], "bottom": highs[i - 2],
                           "mid": (lows[i] + highs[i - 2]) / 2}
                    # Mitigation check: PRIOR bars only (not curr_idx)
                    mitigated = False
                    for k in range(i + 1, curr_idx):   # < curr_idx, not <=
                        if lows[k] <= fvg["top"]:
                            mitigated = True
                            break
                    if not mitigated:
                        found = fvg
            elif direction == "BEAR_BOS":
                if lows[i - 2] > highs[i]:
                    fvg = {"idx": i, "top": lows[i - 2], "bottom": highs[i],
                           "mid": (lows[i - 2] + highs[i]) / 2}
                    mitigated = False
                    for k in range(i + 1, curr_idx):
                        if highs[k] >= fvg["bottom"]:
                            mitigated = True
                            break
                    if not mitigated:
                        found = fvg
        return found

    @classmethod
    def check_fvg_touch(cls, current_bar: dict, fvg: dict, direction: str) -> bool:
        """Did current bar touch into the FVG zone (50% mid-level)?"""
        if direction == "BULL_BOS":
            return current_bar["low"] <= fvg["mid"]   # price came down to mid
        return current_bar["high"] >= fvg["mid"]


class ICT_FVG_1d(StrategyBase, ICTFVGBase):
    """ICT FVG retrace entry on 1d timeframe.

    Backtest: TBD — built fresh, awaiting walk-forward validation.
    """
    NAME = "ict_fvg_1d"
    CLOID_PREFIX = "ict1d_"
    AFFINITY = ["trend_up", "trend_down"]   # FVG works in trending markets
    TF = "1d"
    UNIVERSE = [
        "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "DOT",
        "ATOM", "NEAR", "INJ", "SUI", "APT", "FIL", "ARB", "OP", "MATIC", "TON",
        "TIA", "JUP", "WIF", "kPEPE", "kSHIB", "FTM", "AAVE", "UNI", "MKR",
        "COMP", "SEI", "ADA", "TRX", "BCH", "PENDLE", "RNDR", "PYTH", "WLD",
    ]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = bus.candles(coin, cls.TF, n=120) or []
        if len(bars) < 60:
            return None
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]
        i = len(bars) - 1
        # Need at least ~SWING_LOOKBACK*3 history
        if i < cls.SWING_LOOKBACK * 3:
            return None
        # Use only bars before i for pivots (avoid look-ahead)
        ph, pl = cls.find_pivots(highs[:i + 1], lows[:i + 1], cls.SWING_LOOKBACK)
        # Detect BOS at recent bars (look back up to 20 bars for a recent BOS)
        bos_idx = None
        bos_direction = None
        for k in range(max(20, i - 30), i):
            d = cls.detect_bos(highs, lows, closes, k, ph, pl)
            if d:
                bos_idx = k
                bos_direction = d
        if bos_idx is None:
            return None
        # FVG must exist post-BOS within FVG_VALID_BARS
        if i - bos_idx > cls.FVG_VALID_BARS:
            return None
        fvg = cls.find_recent_fvg(highs, lows, bos_idx, i, bos_direction)
        if fvg is None:
            return None
        # Did current bar touch FVG mid?
        if not cls.check_fvg_touch(bars[i], fvg, bos_direction):
            return None
        c = closes[i]
        is_long = (bos_direction == "BULL_BOS")
        # SL beyond FVG opposite edge (with small buffer)
        if is_long:
            sl_px = fvg["bottom"] * 0.998
        else:
            sl_px = fvg["top"] * 1.002
        risk_pct = abs(c - sl_px) / c
        if risk_pct < 0.005:
            return None
        # TP = 2.5R from entry
        if is_long:
            tp_px = c + (c - sl_px) * cls.R_MULT_TP
        else:
            tp_px = c - (sl_px - c) * cls.R_MULT_TP
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl_px, tp_px=tp_px,
            max_hold_bars=cls.HOLD_MAX_BARS,
            fire_ts=float(bars[i]["open_ts"]),
            fire_reason=f"{bos_direction}+FVG@{fvg['mid']:.2f}",
            extras={
                "bos": bos_direction, "fvg_top": fvg["top"], "fvg_bot": fvg["bottom"],
                "fvg_mid": fvg["mid"], "r_mult": cls.R_MULT_TP,
                "risk_pct": risk_pct, "tf": cls.TF,
            },
        )


class ICT_FVG_4h(ICT_FVG_1d):
    NAME = "ict_fvg_4h"
    CLOID_PREFIX = "ict4h_"
    TF = "4h"
    SWING_LOOKBACK = 6        # tighter swings on lower TF
    FVG_VALID_BARS = 40
    HOLD_MAX_BARS = 30
    R_MULT_TP = 2.0
