"""hl_whale_frontrun — front-run new position opens by top HL wallets.

Stage 1 #5. Council pick (Qwen3 235B) rated +3.2%/mo paper-tested — highest
single-engine edge in Stage 1 lineup.

WORLD-FIRST EDGE on HL specifically. HL is the only major perp venue that
exposes per-wallet position data publicly. CEX (Binance, OKX, Bybit) keep
positions private. This engine exploits the asymmetric transparency.

Mechanic:
  1. signal_bus.whale_poller maintains a list of top-20 HL wallets by 7d PnL,
     polled every 60s for position changes.
  2. Each detected new open / flip / grow is pushed to cache.whale_events.
  3. This engine looks at events in the last ENGINE_EVENT_WINDOW_S window.
  4. Filter: only act on events with notional ≥ ENGINE_MIN_NOTIONAL_USD (so we
     copy big-position opens, not dust).
  5. Filter: skip if multiple top whales took OPPOSITE directions on the same
     coin in the window (signal cancelled).
  6. Confirmation: 5m bar in same direction (don't fade the whale into a
     reversal — only copy when momentum aligned).
  7. Entry: market order on the same side as the whale.
  8. SL: 1.0% (whales hold longer than us — we just want first impulse).
  9. TP: 2.0% (2:1 R:R).
  10. Time stop: 60min — if the whale's directional pressure doesn't move
      price in our favor within 1h, the alpha has decayed.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from strategy_runner.strategies._base import Signal, StrategyBase


log = logging.getLogger("strategy.hl_whale_frontrun")


def _f(k: str, d: float) -> float:
    try:
        return float(os.environ.get(k, d))
    except Exception:
        return d


WF_EVENT_WINDOW_S    = int(_f("WF_EVENT_WINDOW_S", 300))      # consider events last 5min
WF_MIN_NOTIONAL_USD  = _f("WF_MIN_NOTIONAL_USD", 250_000.0)   # only copy ≥$250k opens
WF_REQUIRE_MOMENTUM  = int(_f("WF_REQUIRE_MOMENTUM", 1))      # 1=require 5m alignment
WF_SL_PCT            = _f("WF_SL_PCT", 0.010)
WF_TP_PCT            = _f("WF_TP_PCT", 0.020)
WF_MAX_HOLD_BARS     = int(_f("WF_MAX_HOLD_BARS", 12))        # 60min on 5m
WF_TF                = "5m"


class HLWhaleFrontrun(StrategyBase):
    NAME = "hl_whale_frontrun"
    CLOID_PREFIX = "whlfr"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = WF_TF
    UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK",
                "LTC", "NEAR", "SUI", "APT", "ARB", "OP", "INJ", "SEI"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # 1. Pull whale events for this coin in window
        try:
            since = int(time.time() * 1000) - (WF_EVENT_WINDOW_S * 1000)
            events = bus.whale_events(since_ms=since, coin=coin)
        except Exception:
            return None

        if not events:
            return None

        # 2. Filter by notional + dedupe by wallet (count each whale once per cycle)
        seen_wallets: set = set()
        long_count = 0
        short_count = 0
        long_total_ntl = 0.0
        short_total_ntl = 0.0
        whales_long: list = []
        whales_short: list = []

        for ev in events:
            ntl = float(ev.get("ntl_usd", 0))
            if ntl < WF_MIN_NOTIONAL_USD:
                continue
            wallet = ev.get("wallet", "")
            if wallet in seen_wallets:
                continue
            seen_wallets.add(wallet)
            if ev.get("is_long"):
                long_count += 1
                long_total_ntl += ntl
                whales_long.append(wallet[:10])
            else:
                short_count += 1
                short_total_ntl += ntl
                whales_short.append(wallet[:10])

        if long_count == 0 and short_count == 0:
            return None

        # 3. Direction conflict filter — if both sides have whales, signal is cancelled
        if long_count > 0 and short_count > 0:
            # Tie-break: use net notional, but only if dominant side is >2x the other
            if long_total_ntl > short_total_ntl * 2:
                direction = "long"
            elif short_total_ntl > long_total_ntl * 2:
                direction = "short"
            else:
                return None
        elif long_count > 0:
            direction = "long"
        else:
            direction = "short"

        # 4. Momentum confirmation
        try:
            bars = bus.candles(coin, WF_TF, n=3)
        except Exception:
            return None
        if not bars or len(bars) < 2:
            return None
        last = bars[-1]
        close = float(last["close"])
        open_ = float(last["open"])
        if close <= 0:
            return None

        if WF_REQUIRE_MOMENTUM:
            if direction == "long" and close <= open_:
                return None   # whales long but price red — wait
            if direction == "short" and close >= open_:
                return None

        if direction == "long":
            side = "B"; is_long = True
            sl_px = close * (1 - WF_SL_PCT)
            tp_px = close * (1 + WF_TP_PCT)
            reason = (f"whales_long={long_count} ntl=${long_total_ntl/1e6:.2f}M "
                      f"5m_green wallets={whales_long}")
        else:
            side = "A"; is_long = False
            sl_px = close * (1 + WF_SL_PCT)
            tp_px = close * (1 - WF_TP_PCT)
            reason = (f"whales_short={short_count} ntl=${short_total_ntl/1e6:.2f}M "
                      f"5m_red wallets={whales_short}")

        return Signal(
            coin=coin, side=side, is_long=is_long, ref_price=close,
            sl_px=sl_px, tp_px=tp_px,
            max_hold_bars=WF_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=reason,
            extras={
                "n_whales_long": long_count,
                "n_whales_short": short_count,
                "total_long_ntl_usd": long_total_ntl,
                "total_short_ntl_usd": short_total_ntl,
                "window_s": WF_EVENT_WINDOW_S,
                "min_ntl_usd": WF_MIN_NOTIONAL_USD,
                "wallets_long": whales_long[:5],
                "wallets_short": whales_short[:5],
            },
        )
