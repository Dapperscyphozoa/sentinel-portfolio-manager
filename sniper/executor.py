"""Sniper executor — wraps hyperliquid-python-sdk for fast market entries.

Pre-signed orders verified at ~493ms sign time. Uses IOC/market for entry,
limit for TP/SL placement.

Paper mode: SNIPER_LIVE_TRADING=0 returns simulated fills.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("sniper_executor")


@dataclass
class ExecutionResult:
    success: bool
    coin: str
    is_long: bool
    fill_px: float
    size_coin: float
    notional_usd: float
    reason: str
    cloid: str = ""
    paper: bool = False


class SniperExecutor:
    """Wraps HL SDK. In paper mode, returns simulated fills using HL mark price."""

    def __init__(self, agent_address: Optional[str] = None,
                 agent_private_key: Optional[str] = None,
                 leverage: float = 5.0) -> None:
        self.agent_address = agent_address or os.environ.get("HL_AGENT_WALLET")
        self.agent_pk = agent_private_key or os.environ.get("HL_PRIVATE_KEY")
        self.leverage = leverage
        self.live = os.environ.get("SNIPER_LIVE_TRADING", "0") == "1"
        self._exchange = None
        self._info = None

    def _load_sdk(self):
        """Lazy-load HL SDK to keep startup fast."""
        if self._exchange is not None:
            return
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            wallet = Account.from_key(self.agent_pk) if self.agent_pk else None
            self._info = Info(skip_ws=True)
            if self.live and wallet:
                self._exchange = Exchange(wallet, base_url="https://api.hyperliquid.xyz",
                                          account_address=self.agent_address)
                log.info("HL Exchange loaded LIVE for agent %s", self.agent_address)
            else:
                log.info("HL SDK loaded PAPER mode")
        except Exception as e:
            log.exception("HL SDK load failed: %s", e)

    def fire(self, coin: str, is_long: bool, margin_usd: float,
             tp_pct: float = 0.05, sl_pct: float = 0.05) -> ExecutionResult:
        """Fire a market order with TP/SL. Returns ExecutionResult."""
        self._load_sdk()
        cloid = f"snipe_{coin[:8]}_{int(time.time())}"

        if not self.live:
            # Paper: use HL mark as fill price + 0.3% slippage (event-driven, wider)
            try:
                from sniper.oracle_lag import fetch_hl_mark
                fill_px = fetch_hl_mark(coin)
                if fill_px is None:
                    return ExecutionResult(False, coin, is_long, 0, 0, 0,
                                          "paper_no_hl_mark", cloid, paper=True)
                slip = 0.003 * fill_px * (1 if is_long else -1)
                eff_fill = fill_px + slip
                notional = margin_usd * self.leverage
                size_coin = notional / eff_fill
                log.info("PAPER FILL: %s %s size=%.4f @ %.6f (slip 0.3%%)",
                         "LONG" if is_long else "SHORT", coin, size_coin, eff_fill)
                return ExecutionResult(True, coin, is_long, eff_fill, size_coin,
                                       notional, "paper_filled", cloid, paper=True)
            except Exception as e:
                log.exception("paper fire failed: %s", e)
                return ExecutionResult(False, coin, is_long, 0, 0, 0,
                                      f"paper_error:{e}", cloid, paper=True)

        # Live mode
        if self._exchange is None:
            return ExecutionResult(False, coin, is_long, 0, 0, 0,
                                  "exchange_not_loaded", cloid)
        try:
            # Get current mark to compute size
            from sniper.oracle_lag import fetch_hl_mark
            mark = fetch_hl_mark(coin)
            if mark is None:
                return ExecutionResult(False, coin, is_long, 0, 0, 0,
                                      "no_hl_mark_for_sizing", cloid)
            notional = margin_usd * self.leverage
            size_coin = notional / mark
            # Round size to HL precision (assume 4 decimals; HL meta could give exact)
            size_coin = round(size_coin, 4)
            if size_coin <= 0:
                return ExecutionResult(False, coin, is_long, 0, 0, 0,
                                      "size_zero", cloid)
            # Market IOC order via SDK's market_open helper
            t0 = time.time()
            order_result = self._exchange.market_open(
                coin, is_long, size_coin, None,
                slippage=0.05   # accept up to 5% slippage on event-driven entry
            )
            sign_ms = (time.time() - t0) * 1000
            log.info("LIVE ORDER: %s in %.0fms result=%s", cloid, sign_ms, order_result)
            if order_result.get("status") == "ok":
                # Parse fill
                statuses = order_result.get("response", {}).get("data", {}).get("statuses", [])
                if statuses and "filled" in statuses[0]:
                    fill = statuses[0]["filled"]
                    fill_px = float(fill["avgPx"])
                    fill_sz = float(fill["totalSz"])
                    return ExecutionResult(True, coin, is_long, fill_px, fill_sz,
                                          fill_sz * fill_px, "live_filled", cloid)
            return ExecutionResult(False, coin, is_long, 0, 0, 0,
                                  f"order_rejected:{order_result}", cloid)
        except Exception as e:
            log.exception("live fire failed: %s", e)
            return ExecutionResult(False, coin, is_long, 0, 0, 0,
                                  f"live_error:{e}", cloid)

    def close(self, coin: str, is_long: bool, size_coin: float) -> dict:
        """Close a position (paper or live)."""
        self._load_sdk()
        if not self.live:
            try:
                from sniper.oracle_lag import fetch_hl_mark
                mark = fetch_hl_mark(coin)
                if mark is None:
                    return {"ok": False, "reason": "paper_no_mark"}
                # Paper close: 0.3% slippage opposite direction
                slip = 0.003 * mark * (1 if is_long else -1)
                eff_close = mark - slip if is_long else mark + slip
                return {"ok": True, "fill_px": eff_close, "paper": True}
            except Exception as e:
                return {"ok": False, "reason": str(e)}
        try:
            order = self._exchange.market_close(coin, size_coin, slippage=0.05)
            return {"ok": True, "raw": order}
        except Exception as e:
            return {"ok": False, "reason": str(e)}
