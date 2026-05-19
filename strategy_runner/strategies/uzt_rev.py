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


def _compute_filter_telemetry(bus, coin: str, entry_px: float, zone_edge_px: float,
                              is_long: bool, fire_ts_ms: int) -> dict:
    """Snapshot filter-relevant signals at fire time.

    Recorded into Signal.extras for retrospective analysis once n grows past
    ~100 fires. Three filter hypotheses (per Session 8+ R&D memory):

      1. liq-density-near-zone — total $ of liqs in last N min, and the
         subset that occurred within ±0.5% of the zone edge price. Side-of-
         liq matters: longs liquidations (SELL side) near a long-zone edge
         signal exhaustion of the move into the zone → REV bias improves.
         Hypothesis: +5–10% WR.

      2. OI-delta-into-zone — net OI change over last N min. Falling OI
         while price moves to the zone = unwinding of the trapped side =
         REV-bias improves. Rising OI = fresh momentum, REV worse.
         Hypothesis: +10% WR.

      3. CVD-divergence — net aggressor flow vs price move. Aggressors
         buying/selling against the move into the zone is the classic
         exhaustion signature. Hypothesis: +5–15% WR.

    EVERY filter value is stored but the strategy STILL FIRES on the existing
    UZT_REV v3 condition. Filters are observed, not enforced. Re-evaluate at
    n≥100 fires per memory. Filter mechanism is written down BEFORE sign is
    chosen to avoid the inverted-sign bug noted in Session 8 lessons.
    """
    out: dict = {"telem_version": 1}

    # --- 1. liq-density (5/15/30 min, total and zone-proximal) ---
    try:
        for win_min in (5, 15, 30):
            since = fire_ts_ms - win_min * 60_000
            liqs = bus.liq(since_ms=since, coin=coin) or []
            total_usd = 0.0
            zone_usd = 0.0       # within ±0.5% of zone edge
            long_liq_usd = 0.0   # liqs of LONGS (SELL side in our convention)
            short_liq_usd = 0.0
            for ev in liqs:
                usd = float(ev.get("usd") or 0.0)
                px = float(ev.get("price") or 0.0)
                total_usd += usd
                if ev.get("side") == "SELL":
                    long_liq_usd += usd
                elif ev.get("side") == "BUY":
                    short_liq_usd += usd
                if zone_edge_px > 0 and px > 0:
                    if abs(px - zone_edge_px) / zone_edge_px < 0.005:
                        zone_usd += usd
            out[f"liq_{win_min}m_total_usd"] = round(total_usd, 2)
            out[f"liq_{win_min}m_zone_usd"] = round(zone_usd, 2)
            out[f"liq_{win_min}m_long_usd"] = round(long_liq_usd, 2)
            out[f"liq_{win_min}m_short_usd"] = round(short_liq_usd, 2)
    except Exception as e:
        out["liq_telem_err"] = str(e)[:80]

    # --- 2. OI-delta over 30 min ---
    try:
        oi_hist = bus.oi(coin=coin, n=8) or []   # 5min poll × 8 = 40min back
        if len(oi_hist) >= 2:
            oi_now = float(oi_hist[-1].get("oi_usd") or 0.0)
            oi_prev = float(oi_hist[0].get("oi_usd") or 0.0)
            if oi_prev > 0:
                out["oi_30m_pct_delta"] = round((oi_now - oi_prev) / oi_prev, 5)
                out["oi_now_usd"] = round(oi_now, 0)
        else:
            out["oi_telem_skip"] = "insufficient_history"
    except Exception as e:
        out["oi_telem_err"] = str(e)[:80]

    # --- 3. CVD-divergence (last 30s of aggressor flow vs last 5min price move) ---
    try:
        cvd = bus.cvd(coin=coin, window_ms=30_000) or {}
        out["cvd_30s_net"] = round(float(cvd.get("net") or 0.0), 4)
        out["cvd_30s_buy_usd"] = round(float(cvd.get("buy_usd") or 0.0), 0)
        out["cvd_30s_sell_usd"] = round(float(cvd.get("sell_usd") or 0.0), 0)
    except Exception as e:
        out["cvd_telem_err"] = str(e)[:80]

    return out


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

            # Snapshot filter-relevant telemetry for retrospective analysis.
            # Zone-edge price = SL (sweep wick), which is the boundary the
            # liq density is measured against.
            try:
                telem = _compute_filter_telemetry(
                    bus=bus,
                    coin=coin,
                    entry_px=entry,
                    zone_edge_px=sl,
                    is_long=is_long,
                    fire_ts_ms=int(last_bar["open_ts"]),
                )
            except Exception as _e:
                telem = {"telem_compute_err": str(_e)[:80]}

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
                    "filter_telem": telem,
                },
            )

        return None
