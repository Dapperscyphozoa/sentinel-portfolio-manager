"""HL order wrapper. Wraps hyperliquid-python-sdk and provides deterministic cloid hashing.

cloid format (HL accepts 128-bit hex):
  0x + 32 hex chars = 16 bytes derived from sha256(prefix || coin || ts_ms || nonce)
  We embed the strategy prefix in the FIRST 8 hex chars by re-hashing prefix to 4 bytes
  and using it as a salt. Attribution reads back via prefix match on lookup table.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger("hl_exchange")


def make_cloid(prefix: str, coin: str, nonce: Optional[int] = None, ts_ms: Optional[int] = None) -> str:
    """Return 0x-prefixed 32-char hex cloid (16 bytes), seeded by prefix+coin+ts+nonce."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    if nonce is None:
        nonce = int.from_bytes(os.urandom(4), "big")
    h = hashlib.sha256(f"{prefix}|{coin}|{ts_ms}|{nonce}".encode()).digest()[:16]
    return "0x" + h.hex()


def cloid_matches_prefix(cloid: str, prefix: str, coin: str, lookback_ms: int = 24 * 3600 * 1000) -> bool:
    """Best-effort cloid->prefix match. Real attribution uses a SQLite cloid registry;
    this helper exists only for unit tests of the cloid scheme itself."""
    # cloids are sha256 outputs — not reversible. Attribution happens via the registry
    # in pm/attribution.py, not via cloid parsing. This function intentionally returns
    # False to remind callers to use the registry.
    return False


@dataclass
class OrderResult:
    ok: bool
    cloid: str
    coin: str
    side: str
    size_coin: float
    px: Optional[float]
    raw: dict
    error: Optional[str] = None


