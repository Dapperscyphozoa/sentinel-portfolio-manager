"""Multi-venue CEX WebSocket subscriber for poly-signal-bus.

Subscribes to seven exchanges in parallel and normalizes their feeds to a
common schema: {venue, asset, ts_ms, mid_price, bid, ask}.

Venues + endpoints (verify against current docs before deploy):
  - binance      wss://stream.binance.com:9443/ws       bookTicker
  - coinbase     wss://ws-feed.exchange.coinbase.com    level2/ticker
  - kraken       wss://ws.kraken.com                    ticker
  - bitstamp     wss://ws.bitstamp.net                  live_orders / order_book
  - bitfinex     wss://api-pub.bitfinex.com/ws/2        ticker
  - okx          wss://ws.okx.com:8443/ws/v5/public     bbo-tbt
  - huobi        wss://api.huobi.pro/ws                 market.symbol.bbo (gzipped)

All venues are read-only/public; no auth needed.

Each parsed tick is appended to an in-memory deque (cache.py owns storage).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import websockets


log = logging.getLogger("cex_ws")

# Asset → per-venue symbol mapping
SYMBOLS = {
    "BTC": {
        "binance":  "btcusdt",
        "coinbase": "BTC-USD",
        "kraken":   "XBT/USD",
        "bitstamp": "btcusd",
        "bitfinex": "tBTCUSD",
        "okx":      "BTC-USDT",
        "huobi":    "btcusdt",
    },
    "ETH": {
        "binance":  "ethusdt",
        "coinbase": "ETH-USD",
        "kraken":   "ETH/USD",
        "bitstamp": "ethusd",
        "bitfinex": "tETHUSD",
        "okx":      "ETH-USDT",
        "huobi":    "ethusdt",
    },
}

TickHandler = Callable[[str, str, int, float, float, float], Awaitable[None]]
# (venue, asset, ts_ms, mid, bid, ask)


# ────────────────────────── Binance ──────────────────────────
async def run_binance(assets: list[str], on_tick: TickHandler) -> None:
    streams = "/".join(f"{SYMBOLS[a]['binance']}@bookTicker" for a in assets)
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    data = msg.get("data", {})
                    sym = data.get("s", "").lower()
                    asset = next((a for a in assets if SYMBOLS[a]["binance"] == sym), None)
                    if not asset:
                        continue
                    bid = float(data.get("b", 0))
                    ask = float(data.get("a", 0))
                    if bid <= 0 or ask <= 0:
                        continue
                    mid = (bid + ask) / 2
                    await on_tick("binance", asset, int(time.time() * 1000), mid, bid, ask)
        except Exception as e:
            log.warning(f"binance ws disconnect: {e}; backoff {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ────────────────────────── Coinbase ──────────────────────────
async def run_coinbase(assets: list[str], on_tick: TickHandler) -> None:
    url = "wss://ws-feed.exchange.coinbase.com"
    product_ids = [SYMBOLS[a]["coinbase"] for a in assets]
    sub = {"type": "subscribe", "product_ids": product_ids, "channels": ["ticker"]}
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(json.dumps(sub))
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "ticker":
                        continue
                    pid = msg.get("product_id", "")
                    asset = next((a for a in assets if SYMBOLS[a]["coinbase"] == pid), None)
                    if not asset:
                        continue
                    bid = float(msg.get("best_bid", 0))
                    ask = float(msg.get("best_ask", 0))
                    if bid <= 0 or ask <= 0:
                        continue
                    mid = (bid + ask) / 2
                    await on_tick("coinbase", asset, int(time.time() * 1000), mid, bid, ask)
        except Exception as e:
            log.warning(f"coinbase ws disconnect: {e}; backoff {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ────────────────────────── Kraken ──────────────────────────
async def run_kraken(assets: list[str], on_tick: TickHandler) -> None:
    url = "wss://ws.kraken.com"
    pairs = [SYMBOLS[a]["kraken"] for a in assets]
    sub = {"event": "subscribe", "pair": pairs, "subscription": {"name": "ticker"}}
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(json.dumps(sub))
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    if isinstance(msg, dict):
                        continue  # event/heartbeat
                    # ticker payload: [channelID, {a:[ask,...], b:[bid,...]}, "ticker", pair]
                    if not isinstance(msg, list) or len(msg) < 4:
                        continue
                    payload, _, pair = msg[1], msg[2], msg[3]
                    asset = next((a for a in assets if SYMBOLS[a]["kraken"] == pair), None)
                    if not asset:
                        continue
                    try:
                        bid = float(payload["b"][0])
                        ask = float(payload["a"][0])
                    except (KeyError, IndexError, ValueError):
                        continue
                    if bid <= 0 or ask <= 0:
                        continue
                    mid = (bid + ask) / 2
                    await on_tick("kraken", asset, int(time.time() * 1000), mid, bid, ask)
        except Exception as e:
            log.warning(f"kraken ws disconnect: {e}; backoff {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ────────────────────────── Bitstamp ──────────────────────────
async def run_bitstamp(assets: list[str], on_tick: TickHandler) -> None:
    url = "wss://ws.bitstamp.net"
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                for a in assets:
                    sub = {"event": "bts:subscribe",
                           "data": {"channel": f"order_book_{SYMBOLS[a]['bitstamp']}"}}
                    await ws.send(json.dumps(sub))
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("event") != "data":
                        continue
                    ch = msg.get("channel", "")
                    asset = None
                    for a in assets:
                        if ch.endswith(SYMBOLS[a]["bitstamp"]):
                            asset = a; break
                    if not asset:
                        continue
                    data = msg.get("data", {})
                    bids = data.get("bids") or []
                    asks = data.get("asks") or []
                    if not bids or not asks:
                        continue
                    try:
                        bid = float(bids[0][0]); ask = float(asks[0][0])
                    except (IndexError, ValueError):
                        continue
                    if bid <= 0 or ask <= 0:
                        continue
                    mid = (bid + ask) / 2
                    await on_tick("bitstamp", asset, int(time.time() * 1000), mid, bid, ask)
        except Exception as e:
            log.warning(f"bitstamp ws disconnect: {e}; backoff {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ────────────────────────── Bitfinex ──────────────────────────
async def run_bitfinex(assets: list[str], on_tick: TickHandler) -> None:
    url = "wss://api-pub.bitfinex.com/ws/2"
    chan_to_asset: dict[int, str] = {}
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                for a in assets:
                    sub = {"event": "subscribe", "channel": "ticker",
                           "symbol": SYMBOLS[a]["bitfinex"]}
                    await ws.send(json.dumps(sub))
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    if isinstance(msg, dict):
                        if msg.get("event") == "subscribed":
                            sym = msg.get("symbol", "")
                            asset = next((a for a in assets if SYMBOLS[a]["bitfinex"] == sym), None)
                            if asset:
                                chan_to_asset[msg["chanId"]] = asset
                        continue
                    if not isinstance(msg, list) or len(msg) < 2:
                        continue
                    chan_id, data = msg[0], msg[1]
                    if data == "hb":
                        continue
                    asset = chan_to_asset.get(chan_id)
                    if not asset or not isinstance(data, list) or len(data) < 7:
                        continue
                    # [BID, BID_SIZE, ASK, ASK_SIZE, ...]
                    try:
                        bid = float(data[0]); ask = float(data[2])
                    except (IndexError, ValueError):
                        continue
                    if bid <= 0 or ask <= 0:
                        continue
                    mid = (bid + ask) / 2
                    await on_tick("bitfinex", asset, int(time.time() * 1000), mid, bid, ask)
        except Exception as e:
            log.warning(f"bitfinex ws disconnect: {e}; backoff {backoff:.1f}s")
            chan_to_asset.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ────────────────────────── OKX ──────────────────────────
async def run_okx(assets: list[str], on_tick: TickHandler) -> None:
    url = "wss://ws.okx.com:8443/ws/v5/public"
    args = [{"channel": "bbo-tbt", "instId": SYMBOLS[a]["okx"]} for a in assets]
    sub = {"op": "subscribe", "args": args}
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(json.dumps(sub))
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    arg = msg.get("arg", {})
                    if arg.get("channel") != "bbo-tbt":
                        continue
                    inst = arg.get("instId", "")
                    asset = next((a for a in assets if SYMBOLS[a]["okx"] == inst), None)
                    if not asset:
                        continue
                    data = msg.get("data", [])
                    if not data:
                        continue
                    d = data[0]
                    bids = d.get("bids") or []; asks = d.get("asks") or []
                    if not bids or not asks:
                        continue
                    try:
                        bid = float(bids[0][0]); ask = float(asks[0][0])
                    except (IndexError, ValueError):
                        continue
                    if bid <= 0 or ask <= 0:
                        continue
                    mid = (bid + ask) / 2
                    await on_tick("okx", asset, int(time.time() * 1000), mid, bid, ask)
        except Exception as e:
            log.warning(f"okx ws disconnect: {e}; backoff {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ────────────────────────── Huobi ──────────────────────────
async def run_huobi(assets: list[str], on_tick: TickHandler) -> None:
    url = "wss://api.huobi.pro/ws"
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                for a in assets:
                    sub = {"sub": f"market.{SYMBOLS[a]['huobi']}.bbo", "id": a}
                    await ws.send(json.dumps(sub))
                backoff = 1.0
                async for raw in ws:
                    # Huobi sends gzipped frames
                    try:
                        text = gzip.decompress(raw).decode("utf-8")
                    except (OSError, AttributeError):
                        if isinstance(raw, bytes):
                            text = raw.decode("utf-8", errors="ignore")
                        else:
                            text = raw
                    msg = json.loads(text)
                    if "ping" in msg:
                        await ws.send(json.dumps({"pong": msg["ping"]}))
                        continue
                    ch = msg.get("ch", "")
                    if "bbo" not in ch:
                        continue
                    sym = ch.split(".")[1] if "." in ch else ""
                    asset = next((a for a in assets if SYMBOLS[a]["huobi"] == sym), None)
                    if not asset:
                        continue
                    tick = msg.get("tick", {})
                    try:
                        bid = float(tick.get("bid", 0))
                        ask = float(tick.get("ask", 0))
                    except (TypeError, ValueError):
                        continue
                    if bid <= 0 or ask <= 0:
                        continue
                    mid = (bid + ask) / 2
                    await on_tick("huobi", asset, int(time.time() * 1000), mid, bid, ask)
        except Exception as e:
            log.warning(f"huobi ws disconnect: {e}; backoff {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ────────────────────────── Orchestration ──────────────────────────
VENUE_RUNNERS = {
    "binance":  run_binance,
    "coinbase": run_coinbase,
    "kraken":   run_kraken,
    "bitstamp": run_bitstamp,
    "bitfinex": run_bitfinex,
    "okx":      run_okx,
    "huobi":    run_huobi,
}


async def run_all(assets: list[str], on_tick: TickHandler,
                  venues: Optional[list[str]] = None) -> None:
    """Launch all venue runners concurrently. Each runs forever with own
    reconnect loop, so this gather never returns under normal operation."""
    venues = venues or list(VENUE_RUNNERS.keys())
    tasks = [asyncio.create_task(VENUE_RUNNERS[v](assets, on_tick),
                                 name=f"cex-{v}") for v in venues]
    await asyncio.gather(*tasks, return_exceptions=True)
