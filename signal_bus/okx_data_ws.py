"""OKX WebSocket subscriber — klines + liquidations + markPrice.

Used as the PRIMARY data source when Binance Futures is geoblocked from the
deployment region (US/EU datacenters). OKX accepts all geographies.

Per OKX v5 docs:
  - candle* channels   → BUSINESS endpoint
  - mark-price channel → PUBLIC endpoint
  - liquidation-orders → BUSINESS endpoint
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


_OKX_KLINE_CHANNELS = [
    ("candle1m", "1m"),
    ("candle5m", "5m"),
    ("candle15m", "15m"),
    ("candle1H", "1h"),
    ("candle4H", "4h"),
    ("candle1D", "1d"),
]


def _to_inst(coin: str) -> str:
    return f"{coin.upper()}-USDT-SWAP"


def _from_inst(inst: str) -> str:
    if inst.endswith("-USDT-SWAP"):
        return inst[: -len("-USDT-SWAP")]
    return inst


def _process_event_response(msg: dict) -> bool:
    """Log subscribe / error events. Returns True if msg is a control event
    (caller should skip data processing)."""
    ev = msg.get("event")
    if ev == "subscribe":
        return True
    if ev == "error":
        log.warning("okx subscribe error code=%s msg=%s arg=%s",
                    msg.get("code"), msg.get("msg"), msg.get("arg"))
        return True
    return False


# -------- mark-price + ticker (public) --------

async def _consume_public(coins: list[str], cache: Cache) -> None:
    args = [{"channel": "mark-price", "instId": _to_inst(c)} for c in coins]
    async with websockets.connect(OKX_WS_PUBLIC, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
        cache.ws_alive["binance"] = True  # reuse slot for stats consistency
        CHUNK = 60
        for i in range(0, len(args), CHUNK):
            await ws.send(json.dumps({"op": "subscribe", "args": args[i:i + CHUNK]}))
        log.info("okx PUBLIC ws subscribed: mark-price for %d coins", len(coins))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if _process_event_response(msg):
                continue
            arg = msg.get("arg") or {}
            ch = arg.get("channel", "")
            data = msg.get("data") or []
            if not data:
                continue
            coin = _from_inst(arg.get("instId", ""))
            if ch == "mark-price":
                for row in data:
                    try:
                        mid = float(row.get("markPx", 0))
                    except Exception:
                        continue
                    if mid <= 0:
                        continue
                    with cache._lock:
                        dq = cache.marks[coin]
                        prev = dq[-1] if dq else {"binance_mid": None, "hl_mid": None}
                        dq.append({
                            "ts": int(time.time() * 1000),
                            "binance_mid": mid,
                            "hl_mid": prev.get("hl_mid"),
                        })
                    cache.last_update["binance_ws"] = time.time()


# -------- klines + liquidations (business) --------

async def _consume_business(coins: list[str], cache: Cache) -> None:
    kline_args = []
    for c in coins:
        inst = _to_inst(c)
        for ch, _ in _OKX_KLINE_CHANNELS:
            kline_args.append({"channel": ch, "instId": inst})
    liq_args = [{"channel": "liquidation-orders", "instType": "SWAP"}]

    async with websockets.connect(OKX_WS_BUSINESS, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
        CHUNK = 60
        all_args = liq_args + kline_args
        for i in range(0, len(all_args), CHUNK):
            await ws.send(json.dumps({"op": "subscribe", "args": all_args[i:i + CHUNK]}))
        log.info("okx BUSINESS ws subscribed: klines (%d args) + liquidation-orders", len(kline_args))

        targets = {_to_inst(c) for c in coins}
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if _process_event_response(msg):
                continue
            arg = msg.get("arg") or {}
            ch = arg.get("channel", "")
            data = msg.get("data") or []
            if not data:
                continue

            if ch.startswith("candle"):
                tf = next((t for c, t in _OKX_KLINE_CHANNELS if c == ch), None)
                if not tf:
                    continue
                coin = _from_inst(arg.get("instId", ""))
                for row in data:
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

            elif ch == "liquidation-orders":
                for d in data:
                    inst = d.get("instId", "")
                    if inst not in targets:
                        continue
                    coin = _from_inst(inst)
                    for det in d.get("details") or []:
                        try:
                            side = (det.get("side") or "").upper()
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
    async def loop(name: str, consumer):
        bo = 1.0
        while True:
            try:
                await consumer(coins, cache)
            except Exception as e:
                cache.ws_alive["binance"] = False
                log.warning("okx %s ws disconnect: %s; reconnect in %.1fs", name, e, bo)
                await asyncio.sleep(bo)
                bo = min(bo * 2, 60.0)
            else:
                bo = 1.0

    await asyncio.gather(
        loop("public", _consume_public),
        loop("business", _consume_business),
    )


def run_in_thread(coins: list[str], cache: Cache) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_runner(coins, cache))
    finally:
        loop.close()
