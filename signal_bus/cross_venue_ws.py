"""OKX + Bybit funding-rate WS subscribers.

We don't need full kline/orderbook feeds for cross-venue arb — only funding rate
divergence. Both venues are subscribed in their own asyncio tasks. Rates land
in the shared cache under (coin, venue) keys.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Iterable

import websockets

from .cache import Cache


log = logging.getLogger("cross_venue_ws")


OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
BYBIT_WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"


def _to_okx_inst(coin: str) -> str:
    return f"{coin.upper()}-USDT-SWAP"


def _to_bybit_inst(coin: str) -> str:
    return f"{coin.upper()}USDT"


# -------- OKX --------

async def _consume_okx(coins: list[str], cache: Cache) -> None:
    args = [{"channel": "funding-rate", "instId": _to_okx_inst(c)} for c in coins]
    async with websockets.connect(OKX_WS_PUBLIC, ping_interval=20, ping_timeout=20) as ws:
        cache.ws_alive["okx"] = True
        await ws.send(json.dumps({"op": "subscribe", "args": args}))
        log.info("okx ws subscribed: %d coins", len(coins))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            data = msg.get("data") or []
            for d in data:
                inst = d.get("instId", "")
                if not inst.endswith("-USDT-SWAP"):
                    continue
                coin = inst.replace("-USDT-SWAP", "")
                try:
                    rate = float(d.get("fundingRate", "0"))
                except Exception:
                    continue
                ts = int(d.get("ts") or time.time() * 1000)
                cache.push_funding(coin, ts, rate, venue="okx")
            cache.last_update["okx_ws"] = time.time()


# -------- Bybit --------

async def _consume_bybit(coins: list[str], cache: Cache) -> None:
    args = [f"tickers.{_to_bybit_inst(c)}" for c in coins]
    async with websockets.connect(BYBIT_WS_PUBLIC, ping_interval=20, ping_timeout=20) as ws:
        cache.ws_alive["bybit"] = True
        # bybit allows up to ~10 subscriptions per message; chunk
        CHUNK = 10
        for i in range(0, len(args), CHUNK):
            await ws.send(json.dumps({"op": "subscribe", "args": args[i:i + CHUNK]}))
        log.info("bybit ws subscribed: %d coins", len(coins))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            topic = msg.get("topic", "")
            if not topic.startswith("tickers."):
                continue
            inst = topic.split(".", 1)[1]
            if not inst.endswith("USDT"):
                continue
            coin = inst[:-4]
            d = msg.get("data") or {}
            fr = d.get("fundingRate")
            if fr is None:
                continue
            try:
                rate = float(fr)
            except Exception:
                continue
            ts = int(d.get("ts") or time.time() * 1000)
            cache.push_funding(coin, ts, rate, venue="bybit")
            cache.last_update["bybit_ws"] = time.time()


# -------- runner --------

async def _runner(coins: list[str], cache: Cache) -> None:
    backoff_okx = 1.0
    backoff_byb = 1.0

    async def run_okx():
        nonlocal backoff_okx
        while True:
            try:
                await _consume_okx(coins, cache)
            except Exception as e:
                cache.ws_alive["okx"] = False
                log.warning("okx ws disconnect: %s; reconnect in %.1fs", e, backoff_okx)
                await asyncio.sleep(backoff_okx)
                backoff_okx = min(backoff_okx * 2, 60.0)
            else:
                backoff_okx = 1.0

    async def run_bybit():
        nonlocal backoff_byb
        while True:
            try:
                await _consume_bybit(coins, cache)
            except Exception as e:
                cache.ws_alive["bybit"] = False
                log.warning("bybit ws disconnect: %s; reconnect in %.1fs", e, backoff_byb)
                await asyncio.sleep(backoff_byb)
                backoff_byb = min(backoff_byb * 2, 60.0)
            else:
                backoff_byb = 1.0

    await asyncio.gather(run_okx(), run_bybit())


def run_in_thread(coins: list[str], cache: Cache) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_runner(coins, cache))
    finally:
        loop.close()
