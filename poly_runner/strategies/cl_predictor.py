"""cl_predictor — Chainlink Data Stream prediction strategy.

THE STRUCTURAL EDGE. Reproduces the Chainlink DON aggregator locally; in the
last 60 seconds of a window, if our predicted resolution diverges from PM
book implied probability by more than fee+threshold, we take the side.

This strategy is GATED by Session 4's cl_aggregator_validate.py. Do not
deploy live unless median validation error < 5bps and p95 < 15bps on
100k+ historical samples.
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


class CLPredictor(StrategyBase):
    NAME = "cl_predictor"
    CAPITAL_FRACTION = float(os.environ.get("CL_PRED_CAPITAL_FRACTION", "0.30"))
    REQUIRES_LIVE_CL = True

    # Config (env-overridable; mirrors POLY_SPEC §3.1)
    DIVERGENCE_THRESH = float(os.environ.get("CL_DIVERGENCE_THRESH", "0.05"))
    FEE_ADJ_EDGE_MIN  = float(os.environ.get("CL_FEE_ADJ_EDGE_MIN", "0.02"))
    WINDOW_TIME_REMAIN_MAX = int(os.environ.get("CL_WINDOW_TIME_REMAIN", "60"))
    MAX_POSITION_USD  = float(os.environ.get("CL_MAX_POSITION_USD", "50"))
    MIN_VENUES = int(os.environ.get("CL_MIN_VENUES_REQUIRED", "5"))

    @classmethod
    def evaluate(cls, market: dict, bus) -> Optional[Signal]:
        end_ts = market.get("end_ts")
        if not end_ts:
            return None
        time_remaining = end_ts - time.time()
        if time_remaining > cls.WINDOW_TIME_REMAIN_MAX or time_remaining < 5:
            return None

        asset = market.get("asset")
        cl = bus.cl_predicted(asset)
        if not cl or cl.get("predicted") is None:
            return None
        n_venues = (cl.get("diag") or {}).get("n_after_trim", 0)
        if n_venues < cls.MIN_VENUES:
            return None
        predicted = cl["predicted"]

        start_price = market.get("start_price")
        if not start_price:
            return None

        sigma = bus.realized_vol(asset, lookback_s=60)
        if sigma <= 0:
            return None

        true_prob_up = true_prob_from_cl(predicted, start_price, time_remaining, sigma)

        ip = bus.implied_prob(market["market_id"]) or {}
        yes_ask = ip.get("yes_ask")
        no_ask = ip.get("no_ask")
        if yes_ask is None or no_ask is None:
            return None
        if not (0.01 < yes_ask < 0.99) or not (0.01 < no_ask < 0.99):
            return None

        fee_yes = dynamic_fee(yes_ask)
        fee_no = dynamic_fee(no_ask)
        edge_yes = true_prob_up - yes_ask - fee_yes
        edge_no = (1 - true_prob_up) - no_ask - fee_no

        best_edge = max(edge_yes, edge_no)
        if best_edge < cls.FEE_ADJ_EDGE_MIN:
            return None

        if edge_yes > edge_no:
            kf = kelly_fraction(edge_yes, true_prob_up)
            size = min(kf * cls.MAX_POSITION_USD, cls.MAX_POSITION_USD)
            if size < 1:
                return None
            return Signal(
                strategy=cls.NAME,
                market_id=market["market_id"],
                asset=asset,
                token="YES",
                side="BUY",
                price=yes_ask,
                size_usdc=round(size, 2),
                order_type="FOK",
                edge_bps=edge_yes * 10000,
                cl_predicted=predicted,
                pm_implied=yes_ask,
                fire_reason=f"yes_edge={edge_yes:.4f} tprob={true_prob_up:.3f}",
                extras={
                    "true_prob_up": true_prob_up,
                    "sigma_per_s": sigma,
                    "time_remaining": time_remaining,
                    "n_venues": n_venues,
                    "fee": fee_yes,
                },
            )
        else:
            kf = kelly_fraction(edge_no, 1 - true_prob_up)
            size = min(kf * cls.MAX_POSITION_USD, cls.MAX_POSITION_USD)
            if size < 1:
                return None
            return Signal(
                strategy=cls.NAME,
                market_id=market["market_id"],
                asset=asset,
                token="NO",
                side="BUY",
                price=no_ask,
                size_usdc=round(size, 2),
                order_type="FOK",
                edge_bps=edge_no * 10000,
                cl_predicted=predicted,
                pm_implied=no_ask,
                fire_reason=f"no_edge={edge_no:.4f} tprob_up={true_prob_up:.3f}",
                extras={
                    "true_prob_up": true_prob_up,
                    "sigma_per_s": sigma,
                    "time_remaining": time_remaining,
                    "n_venues": n_venues,
                    "fee": fee_no,
                },
            )
