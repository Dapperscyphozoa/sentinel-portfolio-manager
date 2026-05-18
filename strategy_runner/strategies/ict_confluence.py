from common import edge_filters
"""ICT Confluence Engine — unified OB + FVG + Wick-Sweep entry.

Council-mandated specification (5/5 voters converged):
  - SINGLE module, not 3 separate strategies (avoids self-sabotage trap)
  - Entry requires ≥2 of 3 signals overlapping within 1×ATR
  - ATR-defined SL, 2-3R targets
  - 0.5% risk per trade (council median)
  - Walk-forward GATE: PF > 1.4 AND DD < 35% in EVERY of 5×90d windows

DEFINITIONS (verbatim from council convergence):

  swing_high(i):  highs[i] > all neighbors ±SWING_LB
  swing_low(i):   lows[i]  < all neighbors ±SWING_LB

  BOS_BULL: close > last_swing_high + BOS_ATR_BUFFER × ATR
  BOS_BEAR: close < last_swing_low  - BOS_ATR_BUFFER × ATR

  VALID_OB:  last opposite-color candle ≤ OB_MAX_DISTANCE bars before BOS,
             body_size > OB_BODY_PCT × candle_range

  VALID_FVG: 3-candle gap, gap_size > FVG_MIN_ATR × ATR, unmitigated

  VALID_WICK_SWEEP: wick > WICK_MIN_ATR × ATR past confirmed swing,
                    body closes opposite, body ≥ WICK_BODY_CLOSE_PCT × wick_len

  CONFLUENCE: ≥2 of {OB_zone, FVG_zone, Wick_zone} overlap within
              CONFLUENCE_ATR × ATR. Trade entry = midpoint of confluence zone.

SIZING:
  risk_usd = RISK_PCT × wallet
  sl_distance = abs(entry - sl)
  position_notional = risk_usd / (sl_distance / entry)
  margin = position_notional / leverage
  margin = min(margin, MAX_MARGIN_PCT × wallet)   # safety cap
"""
from __future__ import annotations

from typing import Optional

from ._base import Signal, StrategyBase
from ._indicators import atr as _atr


# ──────────────────────── Pivots ────────────────────────
def find_swings(highs: list[float], lows: list[float],
                lookback: int = 2) -> tuple[list[int], list[int]]:
    """Return (swing_high_indices, swing_low_indices) using ±lookback bars.

    Per council: minimum 2 bars each side for a confirmed pivot.
    """
    ph, pl = [], []
    n = len(highs)
    for i in range(lookback, n - lookback):
        is_ph = all(highs[i] >= highs[j] for j in range(i - lookback, i)) and \
                all(highs[i] > highs[j] for j in range(i + 1, i + lookback + 1))
        is_pl = all(lows[i] <= lows[j] for j in range(i - lookback, i)) and \
                all(lows[i] < lows[j] for j in range(i + 1, i + lookback + 1))
        if is_ph: ph.append(i)
        if is_pl: pl.append(i)
    return ph, pl


# ──────────────────────── Break of Structure ────────────────────────
def detect_bos(highs: list[float], lows: list[float], closes: list[float],
               atr_v: list, curr_idx: int,
               swings_h: list[int], swings_l: list[int],
               atr_buffer: float = 0.5) -> Optional[tuple[str, int]]:
    """Detect BOS at curr_idx. Returns ('BULL'|'BEAR', swing_idx_broken) or None.

    BULL_BOS: close > most_recent_swing_high + atr_buffer × ATR
    BEAR_BOS: close < most_recent_swing_low  - atr_buffer × ATR
    """
    if curr_idx < 10 or atr_v[curr_idx] is None:
        return None
    a = atr_v[curr_idx]
    c = closes[curr_idx]
    # Filter swings to those confirmed BEFORE curr_idx (lookback-confirmed)
    recent_h = [s for s in swings_h if s < curr_idx - 2]
    recent_l = [s for s in swings_l if s < curr_idx - 2]
    if recent_h:
        last_h = highs[recent_h[-1]]
        # Must be a FRESH break (prior bar was below)
        if c > last_h + atr_buffer * a and closes[curr_idx - 1] <= last_h + atr_buffer * a:
            return ("BULL", recent_h[-1])
    if recent_l:
        last_l = lows[recent_l[-1]]
        if c < last_l - atr_buffer * a and closes[curr_idx - 1] >= last_l - atr_buffer * a:
            return ("BEAR", recent_l[-1])
    return None


