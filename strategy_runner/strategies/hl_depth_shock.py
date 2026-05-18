"""hl_depth_shock — orderbook liquidity-shock fade engine.

Stage 1 #6. Council 4/4 voters mentioned, +2.1%/mo est on altcoins.

Mechanic (mean-reversion):
  When bid-side depth at ±0.5% from mid drops >30% within 5 seconds AND
  price has NOT yet moved >10bps, the liquidity has been pulled but price
  hasn't yet caught down. This is the "calm before the drop" — fade by SHORTING.

  Mirror for ask-side shock → LONG.

  Anti-pattern: if price already moved with the depth pull (>10bps), the
  shock has been priced in — skip.

  Hold: short — 5-15min. Liquidity shocks resolve quickly.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from common import edge_filters
from strategy_runner.strategies._base import Signal, StrategyBase


log = logging.getLogger("strategy.hl_depth_shock")


def _f(k: str, d: float) -> float:
    try:
        return float(os.environ.get(k, d))
    except Exception:
        return d


DS_WINDOW_S      = int(_f("DS_WINDOW_S", 5))
DS_SHOCK_PCT_MIN = _f("DS_SHOCK_PCT_MIN", 30.0)              # ≥30% depth drop
DS_PRICE_MOVE_MAX_BPS = _f("DS_PRICE_MOVE_MAX_BPS", 10.0)    # price moved <10bps
DS_MIN_DEPTH_BEFORE_USD = _f("DS_MIN_DEPTH_BEFORE_USD", 30_000.0)
DS_SL_PCT        = _f("DS_SL_PCT", 0.003)                    # tight — short hold
DS_TP_PCT        = _f("DS_TP_PCT", 0.006)                    # 2:1 R:R
DS_MAX_HOLD_BARS = int(_f("DS_MAX_HOLD_BARS", 3))            # 15min on 5m
DS_TF            = "5m"


class HLDepthShock(StrategyBase):
    NAME = "hl_depth_shock"
    CLOID_PREFIX = "dpshk"
    AFFINITY = ["range", "chop", "high_vol"]
    TF = DS_TF
    # Altcoin-heavy universe per Qwen3 235B finding (+2.1%/mo on SOL/WIF/JUP)
    UNIVERSE = ["SOL", "AVAX", "LINK", "DOGE", "NEAR", "SUI", "APT", "ARB",
                "OP", "INJ", "SEI", "WIF", "DOT", "TIA", "JUP"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            ds = bus.depth_shock(coin, window_s=DS_WINDOW_S)
        except Exception:
            return None

        kind = ds.get("shock_kind")
        if not kind:
            return None

        if kind == "bid":
            # Bid liquidity pulled, price hasn't dropped yet → fade with SHORT
            if ds.get("bid_shock_pct", 0) < DS_SHOCK_PCT_MIN:
                return None
            if abs(ds.get("price_move_bps", 0)) > DS_PRICE_MOVE_MAX_BPS:
                return None
            if ds.get("bid_depth_now_usd", 0) < DS_MIN_DEPTH_BEFORE_USD * (1 - DS_SHOCK_PCT_MIN/100):
                # Original depth was tiny — not a meaningful shock
                pass   # allow; flag in reason
            side = "A"; is_long = False
            mid = ds.get("mid", 0)
            sl_px = mid * (1 + DS_SL_PCT)
            tp_px = mid * (1 - DS_TP_PCT)
            reason = (f"bid_shock={ds['bid_shock_pct']:.1f}% spread={ds.get('spread_bps',0):.1f}bps "
                      f"price_lagging={ds.get('price_move_bps',0):.1f}bps")
        elif kind == "ask":
            # Ask liquidity pulled, price hasn't risen yet → fade with LONG
            if ds.get("ask_shock_pct", 0) < DS_SHOCK_PCT_MIN:
                return None
            if abs(ds.get("price_move_bps", 0)) > DS_PRICE_MOVE_MAX_BPS:
                return None
            side = "B"; is_long = True
            mid = ds.get("mid", 0)
            sl_px = mid * (1 - DS_SL_PCT)
            tp_px = mid * (1 + DS_TP_PCT)
            reason = (f"ask_shock={ds['ask_shock_pct']:.1f}% spread={ds.get('spread_bps',0):.1f}bps "
                      f"price_lagging={ds.get('price_move_bps',0):.1f}bps")
        else:
            return None

        if mid <= 0:
            return None

        # ── Stage 2 council filter: CVD alignment confirms direction ──
        cvd_pass, cvd_detail = edge_filters.cvd_alignment(
            bus, coin, is_long, window_ms=15_000, min_z=0.2, min_ratio=0.52,
        )
        if not cvd_pass:
            return None

        return Signal(
            coin=coin, side=side, is_long=is_long, ref_price=mid,
            sl_px=sl_px, tp_px=tp_px,
            max_hold_bars=DS_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=reason,
            extras={
                "shock_kind": kind,
                "bid_shock_pct": ds.get("bid_shock_pct"),
                "ask_shock_pct": ds.get("ask_shock_pct"),
                "price_move_bps": ds.get("price_move_bps"),
                "spread_bps": ds.get("spread_bps"),
                "samples": ds.get("samples"),
            },
        )
