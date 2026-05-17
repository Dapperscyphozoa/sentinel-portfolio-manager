"""lh1 — Liquidation Heatmap, INVERTED (SPEC §3.5).

Sweep wicks INTO equal-high/low clusters mark CONTINUATION (not exhaustion).
Trade WITH the sweep direction:
  - sweep of SSL (equal lows) wick down → LONG (continuation up after stop-hunt fail)
  - sweep of BSL (equal highs) wick up  → SHORT
LH_INVERTED is True by default; old direction available by setting LH_INVERTED=0.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase
from ._indicators import pivot_lows, pivot_highs


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _find_cluster(values: list[float], band_pct: float, min_count: int) -> Optional[float]:
    """Return the centroid of the densest cluster (by count) of values within band_pct."""
    if not values:
        return None
    best_count = 0
    best_centroid: Optional[float] = None
    for anchor in values:
        members = [v for v in values if abs(v - anchor) / anchor <= band_pct]
        if len(members) >= min_count and len(members) > best_count:
            best_count = len(members)
            best_centroid = sum(members) / len(members)
    return best_centroid


class LH1(StrategyBase):
    NAME = "lh1"
    CLOID_PREFIX = "liqhmp_"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = "1h"
    UNIVERSE = [
        "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "NEAR",
        "SUI", "APT", "ARB", "OP", "INJ", "SEI", "TIA", "WIF", "JUP", "DOT",
        "ATOM", "FET", "STG", "POLYX",
    ]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        lb = int(_f("LH_CLUSTER_LOOKBACK", 120))
        plb = int(_f("LH_PIVOT_LOOKBACK", 5))
        band = _f("LH_CLUSTER_BAND_PCT", 0.003)
        min_piv = int(_f("LH_MIN_PIVOTS", 3))
        sweep = _f("LH_SWEEP_PCT", 0.002)
        vol_mult = _f("LH_VOL_SPIKE_MULT", 1.5)
        max_prox = _f("LH_MAX_PROXIMITY_PCT", 0.020)
        sl_buf = _f("LH_SL_BUFFER_PCT", 0.003)
        rr = _f("LH_RR", 3.0)
        max_hold = int(_f("LH_MAX_HOLD_BARS", 8))
        inverted = os.environ.get("LH_INVERTED", "1") in ("1", "true", "yes")

        bars = bus.candles(coin, cls.TF, n=lb + 5)
        if not bars or len(bars) < lb + 2:
            return None

        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        vols = [float(b["volume"]) for b in bars]

        ph = pivot_highs(highs[:-1], plb, plb)
        pl = pivot_lows(lows[:-1], plb, plb)
        ph_vals = [highs[i] for i in ph[-15:]]
        pl_vals = [lows[i] for i in pl[-15:]]

        bsl = _find_cluster(ph_vals, band, min_piv)  # equal-highs cluster
        ssl = _find_cluster(pl_vals, band, min_piv)  # equal-lows cluster

        cur = bars[-1]
        c = float(cur["close"])
        h = float(cur["high"])
        l = float(cur["low"])
        v = float(cur["volume"])
        avg_vol = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else (sum(vols[:-1]) / max(1, len(vols) - 1))
        vol_ok = v > vol_mult * avg_vol and avg_vol > 0

        sweep_up = False
        sweep_down = False
        if bsl is not None and h > bsl * (1 + sweep) and abs(c - bsl) / bsl <= max_prox and vol_ok:
            sweep_up = True
        if ssl is not None and l < ssl * (1 - sweep) and abs(c - ssl) / ssl <= max_prox and vol_ok:
            sweep_down = True
        if not (sweep_up or sweep_down):
            return None

        # Direction mapping:
        # legacy/non-inverted: sweep_up → SHORT (exhaustion), sweep_down → LONG
        # inverted: sweep_up → LONG (continuation), sweep_down → SHORT
        if sweep_up:
            is_long = inverted  # inverted: True (long); legacy: False (short)
        else:
            is_long = not inverted  # inverted: False (short); legacy: True (long)

        if is_long:
            sl = c * (1 - sl_buf)
            tp = c + (c - sl) * rr
            return Signal(
                coin=coin, side="B", is_long=True, ref_price=c,
                sl_px=sl, tp_px=tp, max_hold_bars=max_hold,
                fire_ts=time.time() * 1000,
                fire_reason="sweep_continuation_long",
                extras={"bsl": bsl, "ssl": ssl, "sweep_up": sweep_up, "sweep_down": sweep_down,
                        "inverted": inverted},
            )
        sl = c * (1 + sl_buf)
        tp = c - (sl - c) * rr
        return Signal(
            coin=coin, side="A", is_long=False, ref_price=c,
            sl_px=sl, tp_px=tp, max_hold_bars=max_hold,
            fire_ts=time.time() * 1000,
            fire_reason="sweep_continuation_short",
            extras={"bsl": bsl, "ssl": ssl, "sweep_up": sweep_up, "sweep_down": sweep_down,
                    "inverted": inverted},
        )
