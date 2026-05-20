"""cross_asset — BTC 5m ↔ ETH 5m correlation spread.

When BTC and ETH 5m windows open synchronously and their implied
probabilities diverge beyond historical correlation expectation, fade.
Two-leg trade — both legs hedge net delta, pure spread capture.
"""
from __future__ import annotations

import math
import os
import time
from typing import Optional

from ._base import (
    Signal,
    StrategyBase,
    dynamic_fee,
)


class CrossAsset(StrategyBase):
    NAME = "cross_asset"
    CAPITAL_FRACTION = float(os.environ.get("XA_CAPITAL_FRACTION", "0.10"))
    REQUIRES_LIVE_CL = False  # Doesn't need CL to fire

    DIVERGENCE_THRESH = float(os.environ.get("XA_DIVERGENCE_THRESH", "0.07"))
    CORRELATION_MIN = float(os.environ.get("XA_CORRELATION_MIN", "0.65"))
    LOOKBACK_BARS = int(os.environ.get("XA_CORRELATION_LOOKBACK_BARS", "180"))
    MAX_POSITION_USD = float(os.environ.get("XA_MAX_POSITION_USD", "40"))

    # Lookback-based correlation estimate from Binance ticks
    @classmethod
    def _correlation(cls, bus) -> Optional[float]:
        try:
            r = bus._get("/candles/binance/BTC/1s",
                         params={"n": cls.LOOKBACK_BARS})
            btc = [c["mid"] for c in r if c.get("mid")]
            r2 = bus._get("/candles/binance/ETH/1s",
                          params={"n": cls.LOOKBACK_BARS})
            eth = [c["mid"] for c in r2 if c.get("mid")]
        except Exception:
            return None
        n = min(len(btc), len(eth))
        if n < 30:
            return None
        btc = btc[-n:]; eth = eth[-n:]
        btc_ret = [math.log(btc[i] / btc[i-1]) for i in range(1, n) if btc[i-1] > 0]
        eth_ret = [math.log(eth[i] / eth[i-1]) for i in range(1, n) if eth[i-1] > 0]
        if len(btc_ret) < 20:
            return None
        return _pearson(btc_ret, eth_ret)

    @classmethod
    def evaluate(cls, bus) -> Optional[list[Signal]]:
        """Returns a 2-signal list (BTC leg + ETH leg), or None.

        Note: this strategy is dispatched at runner-level (one call per
        scan cycle, not per market) because it needs both BTC and ETH
        markets simultaneously.
        """
        markets = bus.market_list()
        btc_market = None; eth_market = None
        now = time.time()
        for m in markets:
            asset = m.get("asset"); end_ts = m.get("end_ts")
            if not end_ts or end_ts < now or end_ts - now < 60:
                continue
            if asset == "BTC" and not btc_market:
                btc_market = m
            elif asset == "ETH" and not eth_market:
                eth_market = m
        if not btc_market or not eth_market:
            return None
        # Synchronized windows only (start within 10s of each other)
        st_btc = btc_market.get("start_ts"); st_eth = eth_market.get("start_ts")
        if not st_btc or not st_eth or abs(st_btc - st_eth) > 10:
            return None

        ip_btc = bus.implied_prob(btc_market["market_id"]) or {}
        ip_eth = bus.implied_prob(eth_market["market_id"]) or {}
        yes_mid_btc = ip_btc.get("yes_mid")
        yes_mid_eth = ip_eth.get("yes_mid")
        if yes_mid_btc is None or yes_mid_eth is None:
            return None

        corr = cls._correlation(bus)
        if corr is None or corr < cls.CORRELATION_MIN:
            return None

        spread = yes_mid_btc - yes_mid_eth
        if abs(spread) < cls.DIVERGENCE_THRESH:
            return None

        # Determine legs
        if spread > 0:
            # BTC overpriced → sell BTC YES, buy ETH YES
            btc_token, btc_side = "YES", "SELL"
            eth_token, eth_side = "YES", "BUY"
            btc_px = ip_btc.get("yes_bid") or yes_mid_btc
            eth_px = ip_eth.get("yes_ask") or yes_mid_eth
        else:
            btc_token, btc_side = "YES", "BUY"
            eth_token, eth_side = "YES", "SELL"
            btc_px = ip_btc.get("yes_ask") or yes_mid_btc
            eth_px = ip_eth.get("yes_bid") or yes_mid_eth

        size = cls.MAX_POSITION_USD
        edge_bps = abs(spread) * 10000

        return [
            Signal(
                strategy=cls.NAME, market_id=btc_market["market_id"],
                asset="BTC", token=btc_token, side=btc_side,
                price=btc_px, size_usdc=size, order_type="FOK",
                edge_bps=edge_bps, pm_implied=yes_mid_btc,
                fire_reason=f"xa_spread={spread:.3f} corr={corr:.2f}",
                extras={"leg": "btc", "corr": corr, "spread": spread,
                        "yes_mid_btc": yes_mid_btc, "yes_mid_eth": yes_mid_eth},
            ),
            Signal(
                strategy=cls.NAME, market_id=eth_market["market_id"],
                asset="ETH", token=eth_token, side=eth_side,
                price=eth_px, size_usdc=size, order_type="FOK",
                edge_bps=edge_bps, pm_implied=yes_mid_eth,
                fire_reason=f"xa_spread={spread:.3f} corr={corr:.2f}",
                extras={"leg": "eth", "corr": corr, "spread": spread,
                        "yes_mid_btc": yes_mid_btc, "yes_mid_eth": yes_mid_eth},
            ),
        ]


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx = sum(xs) / n; my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)
