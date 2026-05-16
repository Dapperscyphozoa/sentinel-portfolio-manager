"""cex_dex_arb — Cross-venue funding rate arbitrage (SPEC §3.9).

Two scenarios:
  A) HL funding > CEX funding by > threshold → SHORT HL (collect funding from
     shorting expensive side), LONG cheap CEX hedge
  B) HL funding < CEX funding by > threshold → LONG HL, SHORT cheap CEX hedge

We only execute the HL side (operator-controlled wallet); the cex hedge is
TRACKED in extras for monitoring. The TRUE arb requires a cex hedge to materialise
the funding capture; without it the strategy is a directional funding-tilt play.

WARNING: predecessor strategy returned fictional PF claims. Run honest_backtest
before any live capital.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


class CexDexArb(StrategyBase):
    NAME = "cex_dex_arb"
    CLOID_PREFIX = "cxdxa_"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = "1h"
    UNIVERSE = [
        "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "NEAR",
        "SUI", "APT", "ARB", "OP", "INJ", "SEI", "TIA", "WIF", "JUP", "DOT",
    ]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # threshold in funding-rate units per 8h (Binance) — convert OKX (8h)
        # and Bybit (8h) to same units. We treat all as 8h.
        thr = _f("CDA_FUNDING_SPREAD_THR", 0.00025)
        sl_pct = _f("CDA_SL_PCT", 0.012)
        tp_pct = _f("CDA_TP_PCT", 0.020)
        max_hold = int(_f("CDA_MAX_HOLD_BARS", 8))

        try:
            grouped = bus.funding_multi(coin, hours=2)
        except Exception:
            return None
        if not grouped:
            return None

        # Latest funding per venue (we use HL == ‘hyperliquid' if observed via webData2
        # or fall back to binance as the “DEX-side proxy" if HL not present).
        def latest(rows):
            if not rows:
                return None
            rows = sorted(rows, key=lambda r: r["ts"])
            return rows[-1]["rate"]

        binance_r = latest(grouped.get("binance"))
        okx_r     = latest(grouped.get("okx"))
        bybit_r   = latest(grouped.get("bybit"))
        hl_r      = latest(grouped.get("hyperliquid"))

        # HL acts as the “DEX" side. CEX side = max-magnitude across cex venues.
        if hl_r is None:
            return None
        cex_candidates = [r for r in (binance_r, okx_r, bybit_r) if r is not None]
        if not cex_candidates:
            return None
        cex_r = max(cex_candidates, key=lambda r: abs(r))
        spread = hl_r - cex_r

        if abs(spread) < thr:
            return None

        mark = bus.markprice(coin)
        ref = mark.get("hl_mid") or mark.get("binance_mid")
        if not ref:
            return None
        ref = float(ref)

        # HL funding HIGHER than CEX → longs paying more on HL → expect price
        # to converge down on HL → SHORT HL
        is_short_hl = spread > 0
        if is_short_hl:
            return Signal(
                coin=coin, side="A", is_long=False, ref_price=ref,
                sl_px=ref * (1 + sl_pct), tp_px=ref * (1 - tp_pct),
                max_hold_bars=max_hold, fire_ts=time.time() * 1000,
                fire_reason="hl_funding_premium_short",
                extras={"hl_r": hl_r, "cex_r": cex_r, "spread": spread,
                        "hedge_recommend": {"venue": "cex", "side": "long"}},
            )
        return Signal(
            coin=coin, side="B", is_long=True, ref_price=ref,
            sl_px=ref * (1 - sl_pct), tp_px=ref * (1 + tp_pct),
            max_hold_bars=max_hold, fire_ts=time.time() * 1000,
            fire_reason="hl_funding_discount_long",
            extras={"hl_r": hl_r, "cex_r": cex_r, "spread": spread,
                    "hedge_recommend": {"venue": "cex", "side": "short"}},
        )
