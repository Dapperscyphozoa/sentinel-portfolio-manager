"""poly-runner core: strategy dispatch + position monitor + maker loop.

This module:
  1. Scans all active markets every SCAN_INTERVAL_MS, fires take-side
     strategies (cl_predictor, endgame, cross_asset, reflexivity_emitter)
  2. Runs the maker_quote loop at MAKER_INTERVAL_MS (separate cadence)
  3. Monitors open orders for fills via PM REST + resolves on settlement
  4. All order submission goes through poly_signer_client (Unix socket
     to Rust signer)

Halt token: POST /halt/<name> with X-Halt-Token header.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict
from typing import Optional

from common import poly_bus_client as bus
from common.poly_signer_client import OrderRequest, OrderResponse, next_nonce, sign_and_submit
from common.poly_persistence import connect_poly, init_poly_db
from common import pm_client  # SPM's PM client (we reuse for halt check pattern)
from poly_runner.strategies import REGISTRY
from poly_runner.strategies._base import Quote, Signal


log = logging.getLogger("poly_runner")


# ────────────────────────── Config ──────────────────────────
SCAN_INTERVAL_MS = int(os.environ.get("POLY_SCAN_INTERVAL_MS", "1000"))
MAKER_INTERVAL_MS = int(os.environ.get("MM_REQUOTE_INTERVAL_MS", "250"))
POSITION_MONITOR_MS = int(os.environ.get("POLY_POSITION_MONITOR_MS", "1000"))
LIVE_TRADING = os.environ.get("POLY_LIVE", "0") == "1"
SIGNER_SOCKET = os.environ.get("POLY_SIGNER_SOCKET", "/tmp/poly-signer.sock")
HALT_TOKEN = os.environ.get("HALT_TOKEN", "")


# Per-strategy enable flags + halt state
_halts: dict[str, bool] = {}


def is_enabled(name: str) -> bool:
    if _halts.get(name, False):
        return False
    return os.environ.get(f"POLY_STRATEGY_{name.upper()}_ENABLED", "1") == "1"


def halt(name: str) -> None:
    _halts[name] = True
    log.warning(f"strategy halted: {name}")


def unhalt(name: str) -> None:
    _halts[name] = False


def halt_all() -> None:
    for name in REGISTRY:
        halt(name)
    log.error("HALT_ALL fired")


# ────────────────────────── Order submission ──────────────────────────
def submit_signal(sig: Signal) -> Optional[OrderResponse]:
    """Persist signal + submit via signer (or paper-log if !LIVE)."""
    conn = connect_poly()
    try:
        conn.execute(
            "INSERT INTO poly_signals(ts, strategy, market_id, asset, token, side,"
            " price, size_usdc, edge_bps, cl_predicted, pm_implied, fire_reason, extras_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), sig.strategy, sig.market_id, sig.asset, sig.token,
             sig.side, sig.price, sig.size_usdc, sig.edge_bps,
             sig.cl_predicted, sig.pm_implied, sig.fire_reason,
             json.dumps(sig.extras)),
        )
    finally:
        conn.close()

    if not LIVE_TRADING:
        log.info(f"PAPER {sig.strategy}/{sig.asset}/{sig.token} {sig.side}@{sig.price} "
                 f"${sig.size_usdc} edge={sig.edge_bps:.0f}bps")
        return None

    cloid = f"{sig.strategy[:6]}-{uuid.uuid4().hex[:12]}"
    market = _market_lookup(sig.market_id)
    if not market:
        log.warning(f"market not found at submit time: {sig.market_id}")
        return None
    token_id = market["token_id_yes"] if sig.token == "YES" else market["token_id_no"]

    req = OrderRequest(
        market_id=sig.market_id,
        token_id=str(token_id),
        side="Buy" if sig.side == "BUY" else "Sell",
        price=sig.price,
        size_usdc=sig.size_usdc,
        expiration=0,
        nonce=next_nonce(),
        order_type="Fok" if sig.order_type == "FOK" else "Gtc",
        client_order_id=cloid,
    )
    try:
        resp = sign_and_submit(req, socket_path=SIGNER_SOCKET)
    except Exception as e:
        log.exception("signer submit failed")
        _persist_order(cloid, sig, status="ERROR", error=str(e))
        return None
    _persist_order(cloid, sig, status=resp.status, fill_amount=resp.fill_amount,
                   fill_price=resp.fill_price, error=resp.error,
                   signing_ms=resp.signing_ms, total_ms=resp.total_ms,
                   order_hash=resp.order_hash)
    return resp


def submit_quote(q: Quote) -> tuple[Optional[OrderResponse], Optional[OrderResponse]]:
    """Submit a bid + ask pair for maker_quote."""
    if not LIVE_TRADING:
        log.info(f"PAPER QUOTE {q.market_id} bid={q.bid_price}@${q.bid_size_usdc} "
                 f"ask={q.ask_price}@${q.ask_size_usdc} fair={q.fair_prob:.3f}")
        conn = connect_poly()
        try:
            conn.execute(
                "INSERT INTO poly_quotes(ts, market_id, bid_yes, ask_yes, inventory,"
                " fair_prob, action) VALUES(?,?,?,?,?,?,?)",
                (time.time(), q.market_id, q.bid_price, q.ask_price,
                 q.inventory, q.fair_prob, "PAPER"),
            )
        finally:
            conn.close()
        return None, None

    market = _market_lookup(q.market_id)
    if not market:
        return None, None
    token_id_yes = market["token_id_yes"]

    bid_req = OrderRequest(
        market_id=q.market_id, token_id=str(token_id_yes),
        side="Buy", price=q.bid_price, size_usdc=q.bid_size_usdc,
        expiration=int(time.time()) + 60, nonce=next_nonce(),
        order_type="Gtc",
        client_order_id=f"mm-bid-{uuid.uuid4().hex[:10]}",
    )
    ask_req = OrderRequest(
        market_id=q.market_id, token_id=str(token_id_yes),
        side="Sell", price=q.ask_price, size_usdc=q.ask_size_usdc,
        expiration=int(time.time()) + 60, nonce=next_nonce(),
        order_type="Gtc",
        client_order_id=f"mm-ask-{uuid.uuid4().hex[:10]}",
    )

    try:
        bid_resp = sign_and_submit(bid_req, socket_path=SIGNER_SOCKET)
        ask_resp = sign_and_submit(ask_req, socket_path=SIGNER_SOCKET)
        return bid_resp, ask_resp
    except Exception as e:
        log.exception("quote submit failed")
        return None, None


def _market_lookup(market_id: str) -> Optional[dict]:
    try:
        for m in bus.market_list():
            if m.get("market_id") == market_id:
                return m
    except Exception:
        return None
    return None


def _persist_order(cloid: str, sig: Signal, status: str,
                    fill_amount: Optional[float] = None,
                    fill_price: Optional[float] = None,
                    error: Optional[str] = None,
                    signing_ms: Optional[int] = None,
                    total_ms: Optional[int] = None,
                    order_hash: Optional[str] = None) -> None:
    conn = connect_poly()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO poly_orders"
            "(cloid, order_hash, strategy, market_id, token, side, price, size_usdc,"
            " submit_ts, status, fill_amount, fill_price, signing_ms, total_ms, error,"
            " extras_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cloid, order_hash, sig.strategy, sig.market_id, sig.token,
             sig.side, sig.price, sig.size_usdc, time.time(),
             status, fill_amount or 0, fill_price, signing_ms, total_ms,
             error, json.dumps(sig.extras)),
        )
    finally:
        conn.close()


# ────────────────────────── Scan loop ──────────────────────────
async def scan_loop() -> None:
    """Run take-side strategies across all active markets, every SCAN_INTERVAL_MS."""
    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL_MS / 1000.0)
            markets = bus.market_list()
            for m in markets:
                for name, S in REGISTRY.items():
                    if name in ("maker_quote", "cross_asset"):
                        continue  # dispatched elsewhere
                    if not is_enabled(name):
                        continue
                    try:
                        sig = S.evaluate(m, bus)
                    except Exception as e:
                        log.warning(f"{name}.evaluate({m.get('market_id')}): {e}")
                        continue
                    if sig is None:
                        continue
                    submit_signal(sig)
            # cross_asset is dispatched once per scan, not per market
            if is_enabled("cross_asset"):
                try:
                    sigs = REGISTRY["cross_asset"].evaluate(bus)
                    if sigs:
                        for s in sigs:
                            submit_signal(s)
                except Exception as e:
                    log.warning(f"cross_asset.evaluate: {e}")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scan_loop error")


# ────────────────────────── Maker loop ──────────────────────────
async def maker_loop() -> None:
    if not is_enabled("maker_quote"):
        return
    MM = REGISTRY["maker_quote"]
    while True:
        try:
            await asyncio.sleep(MAKER_INTERVAL_MS / 1000.0)
            markets = bus.market_list()
            for m in markets:
                inv = _get_inventory(m["market_id"], "YES")
                q = MM.quote_market(m, bus, inv)
                if q is None:
                    continue
                submit_quote(q)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("maker_loop error")


def _get_inventory(market_id: str, token: str) -> float:
    conn = connect_poly()
    try:
        cur = conn.execute(
            "SELECT qty, avg_cost FROM poly_positions WHERE market_id=? AND token=?",
            (market_id, token))
        row = cur.fetchone()
        if not row:
            return 0.0
        qty, avg_cost = row
        return (qty or 0.0) * (avg_cost or 0.5)
    finally:
        conn.close()


# ────────────────────────── Position monitor ──────────────────────────
async def position_monitor_loop() -> None:
    """At end of each window (within 10s of end_ts), cancel any unfilled
    GTC orders and let resolution happen.

    Polymarket settles automatically; we don't close positions pre-resolution
    by default. (The user can opt in by setting POLY_EXIT_BEFORE_RESOLVE.)
    """
    while True:
        try:
            await asyncio.sleep(POSITION_MONITOR_MS / 1000.0)
            markets = bus.market_list()
            now = time.time()
            for m in markets:
                if not m.get("end_ts"):
                    continue
                tr = m["end_ts"] - now
                if 0 < tr < 10:
                    # TODO: cancel open GTC orders for this market via signer
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("position_monitor error")


# ────────────────────────── Main ──────────────────────────
async def main() -> None:
    init_poly_db()
    log.info(f"poly-runner starting (LIVE={LIVE_TRADING}, "
             f"strategies={list(REGISTRY.keys())})")
    tasks = [
        asyncio.create_task(scan_loop(), name="scan"),
        asyncio.create_task(maker_loop(), name="maker"),
        asyncio.create_task(position_monitor_loop(), name="monitor"),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(main())