# ──────────────────────── Order Block ────────────────────────
def find_ob(opens: list[float], highs: list[float], lows: list[float],
            closes: list[float], bos_idx: int, direction: str,
            max_distance: int = 5, body_pct: float = 0.50) -> Optional[dict]:
    """Find valid OB: last OPPOSITE-color candle ≤max_distance bars before BOS,
    body > body_pct × range. Returns {'top', 'bottom', 'mid', 'idx'} or None.
    """
    for j in range(bos_idx, max(bos_idx - max_distance, 0) - 1, -1):
        if j < 0 or j >= len(opens):
            continue
        rng = highs[j] - lows[j]
        if rng <= 0:
            continue
        body = abs(closes[j] - opens[j])
        if body / rng < body_pct:
            continue
        if direction == "BULL":
            # Last BEARISH candle (close < open) before bullish BOS
            if closes[j] < opens[j]:
                return {"idx": j, "top": opens[j], "bottom": closes[j],
                        "mid": (opens[j] + closes[j]) / 2}
        else:
            if closes[j] > opens[j]:
                return {"idx": j, "top": closes[j], "bottom": opens[j],
                        "mid": (opens[j] + closes[j]) / 2}
    return None


# ──────────────────────── Fair Value Gap ────────────────────────
def find_fvg(highs: list[float], lows: list[float], atr_v: list,
             scan_start: int, scan_end: int, direction: str,
             min_atr_mult: float = 0.3) -> Optional[dict]:
    """Find unmitigated FVG in [scan_start, scan_end] matching direction.
    Returns {'top','bottom','mid','idx'} or None.

    Mitigation = prior bars (after creation, before current) already touched zone.
    """
    found = None
    for i in range(max(scan_start, 2), scan_end + 1):
        if i >= len(highs) or atr_v[i] is None:
            continue
        a = atr_v[i]
        if direction == "BULL":
            if highs[i - 2] >= lows[i]:
                continue
            gap = lows[i] - highs[i - 2]
            if gap < min_atr_mult * a:
                continue
            top = lows[i]; bottom = highs[i - 2]
            # check mitigation BEFORE curr (scan_end is curr-1)
            mitigated = any(lows[k] <= top for k in range(i + 1, scan_end + 1))
            if not mitigated:
                found = {"idx": i, "top": top, "bottom": bottom, "mid": (top + bottom) / 2}
        else:
            if lows[i - 2] <= highs[i]:
                continue
            gap = lows[i - 2] - highs[i]
            if gap < min_atr_mult * a:
                continue
            top = lows[i - 2]; bottom = highs[i]
            mitigated = any(highs[k] >= bottom for k in range(i + 1, scan_end + 1))
            if not mitigated:
                found = {"idx": i, "top": top, "bottom": bottom, "mid": (top + bottom) / 2}
    return found


