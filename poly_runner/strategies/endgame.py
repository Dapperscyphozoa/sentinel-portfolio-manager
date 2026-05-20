"""endgame — last-30s pure pricing.

In the final 30 seconds of each 5m window, direction is no longer
probabilistic — only "where is BTC right now vs threshold." Trade
aggressively when PM book hasn't repriced.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import (
    Signal,
    StrategyBase,
    dynamic_fee,
    kelly_fraction,
    true_prob_from_cl,
)


class Endgame(StrategyBase):
    NAME = "endgame"
    CAPITAL_FRACTION = float(os.environ.get("EG_CAPITAL_FRACTION", "0.20"))
    REQUIRES_LIVE_CL = True

    WINDOW_TIME_REMAIN_MAX = int(os.environ.get("EG_WINDOW_TIME_REMAIN_MAX", "30"))
    WINDOW_TIME_REMAIN_MIN = int(os.environ.get("EG_WINDOW_TIME_REMAIN_MIN", "5"))
    DIVERGENCE_THRESH = float(os.environ.get("EG_DIVERGENCE_THRESH", "0.03"))
    VOL_GATE_MIN = float(os.environ.get("EG_VOL_GATE_REALIZED_MIN", "0.0005"))
    MAX_POSITION_USD = float(os.environ.get("EG_MAX_POSITION_USD", "30"))

    @classmethod
    def evaluate(cls, market: dict, bus) -> Optional[Signal]:
        end_ts = market.get("end_ts")
        if not end_ts:
            return None
        tr = end_ts - time.time()
        if tr > cls.WINDOW_TIME_REMAIN_MAX or tr < cls.WINDOW_TIME_REMAIN_MIN:
            return None

        asset = market.get("asset")
        cl = bus.cl_predicted(asset)
        if not cl or cl.get("predicted") is None:
            return None
        predicted = cl["predicted"]
        start_price = market.get("start_price")
        if not start_price:
            return None

        sigma = bus.realized_vol(asset, lookback_s=60)
        if sigma < cls.VOL_GATE_MIN:
            return None

        true_prob_up = true_prob_from_cl(predicted, start_price, tr, sigma)

        ip = bus.implied_prob(market["market_id"]) or {}
        yes_ask = ip.get("yes_ask")
        no_ask = ip.get("no_ask")
        if yes_ask is None or no_ask is None:
            return None
        if not (0.01 < yes_ask < 0.99):
            return None

        fee_yes = dynamic_fee(yes_ask)
        fee_no = dynamic_fee(no_ask)
        edge_yes = true_prob_up - yes_ask - fee_yes
        edge_no = (1 - true_prob_up) - no_ask - fee_no
        best = max(edge_yes, edge_no)
        if best < cls.DIVERGENCE_THRESH:
            return None

        if edge_yes >= edge_no:
            kf = kelly_fraction(edge_yes, true_prob_up)
            size = min(kf * cls.MAX_POSITION_USD, cls.MAX_POSITION_USD)
            if size < 1:
                return None
            return Signal(
                strategy=cls.NAME, market_id=market["market_id"], asset=asset,
                token="YES", side="BUY", price=yes_ask,
                size_usdc=round(size, 2), order_type="FOK",
                edge_bps=edge_yes * 10000,
                cl_predicted=predicted, pm_implied=yes_ask,
                fire_reason=f"endgame_yes tr={tr:.0f}s tprob={true_prob_up:.3f}",
                extras={"true_prob_up": true_prob_up, "tr": tr, "sigma": sigma},
            )
        kf = kelly_fraction(edge_no, 1 - true_prob_up)
        size = min(kf * cls.MAX_POSITION_USD, cls.MAX_POSITION_USD)
        if size < 1:
            return None
        return Signal(
            strategy=cls.NAME, market_id=market["market_id"], asset=asset,
            token="NO", side="BUY", price=no_ask,
            size_usdc=round(size, 2), order_type="FOK",
            edge_bps=edge_no * 10000,
            cl_predicted=predicted, pm_implied=no_ask,
            fire_reason=f"endgame_no tr={tr:.0f}s tprob_up={true_prob_up:.3f}",
            extras={"true_prob_up": true_prob_up, "tr": tr, "sigma": sigma},
        )
