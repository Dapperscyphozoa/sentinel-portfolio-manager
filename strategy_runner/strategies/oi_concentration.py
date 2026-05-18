"""oi_concentration — Pre-Cascade OI Concentration Detector.

THESIS:
When Open Interest is at recent extreme highs (top decile) AND price is
near a major support/resistance level, the conditions for a cascading
liquidation are present. The crowded side (longs near support, shorts near
resistance) is at risk of forced unwind.

This is a SIMPLIFIED v1 of the world-first oi_concentration concept.
Full spec uses HL per-wallet OI aggregation; that requires ~150 LOC of bus
infrastructure not yet built. v1 uses aggregate OI as proxy.

SIGNAL:
- TF: 1h
- Compute OI as proxy: (open_interest_now - OI_30d_ago_pct) percentile
  over 30d → flag as "extreme" if >90th percentile
  
  NOTE: Hyperliquid doesn't expose historical OI series via the standard
  candle endpoint. v1 PROXY: use volume-based proxy. When 24h volume is in
  top decile of 30d AND price is near major level, conditions are similar.

- Detect proximity to major S/R: same 48-bar swing high/low as stop_hunt
  but require price WITHIN 1% (tighter — anticipating cascade)

- Direction: fade the imminent cascade
  - Price near support + extreme volume = longs trapped → SHORT
    (when cascade triggers, price drops; entering SHORT pre-cascade)
  - Price near resistance + extreme volume = shorts trapped → LONG

EXIT:
- TP at 2-3% (cascade move target)
- SL at 1% beyond the S/R level (if level holds, no cascade, exit)
- Max hold: 12h

UNIVERSE: top-20 coins by HL OI (typically highest cascade risk)

COUNCIL CAVEAT: "Cascades require a TRIGGER; concentration alone isn't
sufficient." This engine identifies SETUP, not trigger. Expects to be
WRONG often (no cascade fires) but win big when cascade does fire.
Low frequency, asymmetric payoff.

EXPECTED: PF 2.0-3.5 (high R:R but rare fires).
Trades/day: 0.2-0.3 (per council).

v2 ROADMAP: integrate HL /info per-wallet positions for true concentration
metric. Currently proxied via volume percentile.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


OIC_VOLUME_PCTILE = float(os.environ.get("OIC_VOLUME_PCTILE", "0.90"))    # top 10% of 30d
OIC_SWING_LOOKBACK = int(os.environ.get("OIC_SWING_LOOKBACK", "48"))
OIC_PROXIMITY_PCT = float(os.environ.get("OIC_PROXIMITY_PCT", "0.01"))    # within 1% of level
OIC_SL_PCT = float(os.environ.get("OIC_SL_PCT", "0.012"))                  # 1.2% SL
OIC_TP_PCT = float(os.environ.get("OIC_TP_PCT", "0.030"))                  # 3% TP (1:2.5 RR)
OIC_MAX_HOLD_BARS = int(os.environ.get("OIC_MAX_HOLD_BARS", "12"))         # 12h


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
        # Need 30d × 24 = 720 bars for percentile + swing lookback
        bars_needed = 720 + OIC_SWING_LOOKBACK + 2
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
            v = float(bar.get("volume", 0))
        except (KeyError, ValueError, TypeError):
            return None
        if c <= 0:
            return None

        # ── Volume percentile gate (proxy for OI extreme) ──
        # Compute 24h rolling volume, then percentile over 30d
        try:
            volumes = [float(b.get("volume", 0)) for b in candles[:-1]]
        except (ValueError, TypeError):
            return None

        # 24h rolling sum at each point
        rolling_24h = []
        for i in range(24, len(volumes)):
            rolling_24h.append(sum(volumes[i - 24:i]))
        if len(rolling_24h) < 100:
            return None

        current_24h_vol = sum(volumes[-24:])
        # Percentile of current vs distribution
        sorted_vols = sorted(rolling_24h)
        pctile_idx = 0
        for i, val in enumerate(sorted_vols):
            if val <= current_24h_vol:
                pctile_idx = i
        pctile = pctile_idx / len(sorted_vols)
        if pctile < OIC_VOLUME_PCTILE:
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

        # ── Direction logic ──
        # Near support + extreme volume = longs may be trapped → SHORT
        # Near resistance + extreme volume = shorts may be trapped → LONG
        if dist_to_support < OIC_PROXIMITY_PCT and dist_to_support >= 0:
            is_long = False
            # SL just above support (if it holds, cascade aborted)
            sl_px = c * (1 + OIC_SL_PCT)
            tp_px = c * (1 - OIC_TP_PCT)
            reason = f"oic_near_support_d={dist_to_support*100:.2f}%_vol_pct={pctile*100:.0f}"
            extras = {
                "swing_low": swing_low,
                "swing_high": swing_high,
                "dist_to_level_pct": round(dist_to_support * 100, 3),
                "side_setup": "support_short",
                "volume_pctile": round(pctile, 3),
                "current_24h_vol": current_24h_vol,
            }
        elif dist_to_resistance < OIC_PROXIMITY_PCT and dist_to_resistance >= 0:
            is_long = True
            sl_px = c * (1 - OIC_SL_PCT)
            tp_px = c * (1 + OIC_TP_PCT)
            reason = f"oic_near_resistance_d={dist_to_resistance*100:.2f}%_vol_pct={pctile*100:.0f}"
            extras = {
                "swing_low": swing_low,
                "swing_high": swing_high,
                "dist_to_level_pct": round(dist_to_resistance * 100, 3),
                "side_setup": "resistance_long",
                "volume_pctile": round(pctile, 3),
                "current_24h_vol": current_24h_vol,
            }
        else:
            return None  # not near any major level

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
            extras=extras,
        )