class HLExchange:
    """Lazy-init wrapper around hyperliquid-python-sdk.

    The SDK is heavy and requires eth_account; we defer import so common/ can be
    imported in CI/test contexts without secrets present.
    """

    def __init__(
        self,
        agent_wallet: Optional[str] = None,
        private_key: Optional[str] = None,
        account_address: Optional[str] = None,
        base_url: str = "https://api.hyperliquid.xyz",
    ):
        self.agent_wallet = agent_wallet or os.environ.get("HL_AGENT_WALLET")
        self.private_key = private_key or os.environ.get("HL_PRIVATE_KEY")
        # When signer (private_key) is an agent, account_address tells HL which
        # main account to route the order to. Without this, the SDK may sign
        # but HL rejects ("agent not authorized for that account").
        self.account_address = (
            account_address
            or os.environ.get("HL_USER_WALLET")
            or os.environ.get("HL_MAIN_WALLET")
        )
        self.base_url = base_url
        self._exchange = None
        self._info = None

    def _ensure(self):
        if self._exchange is not None:
            return
        if not self.private_key:
            raise RuntimeError("HL_PRIVATE_KEY not set; cannot place live orders")
        from eth_account import Account  # type: ignore
        from hyperliquid.exchange import Exchange  # type: ignore
        from hyperliquid.info import Info  # type: ignore

        wallet = Account.from_key(self.private_key)
        self._info = Info(self.base_url, skip_ws=True)
        # Pass account_address so HL routes the agent-signed order to the main account
        if self.account_address:
            self._exchange = Exchange(
                wallet, self.base_url, account_address=self.account_address
            )
        else:
            self._exchange = Exchange(wallet, self.base_url)

    def _round_size(self, coin: str, size_coin: float, ref_price: float) -> Optional[float]:
        """Truncate (not round) size to coin's szDecimals from HL meta.
        Required before market_open or HL SDK raises 'float_to_wire causes
        rounding'. Truncate-down preserves the risk-sizing budget (round()
        would push size_coin above what margin math allocated, breaching
        per-trade caps).

        Returns None on:
        - meta lookup failure (don't guess on precision — skip)
        - coin not present in HL universe
        - resulting notional < $10 (HL's documented min order size)

        szDecimals cached on first call. Council audit 2026-05-17: 6/6
        unanimous on truncation + min-notional guard.
        """
        self._ensure()
        if not hasattr(self, "_sz_decimals"):
            self._sz_decimals: dict[str, int] = {}
            try:
                meta = self._info.meta()
                for asset in (meta or {}).get("universe", []) or []:
                    name = asset.get("name") or ""
                    sz = int(asset.get("szDecimals", 4))
                    if name:
                        self._sz_decimals[name] = sz
            except Exception:
                log.exception("hl meta lookup failed; cannot size orders safely")
                self._sz_decimals = {}
        if coin not in self._sz_decimals:
            log.warning("no szDecimals for %s; skip order", coin)
            return None
        decs = self._sz_decimals[coin]
        factor = 10 ** decs
        truncated = math.floor(size_coin * factor) / factor
        notional = truncated * ref_price
        if notional < 10.0:
            log.info("skip %s: notional $%.2f below HL $10 min (size=%g, szDec=%d)",
                     coin, notional, truncated, decs)
            return None
        return truncated

    def market_open(
        self,
        coin: str,
        is_buy: bool,
        size_coin: float,
        cloid: str,
        slippage: float = 0.005,
        ref_price: float = 0.0,
    ) -> OrderResult:
        self._ensure()
        # Truncate size to per-asset szDecimals; enforce $10 min notional.
        if ref_price > 0:
            rounded = self._round_size(coin, size_coin, ref_price)
            if rounded is None:
                return OrderResult(
                    ok=False, cloid=cloid, coin=coin,
                    side="B" if is_buy else "A",
                    size_coin=size_coin, px=None, raw={},
                    error="precision_or_min_notional",
                )
            size_coin = rounded
        try:
            from hyperliquid.utils.types import Cloid
            cloid_obj = Cloid.from_str(cloid)
            res = self._exchange.market_open(
                name=coin,
                is_buy=is_buy,
                sz=size_coin,
                slippage=slippage,
                cloid=cloid_obj,
            )
            ok = bool(res and res.get("status") == "ok")
            return OrderResult(
                ok=ok,
                cloid=cloid,
                coin=coin,
                side="B" if is_buy else "A",
                size_coin=size_coin,
                px=None,
                raw=res or {},
                error=None if ok else str(res),
            )
        except Exception as e:
            return OrderResult(
                ok=False, cloid=cloid, coin=coin,
                side="B" if is_buy else "A",
                size_coin=size_coin, px=None, raw={}, error=str(e),
            )

    def market_close(self, coin: str, size_coin: Optional[float] = None, cloid: Optional[str] = None) -> OrderResult:
        self._ensure()
        # Truncate close size to per-asset szDecimals (same float_to_wire fix as
        # market_open). Without this, closes on alt positions with non-trivial
        # decimals (e.g. ARB 203.06, ETH 0.01124) fail with 'float_to_wire causes
        # rounding'. close_retries climbs to MAX, strategy gets halted, position
        # stuck open on HL with no exit path. We bypass the $10 min-notional
        # check here because closing under-min is still better than not closing
        # (otherwise position lives forever).
        if size_coin is not None and size_coin > 0:
            try:
                # Reuse the sz_decimals cache populated by _round_size
                if not hasattr(self, "_sz_decimals"):
                    self._sz_decimals = {}
                    try:
                        meta = self._info.meta()
                        for asset in (meta or {}).get("universe", []) or []:
                            name = asset.get("name") or ""
                            sz = int(asset.get("szDecimals", 4))
                            if name:
                                self._sz_decimals[name] = sz
                    except Exception:
                        pass
                decs = self._sz_decimals.get(coin, 4)
                factor = 10 ** decs
                size_coin = math.floor(size_coin * factor) / factor
            except Exception:
                log.exception("market_close size truncation failed for %s; passing raw", coin)
        try:
            from hyperliquid.utils.types import Cloid
            cloid_obj = Cloid.from_str(cloid) if cloid else None
            res = self._exchange.market_close(coin=coin, sz=size_coin, cloid=cloid_obj)
            ok = bool(res and res.get("status") == "ok")
            return OrderResult(
                ok=ok,
                cloid=cloid or "",
                coin=coin,
                side="close",
                size_coin=size_coin or 0.0,
                px=None,
                raw=res or {},
                error=None if ok else str(res),
            )
        except Exception as e:
            return OrderResult(
                ok=False, cloid=cloid or "", coin=coin, side="close",
                size_coin=size_coin or 0.0, px=None, raw={}, error=str(e),
            )

    def place_brackets(
        self,
        coin: str,
        is_long: bool,
        size_coin: float,
        tp_px: float,
        sl_px: float,
        ref_price: float,
        tp_cloid: str,
        sl_cloid: str,
    ) -> dict:
        """Place HL-native TP + SL trigger orders, reduce-only, against an
        already-open position. Both isMarket=True for safety per council
        2026-05-17 — maker-priced SL can be jumped over in cascades.

        Returns {"tp": OrderResult, "sl": OrderResult}. Either may fail
        independently; trader.open should fall back to poll-based exit if
        EITHER fails (council: keep safety net regardless).

        size_coin and prices are pre-rounded by caller (trader.open already
        applies _round_size to size, but trigger prices must also respect
        the asset's pxDecimals — HL rejects trigger prices that don't).
        """
        self._ensure()
        from hyperliquid.utils.types import Cloid
        # Truncate trigger prices to per-asset pxDecimals (5 - szDecimals for perps)
        sz_decs = self._sz_decimals.get(coin, 4) if hasattr(self, "_sz_decimals") else 4
        px_decs = max(0, 6 - sz_decs - 1)  # HL formula: pxDecimals = 6 - szDecimals - 1 for perps (5 max)
        tp_px_r = round(tp_px, px_decs)
        sl_px_r = round(sl_px, px_decs)
        # Exit side is OPPOSITE of entry — long position closes via SELL
        exit_is_buy = not is_long
        out: dict = {"tp": None, "sl": None}
        # Helper
        def _trigger(name: str, trigger_px: float, cloid_hex: str, tpsl: str) -> OrderResult:
            try:
                cloid_obj = Cloid.from_str(cloid_hex)
                # limit_px must be passed but for isMarket=True triggers it's
                # ignored at fill time. Use trigger_px as a sane default.
                order_type = {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": tpsl}}
                res = self._exchange.order(
                    name=coin,
                    is_buy=exit_is_buy,
                    sz=size_coin,
                    limit_px=trigger_px,
                    order_type=order_type,
                    reduce_only=True,
                    cloid=cloid_obj,
                )
                ok = bool(res and res.get("status") == "ok")
                return OrderResult(
                    ok=ok, cloid=cloid_hex, coin=coin,
                    side="B" if exit_is_buy else "A",
                    size_coin=size_coin, px=trigger_px, raw=res or {},
                    error=None if ok else str(res),
                )
            except Exception as e:
                return OrderResult(
                    ok=False, cloid=cloid_hex, coin=coin,
                    side="B" if exit_is_buy else "A",
                    size_coin=size_coin, px=trigger_px, raw={}, error=str(e),
                )
        out["tp"] = _trigger("tp", tp_px_r, tp_cloid, "tp")
        out["sl"] = _trigger("sl", sl_px_r, sl_cloid, "sl")
        return out

    def cancel_order(self, coin: str, cloid: str) -> OrderResult:
        """Cancel by cloid. Used to clean up an orphan trigger when its
        partner fires (e.g. TP filled → SL should be cancelled).
        """
        self._ensure()
        try:
            from hyperliquid.utils.types import Cloid
            cloid_obj = Cloid.from_str(cloid)
            res = self._exchange.cancel_by_cloid(name=coin, cloid=cloid_obj)
            ok = bool(res and res.get("status") == "ok")
            return OrderResult(
                ok=ok, cloid=cloid, coin=coin, side="cancel",
                size_coin=0.0, px=None, raw=res or {},
                error=None if ok else str(res),
            )
        except Exception as e:
            return OrderResult(
                ok=False, cloid=cloid, coin=coin, side="cancel",
                size_coin=0.0, px=None, raw={}, error=str(e),
            )
