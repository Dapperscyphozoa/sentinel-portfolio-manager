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
        self.leverage = config.get_float("LEVERAGE", 5.0)

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

    def position_loop_once(self, registry=None) -> int:
        """Scan every open trade and close those that hit SL/TP/timeout, OR
        whose strategy.should_close() returns True (e.g. Donchian's
        trail-exit). Returns # closed.

        `registry` is an optional list of StrategyBase classes (used for
        strategy-driven close checks). If None, only SL/TP/timeout is used.
        """
        rows = self.conn.execute(
            "SELECT cloid,strategy,coin,is_long,open_ts,open_px,size_coin,sl_px,tp_px,max_hold_bars "
            "FROM trades WHERE status='open'"
        ).fetchall()
        closed = 0
        now = time.time()
        strat_by_name = {s.NAME: s for s in (registry or [])}
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

            # Strategy-driven exit (e.g. Donchian 10-bar opposite break)
            strat_close = False
            strat_reason = ""
            strat_cls = strat_by_name.get(r["strategy"])
            if strat_cls is not None and not (hit_tp or hit_sl or timed_out):
                try:
                    sc, sr = strat_cls.should_close(r, self.bus)
                    strat_close = bool(sc)
                    strat_reason = sr or "strategy_exit"
                except Exception:
                    log.exception("should_close raised for %s/%s", r["strategy"], coin)

            if not (hit_tp or hit_sl or timed_out or strat_close):
                continue
            reason = "tp" if hit_tp else "sl" if hit_sl else "timeout" if timed_out else strat_reason
            self._close(r, px, reason, now)
            closed += 1
        return closed

    MAX_CLOSE_RETRIES = 5

    def _close(self, trade_row, close_px: float, reason: str, ts: float) -> None:
        cloid = trade_row["cloid"]
        coin = trade_row["coin"]
        strategy = trade_row["strategy"]
        is_long = bool(trade_row["is_long"])
        size_coin = float(trade_row["size_coin"])
        open_px = float(trade_row["open_px"])
        live = self.hl is not None and self._is_live(strategy)

        # LIVE close: must succeed on HL before we mark closed in our DB. If
        # we mark closed prematurely, the position is orphaned: still open on
        # HL with no SL/TP protection, but our position_loop won't see it.
        # On failure: increment retries, leave status='open', let next tick
        # retry. After MAX_CLOSE_RETRIES halts the strategy and alerts.
        if live:
            try:
                res = self.hl.market_close(coin=coin, size_coin=size_coin, cloid=cloid)
            except Exception as e:
                log.exception("hl close raised")
                res = None
                err = str(e)
            else:
                err = None if res.ok else (res.error or "unknown")

            if res is None or not res.ok:
                # leave open, bump retry counter
                self.conn.execute(
                    "UPDATE trades SET close_retries = close_retries + 1 WHERE cloid=?",
                    (cloid,),
                )
                cur = self.conn.execute(
                    "SELECT close_retries FROM trades WHERE cloid=?", (cloid,)
                ).fetchone()
                retries = int(cur["close_retries"]) if cur else 0
                log.error("hl close FAILED (retry %d/%d) %s/%s reason=%s err=%s",
                          retries, self.MAX_CLOSE_RETRIES, strategy, coin, reason, err)
                if retries >= self.MAX_CLOSE_RETRIES:
                    # Halt the strategy via halts table so no new opens.
                    # Existing trades remain open and the operator must
                    # manually reconcile (or the next deploy's reconciler will).
                    try:
                        self.conn.execute(
                            "INSERT INTO halts(ts, strategy, halted, reason, actor) VALUES (?, ?, 1, ?, ?)",
                            (ts, strategy, f"close_failed cloid={cloid} after {retries} retries", "trader"),
                        )
                        log.critical("HALT %s — close failures (cloid=%s)", strategy, cloid)
                    except Exception:
                        log.exception("halt insert failed")
                return  # do NOT mark closed

        # PAPER (or live success): mark closed and write closure row
        pnl = (close_px - open_px) * size_coin * (1 if is_long else -1)
        self.conn.execute("UPDATE trades SET status='closed' WHERE cloid=?", (cloid,))
        self.conn.execute(
            "INSERT INTO closures(cloid,strategy,coin,is_long,open_ts,close_ts,open_px,close_px,size_coin,"
            "pnl_usd,fees_usd,close_reason,extras_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cloid, strategy, coin, int(is_long), float(trade_row["open_ts"]), ts,
             open_px, close_px, size_coin, pnl, 0.0, reason, "{}"),
        )
        log.info("closed %s/%s %s pnl=%.2f", strategy, coin, reason, pnl)
