"""Hyperliquid WS subscriber.

Subscribes:
  userFills  (agent wallet)
  webData2   (account view, includes positions + value)
  activeAssetCtx for cross-venue markPrice augmentation (hl_mid)

Auto-reconnect; on reconnect, request a fresh snapshot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Iterable

import websockets

from .cache import Cache


log = logging.getLogger("hl_ws")

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"

# A short curated set of coins for activeAssetCtx (HL mark). Strategies that need
# more can extend via env. We keep this lean to avoid socket bloat.
DEFAULT_MARK_COINS = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK",
    "LTC", "NEAR", "SUI", "APT", "ARB", "OP", "INJ", "SEI",
    "TIA", "WIF", "JUP", "DOT",
]


def _subscribe_msg(channel: str, **params) -> str:
    return json.dumps({"method": "subscribe", "subscription": {"type": channel, **params}})


async def _consume(wallet: str, cache: Cache, mark_coins: Iterable[str]) -> None:
    async with websockets.connect(HL_WS_URL, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
        cache.ws_alive["hl"] = True
        log.info("hl ws connected for %s", wallet)

        # subscriptions
        await ws.send(_subscribe_msg("userFills", user=wallet))
        await ws.send(_subscribe_msg("webData2", user=wallet))
        for coin in mark_coins:
            await ws.send(_subscribe_msg("activeAssetCtx", coin=coin))
            await ws.send(_subscribe_msg("trades", coin=coin))

        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            ch = msg.get("channel")
            data = msg.get("data")
            if ch == "userFills":
                _on_user_fills(cache, data)
            elif ch == "webData2":
                _on_webdata2(cache, data)
            elif ch == "activeAssetCtx":
                _on_active_asset_ctx(cache, data)
            elif ch == "trades":
                _on_trades(cache, data)
            cache.last_update["hl_ws"] = time.time()


def _on_user_fills(cache: Cache, data) -> None:
    if not data:
        return
    fills = data.get("fills") or []
    is_snapshot = bool(data.get("isSnapshot"))
    for f in fills:
        fid = str(f.get("tid") or f.get("hash") or f.get("oid") or "")
        if not fid:
            continue
        ev = {
            "fill_id": fid,
            "ts": int(f.get("time", 0)),
            "coin": str(f.get("coin", "")).upper(),
            "side": "B" if f.get("side") == "B" else "A",
            "qty": float(f.get("sz", 0)),
            "price": float(f.get("px", 0)),
            "cloid": f.get("cloid"),
            "raw": f,
        }
        with cache._lock:
            cache.hl_fills.append(ev)
        try:
            cache.db.execute(
                "INSERT OR IGNORE INTO hl_fills(fill_id,ts,coin,side,qty,price,cloid,raw_json) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (ev["fill_id"], ev["ts"], ev["coin"], ev["side"], ev["qty"], ev["price"],
                 ev["cloid"], json.dumps(f, default=str)),
            )
        except Exception:
            log.exception("hl_fill persist")


def _on_webdata2(cache: Cache, data) -> None:
    if not isinstance(data, dict):
        return
    cs = data.get("clearinghouseState") or {}
    margin_summary = cs.get("marginSummary") or {}
    asset_positions = cs.get("assetPositions") or []
    positions: list[dict] = []
    for ap in asset_positions:
        pos = ap.get("position") or {}
        coin = str(pos.get("coin", "")).upper()
        if not coin:
            continue
        szi = float(pos.get("szi", 0))
        positions.append({
            "coin": coin,
            "szi": szi,
            "is_long": szi > 0,
            "entry_px": float(pos.get("entryPx") or 0) if pos.get("entryPx") else None,
            "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
            "leverage": pos.get("leverage"),
        })
    with cache._lock:
        cache.hl_account = {
            "value": float(margin_summary.get("accountValue") or 0),
            "margin_used": float(margin_summary.get("totalMarginUsed") or 0),
            "ntl_pos": float(margin_summary.get("totalNtlPos") or 0),
            "withdrawable": float(cs.get("withdrawable") or 0),
            "positions": positions,
            "ts": time.time(),
        }
        cache.hl_positions = positions


def _on_active_asset_ctx(cache: Cache, data) -> None:
    """activeAssetCtx event provides per-coin mark + oracle + funding etc."""
    if not isinstance(data, dict):
        return
    coin = str(data.get("coin", "")).upper()
    if not coin:
        return
    ctx = data.get("ctx") or {}
    mark = ctx.get("markPx") or ctx.get("oraclePx")
    if mark is None:
        return
    try:
        mid = float(mark)
    except Exception:
        return
    # merge with last binance mark
    with cache._lock:
        dq = cache.marks[coin]
        prev = dq[-1] if dq else {"ts": int(time.time() * 1000), "binance_mid": None}
        merged = {
            "ts": int(time.time() * 1000),
            "binance_mid": prev.get("binance_mid"),
            "hl_mid": mid,
        }
        dq.append(merged)
    # Sentinel fix: also extract funding rate (cex_dex_arb requires this).
    # HL activeAssetCtx provides funding as 'funding' (perp annualized -> per-hour
    # converted by HL). We store as-is, venue='hyperliquid'.
    fr = ctx.get("funding")
    if fr is not None:
        try:
            rate = float(fr)
            cache.push_funding(coin, int(time.time() * 1000), rate, venue="hyperliquid")
        except Exception:
            pass




def _on_trades(cache: Cache, data) -> None:
    """HL public trades channel — feed aggressor-side flow into CVD aggregator.
    Schema: list of {coin, side ("B"=buyer-taker, "A"=seller-taker), px, sz, time, hash}.
    """
    if not data:
        return
    if isinstance(data, dict):
        data = [data]
    for t in data:
        try:
            coin = str(t.get("coin", "")).upper()
            if not coin:
                continue
            side = t.get("side")
            sz = float(t.get("sz", 0) or 0)
            px = float(t.get("px", 0) or 0)
            ts = int(t.get("time", 0) or 0)
            if sz <= 0 or px <= 0 or ts <= 0:
                continue
            cache.push_hl_trade(coin, ts, side, sz, px)
        except Exception:
            log.exception("trades parse")


async def _runner(wallet: str, cache: Cache, mark_coins: list[str]) -> None:
    backoff = 1.0
    while True:
        try:
            await _consume(wallet, cache, mark_coins)
        except Exception as e:
            cache.ws_alive["hl"] = False
            log.warning("hl ws disconnect: %s; reconnect in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
        else:
            backoff = 1.0


def run_in_thread(wallet: str, cache: Cache, mark_coins: list[str] | None = None) -> None:
    """`wallet` should be the MAIN (owner) wallet, not the agent. HL agents
    sign-only and report no balance/positions of their own; queries on the
    agent address return empty. SPEC §0 main wallet = 0x3eDaD0...
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    coins = mark_coins or DEFAULT_MARK_COINS
    try:
        loop.run_until_complete(_runner(wallet, cache, coins))
    finally:
        loop.close()
