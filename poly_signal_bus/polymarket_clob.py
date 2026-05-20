"""Polymarket CLOB WebSocket + REST market discovery.

Discovers active `btc-updown-5m-*` and `eth-updown-5m-*` markets via REST,
subscribes to their order books via the CLOB WS.

CLOB API base: https://clob.polymarket.com
Markets API:   https://gamma-api.polymarket.com  (the gamma front-end DB)
CLOB WS:       wss://ws-subscriptions-clob.polymarket.com/ws/market

Schema notes:
  - Each PM market has two outcome tokens (YES and NO), each with its own
    token_id (uint256 string).
  - Order book updates arrive as `book` events for token-level books.
  - We track the YES side and derive NO = 1 - YES at the implied-prob layer.
  - Settlement: PM 5m markets resolve to Chainlink BTC/ETH price at the
    end-of-window minute boundary.

Output schema (per market):
  {
    market_id, asset, start_price, start_ts, end_ts, time_remaining,
    token_id_yes, token_id_no,
    yes_bid, yes_ask, yes_mid, no_bid, no_ask, no_mid,
    yes_implied, no_implied,
    last_update_ts,
  }
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Awaitable, Callable, Optional

import httpx
import websockets

log = logging.getLogger("polymarket_clob")

GAMMA_API = os.environ.get("PM_GAMMA_API", "https://gamma-api.polymarket.com")
CLOB_REST = os.environ.get("PM_CLOB_REST", "https://clob.polymarket.com")
CLOB_WS = os.environ.get("PM_CLOB_WS", "wss://ws-subscriptions-clob.polymarket.com/ws/market")


# ────────────────────────── Market discovery ──────────────────────────
async def discover_active_markets(assets: list[str]) -> list[dict]:
    """Poll the Gamma API for active 5m markets matching asset filters.

    Returns list of normalized market dicts.
    """
    markets = []
    async with httpx.AsyncClient(timeout=10) as client:
        params = {
            "active": "true",
            "closed": "false",
            "limit": 100,
            "tag_id": "100200",  # crypto category; adjust based on PM tags
        }
        try:
            r = await client.get(f"{GAMMA_API}/markets", params=params)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning(f"market discovery failed: {e}")
            return markets

        for m in data:
            slug = (m.get("slug") or "").lower()
            asset = None
            for a in assets:
                if f"{a.lower()}-updown-5m" in slug or f"{a.lower()}-up-or-down-5m" in slug:
                    asset = a
                    break
            if not asset:
                continue
            cond_id = m.get("conditionId") or m.get("condition_id")
            if not cond_id:
                continue
            tokens = m.get("tokens") or m.get("clobTokenIds") or []
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except json.JSONDecodeError:
                    tokens = []
            if len(tokens) < 2:
                continue
            yes_id, no_id = tokens[0], tokens[1]
            # Outcome ordering — sometimes the dict has named outcomes
            outcomes = m.get("outcomes") or []
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = []
            if outcomes and outcomes[0].lower() in ("no", "down"):
                yes_id, no_id = no_id, yes_id

            end_ts = _parse_ts(m.get("endDate") or m.get("end_date_iso"))
            start_ts = _parse_ts(m.get("startDate") or m.get("start_date_iso"))
            start_price = float(m.get("startingPrice") or 0)

            markets.append({
                "market_id": cond_id,
                "slug": slug,
                "asset": asset,
                "start_price": start_price,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "token_id_yes": yes_id,
                "token_id_no": no_id,
                "yes_bid": None, "yes_ask": None,
                "no_bid": None,  "no_ask": None,
                "last_update_ts": None,
            })
    return markets


def _parse_ts(s) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        import datetime
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


# ────────────────────────── WS subscription ──────────────────────────
BookHandler = Callable[[str, dict], Awaitable[None]]
# (market_id, parsed_book_dict)


async def run_clob_ws(token_ids: list[str], on_book: BookHandler) -> None:
    """Subscribe to PM CLOB book updates for the given token IDs.

    The CLOB WS expects:
        {"type": "MARKET", "assets_ids": [token_id, ...]}
    Updates arrive as `book` and `price_change` events.
    """
    if not token_ids:
        log.warning("run_clob_ws called with empty token list")
        return
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(CLOB_WS, ping_interval=20) as ws:
                sub = {"type": "MARKET", "assets_ids": token_ids}
                await ws.send(json.dumps(sub))
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    if isinstance(msg, list):
                        for event in msg:
                            await _dispatch_event(event, on_book)
                    else:
                        await _dispatch_event(msg, on_book)
        except Exception as e:
            log.warning(f"clob ws disconnect: {e}; backoff {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def _dispatch_event(event: dict, on_book: BookHandler) -> None:
    ev_type = (event.get("event_type") or event.get("type") or "").lower()
    if ev_type not in ("book", "price_change", "tick_size_change"):
        return
    token_id = event.get("asset_id") or event.get("assetId")
    market_id = event.get("market") or event.get("market_id")
    if not market_id:
        return
    bids = event.get("bids") or []
    asks = event.get("asks") or []
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    await on_book(market_id, {
        "token_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bids": bids[:5],
        "asks": asks[:5],
        "ts": time.time(),
    })


# ────────────────────────── REST: order book snapshot ──────────────────────────
async def get_book_snapshot(token_id: str) -> Optional[dict]:
    """One-shot REST fetch for cold-start before WS catches up."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{CLOB_REST}/book", params={"token_id": token_id})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"book snapshot failed for {token_id}: {e}")
            return None


# ────────────────────────── REST: market metadata refresh ──────────────────────────
async def get_market_metadata(market_id: str) -> Optional[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{CLOB_REST}/markets/{market_id}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"market meta failed for {market_id}: {e}")
            return None
