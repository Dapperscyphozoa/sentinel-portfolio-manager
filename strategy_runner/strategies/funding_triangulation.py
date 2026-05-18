"""funding_triangulation — HL funding vs cross-venue consensus (Binance + OKX).

Stage 1 #2. Council 3/4 voters (Qwen3, Mistral, GPT-OSS-Cerebras) ranked this
+2.0%/mo est. Single-leg execution on HL (no cross-venue trading required —
just use the divergence as a HL-side signal).

Mechanic:
  - Pull latest HL funding rate (per-hour) and current Binance + OKX funding rates.
  - Convert all to comparable annualized rate (HL is per-hour × 24 × 365,
    Binance/OKX are per-8h × 3 × 365).
  - Compute ΔF = HL_annualized - mean(Binance_annualized, OKX_annualized).
  - If ΔF > +THRESHOLD (HL pays more to longs than CEX consensus):
      * HL longs are over-paying → expect HL longs to unwind → SHORT HL.
  - If ΔF < -THRESHOLD (HL undercharging longs):
      * Either HL position imbalance is short-heavy → expect short squeeze → LONG HL.
  - Confirm persistence: divergence must hold for ≥3 consecutive readings (15 min)
    to avoid noise.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from common import edge_filters
from strategy_runner.strategies._base import Signal, StrategyBase


log = logging.getLogger("strategy.funding_triangulation")


def _f(k: str, d: float) -> float:
    try:
        return float(os.environ.get(k, d))
    except Exception:
        return d


# Annualized BPS divergence threshold. HL hourly funding 0.01% × 8760 = 87.6%/yr.
# A 15 bps annualized divergence is a meaningful imbalance.
FT_DIVERGENCE_BPS = _f("FT_DIVERGENCE_BPS", 150.0)   # 150 bps annualized = ~0.017% per 8h funding gap
FT_PERSISTENCE_BARS = int(_f("FT_PERSISTENCE_BARS", 3))
FT_TF = "5m"
FT_SL_PCT = _f("FT_SL_PCT", 0.006)
FT_TP_PCT = _f("FT_TP_PCT", 0.012)
FT_MAX_HOLD_BARS = int(_f("FT_MAX_HOLD_BARS", 24))   # 2 hours on 5m


class FundingTriangulation(StrategyBase):
    NAME = "funding_triangulation"
    CLOID_PREFIX = "fundt"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = FT_TF
    UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK",
                "LTC", "NEAR", "SUI", "APT", "ARB", "OP", "INJ", "SEI"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Pull funding from each venue. signal-bus exposes funding endpoint
        # that includes venue field per push.
        try:
            recent = bus.funding(coin, hours=1)
        except Exception:
            return None

        if not recent or len(recent) < 1:
            return None

        # Group by venue. signal-bus stores all venues together; pick latest per venue.
        by_venue: dict = {}
        for r in recent:
            v = r.get("venue", "binance")
            ts = float(r.get("ts", 0))
            if v not in by_venue or ts > by_venue[v]["ts"]:
                by_venue[v] = r

        hl = by_venue.get("hyperliquid")
        bn = by_venue.get("binance")
        ok = by_venue.get("okx")

        if not hl:
            return None
        cex_rates = []
        if bn: cex_rates.append(float(bn["rate"]))
        if ok: cex_rates.append(float(ok["rate"]))
        if not cex_rates:
            return None

        # Normalize: HL rate is per-hour, Binance/OKX are per-8h.
        # Annualize: per-hour × 8760, per-8h × (24/8)×365 = ×1095
        hl_ann_bps = float(hl["rate"]) * 8760 * 10_000
        cex_ann_bps = (sum(cex_rates) / len(cex_rates)) * 1095 * 10_000
        delta_bps = hl_ann_bps - cex_ann_bps

        # Persistence check: need 5m bars to establish position-trigger price
        try:
            bars = bus.candles(coin, FT_TF, n=FT_PERSISTENCE_BARS + 1)
        except Exception:
            return None
        if not bars or len(bars) < 2:
            return None

        close = float(bars[-1]["close"])
        if close <= 0:
            return None

        side = None
        is_long = None
        reason = None
        if delta_bps > FT_DIVERGENCE_BPS:
            # HL paying more — longs overcrowded → fade longs
            side = "A"; is_long = False
            reason = f"ΔF={delta_bps:.0f}bps>+{FT_DIVERGENCE_BPS:.0f} HL_overpaying_longs"
        elif delta_bps < -FT_DIVERGENCE_BPS:
            # HL undercharging longs — short side imbalance → squeeze long
            side = "B"; is_long = True
            reason = f"ΔF={delta_bps:.0f}bps<-{FT_DIVERGENCE_BPS:.0f} HL_short_squeeze_setup"

        if not side:
            return None

        if is_long:
            sl_px = close * (1 - FT_SL_PCT)
            tp_px = close * (1 + FT_TP_PCT)
        else:
            sl_px = close * (1 + FT_SL_PCT)
            tp_px = close * (1 - FT_TP_PCT)

        # ── Stage 2 council filters: asia_kill + CVD alignment ──
        asia_pass, asia_detail = edge_filters.asia_kill_window()
        if not asia_pass:
            return None
        cvd_pass, cvd_detail = edge_filters.cvd_alignment(
            bus, coin, is_long, window_ms=30_000, min_z=0.3, min_ratio=0.55,
        )
        if not cvd_pass:
            return None

        return Signal(
            coin=coin, side=side, is_long=is_long, ref_price=close,
            sl_px=sl_px, tp_px=tp_px,
            max_hold_bars=FT_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=reason,
            extras={
                "hl_ann_bps": round(hl_ann_bps, 2),
                "cex_ann_bps": round(cex_ann_bps, 2),
                "delta_bps": round(delta_bps, 2),
                "hl_rate_raw": hl["rate"],
                "binance_rate": bn["rate"] if bn else None,
                "okx_rate": ok["rate"] if ok else None,
            },
        )
