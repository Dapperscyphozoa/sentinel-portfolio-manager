"""HTTP client for the pm service.

Auth: X-PM-Auth header (NOT Bearer) per SPEC §7 legacy convention.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx

from . import config


DEFAULT_TIMEOUT = 10.0


@dataclass
class PMDecision:
    allow: bool
    size_usd: float
    reason: str
    raw: dict


class PMClient:
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT):
        # Lazy: PM_URL is REQUIRED only when actually making requests. This lets
        # the PM service itself load PMClient code without crashing on startup
        # (PM service is the server, doesn't need to call itself). Sentinel
        # discovered the orphan-crash pattern 2026-05-18.
        self._explicit_base = base_url
        self.token = token or os.environ.get("PM_AUTH_TOKEN", "")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    @property
    def base_url(self) -> str:
        if self._explicit_base:
            return self._explicit_base.rstrip("/")
        # Lazy resolve — raises only at first use, not at construction
        return config.get("PM_URL", required=True).rstrip("/")


    def _headers(self) -> dict:
        h = {"content-type": "application/json"}
        if self.token:
            h["X-PM-Auth"] = self.token
        return h

    def check(self, strategy: str, signal: dict) -> PMDecision:
        r = self._client.post(
            f"{self.base_url}/check",
            json={"strategy": strategy, "signal": signal},
            headers=self._headers(),
        )
        r.raise_for_status()
        d = r.json()
        return PMDecision(
            allow=bool(d.get("allow", False)),
            size_usd=float(d.get("size_usd", 0.0)),
            reason=str(d.get("reason", "")),
            raw=d,
        )

    def register_cloid(self, strategy: str, cloid: str, coin: str, side: str) -> dict:
        r = self._client.post(
            f"{self.base_url}/register_cloid",
            json={"strategy": strategy, "cloid": cloid, "coin": coin, "side": side},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    def regime(self) -> dict:
        r = self._client.get(f"{self.base_url}/regime", headers=self._headers())
        r.raise_for_status()
        return r.json()

    def attribution(self, since_ms: Optional[int] = None) -> list[dict]:
        params = {}
        if since_ms is not None:
            params["since"] = since_ms
        r = self._client.get(f"{self.base_url}/attribution", params=params, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()
