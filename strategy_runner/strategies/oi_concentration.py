"""oi_concentration — Pre-Cascade OI Concentration Detector.

v2 (2026-05-18): UPGRADED FROM VOLUME-PROXY TO REAL OI.

Council 5/5 unanimous (2026-05-18): v1 volume-proxy was a structural defect.
Activation was gated on real OI feed. signal-bus now exposes real
openInterest via HL metaAndAssetCtxs (5min poll, 30d in-memory history).

THESIS (unchanged):
When OI is at recent extremes (top decile) AND price is near a major
S/R level, conditions for cascading liquidation exist. Crowded side
(longs near support / shorts near resistance) is at risk of forced unwind.

SIGNAL:
- TF: 1h (for S/R levels via swing high/low)
- OI source: bus.oi(coin) returns 30d × 288 (5min) snapshots
- Compute current_oi percentile vs 30d distribution
- Require pctile >= OIC_OI_PCTILE (default 0.90)
- Require price within OIC_PROXIMITY_PCT (1%) of swing high OR low (48-bar)

DIRECTION (fade the imminent cascade):
- Near support + extreme OI = longs trapped → SHORT
- Near resistance + extreme OI = shorts trapped → LONG

EXIT:
- TP at 3% (cascade target)
- SL at 1.2% beyond entry
- Max hold: 12h

UNIVERSE: top-20 by HL OI (highest cascade risk).
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


# Renamed from OIC_VOLUME_PCTILE (real OI now). Backward-compat env var read.
OIC_OI_PCTILE = float(os.environ.get("OIC_OI_PCTILE",
                       os.environ.get("OIC_VOLUME_PCTILE", "0.90")))
OIC_SWING_LOOKBACK = int(os.environ.get("OIC_SWING_LOOKBACK", "48"))
OIC_PROXIMITY_PCT = float(os.environ.get("OIC_PROXIMITY_PCT", "0.01"))
OIC_SL_PCT = float(os.environ.get("OIC_SL_PCT", "0.012"))
OIC_TP_PCT = float(os.environ.get("OIC_TP_PCT", "0.030"))
OIC_MAX_HOLD_BARS = int(os.environ.get("OIC_MAX_HOLD_BARS", "12"))
OIC_MIN_OI_SAMPLES = int(os.environ.get("OIC_MIN_OI_SAMPLES", "2000"))


DEFAULT_UNIVERSE = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "DOT",
    "NEAR", "INJ", "SUI", "APT", "ARB", "OP", "SEI", "TIA", "WIF", "JUP",
]


class OIConcentration(StrategyBase):
    NAME = "oi_concentration"
    CLOID_PREFIX = "oicon_"
    AFFINITY = ["high_vol", "range", "chop"]
    TF = "1h"
    UNIVERSE = DEFAULT_UNIVERSE

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # 1h candles for S/R only
        candles_needed = OIC_SWING_LOOKBACK + 4
        try:
            candles = bus.candles(coin, cls.TF, n=candles_needed)
        except Exception:
            return None
        if not candles or len(candles) < candles_needed:
            return None

        bar = candles[-2]
        try:
            c = float(bar["close"])
        except (KeyError, ValueError, TypeError):
            return None
        if c <= 0:
            return None

        # ── Real OI percentile gate (v2: replaces v1 volume-proxy) ──
        try:
            oi_history = bus.oi(coin)
        except Exception:
            return None
        if not oi_history or len(oi_history) < OIC_MIN_OI_SAMPLES:
            # Not enough data — be conservative and skip rather than fire on
            # short history. Once signal-bus has been up ~7d this auto-clears.
            return None

        try:
            current_oi = float(oi_history[-1]["oi"])
            historical_ois = [float(r["oi"]) for r in oi_history[:-1] if r.get("oi") is not None]
        except (KeyError, ValueError, TypeError):
            return None
        if current_oi <= 0 or len(historical_ois) < OIC_MIN_OI_SAMPLES - 1:
            return None

        # Percentile: fraction of historical values <= current
        sorted_ois = sorted(historical_ois)
        # bisect-style lookup for percentile
        lo, hi = 0, len(sorted_ois)
        while lo < hi:
            mid = (lo + hi) // 2
            if sorted_ois[mid] <= current_oi:
                lo = mid + 1
            else:
                hi = mid
        pctile = lo / len(sorted_ois) if sorted_ois else 0.0
        if pctile < OIC_OI_PCTILE:
            return None

        # ── Swing levels ──
        prior = candles[-(OIC_SWING_LOOKBACK + 2):-2]
        try:
            swing_low = min(float(b["low"]) for b in prior)
            swing_high = max(float(b["high"]) for b in prior)
        except (KeyError, ValueError, TypeError):
            return None

        dist_to_support = (c - swing_low) / c
        dist_to_resistance = (swing_high - c) / c

        # ── Direction (fade the trapped side) ──
        if 0 <= dist_to_support < OIC_PROXIMITY_PCT:
            is_long = False
            sl_px = c * (1 + OIC_SL_PCT)
            tp_px = c * (1 - OIC_TP_PCT)
            reason = f"oic_near_support_d={dist_to_support*100:.2f}%_oi_pct={pctile*100:.0f}"
            side_setup = "support_short"
            dist = dist_to_support
        elif 0 <= dist_to_resistance < OIC_PROXIMITY_PCT:
            is_long = True
            sl_px = c * (1 - OIC_SL_PCT)
            tp_px = c * (1 + OIC_TP_PCT)
            reason = f"oic_near_resistance_d={dist_to_resistance*100:.2f}%_oi_pct={pctile*100:.0f}"
            side_setup = "resistance_long"
            dist = dist_to_resistance
        else:
            return None

        return Signal(
            coin=coin,
            side="B" if is_long else "A",
            is_long=is_long,
            ref_price=c,
            sl_px=sl_px,
            tp_px=tp_px,
            max_hold_bars=OIC_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=reason,
            extras={
                "swing_low": swing_low,
                "swing_high": swing_high,
                "dist_to_level_pct": round(dist * 100, 3),
                "side_setup": side_setup,
                "oi_pctile": round(pctile, 3),
                "current_oi": current_oi,
                "oi_samples": len(historical_ois),
            },
        )