# ──────────────────────── Wick Sweep ────────────────────────
def find_wick_sweep(opens: list[float], highs: list[float], lows: list[float],
                    closes: list[float], atr_v: list,
                    scan_start: int, scan_end: int, direction: str,
                    swings_h: list[int], swings_l: list[int],
                    swing_lb: int = 2,
                    min_wick_atr: float = 0.5,
                    body_close_pct: float = 0.40) -> Optional[dict]:
    """Find valid wick sweep in [scan_start, scan_end].
    CRITICAL: only sweeps CONFIRMED swings (≥ swing_lb bars before sweep candle).
    """
    found = None
    for i in range(max(scan_start, 5), scan_end + 1):
        if i >= len(highs) or atr_v[i] is None:
            continue
        a = atr_v[i]
        o = opens[i]; h = highs[i]; l = lows[i]; c = closes[i]
        # Only swings confirmed BEFORE the sweep candle
        if direction == "BULL":
            wick_len = min(o, c) - l
            if wick_len < min_wick_atr * a:
                continue
            swept = False; swept_swing = None
            for s in swings_l:
                if s > i - swing_lb - 1: break          # only confirmed swings
                if s < i - 30: continue
                if l < lows[s] and min(o, c) > lows[s]:
                    swept = True; swept_swing = s
            if not swept: continue
            total_range = h - l
            if total_range <= 0: continue
            close_position = (c - l) / total_range
            if close_position < body_close_pct + 0.30:
                continue
            if c <= o: continue
            found = {"idx": i, "swept": swept_swing,
                     "top": min(o, c), "bottom": l, "mid": l + wick_len / 2,
                     "sl_reference": l}
        else:
            wick_len = h - max(o, c)
            if wick_len < min_wick_atr * a:
                continue
            swept = False; swept_swing = None
            for s in swings_h:
                if s > i - swing_lb - 1: break
                if s < i - 30: continue
                if h > highs[s] and max(o, c) < highs[s]:
                    swept = True; swept_swing = s
            if not swept: continue
            total_range = h - l
            if total_range <= 0: continue
            close_position = (h - c) / total_range
            if close_position < body_close_pct + 0.30:
                continue
            if c >= o: continue
            found = {"idx": i, "swept": swept_swing,
                     "top": h, "bottom": max(o, c), "mid": h - wick_len / 2,
                     "sl_reference": h}
    return found


# ──────────────────────── Confluence Check ────────────────────────
def zones_align(zones: list[dict], atr_value: float, tol_atr: float) -> bool:
    """Return True if all pairs of zones overlap within tol_atr × ATR."""
    if len(zones) < 2:
        return False
    mids = [z["mid"] for z in zones]
    span = max(mids) - min(mids)
    return span <= tol_atr * atr_value


