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
# Council Q3 (2026-05-18): unanimous on flat-funding whipsaw as residual
# failure mode after the 3 over-firing bugs were fixed. Require minimum
# 24h funding volatility (std of funding-rate samples) before allowing
# signal — otherwise we are firing on noise in a regime with no edge.
FMOM_MIN_FUNDING_VOL = float(os.environ.get("FMOM_MIN_FUNDING_VOL", "0.0005"))  # 5bps

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

        # ─── Pre-extract timestamps + rates into parallel arrays ───
        # PERF FIX 2026-05-18 (sentinel-flagged by Qwen3 Coder 480B):
        # Prior code did `for s2 in reversed(funding[:i])` inside a loop over
        # ~100 outer iterations. At HL's ~1 sample/sec cadence (N=83k for 24h),
        # the slice copy alone was O(N) per iter, plus an O(N) reverse scan.
        # That's ~8.3M ops per evaluate(), called for 47 coins every 5min.
        #
        # NEW: use bisect on a single pre-built timestamp array. O(N) one-time
        # extraction + O(log N) lookup per baseline ROC = ~100×log(83k) ≈ 1700
        # ops for the baseline loop, vs 8.3M before. ~5000× speedup.
        import bisect
        try:
            ts_list = [int(s.get("ts", 0)) for s in funding]
            rate_list = [float(s.get("rate", 0)) for s in funding]
        except (KeyError, ValueError, TypeError):
            return None
        if len(ts_list) < FMOM_MIN_SAMPLES or ts_list[-1] <= 0:
            return None

        # Current rate = most recent sample. CRITICAL: use the latest sample's
        # timestamp as the "now" reference (honest-backtestable: HistoricalBus
        # filters funding to <= cursor_ms, so latest sample IS backtest's "now").
        rate_now = rate_list[-1]
        now_ms = ts_list[-1]
        lookback_ms = FMOM_LOOKBACK_H * 3_600_000

        # Rate at lookback boundary — bisect for last sample with ts <= target
        target_ts = now_ms - lookback_ms
        idx_past = bisect.bisect_right(ts_list, target_ts) - 1
        if idx_past < 0:
            return None
        rate_past = rate_list[idx_past]
        funding_roc = rate_now - rate_past

        # ─── Z-score baseline (FIX 2026-05-18 #1: full 24h window) ───
        # PRIOR BUG: old code sampled only last 1000 funding entries (~17min).
        # All 60 baseline ROCs shared roughly the same 2h denominator anchor,
        # only the numerator varying across 17min. Std artificially small →
        # every funding movement looked like a 2σ event = 10-30× over-firing.
        # FIX: sample across the FULL funding history (24h+), ~100 baseline
        # ROCs distributed across the window.
        total = len(funding)
        step = max(1, total // 100)
        # Earliest index where we have lookback_ms of history before it
        target_first = ts_list[0] + lookback_ms
        min_required_idx = bisect.bisect_left(ts_list, target_first)
        rocs = []
        for i in range(total - 1, min_required_idx, -step):
            tgt = ts_list[i] - lookback_ms
            j = bisect.bisect_right(ts_list, tgt, 0, i) - 1
            if j >= 0:
                rocs.append(rate_list[i] - rate_list[j])

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

        # ── Flat-funding whipsaw filter (council Q3 2026-05-18) ──
        # Funding-rate std over the 24h window must exceed FMOM_MIN_FUNDING_VOL.
        # In low-funding-vol regimes, the 2nd-derivative signal whipsaws on
        # micro-fluctuations even when normalized by z-score. Add an absolute
        # volatility floor (not just relative-to-self z-score) to bypass noise.
        if FMOM_MIN_FUNDING_VOL > 0:
            try:
                funding_vol = (sum((r - sum(rate_list) / len(rate_list)) ** 2
                                    for r in rate_list) / len(rate_list)) ** 0.5
                if funding_vol < FMOM_MIN_FUNDING_VOL:
                    return None
            except (ZeroDivisionError, ValueError, TypeError):
                pass  # malformed — fall through rather than block

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

        # ─── Divergence gates (FIX 2026-05-18 #2: enforce TRUE divergence) ───
        # PRIOR BUG: thesis requires funding_roc and price_roc to be in
        # OPPOSITE directions (true divergence). Old code only enforced
        # |price_roc| < threshold (magnitude bound), allowing same-sign
        # CONFIRMATION pairs to fire as if they were divergence trades.
        # Live evidence: TIA fired SHORT with funding↑ AND price↑ (+0.74%),
        # SUI fired LONG with funding↓ AND price↓ (-1.12%).
        #
        # FIX (a): keep the magnitude bound (price must be flat).
        # FIX (b): require funding_roc × price_roc < 0 (opposite signs) for
        # genuine divergence. Same-sign pairs are confirmation, not exhaustion.
        if abs(price_roc) > FMOM_PRICE_ROC_MAX:
            return None
        # True-divergence guard: only fire when funding moves OPPOSITE to price
        # (i.e., funding rising while price flat/falling, or vice versa).
        # When price_roc is exactly zero we let it pass — that's the cleanest
        # exhaustion setup.
        if abs(price_roc) > 1e-6 and (funding_roc * price_roc) > 0:
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

    @classmethod
    def should_close(cls, trade_row, bus) -> tuple[bool, str]:
        """Early exit when funding-ROC edge dissipates.

        Implements the exit thesis: "Funding ROC returns to neutral
        (|roc_z| < 0.5)". The whole edge is that the funding-paying side
        capitulates fast; once funding pressure normalizes, the predictive
        signal is gone and we should not be in the trade waiting for SL/TP.

        Also detects sign-flip: if funding_roc reverses direction, the
        original setup is invalidated.

        FIX 2026-05-18: was missing entirely; engine relied purely on
        SL/TP/timeout despite the thesis explicitly calling for an
        edge-decay exit.
        """
        FMOM_Z_EXIT = float(os.environ.get("FMOM_Z_EXIT", "0.5"))
        try:
            coin = trade_row["coin"]
            funding = bus.funding(coin, hours=48)
        except Exception:
            return (False, "")
        if not funding or len(funding) < FMOM_MIN_SAMPLES:
            return (False, "")

        # Recompute current roc_z using the SAME methodology as evaluate()
        # PERF FIX 2026-05-18: bisect-based lookups (was O(N²) reverse scans)
        try:
            import bisect
            try:
                ts_list = [int(s.get("ts", 0)) for s in funding]
                rate_list = [float(s.get("rate", 0)) for s in funding]
            except (KeyError, ValueError, TypeError):
                return (False, "")
            if len(ts_list) < FMOM_MIN_SAMPLES or ts_list[-1] <= 0:
                return (False, "")

            rate_now = rate_list[-1]
            now_ms = ts_list[-1]
            lookback_ms = FMOM_LOOKBACK_H * 3_600_000

            target_ts = now_ms - lookback_ms
            idx_past = bisect.bisect_right(ts_list, target_ts) - 1
            if idx_past < 0:
                return (False, "")
            rate_past = rate_list[idx_past]
            funding_roc = rate_now - rate_past

            # Baseline ROCs (same window-wide sampling as evaluate)
            total = len(funding)
            step = max(1, total // 100)
            target_first = ts_list[0] + lookback_ms
            min_required_idx = bisect.bisect_left(ts_list, target_first)
            rocs = []
            for i in range(total - 1, min_required_idx, -step):
                tgt = ts_list[i] - lookback_ms
                j = bisect.bisect_right(ts_list, tgt, 0, i) - 1
                if j >= 0:
                    rocs.append(rate_list[i] - rate_list[j])

            if len(rocs) < 30:
                return (False, "")
            mean_roc = sum(rocs) / len(rocs)
            var_roc = sum((r - mean_roc) ** 2 for r in rocs) / len(rocs)
            if var_roc <= 0:
                return (False, "")
            std_roc = var_roc ** 0.5
            current_z = (funding_roc - mean_roc) / std_roc

            # Edge dissipated (z returned to neutral)
            if abs(current_z) < FMOM_Z_EXIT:
                return (True, f"fmom_z_neutral_{current_z:+.2f}")

            # Sign flip vs entry setup
            import json as _json
            try:
                extras_blob = trade_row["extras_json"]
                if isinstance(extras_blob, str):
                    extras_blob = _json.loads(extras_blob or "{}")
                inner = extras_blob.get("extras", {}) if isinstance(extras_blob, dict) else {}
                original_z = float(inner.get("funding_roc_z", 0))
                if original_z != 0 and (original_z * current_z) < 0:
                    return (True, f"fmom_z_flipped_orig={original_z:+.2f}_now={current_z:+.2f}")
            except Exception:
                pass
        except Exception:
            pass
        return (False, "")
