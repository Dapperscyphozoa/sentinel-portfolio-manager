"""Oracle-lag detection: HL mark price vs CEX (Binance) reference.

When a new HL listing appears, HL's oracle price often lags the CEX consensus
(or precedes when no CEX equivalent exists). The sniper edge is to:
  1) detect listing
  2) check if same coin trades on Binance
  3) if Binance price differs from HL mark by > threshold, fire trade toward CEX

Returns SnipeDecision with direction + size + expected exit.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("oracle_lag")


HL_INFO = "https://api.hyperliquid.xyz/info"
BINANCE_TICKER = "https://fapi.binance.com/fapi/v1/ticker/price"
BYBIT_TICKER = "https://api.bybit.com/v5/market/tickers"


@dataclass
class SnipeDecision:
    coin: str
    fire: bool
    is_long: bool
    hl_mark: float
    cex_mid: float
    divergence_pct: float        # positive = HL below CEX (long opportunity)
    reason: str
    cex_source: str = ""
    tp_pct: float = 0.05
    sl_pct: float = 0.05
    max_hold_s: int = 1800       # 30 min


def fetch_hl_mark(coin: str, timeout_s: float = 5.0) -> Optional[float]:
    try:
        with httpx.Client(timeout=timeout_s) as cli:
            r = cli.post(HL_INFO, json={"type": "allMids"})
            r.raise_for_status()
            mids = r.json()
        if coin in mids:
            return float(mids[coin])
    except Exception as e:
        log.warning("fetch_hl_mark(%s) failed: %s", coin, e)
    return None


def fetch_binance_price(coin: str, timeout_s: float = 5.0) -> Optional[float]:
    """Try Binance USDT perp. Returns None if symbol not listed."""
    symbol = f"{coin}USDT"
    try:
        with httpx.Client(timeout=timeout_s) as cli:
            r = cli.get(BINANCE_TICKER, params={"symbol": symbol})
            if r.status_code == 200:
                return float(r.json()["price"])
    except Exception as e:
        log.debug("fetch_binance_price(%s) failed: %s", coin, e)
    return None


def fetch_bybit_price(coin: str, timeout_s: float = 5.0) -> Optional[float]:
    """Try Bybit linear perp (USDT-margined)."""
    symbol = f"{coin}USDT"
    try:
        with httpx.Client(timeout=timeout_s) as cli:
            r = cli.get(BYBIT_TICKER, params={"category": "linear", "symbol": symbol})
            if r.status_code == 200:
                data = r.json()
                if data.get("retCode") == 0:
                    tickers = data.get("result", {}).get("list", [])
                    if tickers:
                        return float(tickers[0]["lastPrice"])
    except Exception as e:
        log.debug("fetch_bybit_price(%s) failed: %s", coin, e)
    return None


def cex_consensus(coin: str, min_venues: int = 1) -> tuple[Optional[float], str]:
    """Return (consensus_mid, source_description) across CEX venues."""
    prices = {}
    p = fetch_binance_price(coin)
    if p: prices["binance"] = p
    p = fetch_bybit_price(coin)
    if p: prices["bybit"] = p
    if len(prices) < min_venues:
        return None, "no_cex_listing"
    avg = sum(prices.values()) / len(prices)
    return avg, f"{','.join(prices.keys())}"


def evaluate_snipe(coin: str, divergence_threshold: float = 0.05,
                   require_cex: bool = True,
                   listing_age_s: float = 0.0) -> SnipeDecision:
    """Evaluate whether to snipe a new listing.

    Args:
        coin: HL coin symbol (e.g. "NEWCOIN")
        divergence_threshold: minimum |HL-CEX|/CEX to fire (default 5%)
        require_cex: if True, no trade fires when coin doesn't exist on CEX
        listing_age_s: how old the listing is (used for diagnostics)

    Returns: SnipeDecision (always returned, fire=False if no opportunity)
    """
    hl_mark = fetch_hl_mark(coin)
    if hl_mark is None:
        return SnipeDecision(coin, False, False, 0.0, 0.0, 0.0,
                            "hl_mark_unavailable")

    cex_mid, cex_src = cex_consensus(coin)
    if cex_mid is None:
        return SnipeDecision(coin, False, False, hl_mark, 0.0, 0.0,
                            "no_cex_listing")

    divergence_pct = (cex_mid - hl_mark) / cex_mid   # +ve = HL below CEX
    abs_div = abs(divergence_pct)

    if abs_div < divergence_threshold:
        return SnipeDecision(coin, False, divergence_pct > 0, hl_mark, cex_mid,
                            divergence_pct,
                            f"div_below_threshold:{abs_div:.4f}<{divergence_threshold:.4f}",
                            cex_source=cex_src)

    # Direction: trade HL toward CEX consensus
    # HL below CEX → long HL (buy cheap, expect convergence up)
    # HL above CEX → short HL
    is_long = divergence_pct > 0
    return SnipeDecision(
        coin=coin, fire=True, is_long=is_long,
        hl_mark=hl_mark, cex_mid=cex_mid, divergence_pct=divergence_pct,
        reason=f"divergence={divergence_pct:+.2%}",
        cex_source=cex_src,
        tp_pct=min(abs_div * 0.7, 0.05),    # target capturing 70% of divergence, cap at 5%
        sl_pct=0.05,                        # 5% adverse cap
        max_hold_s=1800,
    )
