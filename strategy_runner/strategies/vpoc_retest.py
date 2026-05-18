"""vpoc_retest — Naked Weekly Volume Profile POC Retest.

THESIS:
Volume Profile reveals price levels where the most volume traded — the
Point of Control (POC) is the highest-volume price bin in a defined period.

"Naked" POCs are POCs from prior weeks that haven't been "retested" — price
hasn't returned within 0.5% of them. Institutions return to naked POCs to
fill larger orders → mean-reversion magnet.

SIGNAL:
- TF: 1h (so we have enough granularity for the weekly profile)
- Compute weekly volume profile: bin closes into 50 price buckets weighted
  by candle volume, find POC = bin with highest volume
- Identify naked POCs: POCs from prior 4 weeks NOT yet retested (price
  hasn't been within 0.5% of POC since it was established)
- Fire when current price approaches naked POC from above (sell-side
  retest → LONG) or below (buy-side retest → SHORT)
- Confirmation: bar that touches POC must close in direction of mean-rev
  (long: close > open; short: close < open) — momentum confirms the bounce

EXIT:
- TP at 50% of distance to next major level (or +2× ATR)
- SL beyond the POC level (price ignores POC → no edge)
- Max hold: 48h (2 days)

UNIVERSE (council-tightened):
BTC, ETH, SOL, BNB, XRP — only coins with sufficient institutional flow
to respect VPOCs. Memes excluded per operator's coin-specificity argument.

EXPECTED: PF 1.6-2.2, 1.1-2.8 trades/day per council estimates.

COUNCIL CAVEAT: "memes don't respect VPOCs". Universe filter is the
critical risk control — do NOT add altcoins without confirming volume
profile respect first.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


VPOC_BINS = int(os.environ.get("VPOC_BINS", "50"))
VPOC_LOOKBACK_BARS = int(os.environ.get("VPOC_LOOKBACK_BARS", "168"))    # 7d × 24h = 1w on 1h TF
VPOC_RETEST_PCT = float(os.environ.get("VPOC_RETEST_PCT", "0.005"))       # within 0.5% of POC
VPOC_NAKED_LOOKBACK_WEEKS = int(os.environ.get("VPOC_NAKED_LOOKBACK_WEEKS", "4"))
VPOC_SL_PCT = float(os.environ.get("VPOC_SL_PCT", "0.015"))               # 1.5% SL
VPOC_TP_PCT = float(os.environ.get("VPOC_TP_PCT", "0.025"))               # 2.5% TP (1:~1.7 RR)
VPOC_OI_FILTER_ENABLED = int(os.environ.get("VPOC_OI_FILTER_ENABLED", "1"))
VPOC_OI_MIN_PCT_DELTA = float(os.environ.get("VPOC_OI_MIN_PCT_DELTA", "0.002"))
VPOC_VOL_FILTER_ENABLED = os.environ.get("VPOC_VOL_FILTER_ENABLED", "1") == "1"
VPOC_VOL_MIN_RATIO = float(os.environ.get("VPOC_VOL_MIN_RATIO", "1.5"))
VPOC_MAX_HOLD_BARS = int(os.environ.get("VPOC_MAX_HOLD_BARS", "48"))      # 48h


# Restricted universe per council guidance — institutional-flow coins only
DEFAULT_UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP"]


def _compute_poc(candles: list[dict], num_bins: int) -> Optional[tuple[float, float]]:
    """Compute volume-weighted POC for a list of candles.
    
    Returns (poc_price, total_volume_in_poc_bin) or None.
    """
    if not candles:
        return None
    try:
        prices = [(float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0 for c in candles]
        volumes = [float(c.get("volume", 0)) for c in candles]
    except (KeyError, ValueError, TypeError):
        return None
    if not any(volumes) or len(prices) < 10:
        return None

    p_min = min(prices)
    p_max = max(prices)
    if p_max <= p_min:
        return None
    bin_size = (p_max - p_min) / num_bins
    if bin_size <= 0:
        return None

    bins = [0.0] * num_bins
    for px, vol in zip(prices, volumes):
        idx = min(int((px - p_min) / bin_size), num_bins - 1)
        bins[idx] += vol

    max_vol = max(bins)
    if max_vol <= 0:
        return None
    poc_idx = bins.index(max_vol)
    poc_price = p_min + (poc_idx + 0.5) * bin_size
    return poc_price, max_vol


class VPOCRetest(StrategyBase):
    NAME = "vpoc_retest"
    CLOID_PREFIX = "vpocr_"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = "1h"
    UNIVERSE = DEFAULT_UNIVERSE

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Need at least VPOC_NAKED_LOOKBACK_WEEKS + 1 weeks of 1h data
        bars_needed = VPOC_LOOKBACK_BARS * (VPOC_NAKED_LOOKBACK_WEEKS + 1) + 10
        try:
            candles = bus.candles(coin, cls.TF, n=bars_needed)
        except Exception:
            return None
        if not candles or len(candles) < bars_needed:
            return None

        # Last closed bar
        bar = candles[-2]
        try:
            o = float(bar["open"]); h = float(bar["high"])
            l = float(bar["low"]); c = float(bar["close"])
        except (KeyError, ValueError, TypeError):
            return None

        # Compute prior-week POCs (looking back VPOC_NAKED_LOOKBACK_WEEKS weeks)
        weekly_pocs = []
        for w in range(1, VPOC_NAKED_LOOKBACK_WEEKS + 1):
            # Bars for that week: [-2 - w*VPOC_LOOKBACK_BARS : -2 - (w-1)*VPOC_LOOKBACK_BARS]
            end = -2 - (w - 1) * VPOC_LOOKBACK_BARS
            start = end - VPOC_LOOKBACK_BARS
            week_bars = candles[start:end] if end != 0 else candles[start:]
            if len(week_bars) < VPOC_LOOKBACK_BARS // 2:
                continue
            poc_result = _compute_poc(week_bars, VPOC_BINS)
            if poc_result:
                weekly_pocs.append({"week_ago": w, "poc": poc_result[0]})

        if not weekly_pocs:
            return None

        # Identify naked POCs: POCs not retested (price hasn't been within 0.5%)
        # since they were established. Check by scanning all bars between
        # POC's week and now.
        naked_pocs = []
        for entry in weekly_pocs:
            poc_px = entry["poc"]
            w = entry["week_ago"]
            # Bars from week w-1 to current
            check_start = -2 - (w - 1) * VPOC_LOOKBACK_BARS
            bars_since = candles[check_start:-1]
            retested = False
            for bar_check in bars_since:
                try:
                    bh = float(bar_check["high"])
                    bl = float(bar_check["low"])
                except (KeyError, ValueError, TypeError):
                    continue
                # POC is retested if any bar's range includes it (with buffer)
                if bl <= poc_px * (1 + VPOC_RETEST_PCT) and bh >= poc_px * (1 - VPOC_RETEST_PCT):
                    retested = True
                    break
            if not retested:
                naked_pocs.append(poc_px)

        if not naked_pocs:
            return None

        # Find closest naked POC to current price
        closest_poc = min(naked_pocs, key=lambda p: abs(p - c))
        dist_pct = abs(c - closest_poc) / c

        # Fire when current bar touches POC (within retest window)
        if dist_pct > VPOC_RETEST_PCT:
            return None

        # Direction: mean-revert AWAY from POC after retest
        # If price came from ABOVE and tagged POC (closest_poc < c slightly), reject = LONG
        # If price came from BELOW and tagged POC (closest_poc > c slightly), reject = SHORT
        # Use bar body direction as confirmation: close > open = bullish reject (LONG)
        body_up = c > o

        if closest_poc <= c and body_up:
            # Approached from above, touched POC, bullish close → LONG
            is_long = True
        elif closest_poc >= c and not body_up:
            # Approached from below, touched POC, bearish close → SHORT
            is_long = False
        else:
            # No clean rejection — body doesn't confirm direction
            return None

        if is_long:
            sl_px = c * (1 - VPOC_SL_PCT)
            tp_px = c * (1 + VPOC_TP_PCT)
        else:
            sl_px = c * (1 + VPOC_SL_PCT)
            tp_px = c * (1 - VPOC_TP_PCT)

        # ── Stage 2 council filter: VPOC volume gate (+0.5% WR per Mistral) ──
        # Require retest bar's volume to be ≥1.5× rolling average. Low-vol
        # retests are weak signals.
        vol_detail = {}
        if VPOC_VOL_FILTER_ENABLED:
            # Use last 20 bars' volumes; bar -2 is our trigger
            recent_bars = candles[-22:-1]
            passes, vol_detail = edge_filters.vpoc_min_volume(
                recent_bars, vpoc_bar_idx=len(recent_bars) - 1,
                min_volume_ratio=VPOC_VOL_MIN_RATIO,
            )
            if not passes:
                return None

        # ── Stage 2 council filter: OI-delta confirms participation (+0.4-22% WR) ──
        oi_detail = {}
        if VPOC_OI_FILTER_ENABLED:
            passes, oi_detail = edge_filters.oi_delta_increasing(
                bus, coin,
                lookback_n=6,
                min_pct_delta=VPOC_OI_MIN_PCT_DELTA,
            )
            if not passes:
                return None

        return Signal(
            coin=coin,
            side="B" if is_long else "A",
            is_long=is_long,
            ref_price=c,
            sl_px=sl_px,
            tp_px=tp_px,
            max_hold_bars=VPOC_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=f"naked_poc_retest_d={dist_pct*100:.2f}%",
            extras={
                "naked_poc": closest_poc,
                "distance_pct": round(dist_pct * 100, 3),
                "naked_pocs_total": len(naked_pocs),
                "weeks_lookback": VPOC_NAKED_LOOKBACK_WEEKS,
                **oi_detail,
                **vol_detail,
            },
        )
