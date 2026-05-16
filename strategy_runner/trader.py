"""Position lifecycle: open, monitor for SL/TP/timeout, close.

Paper mode (LIVE_TRADING=0 or STRATEGY_<NAME>_LIVE=0) records signals + simulated
fills to SQLite without hitting HL.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from common import config, persistence
from common.bus_client import BusClient
from common.hl_exchange import HLExchange, make_cloid
from common.pm_client import PMClient

from .strategies._base import Signal, StrategyBase


log = logging.getLogger("trader")


@dataclass
class OpenResult:
    ok: bool
    cloid: str
    error: Optional[str] = None


class Trader:
    def __init__(self, conn, bus: BusClient, pm: PMClient, hl: Optional[HLExchange] = None):
        self.conn = conn
        self.bus = bus
        self.pm = pm
        self.hl = hl
        self.live_default = config.get_bool("LIVE_TRADING", default=False)
        self.leverage = config.get_int("LEVERAGE", 5)

    def _is_live(self, strategy: str) -> bool:
        # Per-strategy override > global default
        per = config.get(f"STRATEGY_{strategy.upper()}_LIVE")
        if per is not None:
            return per.strip().lower() in ("1", "true", "yes", "on")
        return self.live_default

    def open(self, strategy: StrategyBase, sig: Signal, size_usd: float) -> OpenResult:
        cloid = make_cloid(strategy.CLOID_PREFIX, sig.coin)
        size_coin = size_usd / sig.ref_price if sig.ref_price > 0 else 0.0
        live = self._is_live(strategy.NAME)
        open_ts = time.time()

        # PM-side cloid registration
        try:
            self.pm.register_cloid(strategy.NAME, cloid, sig.coin, sig.side)
        except Exception:
            log.exception("pm.register_cloid")  # non-fatal

        # signal row
        self.conn.execute(
            "INSERT INTO signals(ts,strategy,coin,side,is_long,ref_price,sl_px,tp_px,max_hold_bars,fire_reason,extras_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sig.fire_ts / 1000.0, strategy.NAME, sig.coin, sig.side, int(sig.is_long),
             sig.ref_price, sig.sl_px, sig.tp_px, sig.max_hold_bars, sig.fire_reason,
             json.dumps(sig.extras, default=str)),
        )

        err: Optional[str] = None
        if live:
            if self.hl is None:
                err = "live but no HL exchange wired"
            else:
                res = self.hl.market_open(
                    coin=sig.coin, is_buy=sig.is_long, size_coin=size_coin, cloid=cloid,
                )
                if not res.ok:
                    err = res.error

        status = "open" if (live and err is None) or not live else "open_failed"

        self.conn.execute(
            "INSERT OR IGNORE INTO trades(cloid,strategy,coin,side,is_long,open_ts,open_px,size_usd,size_coin,"
            "sl_px,tp_px,max_hold_bars,status,extras_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cloid, strategy.NAME, sig.coin, sig.side, int(sig.is_long), open_ts,
             sig.ref_price, size_usd, size_coin, sig.sl_px, sig.tp_px,
             sig.max_hold_bars, status,
             json.dumps({"live": live, "fire_reason": sig.fire_reason, "extras": sig.extras}, default=str)),
        )
        return OpenResult(ok=(err is None), cloid=cloid, error=err)

    def position_loop_once(self) -> int:
        """Scan every open trade and close those that hit SL/TP/timeout. Returns # closed."""
        rows = self.conn.execute(
            "SELECT cloid,strategy,coin,is_long,open_ts,open_px,size_coin,sl_px,tp_px,max_hold_bars "
            "FROM trades WHERE status='open'"
        ).fetchall()
        closed = 0
        now = time.time()
        for r in rows:
            coin = r["coin"]
            try:
                m = self.bus.markprice(coin)
            except Exception:
                continue
            px = (m.get("hl_mid") or m.get("binance_mid"))
            if not px:
                continue
            px = float(px)
            is_long = bool(r["is_long"])
            hit_tp = (is_long and px >= r["tp_px"]) or (not is_long and px <= r["tp_px"])
            hit_sl = (is_long and px <= r["sl_px"]) or (not is_long and px >= r["sl_px"])
            # tf is 1h proxy: max_hold_bars * 3600s. Strategies may override via extras, ignored for v1.
            timed_out = (now - r["open_ts"]) > r["max_hold_bars"] * 3600
            if not (hit_tp or hit_sl or timed_out):
                continue
            reason = "tp" if hit_tp else "sl" if hit_sl else "timeout"
            self._close(r, px, reason, now)
            closed += 1
        return closed

    def _close(self, trade_row, close_px: float, reason: str, ts: float) -> None:
        cloid = trade_row["cloid"]
        coin = trade_row["coin"]
        is_long = bool(trade_row["is_long"])
        size_coin = float(trade_row["size_coin"])
        open_px = float(trade_row["open_px"])
        pnl = (close_px - open_px) * size_coin * (1 if is_long else -1)
        if self.hl is not None and self._is_live(trade_row["strategy"]):
            try:
                self.hl.market_close(coin=coin, size_coin=size_coin, cloid=cloid)
            except Exception:
                log.exception("hl close")
        self.conn.execute("UPDATE trades SET status='closed' WHERE cloid=?", (cloid,))
        self.conn.execute(
            "INSERT INTO closures(cloid,strategy,coin,is_long,open_ts,close_ts,open_px,close_px,size_coin,"
            "pnl_usd,fees_usd,close_reason,extras_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cloid, trade_row["strategy"], coin, int(is_long), float(trade_row["open_ts"]), ts,
             open_px, close_px, size_coin, pnl, 0.0, reason, "{}"),
        )
        log.info("closed %s/%s %s pnl=%.2f", trade_row["strategy"], coin, reason, pnl)
