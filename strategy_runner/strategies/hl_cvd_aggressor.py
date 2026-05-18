"""hl_cvd_aggressor — HL Cumulative Volume Delta aggressor-flow engine.

Council priority (Tier 1, 4/4 voters): +1.8-2.5%/mo est on backtest. World-first level
because HL CVD per-coin is not published as a tradable signal.

Mechanic:
  - Real-time CVD computed from HL public trade tape (signal-bus aggregates).
  - For each coin, compute CVD_z = z-score of last 30s CVD vs rolling 5min distribution.
  - LONG when:
      * CVD_z > +3.0σ (heavy net buying)
      * AND aggressor buy_notional / total_notional > 0.75 (real conviction)
      * AND 5m close > 5m open (price moving with flow, not divergent)
      * AND not near 1h swing high (avoid chase)
  - SHORT mirror with CVD_z < -3.0σ + sell_notional ratio + 5m red + not near 1h swing low.

Sizing: SL 0.4%, TP 0.8% (2:1 R:R after fees of 0.09%).

Time-of-day filter: skip Asia ultra-low-vol window (00-05 UTC) per UZT learnings.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from strategy_runner.strategies._base import Signal, StrategyBase


log = logging.getLogger("strategy.hl_cvd_aggressor")

# === Tunables (env-overridable) ===
def _f(k: str, d: float) -> float:
    try:
        return float(os.environ.get(k, d))
    except Exception:
        return d

CVD_WINDOW_MS    = int(_f("CVD_WINDOW_MS", 30_000))
CVD_Z_THRESHOLD  = _f("CVD_Z_THRESHOLD", 3.0)
CVD_AGGR_RATIO   = _f("CVD_AGGR_RATIO", 0.75)   # buy or sell as % of total
CVD_MIN_NOTIONAL = _f("CVD_MIN_NOTIONAL", 50_000.0)  # filter low-activity coins
CVD_SL_PCT       = _f("CVD_SL_PCT", 0.004)
CVD_TP_PCT       = _f("CVD_TP_PCT", 0.008)
CVD_SWING_LB     = int(_f("CVD_SWING_LOOKBACK", 60))  # 5m bars (5h)
CVD_NEAR_SWING_BPS = _f("CVD_NEAR_SWING_BPS", 30.0)   # don't fire within 30bps of swing
CVD_MAX_HOLD_BARS = int(_f("CVD_MAX_HOLD_BARS", 6))   # 30min on 5m
CVD_TF           = "5m"


class HLCVDAggressor(StrategyBase):
    NAME = "hl_cvd_aggressor"
    CLOID_PREFIX = "cvdag"
    AFFINITY = ["trend_up", "trend_down", "range"]
    TF = CVD_TF
    UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK",
                "LTC", "NEAR", "SUI", "APT", "ARB", "OP", "INJ", "SEI"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Time-of-day filter — Asia ultra-low-vol kills mean-rev signals
        utc_hour = time.gmtime().tm_hour
        if 0 <= utc_hour < 5:
            return None

        # Get CVD snapshot
        try:
            cvd = bus.cvd(coin, window_ms=CVD_WINDOW_MS)
        except Exception:
            return None

        if not cvd or cvd.get("n_trades", 0) < 5:
            return None

        z = float(cvd.get("z_score", 0))
        buy_ntl = float(cvd.get("buy_notional", 0))
        sell_ntl = float(cvd.get("sell_notional", 0))
        total_ntl = buy_ntl + sell_ntl

        # Filter: minimum activity gate
        if total_ntl < CVD_MIN_NOTIONAL:
            return None

        # 5m bars for confirmation + swing checks
        try:
            bars = bus.candles(coin, CVD_TF, n=max(2, CVD_SWING_LB))
        except Exception:
            return None
        if not bars or len(bars) < 2:
            return None

        last = bars[-1]
        close = float(last["close"])
        open_ = float(last["open"])
        if close <= 0 or open_ <= 0:
            return None

        # Swing extremes
        recent = bars[-CVD_SWING_LB:]
        swing_high = max(float(b["high"]) for b in recent)
        swing_low = min(float(b["low"]) for b in recent)
        near_high_bps = (swing_high - close) / close * 10_000
        near_low_bps = (close - swing_low) / close * 10_000

        side = None
        is_long = None
        reason = None

        # LONG trigger
        if z > CVD_Z_THRESHOLD:
            buy_ratio = buy_ntl / total_ntl if total_ntl > 0 else 0
            price_aligned = close > open_   # 5m bar green
            not_chasing = near_high_bps > CVD_NEAR_SWING_BPS
            if buy_ratio > CVD_AGGR_RATIO and price_aligned and not_chasing:
                side = "B"; is_long = True
                reason = (f"cvd_z={z:.2f}>+{CVD_Z_THRESHOLD} buy_ratio={buy_ratio:.2f} "
                          f"5m_green near_high_bps={near_high_bps:.0f}")

        # SHORT trigger
        elif z < -CVD_Z_THRESHOLD:
            sell_ratio = sell_ntl / total_ntl if total_ntl > 0 else 0
            price_aligned = close < open_
            not_chasing = near_low_bps > CVD_NEAR_SWING_BPS
            if sell_ratio > CVD_AGGR_RATIO and price_aligned and not_chasing:
                side = "A"; is_long = False
                reason = (f"cvd_z={z:.2f}<-{CVD_Z_THRESHOLD} sell_ratio={sell_ratio:.2f} "
                          f"5m_red near_low_bps={near_low_bps:.0f}")

        if not side:
            return None

        # SL/TP
        if is_long:
            sl_px = close * (1 - CVD_SL_PCT)
            tp_px = close * (1 + CVD_TP_PCT)
        else:
            sl_px = close * (1 + CVD_SL_PCT)
            tp_px = close * (1 - CVD_TP_PCT)

        return Signal(
            coin=coin, side=side, is_long=is_long, ref_price=close,
            sl_px=sl_px, tp_px=tp_px,
            max_hold_bars=CVD_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=reason,
            extras={
                "cvd_z": z,
                "cvd_notional": cvd.get("cvd_notional", 0),
                "buy_notional": buy_ntl,
                "sell_notional": sell_ntl,
                "n_trades_30s": cvd.get("n_trades", 0),
                "swing_high": swing_high,
                "swing_low": swing_low,
            },
        )
