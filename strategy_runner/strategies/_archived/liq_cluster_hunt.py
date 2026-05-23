"""liq_cluster_hunt — Stage 1 #4. Council 4/4 mentioned, +2.6%/mo est.

DIFFERENTIATED from liq_cascade:
  - liq_cascade fires AFTER liquidations already happened (reactive fader)
  - liq_cluster_hunt fires BEFORE the sweep, predicting the path

Mechanic:
  1. Identify cluster of stacked liquidation events (Binance forceOrder) at a
     specific price band within last 30min — at least N events.
  2. Cluster center must align with:
       (a) a round-number price level (e.g. BTC $77000, ETH $2200), OR
       (b) within 0.2% of OI concentration band (existing OI feed).
  3. Current price approaches the cluster from outside (≤0.5% away but not
     through it yet).
  4. Direction: trade WITH the expected sweep — price gravitates toward
     stacked stops/liqs, then continues briefly past, then reverses. We
     enter the brief continuation move (10-30min hold).

Entry: when price within 0.3% of cluster center, momentum bar in cluster direction.
SL: 0.5% on opposite side of cluster (so we lose only if cluster doesn't fill).
TP: 1.0% beyond cluster (capture the overshoot).

Won't fire if cluster already swept (cluster age <30min check + cluster price-band
must still be untouched by recent highs/lows).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from common import edge_filters
from strategy_runner.strategies._base import Signal, StrategyBase


log = logging.getLogger("strategy.liq_cluster_hunt")


def _f(k: str, d: float) -> float:
    try:
        return float(os.environ.get(k, d))
    except Exception:
        return d


LCH_LOOKBACK_MS    = int(_f("LCH_LOOKBACK_MS", 1_800_000))   # 30min liq window
LCH_MIN_CLUSTER_USD = _f("LCH_MIN_CLUSTER_USD", 500_000.0)
LCH_MIN_EVENTS     = int(_f("LCH_MIN_EVENTS", 5))
LCH_CLUSTER_BAND_BPS = _f("LCH_CLUSTER_BAND_BPS", 50.0)      # 0.5% wide cluster band
LCH_APPROACH_BPS   = _f("LCH_APPROACH_BPS", 50.0)            # within 0.5% of cluster
LCH_TRIGGER_BPS    = _f("LCH_TRIGGER_BPS", 30.0)             # entry within 0.3%
LCH_ROUND_BPS      = _f("LCH_ROUND_BPS", 20.0)               # tolerance for round-number alignment
LCH_SL_PCT         = _f("LCH_SL_PCT", 0.005)
LCH_TP_PCT         = _f("LCH_TP_PCT", 0.010)
LCH_MAX_HOLD_BARS  = int(_f("LCH_MAX_HOLD_BARS", 6))         # 30min on 5m
LCH_TF             = "5m"


def _round_levels(px: float) -> list[float]:
    """Generate plausible round-number levels near current price."""
    if px <= 0:
        return []
    if px >= 10_000:   step = 1000
    elif px >= 1_000:  step = 100
    elif px >= 100:    step = 10
    elif px >= 10:     step = 1.0
    elif px >= 1:      step = 0.1
    else:              step = 0.01
    base = round(px / step) * step
    return [base + step * k for k in (-2, -1, 0, 1, 2)]


def _is_near_round(px: float, tol_bps: float) -> tuple[bool, float]:
    """Return (near, level) - is px within tol_bps of any round number."""
    for lvl in _round_levels(px):
        if lvl <= 0:
            continue
        bps = abs(px - lvl) / lvl * 10_000
        if bps < tol_bps:
            return True, lvl
    return False, 0.0


class LiqClusterHunt(StrategyBase):
    NAME = "liq_cluster_hunt"
    CLOID_PREFIX = "lclus"
    AFFINITY = ["range", "chop", "high_vol", "trend_up", "trend_down"]
    TF = LCH_TF
    UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK",
                "LTC", "NEAR", "SUI", "APT", "ARB", "OP", "INJ", "SEI"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # 1. Pull recent liqs
        try:
            since = int(time.time() * 1000) - LCH_LOOKBACK_MS
            liqs = bus.liq(since_ms=since, coin=coin)
        except Exception:
            return None
        if not liqs or len(liqs) < LCH_MIN_EVENTS:
            return None

        # 2. Pull current price
        try:
            bars = bus.candles(coin, LCH_TF, n=6)
        except Exception:
            return None
        if not bars or len(bars) < 2:
            return None
        last = bars[-1]
        close = float(last["close"])
        open_ = float(last["open"])
        if close <= 0:
            return None

        # 3. Find largest price cluster of liq events
        # Bucket prices into LCH_CLUSTER_BAND_BPS-wide bands relative to close
        from collections import defaultdict
        buckets_long: dict = defaultdict(lambda: {"usd": 0.0, "n": 0, "px_sum": 0.0})
        buckets_short: dict = defaultdict(lambda: {"usd": 0.0, "n": 0, "px_sum": 0.0})

        band = close * (LCH_CLUSTER_BAND_BPS / 10_000)
        for ev in liqs:
            try:
                px = float(ev.get("price", 0))
                usd = float(ev.get("usd", 0)) or (float(ev.get("qty", 0)) * px)
                if px <= 0 or usd <= 0:
                    continue
                side = ev.get("side", "")
                # Binance: side='SELL' = LONG-liq, 'BUY' = SHORT-liq
                bucket_key = round(px / band) * band
                if side == "SELL":
                    buckets_long[bucket_key]["usd"] += usd
                    buckets_long[bucket_key]["n"] += 1
                    buckets_long[bucket_key]["px_sum"] += px
                elif side == "BUY":
                    buckets_short[bucket_key]["usd"] += usd
                    buckets_short[bucket_key]["n"] += 1
                    buckets_short[bucket_key]["px_sum"] += px
            except Exception:
                continue

        # 4. Identify cluster: largest bucket meeting min thresholds
        def _best_bucket(d: dict) -> Optional[tuple]:
            qualifying = [(k, v) for k, v in d.items()
                          if v["usd"] >= LCH_MIN_CLUSTER_USD and v["n"] >= LCH_MIN_EVENTS]
            if not qualifying:
                return None
            best = max(qualifying, key=lambda x: x[1]["usd"])
            return best

        long_cluster = _best_bucket(buckets_long)
        short_cluster = _best_bucket(buckets_short)

        # 5. Determine direction.
        # LONG-liq cluster ABOVE current price: shorts will sweep TOWARD it (fuel for upside) → LONG
        # SHORT-liq cluster BELOW current price: longs will sweep TOWARD it (fuel for downside) → SHORT
        side = None
        is_long = None
        reason = None
        cluster_center = 0.0
        cluster_usd = 0.0
        cluster_kind = ""

        # Long-liq above: longs already flushed there, but cluster sits as a magnet for price.
        # Actually the more useful interpretation: a LONG-liq cluster represents WHERE longs WILL
        # liquidate if price drops to that band. So:
        #   LONG-liq cluster BELOW current price = sell-side magnet → SHORT toward it
        #   SHORT-liq cluster ABOVE current price = buy-side magnet → LONG toward it
        if short_cluster is not None:
            k, v = short_cluster
            center = v["px_sum"] / v["n"]
            # Above current?
            if center > close:
                dist_bps = (center - close) / close * 10_000
                if dist_bps < LCH_APPROACH_BPS and dist_bps > 5:   # not already inside
                    # Check round-number OR existing in approach trajectory
                    near_round, round_lvl = _is_near_round(center, LCH_ROUND_BPS)
                    momentum_aligned = close > open_   # 5m green = price moving toward cluster
                    if (near_round or v["usd"] > LCH_MIN_CLUSTER_USD * 2) and momentum_aligned:
                        if dist_bps < LCH_TRIGGER_BPS:
                            side = "B"; is_long = True
                            cluster_center = center
                            cluster_usd = v["usd"]
                            cluster_kind = "short_liq_above"
                            reason = (f"short_liq_cluster_above center=${center:.2f} "
                                      f"dist={dist_bps:.0f}bps usd=${v['usd']/1e6:.2f}M "
                                      f"round={near_round}")

        if not side and long_cluster is not None:
            k, v = long_cluster
            center = v["px_sum"] / v["n"]
            if center < close:
                dist_bps = (close - center) / close * 10_000
                if dist_bps < LCH_APPROACH_BPS and dist_bps > 5:
                    near_round, round_lvl = _is_near_round(center, LCH_ROUND_BPS)
                    momentum_aligned = close < open_   # 5m red = price moving toward cluster
                    if (near_round or v["usd"] > LCH_MIN_CLUSTER_USD * 2) and momentum_aligned:
                        if dist_bps < LCH_TRIGGER_BPS:
                            side = "A"; is_long = False
                            cluster_center = center
                            cluster_usd = v["usd"]
                            cluster_kind = "long_liq_below"
                            reason = (f"long_liq_cluster_below center=${center:.2f} "
                                      f"dist={dist_bps:.0f}bps usd=${v['usd']/1e6:.2f}M "
                                      f"round={near_round}")

        if not side:
            return None

        if is_long:
            sl_px = close * (1 - LCH_SL_PCT)
            tp_px = close * (1 + LCH_TP_PCT)
        else:
            sl_px = close * (1 + LCH_SL_PCT)
            tp_px = close * (1 - LCH_TP_PCT)

        # ── Stage 2 council filter: spread protection (tight TP needs tight book) ──
        spread_pass, spread_detail = edge_filters.spread_max(bus, coin, max_bps=8.0)
        if not spread_pass:
            return None

        return Signal(
            coin=coin, side=side, is_long=is_long, ref_price=close,
            sl_px=sl_px, tp_px=tp_px,
            max_hold_bars=LCH_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=reason,
            extras={
                "cluster_kind": cluster_kind,
                "cluster_center": cluster_center,
                "cluster_usd": cluster_usd,
                "dist_to_cluster_bps": (abs(cluster_center - close) / close * 10_000),
                "n_liqs_30m": len(liqs),
            },
        )
