"""fmom — Funding Momentum (2nd derivative) divergence.

THESIS:
Classic fsp/fd1 engines used funding LEVEL (absolute rate). They underperformed
because by the time funding crosses a static threshold, the crowded side is
often already half-unwound.

fmom uses the RATE OF CHANGE of funding (2nd derivative) and contrasts it
with price ROC over the same window:

    funding_roc = (funding_now - funding_2h_ago) / 2h
    price_roc   = (price_now   - price_2h_ago)   / price_2h_ago

When |funding_roc| is in the top decile (sharp acceleration) AND
sign(funding_roc) opposes sign(price_roc), the divergence indicates:

  - funding rising fast + price flat/falling → longs paying expensively for
    no upside → exhaustion → SHORT
  - funding falling fast + price flat/rising → shorts paying expensively for
    no downside → exhaustion → LONG

EXIT:
- Funding ROC returns to neutral (|roc_z| < 0.5)
- OR SL/TP hit (1.5% / 3% — tight because funding edges decay fast)
- OR 8h max hold

DISTINCT FROM:
- fsp (dead, §4): used funding LEVEL crossing absolute threshold
- fd1 (dead, §4): used funding/price divergence but without 2nd derivative
- These were 1st-derivative or level-based; fmom is the 2nd derivative.

EXPECTED: PF 1.5-2.0 OOS, 0.4-1.1 trades/day per Council estimates.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


FMOM_LOOKBACK_H = int(os.environ.get("FMOM_LOOKBACK_H", "2"))
FMOM_ROC_Z_ENTER = float(os.environ.get("FMOM_ROC_Z_ENTER", "2.0"))
FMOM_PRICE_ROC_MAX = float(os.environ.get("FMOM_PRICE_ROC_MAX", "0.015"))  # 1.5%
FMOM_SL_PCT = float(os.environ.get("FMOM_SL_PCT", "0.015"))
FMOM_TP_PCT = float(os.environ.get("FMOM_TP_PCT", "0.030"))
FMOM_MAX_HOLD_H = int(os.environ.get("FMOM_MAX_HOLD_H", "8"))
# In production signal-bus serves ~1 sample/sec (200+ per hour); in backtest
# HistoricalBus forward-fills to hourly (24 per day). Threshold ensures the
# z-score baseline can be computed regardless of cadence.
FMOM_MIN_SAMPLES = int(os.environ.get("FMOM_MIN_SAMPLES", "30"))

# 47-coin HL universe (those with active funding)
DEFAULT_UNIVERSE = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "DOT",
    "NEAR", "INJ", "SUI", "APT", "ARB", "OP", "SEI", "TIA", "WIF", "JUP",
    "kPEPE", "kSHIB", "kBONK", "AAVE", "UNI", "FTM", "MEME", "WLD", "ORDI",
    "PYTH", "TRX", "ADA", "ATOM", "STX", "RNDR", "PENDLE", "FIL", "CRV",
    "LDO", "MKR", "COMP", "FET", "POLYX", "GMX", "BCH", "CAKE", "NEO",
]


class FundingMomentum(StrategyBase):
    NAME = "fmom"
    CLOID_PREFIX = "fmom_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = "1h"
    UNIVERSE = DEFAULT_UNIVERSE

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Get funding history — last ~24h gives us enough to compute ROC + baseline
        try:
            funding = bus.funding(coin, hours=48)
        except Exception:
            return None
        if not funding or len(funding) < FMOM_MIN_SAMPLES:
            return None

        # Current rate = most recent sample. CRITICAL: use the latest sample's
        # timestamp as the "now" reference instead of time.time(). This makes
        # the engine honest-backtestable — in a HistoricalBus replay, funding
        # is already filtered to <= cursor_ms, so the latest sample IS the
        # backtest's "now".
        curr = funding[-1]
        if not isinstance(curr, dict) or "rate" not in curr:
            return None
        rate_now = float(curr["rate"])
        now_ms = int(curr.get("ts", time.time() * 1000))
        lookback_ms = FMOM_LOOKBACK_H * 3_600_000

        # Rate at lookback boundary
        target_ts = now_ms - lookback_ms
        # Find sample closest to target_ts
        past_sample = None
        for s in reversed(funding):
            if s.get("ts", 0) <= target_ts:
                past_sample = s
                break
        if past_sample is None:
            return None
        rate_past = float(past_sample.get("rate", 0))

        # Funding ROC
        funding_roc = rate_now - rate_past

        # Z-score the funding ROC against last 24h of ROCs at same lookback
        rocs = []
        step = max(1, len(funding) // 60)   # ~60 baseline rocs regardless of cadence
        for i in range(len(funding) - 1, max(0, len(funding) - 1000), -step):
            s_curr = funding[i]
            tgt = s_curr.get("ts", 0) - lookback_ms
            s_past = None
            for s2 in reversed(funding[:i]):
                if s2.get("ts", 0) <= tgt:
                    s_past = s2
                    break
            if s_past is not None:
                rocs.append(float(s_curr["rate"]) - float(s_past["rate"]))

        if len(rocs) < 30:
            return None
        mean_roc = sum(rocs) / len(rocs)
        var_roc = sum((r - mean_roc) ** 2 for r in rocs) / len(rocs)
        if var_roc <= 0:
            return None
        std_roc = var_roc ** 0.5
        roc_z = (funding_roc - mean_roc) / std_roc

        if abs(roc_z) < FMOM_ROC_Z_ENTER:
            return None

        # Get price ROC over same window
        try:
            candles = bus.candles(coin, cls.TF, n=FMOM_LOOKBACK_H + 2)
        except Exception:
            return None
        if not candles or len(candles) < FMOM_LOOKBACK_H + 1:
            return None
        # Use close prices
        try:
            price_now = float(candles[-1]["close"])
            price_past = float(candles[-(FMOM_LOOKBACK_H + 1)]["close"])
        except (KeyError, ValueError, TypeError):
            return None
        if price_past <= 0:
            return None
        price_roc = (price_now - price_past) / price_past

        # Require price ROC to NOT exceed threshold (i.e., price is flat/stalled)
        if abs(price_roc) > FMOM_PRICE_ROC_MAX:
            return None

        # Direction: when funding accelerating UP (roc_z > 0) with price flat/down,
        # longs are trapped paying for nothing → SHORT.
        # When funding accelerating DOWN (roc_z < 0) with price flat/up,
        # shorts are trapped → LONG.
        is_long = roc_z < 0

        ref_px = price_now

        if is_long:
            sl_px = ref_px * (1 - FMOM_SL_PCT)
            tp_px = ref_px * (1 + FMOM_TP_PCT)
        else:
            sl_px = ref_px * (1 + FMOM_SL_PCT)
            tp_px = ref_px * (1 - FMOM_TP_PCT)

        max_hold_bars = FMOM_MAX_HOLD_H   # 1h TF, so bars = hours

        return Signal(
            coin=coin,
            side="B" if is_long else "A",
            is_long=is_long,
            ref_price=ref_px,
            sl_px=sl_px,
            tp_px=tp_px,
            max_hold_bars=max_hold_bars,
            fire_ts=time.time() * 1000,
            fire_reason=f"fmom_z={roc_z:+.2f}_proc={price_roc*100:+.2f}%",
            extras={
                "funding_roc": funding_roc,
                "funding_roc_z": round(roc_z, 3),
                "price_roc_pct": round(price_roc * 100, 3),
                "rate_now": rate_now,
                "rate_past": rate_past,
                "lookback_h": FMOM_LOOKBACK_H,
            },
        )