# ──────────────────────── The Engine ────────────────────────
class ICT_Confluence_4h(StrategyBase):
    """Unified ICT entry: ≥2 of {OB, FVG, Wick-sweep} aligned within 1×ATR.

    Built per council unanimous spec. Walk-forward gated: must pass
    PF > 1.4 AND DD < 35% in EVERY of 5×90d windows BEFORE deploy.

    Promotion audit 2026-05-18 (council unanimous CRITICAL on hl_settle_5m
    asymmetry → checked here):
      - Full-sample direction asymmetry 5pp (symmetric, no short-only ban)
      - OOS direction asymmetry 13pp (LONG WR 46.3% vs SHORT WR 59.5%) —
        flag for monitoring at n≥30 live; auto-flip to short-only if it
        persists. Env override ICT_4H_SHORT_ONLY=1 enables ahead of n=30.
      - Coin bleeders (backtest n≥3, WR<50%, PF<1.0): ETH, AAVE, PENDLE
        → blocked at evaluate() entry.
    """
    NAME = "ict_confluence_4h"
    CLOID_PREFIX = "ictc_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop", "high_vol"]
    TF = "4h"
    UNIVERSE = [
        "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "DOT",
        "ATOM", "NEAR", "INJ", "SUI", "APT", "FIL", "ARB", "OP", "MATIC", "TON",
        "TIA", "JUP", "WIF", "kPEPE", "kSHIB", "FTM", "AAVE", "UNI", "MKR",
        "COMP", "SEI", "ADA", "TRX", "BCH", "PENDLE", "RNDR", "PYTH", "WLD",
    ]
    # Bleeder denylist — backtest n=266: ETH 2/10 PF 0.61, AAVE 2/5 PF 0.68,
    # PENDLE 2/6 PF 0.74. Universe-wide WR 57% / PF 3.18; these 3 are the only
    # statistically significant drags. Reviewed quarterly.
    COIN_DENYLIST: set = {"ETH", "AAVE", "PENDLE"}
    # Env-flippable short-only mode (default off; flip to 1 if OOS asymmetry
    # persists past n≥30 live trades).
    import os as _os
    SHORT_ONLY: bool = _os.environ.get("ICT_4H_SHORT_ONLY", "0") == "1"
    # Council Q2 funding-polarity gate (opt-in, default off). Mixed council
    # vote on long-side asymmetry fix — operator can A/B test:
    #   ICT_LONG_FUNDING_MAX   — if set, longs ONLY fire when funding < this
    #                            (negative funding = shorts paying, supporting longs)
    #   ICT_SHORT_FUNDING_MIN  — if set, shorts ONLY fire when funding > this
    #                            (positive funding = longs paying, supporting shorts)
    # Default unset (NaN) → no filter (current behaviour preserved).
    # Safe env→float (malformed → NaN, filter disabled). Inlined because
    # class-body cannot reference helper methods defined later.
    try:
        LONG_FUNDING_MAX: float = float(_os.environ.get("ICT_LONG_FUNDING_MAX", "nan"))
    except (TypeError, ValueError):
        LONG_FUNDING_MAX = float("nan")
    try:
        SHORT_FUNDING_MIN: float = float(_os.environ.get("ICT_SHORT_FUNDING_MIN", "nan"))
    except (TypeError, ValueError):
        SHORT_FUNDING_MIN = float("nan")

    # Council-set thresholds
    SWING_LB = 2
    BOS_ATR_BUFFER = 0.5
    OB_MAX_DISTANCE = 5
    OB_BODY_PCT = 0.50
    FVG_MIN_ATR = 0.3
    WICK_MIN_ATR = 0.5
    WICK_BODY_CLOSE_PCT = 0.40
    CONFLUENCE_ATR_TOL = 1.0
    BOS_VALID_BARS = 20            # confluence zone must form within N bars of BOS
    R_MULT_TP = 2.5                # TP at 2.5R
    SL_BUFFER_ATR = 0.2            # SL = zone extreme + 0.2 × ATR
    HOLD_MAX_BARS = 30
    RISK_PCT = 0.005               # 0.5% risk per trade

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Bleeder denylist (council promotion audit 2026-05-18)
        if coin in cls.COIN_DENYLIST:
            return None
        bars = bus.candles(coin, cls.TF, n=200) or []
        if len(bars) < 60:
            return None
        opens = [b["open"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        closes = [b["close"] for b in bars]
        atr_v = _atr(highs, lows, closes, 14)
        i = len(bars) - 1
        if atr_v[i] is None:
            return None
        a = atr_v[i]
        c = closes[i]

        swings_h, swings_l = find_swings(highs, lows, cls.SWING_LB)
        if not swings_h or not swings_l:
            return None

        # Look for the most recent BOS within the last 20-30 bars
        recent_bos = None
        for k in range(max(10, i - 30), i + 1):
            d = detect_bos(highs, lows, closes, atr_v, k, swings_h, swings_l,
                           cls.BOS_ATR_BUFFER)
            if d:
                recent_bos = (k, d[0], d[1])   # (bos_idx, direction, swing_broken)
        if recent_bos is None:
            return None
        bos_idx, direction, swing_broken = recent_bos
        if i - bos_idx > cls.BOS_VALID_BARS:
            return None

        # Build the three potential zones
        ob = find_ob(opens, highs, lows, closes, bos_idx, direction,
                     cls.OB_MAX_DISTANCE, cls.OB_BODY_PCT)
        fvg = find_fvg(highs, lows, atr_v,
                       max(bos_idx - cls.OB_MAX_DISTANCE, 2), i - 1,
                       direction, cls.FVG_MIN_ATR)
        wick = find_wick_sweep(opens, highs, lows, closes, atr_v,
                               max(bos_idx - 2, 5), i, direction,
                               swings_h, swings_l,
                               cls.WICK_MIN_ATR, cls.WICK_BODY_CLOSE_PCT)

        zones = [z for z in (ob, fvg, wick) if z is not None]
        if len(zones) < 2:
            return None

        # Confluence: at least 2 zones aligned within tolerance
        # Find the best 2-or-3 zone alignment
        if not zones_align(zones, a, cls.CONFLUENCE_ATR_TOL):
            # Try pairwise — if any pair aligns, take it
            best_pair = None
            for x in range(len(zones)):
                for y in range(x + 1, len(zones)):
                    if zones_align([zones[x], zones[y]], a, cls.CONFLUENCE_ATR_TOL):
                        best_pair = [zones[x], zones[y]]
                        break
                if best_pair: break
            if best_pair is None:
                return None
            zones = best_pair

        # Current bar must be touching the confluence zone
        zone_top = max(z["top"] for z in zones)
        zone_bot = min(z["bottom"] for z in zones)
        touching = (lows[i] <= zone_top and highs[i] >= zone_bot)
        if not touching:
            return None

        # Entry at confluence midpoint
        entry_px = sum(z["mid"] for z in zones) / len(zones)
        is_long = (direction == "BULL")

        # Short-only gate (council promotion audit: OOS LONG WR 46.3% vs SHORT 59.5%)
        if cls.SHORT_ONLY and is_long:
            return None

        # ── Council Q2 funding-polarity gate (opt-in 2026-05-18) ──
        # Operator can enable via env. nan → disabled (default). Fail-soft:
        # missing funding data → fall through (do not block).
        import math as _math
        if (not _math.isnan(cls.LONG_FUNDING_MAX) and is_long) or \
           (not _math.isnan(cls.SHORT_FUNDING_MIN) and not is_long):
            try:
                f_hist = bus.funding(coin, hours=1)
                if f_hist:
                    rate_now = float(f_hist[-1].get("rate", 0))
                    if is_long and rate_now >= cls.LONG_FUNDING_MAX:
                        return None
                    if (not is_long) and rate_now <= cls.SHORT_FUNDING_MIN:
                        return None
            except Exception:
                pass    # fail-soft

        # SL: beyond the deepest zone edge + ATR buffer
        if is_long:
            sl_px = zone_bot - cls.SL_BUFFER_ATR * a
        else:
            sl_px = zone_top + cls.SL_BUFFER_ATR * a

        risk_dist = abs(entry_px - sl_px)
        if risk_dist < 0.003 * entry_px:    # min 0.3% risk distance
            return None

        # TP: 2.5R
        if is_long:
            tp_px = entry_px + risk_dist * cls.R_MULT_TP
        else:
            tp_px = entry_px - risk_dist * cls.R_MULT_TP

        # ────────── KRONOS CONFIRMATION GATE ──────────
        # Per council Option A unanimous: use Kronos as direction-only confirmation.
        # If Kronos disagrees with ICT direction, skip the trade.
        # If Kronos unavailable or FLAT, allow (default-allow on indecision).
        kronos_info = None
        try:
            from common import kronos_gate
            if kronos_gate.is_enabled():
                kronos_info = kronos_gate.predict_direction(coin, bars, pred_len=6)
                if kronos_info is not None:
                    if not kronos_gate.agrees(kronos_info["direction"], is_long):
                        return None    # Kronos disagrees → skip
        except Exception:
            pass    # silent fail-open if Kronos errors

        extras = {
            "direction": direction, "bos_idx": bos_idx,
            "n_zones": len(zones),
            "has_ob": ob is not None, "has_fvg": fvg is not None,
            "has_wick": wick is not None,
            "zone_top": zone_top, "zone_bot": zone_bot,
            "r_mult_tp": cls.R_MULT_TP,
            "atr": a, "risk_pct_of_price": risk_dist / entry_px,
            "tf": cls.TF,
        }
        if kronos_info is not None:
            extras["kronos_direction"] = kronos_info["direction"]
            extras["kronos_pred_return"] = kronos_info["pred_return"]
            extras["kronos_cached"] = kronos_info.get("cached", False)

        # ── Stage 2 council filter: OI-delta-increasing on trigger (+22% PF) ──
        if ICT_OI_FILTER_ENABLED:
            passes, oi_detail = edge_filters.oi_delta_increasing(
                bus, coin,
                lookback_n=ICT_OI_LOOKBACK_N,
                min_pct_delta=ICT_OI_MIN_PCT_DELTA,
            )
            extras.update(oi_detail)
            if not passes:
                return None

        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=entry_px, sl_px=sl_px, tp_px=tp_px,
            max_hold_bars=cls.HOLD_MAX_BARS,
            fire_ts=float(bars[i]["open_ts"]),
            fire_reason=f"{direction}_BOS+{len(zones)}of3_confluence"
                        + (f"+K_{kronos_info['direction']}" if kronos_info else ""),
            extras=extras,
        )


class ICT_Confluence_1d(ICT_Confluence_4h):
    NAME = "ict_confluence_1d"
    CLOID_PREFIX = "ictd_"
    TF = "1d"
    BOS_VALID_BARS = 10
    HOLD_MAX_BARS = 15
