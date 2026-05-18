"""UZT — Unified Zone Trading (Lesson #2 framework).

Each HTF zone is a fork: either it HOLDS (Reversal path) or it BREAKS (Continuation path).
The invalidation of one path is the trigger for the other. We harvest whichever the tape
provides; we never trade both on the same zone.

Single-TF deployment trick:
  Strategy declares TF="15m" so the backtest harness loads only 15m klines (the harness
  is single-TF). HTF 4h bars are derived inside evaluate() by aggregating 16 × 15m → 1×4h.
  This keeps the pure evaluate(coin, bus) contract intact (no extra bus calls, no SQLite
  zone table). The 15m candle stream IS the persistent state — every scan re-derives zones
  and replays the LTF state machine to "now". If the last 15m bar resolved a state machine
  to FILLED, we fire.

State machine (per zone, replayed deterministically from 15m bars):
  IDLE → IN_ZONE → SWEPT → REVERSAL_ARMED → FIRE_REV    (Path A)
                 → BROKEN → CONTINUATION_ARMED → FIRE_CON  (Path B)
  Invalidation flips bridge the two paths (sweep that then breaks → continuation;
  break that then reclaims with sweep → reversal).

Backtest claim from Lesson #2 (50d × 57 perps): WR 56.7%, PF 1.84, avgR +0.301.
OOS 22d split: WR 56.9%, PF 1.75, avgR +0.277. UZT is GATED — must clear honest
walk-forward via scripts/backtest_harness.py before live capital.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ._base import Signal, StrategyBase
from ._indicators import atr as _atr


# ───────────────────────── 4h aggregation ─────────────────────────


def _aggregate_15m_to_4h(bars_15m: list[dict]) -> list[dict]:
    """Aggregate 15m bars to 4h bars. Each 4h bar = 16 × 15m bars.

    Aligns by absolute UTC time so the same wall-clock 4h slot always groups the
    same 15m bars (00:00-04:00, 04:00-08:00, ...). Drops the head/tail partial
    buckets — we only return COMPLETE 4h bars.
    """
    if not bars_15m:
        return []
    SLOT_MS = 4 * 3600_000
    buckets: dict[int, list[dict]] = {}
    for b in bars_15m:
        slot = (b["open_ts"] // SLOT_MS) * SLOT_MS
        buckets.setdefault(slot, []).append(b)
    out: list[dict] = []
    for slot in sorted(buckets):
        chunk = buckets[slot]
        if len(chunk) < 16:   # incomplete 4h slot — skip
            continue
        out.append({
            "open_ts": slot,
            "open": chunk[0]["open"],
            "high": max(b["high"] for b in chunk),
            "low": min(b["low"] for b in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(b["volume"] for b in chunk),
        })
    return out


# ───────────────────────── Zone identification ─────────────────────────


@dataclass
class Zone:
    side: str            # "supply" or "demand"
    top: float
    bot: float
    formed_ts: int       # 4h bar open_ts
    formed_idx_4h: int   # index in bars_4h
    atr_at_form: float

    @property
    def mid(self) -> float:
        return (self.top + self.bot) / 2

    @property
    def height(self) -> float:
        return self.top - self.bot


def _find_pivots(highs: list[float], lows: list[float], lb: int) -> tuple[list[int], list[int]]:
    """Return (pivot_high_indices, pivot_low_indices) using ±lb bars confirmation."""
    ph, pl = [], []
    for i in range(lb, len(highs) - lb):
        if all(highs[i] >= highs[j] for j in range(i - lb, i)) and \
           all(highs[i] > highs[j] for j in range(i + 1, i + lb + 1)):
            ph.append(i)
        if all(lows[i] <= lows[j] for j in range(i - lb, i)) and \
           all(lows[i] < lows[j] for j in range(i + 1, i + lb + 1)):
            pl.append(i)
    return ph, pl


def _find_zones(bars_4h: list[dict], pivot_lb: int, disp_atr_mult: float,
                atr_period: int = 14) -> list[Zone]:
    """Identify HTF supply/demand zones.

    A SUPPLY zone is formed by the last bullish 4h candle BEFORE a confirmed pivot high
    that was followed by a downward impulse displacing ≥ disp_atr_mult × ATR.
    A DEMAND zone mirrors at pivot lows. Zone bounds = [body_open, wick_high] for supply,
    [wick_low, body_open] for demand.
    """
    if len(bars_4h) < pivot_lb * 2 + atr_period + 5:
        return []
    highs = [b["high"] for b in bars_4h]
    lows = [b["low"] for b in bars_4h]
    closes = [b["close"] for b in bars_4h]
    opens = [b["open"] for b in bars_4h]
    atr_series = _atr(highs, lows, closes, n=atr_period)
    ph, pl = _find_pivots(highs, lows, pivot_lb)

    zones: list[Zone] = []

    # SUPPLY zones from pivot highs
    for piv in ph:
        a = atr_series[piv] if piv < len(atr_series) else None
        if a is None or a <= 0:
            continue
        # Displacement check: next ≤ 3 bars push price down by ≥ disp_atr_mult × ATR
        look = bars_4h[piv + 1: piv + 4]
        if not look:
            continue
        max_low_after = min(b["low"] for b in look)
        if (highs[piv] - max_low_after) < disp_atr_mult * a:
            continue
        # Find last bullish (close>open) candle at-or-before piv
        zone_idx = None
        for j in range(piv, max(piv - 6, -1), -1):
            if closes[j] > opens[j]:
                zone_idx = j
                break
        if zone_idx is None:
            continue
        top = highs[zone_idx]                       # wick high
        bot = max(opens[zone_idx], closes[zone_idx])  # body top (above body open in bull candle = close, but supply zone bottom = the higher of body open/close)
        # For a supply zone formed by a bull candle, the institutional offer sat between
        # the close and the wick high — that's where retail bought into resting offers.
        bot = closes[zone_idx]                      # body close (top of bull body)
        if bot >= top:
            continue
        zones.append(Zone("supply", top, bot,
                          bars_4h[zone_idx]["open_ts"], zone_idx, a))

    # DEMAND zones from pivot lows (mirror)
    for piv in pl:
        a = atr_series[piv] if piv < len(atr_series) else None
        if a is None or a <= 0:
            continue
        look = bars_4h[piv + 1: piv + 4]
        if not look:
            continue
        max_high_after = max(b["high"] for b in look)
        if (max_high_after - lows[piv]) < disp_atr_mult * a:
            continue
        zone_idx = None
        for j in range(piv, max(piv - 6, -1), -1):
            if closes[j] < opens[j]:    # last bearish candle
                zone_idx = j
                break
        if zone_idx is None:
            continue
        bot = lows[zone_idx]
        top = closes[zone_idx]          # body close (bottom of bear body)
        if bot >= top:
            continue
        zones.append(Zone("demand", top, bot,
                          bars_4h[zone_idx]["open_ts"], zone_idx, a))

    return zones


# ───────────────────────── LTF state machine ─────────────────────────


def _evaluate_zone_state(
    zone: Zone,
    bars_15m: list[dict],
    *,
    break_atr_mult: float,
    approach_pct: float,
    retest_tol_pct: float,
    vol_mult: float,
    atr_period_15m: int = 14,
    whipsaw_cooldown_bars: int = 2,
) -> Optional[dict]:
    """Replay the state machine for one zone over bars_15m. Returns a fill dict if
    the LAST bar triggers a FIRE_REV or FIRE_CON, else None.

    Whipsaw mitigation: after a BROKEN state, require `whipsaw_cooldown_bars` of
    no reclaim before allowing CONTINUATION_ARMED to fire. (sentinel-audit fix.)
    """
    # Find first 15m index at-or-after zone formation
    start_idx = None
    for i, b in enumerate(bars_15m):
        if b["open_ts"] >= zone.formed_ts:
            start_idx = i
            break
    if start_idx is None or start_idx >= len(bars_15m) - 2:
        return None
    if len(bars_15m) - start_idx < atr_period_15m + 5:
        return None

    highs = [b["high"] for b in bars_15m]
    lows = [b["low"] for b in bars_15m]
    closes = [b["close"] for b in bars_15m]
    vols = [b["volume"] for b in bars_15m]
    atr_15m = _atr(highs, lows, closes, n=atr_period_15m)
    last_idx = len(bars_15m) - 1

    state = "IDLE"
    sweep_wick: Optional[float] = None      # extreme of the sweep
    sweep_idx: Optional[int] = None
    break_idx: Optional[int] = None
    pivot_for_mss: Optional[float] = None   # last opposite pivot before sweep
    fired_already = False                   # one fire per zone, ever

    # Helpers
    def vol_median(i: int) -> float:
        window = vols[max(0, i - 20):i]
        if not window:
            return 0.0
        s = sorted(window)
        return s[len(s) // 2]

    for i in range(start_idx + 1, len(bars_15m)):
        if fired_already:
            return None     # zone consumed earlier in history
        a = atr_15m[i] if i < len(atr_15m) else None
        if a is None or a <= 0:
            continue
        c = closes[i]
        h = highs[i]
        lo = lows[i]
        v = vols[i]
        vmed = vol_median(i)

        # IN_ZONE check: price within approach_pct of zone midpoint
        in_zone = (lo <= zone.top * (1 + approach_pct)) and (h >= zone.bot * (1 - approach_pct))
        # Strict containment for SWEPT detection
        deeply_in = (lo <= zone.top) and (h >= zone.bot)

        # ── Path B: BREAK detection (works from any non-fired state) ──
        if zone.side == "supply":
            broke_up = c > zone.top and (c - zone.top) > break_atr_mult * a * 0.5
            # require body bar (close past) with displacement (full bar range)
            displaced = (h - lo) > break_atr_mult * a
            if broke_up and displaced and state != "BROKEN":
                state = "BROKEN"
                break_idx = i
                continue
        else:  # demand
            broke_dn = c < zone.bot and (zone.bot - c) > break_atr_mult * a * 0.5
            displaced = (h - lo) > break_atr_mult * a
            if broke_dn and displaced and state != "BROKEN":
                state = "BROKEN"
                break_idx = i
                continue

        # ── BROKEN → CONTINUATION_ARMED ──
        if state == "BROKEN" and break_idx is not None:
            # Whipsaw cooldown: require N bars without reclaim
            since_break = i - break_idx
            if since_break < whipsaw_cooldown_bars:
                # Check for instant reclaim (invalidation flip back)
                if zone.side == "supply" and c < zone.bot:
                    state = "IDLE"
                    break_idx = None
                    continue
                if zone.side == "demand" and c > zone.top:
                    state = "IDLE"
                    break_idx = None
                    continue
                continue
            # Retest of the broken level
            if zone.side == "supply":
                # broken upward → retest of zone.top from above
                level = zone.top
                touched = lo <= level * (1 + retest_tol_pct) and lo >= level * (1 - retest_tol_pct)
                if touched and c > level:
                    # FIRE CONTINUATION LONG
                    if i != last_idx:
                        fired_already = True   # consumed earlier
                        continue
                    sl = bars_15m[break_idx]["low"] * (1 - 0.003)
                    risk = c - sl
                    if risk <= 0:
                        return None
                    return {
                        "path": "CON",
                        "is_long": True,
                        "ref_price": c,
                        "sl_px": sl,
                        "tp1_px": c + 1.5 * risk,
                        "tp_px": c + 3.0 * risk,
                        "fire_reason": f"UZT_CON_LONG_supply@{zone.top:.4f}",
                        "zone_side": zone.side,
                    }
            else:
                level = zone.bot
                touched = h >= level * (1 - retest_tol_pct) and h <= level * (1 + retest_tol_pct)
                if touched and c < level:
                    if i != last_idx:
                        fired_already = True
                        continue
                    sl = bars_15m[break_idx]["high"] * (1 + 0.003)
                    risk = sl - c
                    if risk <= 0:
                        return None
                    return {
                        "path": "CON",
                        "is_long": False,
                        "ref_price": c,
                        "sl_px": sl,
                        "tp1_px": c - 1.5 * risk,
                        "tp_px": c - 3.0 * risk,
                        "fire_reason": f"UZT_CON_SHORT_demand@{zone.bot:.4f}",
                        "zone_side": zone.side,
                    }
            continue

        # ── Path A: SWEEP detection (price wicks past prior LTF pivot, closes back inside zone) ──
        if state in ("IDLE", "IN_ZONE") and deeply_in:
            state = "IN_ZONE"
            # Identify a prior 15m pivot in zone direction within last 20 bars
            window_lo = max(start_idx, i - 20)
            prior_lows = [(j, lows[j]) for j in range(window_lo, i)]
            prior_highs = [(j, highs[j]) for j in range(window_lo, i)]
            if zone.side == "supply":
                # sweep takes out a prior swing high above zone.top, then closes back inside
                if prior_highs:
                    prior_high_max = max(prior_highs, key=lambda x: x[1])[1]
                    if h > prior_high_max and c < prior_high_max and c <= zone.top \
                       and v >= vol_mult * vmed:
                        state = "SWEPT"
                        sweep_wick = h
                        sweep_idx = i
                        pivot_for_mss = min(b["low"] for b in bars_15m[window_lo:i])
                        continue
            else:
                if prior_lows:
                    prior_low_min = min(prior_lows, key=lambda x: x[1])[1]
                    if lo < prior_low_min and c > prior_low_min and c >= zone.bot \
                       and v >= vol_mult * vmed:
                        state = "SWEPT"
                        sweep_wick = lo
                        sweep_idx = i
                        pivot_for_mss = max(b["high"] for b in bars_15m[window_lo:i])
                        continue

        # ── SWEPT → REVERSAL_ARMED (MSS body-close past opposite pivot with displacement) ──
        if state == "SWEPT" and sweep_idx is not None and pivot_for_mss is not None:
            displaced_mss = (h - lo) > 1.2 * a
            if zone.side == "supply":
                # MSS down: body close below `pivot_for_mss` (recent swing low)
                if c < pivot_for_mss and displaced_mss and v >= vol_mult * vmed:
                    # ARMED — fire on retest. The MSS candle itself is the entry trigger
                    # if zone.mid is between its high and low; otherwise wait one bar.
                    entry = zone.mid
                    if not (lo <= entry <= h):
                        # Need retest — check current bar
                        if not (lo <= entry <= h):
                            continue
                    if i != last_idx:
                        fired_already = True
                        continue
                    sl = sweep_wick * (1 + 0.0003)
                    risk = sl - entry
                    if risk <= 0:
                        return None
                    return {
                        "path": "REV",
                        "is_long": False,
                        "ref_price": entry,
                        "sl_px": sl,
                        "tp1_px": entry - 1.5 * risk,
                        "tp_px": entry - 3.0 * risk,
                        "fire_reason": f"UZT_REV_SHORT_supply@{zone.mid:.4f}",
                        "zone_side": zone.side,
                    }
            else:
                if c > pivot_for_mss and displaced_mss and v >= vol_mult * vmed:
                    entry = zone.mid
                    if not (lo <= entry <= h):
                        continue
                    if i != last_idx:
                        fired_already = True
                        continue
                    sl = sweep_wick * (1 - 0.0003)
                    risk = entry - sl
                    if risk <= 0:
                        return None
                    return {
                        "path": "REV",
                        "is_long": True,
                        "ref_price": entry,
                        "sl_px": sl,
                        "tp1_px": entry + 1.5 * risk,
                        "tp_px": entry + 3.0 * risk,
                        "fire_reason": f"UZT_REV_LONG_demand@{zone.mid:.4f}",
                        "zone_side": zone.side,
                    }
    return None


# ───────────────────────── Strategy class ─────────────────────────


class UZT(StrategyBase):
    """Unified Zone Trading. Two paths per HTF zone: REVERSAL (hold) and CONTINUATION (break).

    Status: PROVISIONAL — must clear scripts/backtest_harness.py walk-forward gate before
    PM lifecycle promotion to live capital. Sentinel audit (MODERATE 77%) approved the
    architecture with whipsaw-cooldown mitigation, which is implemented here.
    """

    NAME = "uzt"
    CLOID_PREFIX = "uzt_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]   # bidirectional engine
    TF = "15m"   # LTF scan; 4h HTF derived by internal aggregation

    UNIVERSE = [
        "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "DOT",
        "ATOM", "NEAR", "INJ", "SUI", "APT", "FIL", "ARB", "OP", "TIA",
        "JUP", "WIF", "kPEPE", "kSHIB", "AAVE", "UNI", "MKR", "COMP", "SEI",
        "ADA", "TRX", "BCH", "PENDLE", "RNDR", "PYTH", "WLD",
    ]

    # ── tunable parameters (Lesson #2 defaults + sentinel whipsaw fix) ──
    HTF_PIVOT_LB = 5
    HTF_DISP_ATR = 1.5
    LTF_BREAK_ATR = 1.2
    LTF_APPROACH_PCT = 0.03           # 3% of zone midpoint = IN_ZONE
    LTF_RETEST_TOL_PCT = 0.005        # 0.5% retest tolerance
    LTF_VOL_MULT = 0.7                # vs 20-bar median
    WHIPSAW_COOLDOWN_BARS = 2         # sentinel mitigation: 2 × 15m no reclaim before arming CON
    HOLD_MAX_BARS = 40                # 40 × 15m = 10h
    MAX_ZONES_TO_SCAN = 6             # most-recent N zones only (perf cap)

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Pull ~20 days of 15m to give the 4h aggregation enough history
        bars_15m = bus.candles(coin, "15m", n=2000) or []
        if len(bars_15m) < 500:
            return None
        bars_4h = _aggregate_15m_to_4h(bars_15m)
        if len(bars_4h) < 30:
            return None

        zones = _find_zones(bars_4h, cls.HTF_PIVOT_LB, cls.HTF_DISP_ATR)
        if not zones:
            return None

        # Most recent zones only (perf + relevance)
        zones = sorted(zones, key=lambda z: z.formed_ts)[-cls.MAX_ZONES_TO_SCAN:]

        last_bar = bars_15m[-1]
        last_close = last_bar["close"]

        # Only consider zones whose midpoint is within 6% of current price
        near = [z for z in zones if abs(z.mid - last_close) / last_close < 0.06]
        if not near:
            return None

        for zone in near:
            fill = _evaluate_zone_state(
                zone, bars_15m,
                break_atr_mult=cls.LTF_BREAK_ATR,
                approach_pct=cls.LTF_APPROACH_PCT,
                retest_tol_pct=cls.LTF_RETEST_TOL_PCT,
                vol_mult=cls.LTF_VOL_MULT,
                whipsaw_cooldown_bars=cls.WHIPSAW_COOLDOWN_BARS,
            )
            if fill is None:
                continue
            return Signal(
                coin=coin,
                side="B" if fill["is_long"] else "A",
                is_long=fill["is_long"],
                ref_price=fill["ref_price"],
                sl_px=fill["sl_px"],
                tp_px=fill["tp_px"],
                max_hold_bars=cls.HOLD_MAX_BARS,
                fire_ts=float(last_bar["open_ts"]),
                fire_reason=fill["fire_reason"],
                extras={
                    "path": fill["path"],
                    "zone_side": fill["zone_side"],
                    "zone_top": zone.top,
                    "zone_bot": zone.bot,
                    "tp1_px": fill["tp1_px"],
                    "tf_ltf": "15m",
                    "tf_htf": "4h",
                    "audit_status": "PROVISIONAL",
                },
            )
        return None
