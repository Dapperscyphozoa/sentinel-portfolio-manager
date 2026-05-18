"""HTTP client for the signal-bus service."""
from __future__ import annotations

from typing import Optional

import httpx

from . import config


DEFAULT_TIMEOUT = 10.0


class BusClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = (base_url or config.get("SIGNAL_BUS_URL", required=True)).rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def candles(self, coin: str, tf: str, n: int = 200) -> list[dict]:
        r = self._client.get(f"{self.base_url}/candles/{coin}/{tf}", params={"n": n})
        r.raise_for_status()
        return r.json()

    def liq(self, since_ms: Optional[int] = None, coin: Optional[str] = None) -> list[dict]:
        params = {}
        if since_ms is not None:
            params["since"] = since_ms
        if coin is not None:
            params["coin"] = coin
        r = self._client.get(f"{self.base_url}/liq", params=params)
        r.raise_for_status()
        return r.json()

    def funding(self, coin: str, hours: int = 12, venue: Optional[str] = None) -> list[dict]:
        params = {"hours": hours}
        if venue:
            params["venue"] = venue
        r = self._client.get(f"{self.base_url}/funding/{coin}", params=params)
        r.raise_for_status()
        return r.json()

    def funding_multi(self, coin: str, hours: int = 12) -> dict:
        r = self._client.get(f"{self.base_url}/funding_multi/{coin}", params={"hours": hours})
        r.raise_for_status()
        return r.json()

    def markprice(self, coin: str) -> dict:
        r = self._client.get(f"{self.base_url}/markprice/{coin}")
        r.raise_for_status()
        return r.json()

    def oi(self, coin: str, n: int = 8640) -> list[dict]:
        """HL openInterest history for coin — last n snapshots (60s apart)."""
        r = self._client.get(f"{self.base_url}/oi/{coin}", params={"n": n})
        r.raise_for_status()
        return r.json()

    def cvd(self, coin: str, window_ms: int = 30_000) -> dict:
        """HL Cumulative Volume Delta — aggressor flow imbalance.
        Returns: {window_ms, n_trades, cvd_size, cvd_notional, buy_notional, sell_notional,
                  rolling_5m_sigma, z_score}.
        Z-score: cvd_size normalized vs rolling 5min distribution.
        """
        r = self._client.get(f"{self.base_url}/cvd/{coin}",
                             params={"window_ms": window_ms},
                             timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def whale_events(self, since_ms: int = 0, coin: Optional[str] = None) -> list[dict]:
        """Whale-wallet new-position-open events (Stage 1 #5).
        Each event: {ts, wallet, coin, is_long, size, entry_px, ntl_usd, kind, delta_ntl_usd}.
        kind: 'new'|'flip'|'grow'.
        """
        params: dict = {"since": since_ms}
        if coin:
            params["coin"] = coin
        r = self._client.get(f"{self.base_url}/whale_events",
                             params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def l2book(self, coin: str) -> dict:
        """Latest HL L2 book snapshot for coin (Stage 1 #6)."""
        r = self._client.get(f"{self.base_url}/l2book/{coin}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def depth_shock(self, coin: str, window_s: int = 5) -> dict:
        """Detect bid/ask depth shock in last window_s seconds.
        Returns shock_kind: 'bid'|'ask'|None.
        """
        r = self._client.get(f"{self.base_url}/depth_shock/{coin}",
                             params={"window_s": window_s}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def hl_account(self) -> dict:
        r = self._client.get(f"{self.base_url}/hl/account")
        r.raise_for_status()
        return r.json()

    def hl_fills(self, since_ms: Optional[int] = None) -> list[dict]:
        params = {}
        if since_ms is not None:
            params["since"] = since_ms
        r = self._client.get(f"{self.base_url}/hl/fills", params=params)
        r.raise_for_status()
        return r.json()

    def hl_positions(self) -> list[dict]:
        r = self._client.get(f"{self.base_url}/hl/positions")
        r.raise_for_status()
        return r.json()

    def hl_confluence(self, coin: str, since_ms: Optional[int] = None) -> dict:
        params = {}
        if since_ms is not None:
            params["since"] = since_ms
        r = self._client.get(f"{self.base_url}/hl/confluence/{coin.upper()}", params=params)
        r.raise_for_status()
        return r.json()

    def hlp_position(self, coin: str) -> Optional[dict]:
        """HLP (Hyperliquidity Provider) vault positioning for a coin.
        
        Returns dict with: net_size, net_usd, vault_count, ts, zscore_7d,
        history_n. Returns None if HLP has no position in this coin or
        endpoint unavailable.
        """
        try:
            r = self._client.get(f"{self.base_url}/hlp_position/{coin.upper()}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def hlp_positions(self) -> dict:
        """All HLP positions {coin: {net_size, net_usd, vault_count, ts}}."""
        try:
            r = self._client.get(f"{self.base_url}/hlp_positions")
            r.raise_for_status()
            return r.json() or {}
        except Exception:
            return {}

    def health(self) -> dict:
        r = self._client.get(f"{self.base_url}/health")
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()
