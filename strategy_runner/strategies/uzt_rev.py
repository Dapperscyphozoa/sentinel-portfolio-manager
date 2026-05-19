"""UZT_REV — Unified Zone Trading, reversal-only ship config (v3).

Derived from `uzt.py` (Lesson #2 bidirectional implementation, RED per §1.5
honest backtest). v3 strips the strategy to its winning subset:

  - REVERSAL path only (CONTINUATION path confirmed dead on perps across
    28 exit-policy variants; all CON variants negative-EV per
    exit_sweep_120d_x_20).
  - Single TP at 5R (B3 policy). No partial scale, no BE move.
  - Signal SL = sweep-wick + 0.03% buffer (as in v1 REV branch).
  - 40-bar hard time stop (10h on 15m).
  - Asia hours blocked (00-05h UTC) — fire-time filter, microstructure story
    (US/EU MM rotation hands liquidity to thin Asia tape, sweep noise > signal).
  - 16-coin tier-1 universe (KEEP cohort with positive per-coin Total R
    across 120d×30 backtest).

Top-30 backtest result (OKX SWAP, 120d, B3, Asia filter):
  n=41, WR 68.3%, PF 6.92, expectancy +1.707R/trade, Total +69.97R.

Three-sample consistency (90d×20 PF 5.18 → 120d×20 PF 5.69 → 120d×30 PF 6.92).

Loaded via STRATEGY_UZT_REV_ENABLED=1; paper mode via STRATEGY_UZT_REV_LIVE=0.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase
# Reuse identification + state-machine helpers from the v1 module. The helpers
# are pure functions over bar data and contain no v1-specific exit logic; we
# override exit policy at the strategy-class level below.
from .uzt import (
    _aggregate_15m_to_4h,
    _find_zones,
    _evaluate_zone_state,
)


def _envi(k, d): return int(os.environ.get(k, d))
def _envf(k, d): return float(os.environ.get(k, d))
def _envb(k, d): return os.environ.get(k, str(d)).lower() in ("1", "true", "yes", "on")


# ── v3 ship-config parameters (env-overridable) ──
UZT_REV_HTF_PIVOT_LB    = _envi("UZT_REV_HTF_PIVOT_LB", 5)
UZT_REV_HTF_DISP_ATR    = _envf("UZT_REV_HTF_DISP_ATR", 1.5)
UZT_REV_LTF_BREAK_ATR   = _envf("UZT_REV_LTF_BREAK_ATR", 1.2)
UZT_REV_LTF_APPROACH    = _envf("UZT_REV_LTF_APPROACH_PCT", 0.03)
UZT_REV_LTF_RETEST_TOL  = _envf("UZT_REV_LTF_RETEST_TOL_PCT", 0.005)
UZT_REV_LTF_VOL_MULT    = _envf("UZT_REV_LTF_VOL_MULT", 0.7)
UZT_REV_WHIPSAW_BARS    = _envi("UZT_REV_WHIPSAW_COOLDOWN_BARS", 2)
UZT_REV_HOLD_MAX_BARS   = _envi("UZT_REV_HOLD_MAX_BARS", 40)   # 10h on 15m
UZT_REV_MAX_ZONES       = _envi("UZT_REV_MAX_ZONES_TO_SCAN", 6)
UZT_REV_TP_R            = _envf("UZT_REV_TP_R", 5.0)
UZT_REV_BLOCK_ASIA      = _envb("UZT_REV_BLOCK_ASIA", True)
UZT_REV_ASIA_START_H    = _envi("UZT_REV_ASIA_START_H", 0)    # UTC hour, inclusive
UZT_REV_ASIA_END_H      = _envi("UZT_REV_ASIA_END_H", 5)      # UTC hour, exclusive


def _in_asia_window(ts_ms: int) -> bool:
    """True if ts falls in 00:00-05:00 UTC (the blocked window)."""
    h = time.gmtime(ts_ms / 1000).tm_hour
    return UZT_REV_ASIA_START_H <= h < UZT_REV_ASIA_END_H


class UZT_REV(StrategyBase):
    """Reversal-only ship config. REV path of UZT, single 5R TP, Asia blocked."""

    NAME = "uzt_rev"
    CLOID_PREFIX = "uztrv_"
    # Bidirectional engine — fires REV in either direction. PM gate
    # filters by per-trade regime alignment via trend_direction_aware.
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = "15m"

    # 16-coin tier-1 universe (positive per-coin Total R across 120d×30
    # backtest). Tier-2 WATCH coin AVAX retained — telemetry will decide
    # admission. Blocked coins (BTC, XRP, JUP, AAVE, TIA, COMP) excluded
    # at universe level — re-admit individually at n=5 each post-live.
    UNIVERSE = [
        "UNI", "ETH", "ATOM", "FIL", "BNB", "LTC", "NEAR", "SOL",
        "APT", "ARB", "WIF", "DOGE", "DOT", "SUI", "APE",
        "AVAX",
    ]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Pull ~20 days of 15m so 4h aggregation has ≥30 HTF bars.
        bars_15m = bus.candles(coin, "15m", n=2000) or []
        if len(bars_15m) < 500:
            return None
        bars_4h = _aggregate_15m_to_4h(bars_15m)
        if len(bars_4h) < 30:
            return None

        zones = _find_zones(bars_4h, UZT_REV_HTF_PIVOT_LB, UZT_REV_HTF_DISP_ATR)
        if not zones:
            return None
        zones = sorted(zones, key=lambda z: z.formed_ts)[-UZT_REV_MAX_ZONES:]

        last_bar = bars_15m[-1]
        last_close = last_bar["close"]

        near = [z for z in zones if abs(z.mid - last_close) / last_close < 0.06]
        if not near:
            return None

        # Asia filter: gate at fire time, not zone time. Block any fire whose
        # last 15m bar opens in 00-05 UTC.
        if UZT_REV_BLOCK_ASIA and _in_asia_window(last_bar["open_ts"]):
            return None

        for zone in near:
            fill = _evaluate_zone_state(
                zone, bars_15m,
                break_atr_mult=UZT_REV_LTF_BREAK_ATR,
                approach_pct=UZT_REV_LTF_APPROACH,
                retest_tol_pct=UZT_REV_LTF_RETEST_TOL,
                vol_mult=UZT_REV_LTF_VOL_MULT,
                whipsaw_cooldown_bars=UZT_REV_WHIPSAW_BARS,
            )
            if fill is None:
                continue

            # Drop CON fires — only ship REVERSAL.
            if fill.get("path") != "REV":
                continue

            is_long = bool(fill["is_long"])
            entry = float(fill["ref_price"])
            sl = float(fill["sl_px"])
            risk = (entry - sl) if is_long else (sl - entry)
            if risk <= 0:
                continue

            # Override TP: single 5R (v1 used 3R + scaling ladder).
            tp = entry + UZT_REV_TP_R * risk if is_long else entry - UZT_REV_TP_R * risk

            return Signal(
                coin=coin,
                side=("B" if is_long else "A"),
                is_long=is_long,
                ref_price=entry,
                sl_px=sl,
                tp_px=tp,
                max_hold_bars=UZT_REV_HOLD_MAX_BARS,
                fire_ts=float(last_bar["open_ts"]),
                fire_reason=fill["fire_reason"].replace("UZT_", "UZT_REV_v3_"),
                extras={
                    "zone_side": fill["zone_side"],
                    "path": "REV",
                    "tp_r": UZT_REV_TP_R,
                    "asia_blocked": False,
                    "audit_status": "PROVISIONAL",
                    "ship_version": "v3",
                },
            )

        return None
