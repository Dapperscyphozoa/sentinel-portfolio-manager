"""Chainlink Data Stream subscriber.

Subscribes to the Chainlink Data Streams WebSocket for BTC/USD and ETH/USD
on Polygon mainnet. The Data Streams API requires:

  - A streams URL (regional WS endpoint; e.g. wss://api.dataengine.chain.link/api/v1/ws)
  - HMAC-signed auth header per request (CL_API_KEY + CL_API_SECRET)
  - Subscription to feed IDs (the on-chain hex IDs for BTC/USD, ETH/USD)

Feed IDs (verify in Chainlink Streams docs before mainnet):
  BTC/USD on Polygon:  0x0003c915006ba88731510bb995c190e80b5c0a76d7a16eba9d0a2c1c47b2cebd  (verify)
  ETH/USD on Polygon:  0x000368187f0a3ea60bb95f06aa14e0a8de9d0b18d8f8d31a8e1c0b75fc4c1f30  (verify)

Each received report contains:
  {feedID, validFromTimestamp, observationsTimestamp, nativeFee, linkFee,
   expiresAt, benchmarkPrice (int192), bid, ask}

We expose only the benchmarkPrice as a normalized float, plus the
observation timestamp for divergence-vs-prediction analysis.

FALLBACK: if Data Streams API isn't accessible (e.g. operator hasn't
provisioned), this module polls the on-chain aggregator via Polygon RPC
every 15s as a degraded baseline. Set CL_MODE=streams|onchain.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Awaitable, Callable, Optional

import websockets

log = logging.getLogger("chainlink_stream")

CLHandler = Callable[[str, int, float], Awaitable[None]]
# (asset, ts_ms, price)

# These IDs and URL must be verified against Chainlink Streams documentation
# before mainnet trust. Defaults below are placeholders pending verification.
DEFAULT_STREAMS_URL = os.environ.get(
    "CL_STREAMS_URL",
    "wss://api.testnet-dataengine.chain.link/api/v1/ws",
)
FEED_IDS = {
    "BTC": os.environ.get(
        "CL_FEED_BTC",
        "0x0003c915006ba88731510bb995c190e80b5c0a76d7a16eba9d0a2c1c47b2cebd",
    ),
    "ETH": os.environ.get(
        "CL_FEED_ETH",
        "0x000368187f0a3ea60bb95f06aa14e0a8de9d0b18d8f8d31a8e1c0b75fc4c1f30",
    ),
}


def _sign_request(method: str, path: str, body: bytes,
                  api_key: str, api_secret: str) -> dict:
    ts = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{method} {path} {body_hash} {api_key} {ts}".encode("utf-8")
    sig = hmac.new(api_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {
        "Authorization": api_key,
        "X-Authorization-Timestamp": ts,
        "X-Authorization-Signature-SHA256": sig,
    }


async def run_streams(assets: list[str], on_report: CLHandler,
                       streams_url: str = DEFAULT_STREAMS_URL) -> None:
    """Run the Chainlink Data Streams WS subscriber forever."""
    api_key = os.environ.get("CL_API_KEY", "")
    api_secret = os.environ.get("CL_API_SECRET", "")
    if not api_key or not api_secret:
        log.warning("CL_API_KEY / CL_API_SECRET not set; cannot stream. "
                    "Falling back to on-chain polling.")
        await run_onchain_poll(assets, on_report)
        return

    feed_ids = [FEED_IDS[a] for a in assets if a in FEED_IDS]
    path = "/api/v1/ws"
    # Connect with signed headers; the exact handshake format must match
    # Chainlink Streams API docs (verify before deploy).
    headers = _sign_request("GET", path, b"", api_key, api_secret)

    backoff = 1.0
    while True:
        try:
            async with websockets.connect(streams_url,
                                         extra_headers=headers,
                                         ping_interval=20) as ws:
                sub = {"action": "subscribe", "feedIDs": feed_ids}
                await ws.send(json.dumps(sub))
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    report = msg.get("report") or msg
                    feed_id = report.get("feedID") or report.get("feed_id")
                    asset = next((a for a, fid in FEED_IDS.items() if fid == feed_id), None)
                    if not asset:
                        continue
                    raw_price = report.get("benchmarkPrice") or report.get("price")
                    if raw_price is None:
                        continue
                    try:
                        # CL reports prices as int192 with 18 decimals
                        price = float(int(raw_price)) / 1e18
                    except (TypeError, ValueError):
                        try:
                            price = float(raw_price)
                        except ValueError:
                            continue
                    ts_ms = int(report.get("observationsTimestamp",
                                           time.time())) * 1000 \
                        if isinstance(report.get("observationsTimestamp"), (int, float)) \
                        and report["observationsTimestamp"] < 2e10 \
                        else int(time.time() * 1000)
                    await on_report(asset, ts_ms, price)
        except Exception as e:
            log.warning(f"cl_streams disconnect: {e}; backoff {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ────────────────────────── On-chain fallback ──────────────────────────
# Polls the legacy aggregator contracts as a slow baseline (~heartbeat 30s).
# Use only if Data Streams API isn't provisioned.

POLYGON_RPC = os.environ.get("POLYGON_RPC", "https://polygon-rpc.com")
AGGREGATOR_ADDRESSES = {
    "BTC": os.environ.get(
        "CL_AGGREGATOR_BTC",
        "0xc907E116054Ad103354f2D350FD2514433D57F6f",  # Polygon BTC/USD
    ),
    "ETH": os.environ.get(
        "CL_AGGREGATOR_ETH",
        "0xF9680D99D6C9589e2a93a78A04A279e509205945",  # Polygon ETH/USD
    ),
}


async def run_onchain_poll(assets: list[str], on_report: CLHandler,
                            interval_s: float = 15.0) -> None:
    """Degraded fallback: eth_call latestRoundData() every interval_s."""
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            for asset in assets:
                if asset not in AGGREGATOR_ADDRESSES:
                    continue
                addr = AGGREGATOR_ADDRESSES[asset]
                # latestRoundData() selector = 0xfeaf968c
                payload = {
                    "jsonrpc": "2.0", "method": "eth_call", "id": 1,
                    "params": [{"to": addr, "data": "0xfeaf968c"}, "latest"],
                }
                try:
                    r = await client.post(POLYGON_RPC, json=payload)
                    j = r.json()
                    res = j.get("result", "")
                    if len(res) < 2 + 5 * 64:
                        continue
                    # decode answer (int256, slot 2)
                    raw = res[2:]
                    slot_answer = raw[64:128]
                    answer = int(slot_answer, 16)
                    if answer >= 2**255:
                        answer -= 2**256
                    # Polygon BTC/USD + ETH/USD aggregators use 8 decimals
                    price = answer / 1e8
                    await on_report(asset, int(time.time() * 1000), price)
                except Exception as e:
                    log.warning(f"cl_onchain_poll {asset}: {e}")
            await asyncio.sleep(interval_s)


async def run(assets: list[str], on_report: CLHandler) -> None:
    """Entry point. Selects mode via env var CL_MODE."""
    mode = os.environ.get("CL_MODE", "streams").lower()
    if mode == "onchain":
        await run_onchain_poll(assets, on_report)
    else:
        await run_streams(assets, on_report)
