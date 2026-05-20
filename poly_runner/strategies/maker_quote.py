"""maker_quote — continuous LP with 25% taker-rebate capture.

Posts limit orders both sides of fair value (computed from cl_predictor's
true_prob), inventory-aware skew, ~4 Hz refresh per market. Latency
doesn't dominate; pricing accuracy does.

This strategy exposes a different contract from take-side strategies — it
returns a Quote (bid+ask pair) per market, not a one-shot Signal. The
runner has a dedicated maker_loop that calls cls.quote_market(...) every
MM_REQUOTE_INTERVAL_MS.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import (
    Quote,
    StrategyBase,
    dynamic_fee,
    true_prob_from_cl,
)


class MakerQuote(StrategyBase):
    NAME = "maker_quote"
    CAPITAL_FRACTION = float(os.environ.get("MM_CAPITAL_FRACTION", "0.30"))
    REQUIRES_LIVE_CL = True

    QUOTE_SPREAD_BPS = float(os.environ.get("MM_QUOTE_SPREAD_BPS", "80"))
    INVENTORY_SKEW = float(os.environ.get("MM_INVENTORY_SKEW_RISK", "2.0"))
    MAX_INVENTORY_USD = float(os.environ.get("MM_MAX_INVENTORY_USD", "100"))
    REQUOTE_INTERVAL_MS = int(os.environ.get("MM_REQUOTE_INTERVAL_MS", "250"))
    CANCEL_ON_TICK_PCT = float(os.environ.get("MM_CANCEL_ON_TICK_PCT", "0.001"))
    QUOTE_SIZE_USDC = float(os.environ.get("MM_QUOTE_SIZE_USDC", "10"))

    # Class-level state: previous fair_prob per market (for tick-shift detection)
    _last_fair: dict[str, float] = {}

    @classmethod
    def quote_market(cls, market: dict, bus, inventory_usdc: float = 0.0
                     ) -> Optional[Quote]:
        """Return a Quote pair, or None if conditions not met."""
        end_ts = market.get("end_ts")
        if not end_ts:
            return None
        tr = end_ts - time.time()
        # Don't make markets in the last 30s — endgame takes over
        if tr < 30 or tr > 280:
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
        if sigma <= 0:
            return None
        fair_prob = true_prob_from_cl(predicted, start_price, tr, sigma)

        # Inventory skew: positive inventory in YES means we want to bleed
        # YES, so shift quotes down (more aggressive ask, less aggressive bid)
        skew = cls.INVENTORY_SKEW * (inventory_usdc / cls.MAX_INVENTORY_USD)
        spread = cls.QUOTE_SPREAD_BPS / 10000.0

        bid_yes = max(0.02, fair_prob - spread - skew)
        ask_yes = min(0.98, fair_prob + spread - skew)
        if ask_yes <= bid_yes:
            return None

        cls._last_fair[market["market_id"]] = fair_prob

        return Quote(
            strategy=cls.NAME,
            market_id=market["market_id"],
            token="YES",
            bid_price=round(bid_yes, 3),
            bid_size_usdc=cls.QUOTE_SIZE_USDC,
            ask_price=round(ask_yes, 3),
            ask_size_usdc=cls.QUOTE_SIZE_USDC,
            fair_prob=fair_prob,
            inventory=inventory_usdc,
            extras={"tr": tr, "sigma": sigma, "skew": skew},
        )

    @classmethod
    def should_cancel(cls, market_id: str, current_fair: float) -> bool:
        prev = cls._last_fair.get(market_id)
        if prev is None:
            return False
        return abs(current_fair - prev) > cls.CANCEL_ON_TICK_PCT
