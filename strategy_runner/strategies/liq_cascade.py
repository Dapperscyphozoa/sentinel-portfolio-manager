"""liq_cascade — Liquidation Cascade Fader (SPEC §3.8).

Detect cascading liquidations on a coin (cluster of forceOrder events within a
short window crossing a USD threshold) and FADE the move. Long-liquidation
clusters (longs getting flushed → price diving) → LONG; short-liq clusters → SHORT.

Freshness: require that the prior window did NOT already trigger, so we only
fire once per cascade event.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


class LiqCascade(StrategyBase):
    NAME = "liq_cascade"
    CLOID_PREFIX = "liqcs_"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = "1m"
    UNIVERSE = [
        "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "NEAR",
        "SUI", "APT", "ARB", "OP", "INJ", "SEI", "TIA", "WIF", "JUP", "DOT",
    ]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        window_s = int(_f("LC_WINDOW_SEC", 60))
        min_usd = _f("LC_MIN_USD", 250_000)
        min_events = int(_f("LC_MIN_EVENTS", 3))
        sl_pct = _f("LC_SL_PCT", 0.012)
        tp_pct = _f("LC_TP_PCT", 0.025)
        max_hold = int(_f("LC_MAX_HOLD_BARS", 15))

        now_ms = int(time.time() * 1000)
        window_start = now_ms - window_s * 1000
        prior_start = window_start - window_s * 1000
        try:
            recent = bus.liq(since_ms=prior_start, coin=coin)
        except Exception:
            return None
        if not recent:
            return None

        cur_events = [e for e in recent if e["ts"] >= window_start]
        prior_events = [e for e in recent if prior_start <= e["ts"] < window_start]
        if len(cur_events) < min_events:
            return None

        # In Binance forceOrder feed, side='SELL' = LONG liquidations (longs being
        # market-sold), side='BUY' = SHORT liquidations (shorts being market-bought).
        cur_long_liq = sum(e["usd"] for e in cur_events if e["side"] == "SELL")
        cur_short_liq = sum(e["usd"] for e in cur_events if e["side"] == "BUY")
        prior_long_liq = sum(e["usd"] for e in prior_events if e["side"] == "SELL")
        prior_short_liq = sum(e["usd"] for e in prior_events if e["side"] == "BUY")

        cascade_long_liqs = cur_long_liq >= min_usd
        cascade_short_liqs = cur_short_liq >= min_usd
        prior_cascade_long = prior_long_liq >= min_usd
        prior_cascade_short = prior_short_liq >= min_usd

        fire_long = cascade_long_liqs and not prior_cascade_long and cur_long_liq > cur_short_liq
        fire_short = cascade_short_liqs and not prior_cascade_short and cur_short_liq > cur_long_liq
        if not (fire_long or fire_short):
            return None

        mark = bus.markprice(coin)
        ref = mark.get("hl_mid") or mark.get("binance_mid")
        if not ref:
            return None
        ref = float(ref)

        if fire_long:
            return Signal(
                coin=coin, side="B", is_long=True, ref_price=ref,
                sl_px=ref * (1 - sl_pct), tp_px=ref * (1 + tp_pct),
                max_hold_bars=max_hold, fire_ts=time.time() * 1000,
                fire_reason="long_liq_cascade_fade",
                extras={"cur_long_liq_usd": cur_long_liq,
                        "events": len(cur_events), "window_s": window_s},
            )
        return Signal(
            coin=coin, side="A", is_long=False, ref_price=ref,
            sl_px=ref * (1 + sl_pct), tp_px=ref * (1 - tp_pct),
            max_hold_bars=max_hold, fire_ts=time.time() * 1000,
            fire_reason="short_liq_cascade_fade",
            extras={"cur_short_liq_usd": cur_short_liq,
                    "events": len(cur_events), "window_s": window_s},
        )
