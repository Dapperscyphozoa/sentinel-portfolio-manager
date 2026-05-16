"""OKX WebSocket subscriber — klines + liquidations + markPrice.

Used as the PRIMARY data source when Binance Futures is geoblocked from the
deployment region (US/EU datacenters). OKX accepts all geographies.

Schema-compatible with binance_ws (writes the same cache fields), so strategies
need no changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Iterable

import websockets

from .cache import Cache


log = logging.getLogger("okx_data_ws")


OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"


# OKX channel → our internal TF tag
_OKX_KLINE_CHANNELS = [
    ("candle1m", "1m"),
    ("candle5m", "5m"),
    ("candle15m", "15m"),
    ("candle1H", "1h"),
]


def _to_inst(coin: str) -> str:
    return f"{coin.upper()}-USDT-SWAP"


def _from_inst(inst: str) -> str:
    if inst.endswith("-USDT-SWAP"):
        return inst[: -len("-USDT-SWAP")]
    return inst


# -------- klines + markPrice (public channel) --------

async def _consume_klines(coins: list[str], cache: Cache) -> None:
    args = []
    for c in coins:
        inst = _to_inst(c)
        for ch, _ in _OKX_KLINE_CHANNELS:
            args.append({"channel": ch, "instId": inst})
        args.append({"channel": "mark-price", "instId": inst})

    async with websockets.connect(OKX_WS_PUBLIC, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
        cache.ws_alive["binance"] = True  # reuse the slot to keep stats consistent
        # OKX accepts up to ~64 subs per message safely; chunk
        CHUNK = 60
        for i in range(0, len(args), CHUNK):
            await ws.send(json.dumps({"op": "subscribe", "args": args[i:i + CHUNK]}))
        log.info("okx public ws subscribed: %d args across %d coins", len(args), len(coins))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            arg = msg.get("arg") or {}
            ch = arg.get("channel", "")
            data = msg.get("data") or []
            if not data:
                continue
            inst = arg.get("instId", "")
            coin = _from_inst(inst)
            if ch.startswith("candle"):
                # find our internal tf
                tf = next((t for c, t in _OKX_KLINE_CHANNELS if c == ch), None)
                if not tf:
                    continue
                for row in data:
                    # row = [ts, o, h, l, c, vol, volCcy, ...]
                    try:
                        bar = {
                            "open_ts": int(row[0]),
                            "open": float(row[1]),
                            "high": float(row[2]),
                            "low": float(row[3]),
                            "close": float(row[4]),
                            "volume": float(row[5]),
                            "closed": bool(int(row[8])) if len(row) > 8 else False,
                        }
                    except (ValueError, IndexError):
                        continue
                    cache.push_kline(coin, tf, bar)
            elif ch == "mark-price":
                for row in data:
                    try:
                        mid = float(row.get("markPx", 0))
                        if mid <= 0:
                            continue
                    except Exception:
                        continue
                    with cache._lock:
                        dq = cache.marks[coin]
                        prev = dq[-1] if dq else {"binance_mid": None, "hl_mid": None}
                        dq.append({
                            "ts": int(time.time() * 1000),
                            "binance_mid": mid,  # OKX serves as binance-equivalent here
                            "hl_mid": prev.get("hl_mid"),
                        })
                    cache.last_update["binance_ws"] = time.time()


# -------- liquidations (business channel) --------

async def _consume_liqs(coins: list[str], cache: Cache) -> None:
    """OKX 'liquidation-orders' channel is on the BUSINESS ws endpoint and
    subscribes by instType, not per-instId. We subscribe SWAP only."""
    async with websockets.connect(OKX_WS_BUSINESS, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
        await ws.send(json.dumps({
            "op": "subscribe",
            "args": [{"channel": "liquidation-orders", "instType": "SWAP"}],
        }))
        log.info("okx business ws subscribed: liquidation-orders SWAP")
        targets = {_to_inst(c) for c in coins}
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            data = msg.get("data") or []
            for d in data:
                # d has instId at top level, details array with each liq
                inst = d.get("instId", "")
                if inst not in targets:
                    continue
                coin = _from_inst(inst)
                for det in d.get("details") or []:
                    try:
                        side = det.get("side", "").upper()  # "buy" or "sell"
                        # In OKX, side describes the position direction being
                        # liquidated. We mirror Binance convention: SELL = long
                        # liquidation (forced market sell), BUY = short liq.
                        # OKX 'side' = the liq order's side, which IS the same
                        # as Binance's force order side semantics.
                        sz = float(det.get("sz", 0))
                        px = float(det.get("bkPx") or det.get("fillPx") or 0)
                        ts = int(det.get("ts", time.time() * 1000))
                    except (ValueError, TypeError):
                        continue
                    if px <= 0 or sz <= 0:
                        continue
                    cache.push_liq({
                        "ts": ts, "coin": coin,
                        "side": "SELL" if side == "SELL" else "BUY",
                        "qty": sz, "price": px, "usd": sz * px,
                    })


# -------- runner --------

async def _runner(coins: list[str], cache: Cache) -> None:
    bo = 1.0

    async def klines():
        nonlocal bo
        while True:
            try:
                await _consume_klines(coins, cache)
            except Exception as e:
                cache.ws_alive["binance"] = False
                log.warning("okx klines ws disconnect: %s; reconnect in %.1fs", e, bo)
                await asyncio.sleep(bo)
                bo = min(bo * 2, 60.0)
            else:
                bo = 1.0

    async def liqs():
        local_bo = 1.0
        while True:
            try:
                await _consume_liqs(coins, cache)
            except Exception as e:
                log.warning("okx liqs ws disconnect: %s; reconnect in %.1fs", e, local_bo)
                await asyncio.sleep(local_bo)
                local_bo = min(local_bo * 2, 60.0)
            else:
                local_bo = 1.0

    await asyncio.gather(klines(), liqs())


def run_in_thread(coins: list[str], cache: Cache) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_runner(coins, cache))
    finally:
        loop.close()
