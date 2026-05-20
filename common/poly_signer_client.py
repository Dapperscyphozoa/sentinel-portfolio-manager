"""Python client for the Rust poly-signer microservice.

Talks over a Unix domain socket. Each call writes one JSON request and reads
one JSON response. The Rust side spawns one tokio task per connection, so
many concurrent calls from the runner are fine.

Wire schema mirrors `poly_signer/src/main.rs`:

Request:
    {market_id, token_id, side: "Buy"|"Sell", price, size_usdc,
     expiration, nonce, order_type: "Gtc"|"Fok", client_order_id}

Response:
    {client_order_id, order_hash, status: "Posted"|"Filled"|"PartialFill"|"Rejected"|"Error",
     fill_amount: float|null, fill_price: float|null, error: str|null,
     signing_ms: int, total_ms: int}
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, asdict
from typing import Literal, Optional


DEFAULT_SOCKET = "/tmp/poly-signer.sock"
DEFAULT_TIMEOUT_S = 3.0


@dataclass
class OrderRequest:
    market_id: str
    token_id: str          # uint256 as decimal string
    side: Literal["Buy", "Sell"]
    price: float           # 0.01..=0.99
    size_usdc: float
    expiration: int        # unix seconds; 0 = never
    nonce: int             # monotonic per maker; runner manages
    order_type: Literal["Gtc", "Fok"]
    client_order_id: str


@dataclass
class OrderResponse:
    client_order_id: str
    order_hash: str
    status: str
    fill_amount: Optional[float]
    fill_price: Optional[float]
    error: Optional[str]
    signing_ms: int
    total_ms: int


# Process-wide nonce lock (the Rust side trusts what we send; we serialize
# nonce assignment here to avoid stale-nonce rejections).
_nonce_lock = threading.Lock()
_next_nonce = int(time.time() * 1000)


def next_nonce() -> int:
    global _next_nonce
    with _nonce_lock:
        _next_nonce += 1
        return _next_nonce


def sign_and_submit(
    req: OrderRequest,
    socket_path: str = DEFAULT_SOCKET,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> OrderResponse:
    """Blocking call. Returns OrderResponse or raises on socket/timeout."""
    payload = json.dumps(asdict(req)).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(socket_path)
        s.sendall(payload)
        s.shutdown(socket.SHUT_WR)  # signal end-of-request to Rust read_to_string
        chunks = []
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks).decode("utf-8")
    if not raw:
        raise RuntimeError("poly-signer returned empty response")
    j = json.loads(raw)
    return OrderResponse(**j)


def cancel(order_id: str, socket_path: str = DEFAULT_SOCKET) -> dict:
    """Send a CANCEL action. Rust side dispatches via POST /order/cancel."""
    payload = json.dumps({"action": "cancel", "order_id": order_id}).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(DEFAULT_TIMEOUT_S)
        s.connect(socket_path)
        s.sendall(payload)
        s.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            c = s.recv(4096)
            if not c:
                break
            data += c
    return json.loads(data.decode("utf-8"))


if __name__ == "__main__":
    # Smoke check (assumes signer running locally with POLY_DRY_RUN=1)
    req = OrderRequest(
        market_id="test-market-id",
        token_id="0",
        side="Buy",
        price=0.50,
        size_usdc=1.0,
        expiration=0,
        nonce=next_nonce(),
        order_type="Gtc",
        client_order_id="smoke-0001",
    )
    resp = sign_and_submit(req)
    print(json.dumps(asdict(resp), indent=2))
