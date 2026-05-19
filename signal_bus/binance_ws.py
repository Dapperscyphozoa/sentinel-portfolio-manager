"""Binance Futures combined WS stream.

Subscribes klines (1m/5m/15m/1h) + !forceOrder@arr (all-symbol liq) + per-symbol markPrice.
Auto-reconnect with exponential backoff. Designed to run in its own asyncio loop in a daemon thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Iterable

import websockets

from .cache import Cache


log = logging.getLogger("binance_ws")


# Binance 2026 WS endpoint split (per docs/derivatives/.../Important-WebSocket-Change-Notice):
# klines, markPrice, and !forceOrder@arr are all classified as /market streams.
# Legacy `wss://fstream.binance.com/stream?streams=...` is being decommissioned;
# liq (forceOrder) stopped delivering on the legacy URL ~2026-04 while klines
# degraded gradually. Migrate to /market/stream?streams=... 2026-05-19.
BINANCE_WS_BASE = "wss://fstream.binance.com/market/stream?streams="
TIMEFRAMES = ("1m", "5m", "15m", "1h")


def coin_from_symbol(symbol: str) -> str:
    """BTCUSDT -> BTC; ETHBUSD -> ETH."""
    s = symbol.upper()
    for suffix in ("USDT", "USDC", "BUSD"):
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s


def build_streams(symbols: Iterable[str]) -> list[str]:
    streams: list[str] = []
    for sym in symbols:
        s = sym.lower()
        for tf in TIMEFRAMES:
            streams.append(f"{s}@kline_{tf}")
        streams.append(f"{s}@markPrice@1s")
    # NOTE: !forceOrder@arr is intentionally NOT included here. When appended to
    # a large combined-stream URL it lands in the smaller trailing chunk which
    # silently fails to deliver forceOrder events (deployed 2026-05-19: 0 liqs
    # in 1h vs sandbox-isolated 2 liqs in 25s on the same URL). Isolated onto a
    # dedicated single-stream WS via _consume_liq() in _runner() below.
    return streams


def chunk(seq: list[str], n: int) -> list[list[str]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


async def _consume(url: str, cache: Cache) -> None:
    async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
        cache.ws_alive["binance"] = True
        log.info("binance ws connected: %s", url[:100])
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            stream = msg.get("stream", "")
            data = msg.get("data", msg)
            if "@kline_" in stream:
                _on_kline(cache, data)
            elif stream.startswith("!forceOrder"):
                _on_liq(cache, data)
            elif "@markPrice" in stream:
                _on_mark(cache, data)


def _on_kline(cache: Cache, data: dict) -> None:
    k = data.get("k") or {}
    sym = k.get("s") or data.get("s")
    if not sym:
        return
    coin = coin_from_symbol(sym)
    tf = k.get("i")
    if tf not in TIMEFRAMES:
        return
    bar = {
        "open_ts": int(k["t"]),
        "open": float(k["o"]),
        "high": float(k["h"]),
        "low": float(k["l"]),
        "close": float(k["c"]),
        "volume": float(k["v"]),
        "closed": bool(k.get("x", False)),
    }
    cache.push_kline(coin, tf, bar)


def _on_liq(cache: Cache, data: dict) -> None:
    o = data.get("o") or {}
    sym = o.get("s")
    if not sym:
        return
    coin = coin_from_symbol(sym)
    qty = float(o.get("q", 0))
    price = float(o.get("p", 0) or o.get("ap", 0))
    side = o.get("S", "")  # 'BUY' = short liquidation, 'SELL' = long liquidation
    ev = {
        "ts": int(o.get("T") or data.get("E") or time.time() * 1000),
        "coin": coin,
        "side": side,
        "qty": qty,
        "price": price,
        "usd": qty * price,
    }
    cache.push_liq(ev)


def _on_mark(cache: Cache, data: dict) -> None:
    sym = data.get("s")
    if not sym:
        return
    coin = coin_from_symbol(sym)
    mid = float(data.get("p", 0))
    if mid <= 0:
        return
    m = {
        "ts": int(data.get("E") or time.time() * 1000),
        "binance_mid": mid,
        "hl_mid": None,
    }
    cache.push_mark(coin, m)
    # funding rate is bundled in markPrice stream (field 'r')
    r = data.get("r")
    if r is not None:
        try:
            rate = float(r)
            cache.push_funding(coin, m["ts"], rate, venue="binance")
        except Exception:
            pass


async def _consume_liq(cache: Cache) -> None:
    """Dedicated WS for !forceOrder@arr. Isolated from the main combined-stream
    chunks because liq events were silently dropped when appended to a large
    multi-stream URL (see build_streams note).

    Empirical (2026-05-19): the path-based single-stream URL
    `/ws/!forceOrder@arr` accepts the connection but delivers ZERO events.
    Only the combined-stream URL `/market/stream?streams=!forceOrder@arr`
    works, so we use that with a single stream. Envelope is the standard
    combined-stream `{"stream": "!forceOrder@arr", "data": {...}}` wrapper."""
    url = "wss://fstream.binance.com/market/stream?streams=!forceOrder@arr"
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                log.info("binance liq ws connected: %s", url)
                backoff = 1.0
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    # Combined-stream envelope: {"stream":"!forceOrder@arr","data":{"e":"forceOrder","o":{...}}}
                    data = msg.get("data") if isinstance(msg, dict) else None
                    if not data:
                        continue
                    _on_liq(cache, data)
        except Exception as e:
            log.warning("binance liq ws disconnect: %s; reconnect in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def _runner(symbols: list[str], cache: Cache) -> None:
    streams = build_streams(symbols)
    # Binance recommends ≤200 streams per conn; chunk if needed
    chunks = chunk(streams, 180)
    backoff = 1.0

    async def one(stream_chunk: list[str]):
        nonlocal backoff
        url = BINANCE_WS_BASE + "/".join(stream_chunk)
        while True:
            try:
                await _consume(url, cache)
            except Exception as e:
                cache.ws_alive["binance"] = False
                log.warning("binance ws disconnect: %s; reconnect in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            else:
                backoff = 1.0

    # Run klines/markPrice chunks AND dedicated liq stream in parallel
    await asyncio.gather(
        *[one(c) for c in chunks],
        _consume_liq(cache),
    )


def run_in_thread(symbols: list[str], cache: Cache) -> None:
    """Entrypoint for daemon thread. Owns its own asyncio loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_runner(symbols, cache))
    finally:
        loop.close()
