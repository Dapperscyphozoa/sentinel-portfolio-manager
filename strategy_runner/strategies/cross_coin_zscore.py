"""cross_coin_zscore — pair ratio z-score divergence engine.

Stage 1 #3. Council pick — pure compute, no new infrastructure.

Mechanic:
  - For each (anchor, satellite) pair, compute price ratio R = px_satellite / px_anchor.
  - Compute 30-min rolling z-score of R (using 5m bars × 6 bars = 30 min lookback NO,
    actually 5m bars × 60 bars = 5 hours for stable sigma estimation).
  - When z_R > +2.0σ: ratio is stretched HIGH → satellite over-extended vs anchor →
      SHORT satellite (we expect mean-reversion of ratio).
  - When z_R < -2.0σ: ratio LOW → satellite under-extended vs anchor →
      LONG satellite.
  - Single-leg execution on HL (trade only the satellite — anchor is reference).
  - Filter: anchor must be moving in same direction OR sideways (don't fade trends
    when anchor itself is breaking out).

Pairs chosen for stable cointegration on crypto perps:
  - ETH/BTC (canonical)
  - SOL/ETH
  - BNB/BTC
  - AVAX/ETH
  - DOGE/BTC
  - LINK/ETH
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from common import edge_filters
from strategy_runner.strategies._base import Signal, StrategyBase


log = logging.getLogger("strategy.cross_coin_zscore")


def _f(k: str, d: float) -> float:
    try:
        return float(os.environ.get(k, d))
    except Exception:
        return d


CCZ_TF = "5m"
CCZ_LOOKBACK_BARS = int(_f("CCZ_LOOKBACK_BARS", 60))   # 5h on 5m
CCZ_Z_THRESHOLD = _f("CCZ_Z_THRESHOLD", 2.0)
CCZ_SL_PCT = _f("CCZ_SL_PCT", 0.008)
CCZ_TP_PCT = _f("CCZ_TP_PCT", 0.016)   # 2:1 R:R
CCZ_MAX_HOLD_BARS = int(_f("CCZ_MAX_HOLD_BARS", 24))   # 2h on 5m


# (satellite, anchor)
PAIRS = [
    ("ETH", "BTC"),
    ("SOL", "ETH"),
    ("BNB", "BTC"),
    ("AVAX", "ETH"),
    ("DOGE", "BTC"),
    ("LINK", "ETH"),
    ("ARB", "ETH"),
    ("OP", "ETH"),
    ("SUI", "SOL"),
    ("APT", "SOL"),
]


def _stats(values: list[float]) -> tuple[float, float]:
    if len(values) < 2:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return mean, var ** 0.5


class CrossCoinZScore(StrategyBase):
    NAME = "cross_coin_zscore"
    CLOID_PREFIX = "ccoin"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = CCZ_TF
    # Universe is the satellites — the anchor is reference data only
    UNIVERSE = list({sat for sat, _ in PAIRS})

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Find which pair(s) include this coin as satellite
        anchors = [a for s, a in PAIRS if s == coin]
        if not anchors:
            return None

        # Use the first matching pair (could iterate for multi-anchor cases)
        anchor = anchors[0]

        try:
            sat_bars = bus.candles(coin, CCZ_TF, n=CCZ_LOOKBACK_BARS + 5)
            anc_bars = bus.candles(anchor, CCZ_TF, n=CCZ_LOOKBACK_BARS + 5)
        except Exception:
            return None

        if not sat_bars or not anc_bars:
            return None
        if len(sat_bars) < CCZ_LOOKBACK_BARS or len(anc_bars) < CCZ_LOOKBACK_BARS:
            return None

        # Align: pair up by index (assumes both are 5m closed bars in sync)
        n = min(len(sat_bars), len(anc_bars), CCZ_LOOKBACK_BARS)
        sat_closes = [float(b["close"]) for b in sat_bars[-n:]]
        anc_closes = [float(b["close"]) for b in anc_bars[-n:]]

        if any(c <= 0 for c in sat_closes + anc_closes):
            return None

        ratios = [s / a for s, a in zip(sat_closes, anc_closes)]
        curr_ratio = ratios[-1]
        hist_ratios = ratios[:-1]   # exclude current bar to avoid lookahead

        mean, sigma = _stats(hist_ratios)
        if sigma <= 0:
            return None
        z = (curr_ratio - mean) / sigma

        # Anchor trend filter — don't fade ratio if anchor breaking out hard
        if len(anc_closes) >= 12:
            anc_recent = anc_closes[-1]
            anc_baseline = anc_closes[-12]
            anc_pct = (anc_recent - anc_baseline) / anc_baseline
            anchor_in_breakout = abs(anc_pct) > 0.03   # >3% in last 60min
        else:
            anchor_in_breakout = False

        side = None
        is_long = None
        reason = None

        sat_close = sat_closes[-1]

        if z > CCZ_Z_THRESHOLD and not anchor_in_breakout:
            # Ratio stretched HIGH — satellite over-extended → SHORT
            side = "A"; is_long = False
            reason = f"z={z:.2f}>+{CCZ_Z_THRESHOLD} pair={coin}/{anchor} fade_overextension"
        elif z < -CCZ_Z_THRESHOLD and not anchor_in_breakout:
            # Ratio stretched LOW — satellite under-extended → LONG
            side = "B"; is_long = True
            reason = f"z={z:.2f}<-{CCZ_Z_THRESHOLD} pair={coin}/{anchor} mean_revert_long"

        if not side:
            return None

        if is_long:
            sl_px = sat_close * (1 - CCZ_SL_PCT)
            tp_px = sat_close * (1 + CCZ_TP_PCT)
        else:
            sl_px = sat_close * (1 + CCZ_SL_PCT)
            tp_px = sat_close * (1 - CCZ_TP_PCT)

        # ── Stage 2 council filter: asia_kill ──
        asia_pass, asia_detail = edge_filters.asia_kill_window()
        if not asia_pass:
            return None

        return Signal(
            coin=coin, side=side, is_long=is_long, ref_price=sat_close,
            sl_px=sl_px, tp_px=tp_px,
            max_hold_bars=CCZ_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=reason,
            extras={
                "anchor": anchor,
                "ratio_curr": curr_ratio,
                "ratio_mean": mean,
                "ratio_sigma": sigma,
                "z_score": z,
                "anchor_breakout": anchor_in_breakout,
                "satellite_px": sat_close,
                "anchor_px": anc_closes[-1],
            },
        )
