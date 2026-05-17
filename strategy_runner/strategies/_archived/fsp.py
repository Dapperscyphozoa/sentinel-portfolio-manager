"""fsp — Funding Spike Predator (SPEC §3.1).

LONG when funding ≤ -F_NEG sustained CONSEC hours (shorts paying punitively);
SHORT when ≥ +F_POS sustained CONSEC hours. Fire only on regime ENTRY (i.e.
condition was NOT met on the prior window).
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


class FSP(StrategyBase):
    NAME = "fsp"
    CLOID_PREFIX = "fspv1_"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = "1h"
    UNIVERSE = [
        "INJ", "SNX", "YGG", "FTT", "FET", "ATOM", "SEI", "OP", "APE", "POLYX",
        "GAS", "BSV", "COMP", "DOT", "ARK", "SOL", "LINK", "DOGE", "LTC", "NEAR",
        "SUI", "AVAX", "XRP", "BLUR", "BANANA", "W", "STG", "JUP", "WIF", "TIA",
    ]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        F_NEG = _f("FSP_F_NEG", 0.0003)
        F_POS = _f("FSP_F_POS", 0.0003)
        CONSEC = int(_f("FSP_CONSEC", 3))
        TP_PCT = _f("FSP_TP_PCT", 0.030)
        SL_PCT = _f("FSP_SL_PCT", 0.010)
        MAX_HOLD = int(_f("FSP_MAX_HOLD_H", 48))

        rows = bus.funding(coin, hours=CONSEC + 2)
        if not rows or len(rows) < CONSEC + 1:
            return None
        rates = [float(r["rate"]) for r in rows]

        window = rates[-CONSEC:]
        prior = rates[-(CONSEC + 1):-1]

        all_neg_w = all(r <= -F_NEG for r in window)
        all_neg_p = all(r <= -F_NEG for r in prior)
        all_pos_w = all(r >= F_POS for r in window)
        all_pos_p = all(r >= F_POS for r in prior)

        fire_long = all_neg_w and not all_neg_p
        fire_short = all_pos_w and not all_pos_p
        if not (fire_long or fire_short):
            return None

        mark = bus.markprice(coin)
        ref = mark.get("binance_mid") or mark.get("hl_mid")
        if not ref:
            return None
        ref = float(ref)

        if fire_long:
            return Signal(
                coin=coin, side="B", is_long=True, ref_price=ref,
                sl_px=ref * (1 - SL_PCT), tp_px=ref * (1 + TP_PCT),
                max_hold_bars=MAX_HOLD, fire_ts=time.time() * 1000,
                fire_reason="funding_sustained_negative",
                extras={"window": window, "F_NEG": F_NEG, "consec": CONSEC},
            )
        return Signal(
            coin=coin, side="A", is_long=False, ref_price=ref,
            sl_px=ref * (1 + SL_PCT), tp_px=ref * (1 - TP_PCT),
            max_hold_bars=MAX_HOLD, fire_ts=time.time() * 1000,
            fire_reason="funding_sustained_positive",
            extras={"window": window, "F_POS": F_POS, "consec": CONSEC},
        )
