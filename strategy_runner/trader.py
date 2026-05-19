"""Position lifecycle: open, monitor for SL/TP/timeout, close.

Paper mode (LIVE_TRADING=0 or STRATEGY_<NAME>_LIVE=0) records signals + simulated
fills to SQLite without hitting HL.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from common import config, halt as _halt, persistence
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

    def is_coin_locked(self, coin: str, failed_cooldown_s: int = 60) -> tuple[bool, str]:
        """Synchronous, in-process check: is this coin already held by ANY engine,
        OR did a recent HL order-place fail for this coin?

        Returns (locked, reason). Used by runner pre-check before pm.check
        and as defense-in-depth inside open().
        """
        coin = (coin or "").upper()
        if not coin:
            return True, "no_coin"
        row = self.conn.execute(
            "SELECT status FROM trades WHERE coin=? "
            "AND status IN ('open','pending') LIMIT 1",
            (coin,),
        ).fetchone()
        if row is not None:
            return True, f"coin_locked:{row['status']}"
        # Recent open_failed cooldown — prevents hammering HL with retries on
        # a coin that just rejected (the "APT 71 times in 24h" failure mode).
        cutoff = time.time() - failed_cooldown_s
        row = self.conn.execute(
            "SELECT 1 FROM trades WHERE coin=? AND status='open_failed' "
            "AND open_ts > ? LIMIT 1",
            (coin, cutoff),
        ).fetchone()
        if row is not None:
            return True, "coin_recently_failed"
        return False, ""

    def open(self, strategy: StrategyBase, sig: Signal, size_usd: float) -> OpenResult:
        cloid = make_cloid(strategy.CLOID_PREFIX, sig.coin)
        # Position sizing: size_coin = size_usd / ref_price. PM passes size_usd
        # as MARGIN (e.g. 5% wallet); the position notional on HL therefore
        # equals margin, not margin × leverage. Operator-confirmed 2026-05-19:
        # this conservative sizing is intentional. SPEC.md / pm/pretrade.py
        # header text still references "notional = 25% wallet" — that line
        # is descriptive of the registry's pre-bug shape, not current intent.
        size_coin = size_usd / sig.ref_price if sig.ref_price > 0 else 0.0
        live = self._is_live(strategy.NAME)
        open_ts = time.time()
        coin_upper = (sig.coin or "").upper()

        # PM-side cloid registration (kept for attribution; non-fatal)
        try:
            self.pm.register_cloid(strategy.NAME, cloid, sig.coin, sig.side)
        except Exception:
            log.exception("pm.register_cloid")

        # signal row (always recorded for telemetry, regardless of lock outcome)
        self.conn.execute(
            "INSERT INTO signals(ts,strategy,coin,side,is_long,ref_price,sl_px,tp_px,max_hold_bars,fire_reason,extras_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sig.fire_ts / 1000.0, strategy.NAME, sig.coin, sig.side, int(sig.is_long),
             sig.ref_price, sig.sl_px, sig.tp_px, sig.max_hold_bars, sig.fire_reason,
             json.dumps(sig.extras, default=str)),
        )

        # ─── 1_GLOBAL coin lock: reserve slot atomically BEFORE placing HL order ───
        # Insert 'pending' row first. Partial unique index on trades(coin)
        # WHERE status IN ('open','pending') makes this race-safe: if another
        # engine already locked this coin (open OR pending), IntegrityError
        # fires and we abort without touching HL.
        try:
            self.conn.execute(
                "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,open_px,size_usd,size_coin,"
                "sl_px,tp_px,max_hold_bars,status,extras_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                (cloid, strategy.NAME, coin_upper, sig.side, int(sig.is_long), open_ts,
                 sig.ref_price, size_usd, size_coin, sig.sl_px, sig.tp_px,
                 sig.max_hold_bars,
                 json.dumps({"live": live, "fire_reason": sig.fire_reason,
                             "extras": sig.extras, "stage": "pending"}, default=str)),
            )
        except sqlite3.IntegrityError as e:
            # Coin already locked by another engine's open/pending row, OR
            # duplicate cloid (extremely unlikely given salted prefix).
            log.info("trader.open coin_locked %s/%s — skipping (race lost or active position)",
                     strategy.NAME, sig.coin)
            return OpenResult(ok=False, cloid=cloid, error=f"coin_locked:{e}")

        # ─── HL order placement ───
        # Wrapped in try/finally so the 'pending' row ALWAYS transitions to a
        # terminal status, even if an unexpected exception propagates from
        # hl.market_open. Without this, an exception here would strand the
        # row at status='pending' and the coin lock would never release.
        err: Optional[str] = None
        tp_cloid: Optional[str] = None
        sl_cloid: Optional[str] = None
        bracket_status: Optional[dict] = None
        try:
            if live:
                if self.hl is None:
                    err = "live but no HL exchange wired"
                else:
                    res = self.hl.market_open(
                        coin=sig.coin, is_buy=sig.is_long, size_coin=size_coin, cloid=cloid,
                        ref_price=sig.ref_price,
                    )
                    if not res.ok:
                        err = res.error
                    else:
                        # Entry filled — place TP + SL brackets.
                        tp_cloid = make_cloid(strategy.CLOID_PREFIX + "tp_", sig.coin)
                        sl_cloid = make_cloid(strategy.CLOID_PREFIX + "sl_", sig.coin)
                        # ── Liquidation-aware SL safety (operator 2026-05-18) ──
                        # Discovered after orphan ETH position had its 10% SL placed
                        # BELOW liq level on a 12x iso position — SL unreachable
                        # because liq triggered first. Generic fix: ensure SL is
                        # always at least LIQ_BUFFER_PCT above (long) / below
                        # (short) the maintenance-margin liquidation price.
                        # Liq formula for iso: liq_px = entry * (1 ± 1/(2L)).
                        # Buffer 0.5% provides slack against funding drift,
                        # mark-vs-trade px diff, and partial-fill rounding.
                        fill_px = res.fill_px if (getattr(res, "fill_px", None)) else sig.ref_price
                        lev = float(self.leverage) if self.leverage else 5.0
                        try:
                            mm_pct = 1.0 / (2.0 * lev)
                            if sig.is_long:
                                liq_px = fill_px * (1.0 - mm_pct)
                                safe_sl = liq_px * 1.005   # 0.5% buffer above liq
                                effective_sl = max(float(sig.sl_px), safe_sl)
                            else:
                                liq_px = fill_px * (1.0 + mm_pct)
                                safe_sl = liq_px * 0.995   # 0.5% buffer below liq
                                effective_sl = min(float(sig.sl_px), safe_sl)
                            if abs(effective_sl - float(sig.sl_px)) > 1e-8:
                                log.warning(
                                    "SL override %s/%s: strategy_sl=%.4f below_liq=%.4f → safe_sl=%.4f (lev=%.0fx)",
                                    strategy.NAME, sig.coin, float(sig.sl_px), liq_px, effective_sl, lev,
                                )
                        except Exception:
                            log.exception("safe_sl computation failed; using strategy sl_px as-is")
                            effective_sl = float(sig.sl_px)
                        sl_placed_ok = False
                        try:
                            bracket = self.hl.place_brackets(
                                coin=sig.coin, is_long=bool(sig.is_long),
                                size_coin=res.size_coin or size_coin,
                                tp_px=sig.tp_px, sl_px=effective_sl,
                                ref_price=sig.ref_price,
                                tp_cloid=tp_cloid, sl_cloid=sl_cloid,
                            )
                            bracket_status = {
                                "tp_ok": bool(bracket["tp"] and bracket["tp"].ok),
                                "tp_err": (bracket["tp"].error if bracket["tp"] else None),
                                "sl_ok": bool(bracket["sl"] and bracket["sl"].ok),
                                "sl_err": (bracket["sl"].error if bracket["sl"] else None),
                            }
                            sl_placed_ok = bracket_status["sl_ok"]
                            if not bracket_status["tp_ok"] or not bracket_status["sl_ok"]:
                                log.warning("bracket place FAILED %s/%s: %s",
                                            strategy.NAME, sig.coin, bracket_status)
                            else:
                                log.info("brackets placed %s/%s tp=%s sl=%s",
                                         strategy.NAME, sig.coin, tp_cloid[:14], sl_cloid[:14])
                        except Exception as e:
                            log.exception("place_brackets crashed %s/%s", strategy.NAME, sig.coin)
                            bracket_status = {"error": str(e)}

                        # ── Atomic-SL guarantee: if the stop-loss bracket did
                        # NOT place, the position is unprotected. The 60s
                        # position-poll fallback is not a substitute for a
                        # native trigger (stale-mark + missed wicks). Roll back
                        # the entry: emergency market_close + flag the strategy.
                        # If the rollback close ALSO fails, halt the strategy
                        # so no new opens stack on top of an unprotected one.
                        if not sl_placed_ok:
                            try:
                                # Prefer the actual filled qty; fall back to the
                                # requested size only when the exchange did not
                                # report one. Reject zero/None defensively — a
                                # market_close(size_coin=0) would silently no-op.
                                emergency_size = res.size_coin if (res.size_coin and res.size_coin > 0) else size_coin
                                if not emergency_size or emergency_size <= 0:
                                    raise ValueError(f"emergency close: no valid size (res={res.size_coin}, req={size_coin})")
                                close_res = self.hl.market_close(
                                    coin=sig.coin,
                                    size_coin=emergency_size,
                                )
                                if close_res and getattr(close_res, "ok", False):
                                    log.critical(
                                        "SL bracket failed %s/%s — emergency closed "
                                        "the entry (no native stop). %s",
                                        strategy.NAME, sig.coin, bracket_status,
                                    )
                                    err = "sl_bracket_failed_closed"
                                    # Booking: the entry did fill and was
                                    # closed immediately. PnL is near-zero
                                    # (one round-trip slippage) minus 2×
                                    # taker fees. Write a closures row so
                                    # /attribution doesn't show a P&L hole.
                                    try:
                                        fill_px_for_close = getattr(close_res, "fill_px", None) or fill_px
                                        notional_filled = float(emergency_size) * float(fill_px)
                                        fees_emerg = notional_filled * 0.00045 * 2
                                        pnl_emerg = ((float(fill_px_for_close) - float(fill_px))
                                                     * float(emergency_size)
                                                     * (1 if sig.is_long else -1)) - fees_emerg
                                        self.conn.execute(
                                            "INSERT OR IGNORE INTO closures(cloid, strategy, coin, is_long, "
                                            "open_ts, close_ts, open_px, close_px, size_coin, pnl_usd, "
                                            "fees_usd, close_reason, extras_json) "
                                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                            (cloid, strategy.NAME, sig.coin, int(sig.is_long),
                                             open_ts, time.time(), float(fill_px),
                                             float(fill_px_for_close), float(emergency_size),
                                             pnl_emerg, fees_emerg,
                                             "sl_bracket_failed_emergency_close",
                                             json.dumps({"bracket_status": bracket_status,
                                                          "emergency_size": emergency_size}, default=str)),
                                        )
                                    except Exception:
                                        log.exception("emergency-close closures insert failed for %s", cloid)
                                else:
                                    log.critical(
                                        "SL bracket FAILED + emergency close FAILED %s/%s "
                                        "— position open WITHOUT native stop. Halting strategy. %s",
                                        strategy.NAME, sig.coin,
                                        getattr(close_res, "error", None) or close_res,
                                    )
                                    err = "sl_bracket_failed_unprotected"
                                    try:
                                        _halt.set_halt(
                                            self.conn, strategy.NAME, halted=True,
                                            reason=f"sl_bracket_failed_unprotected cloid={cloid}",
                                            actor="trader",
                                        )
                                    except Exception:
                                        log.exception("halt-on-bracket-fail set_halt failed")
                            except Exception:
                                log.exception(
                                    "SL bracket failed + emergency close raised %s/%s — "
                                    "halting strategy", strategy.NAME, sig.coin,
                                )
                                err = "sl_bracket_failed_close_raised"
                                try:
                                    _halt.set_halt(
                                        self.conn, strategy.NAME, halted=True,
                                        reason=f"sl_bracket_failed_close_raised cloid={cloid}",
                                        actor="trader",
                                    )
                                except Exception:
                                    log.exception("halt-on-bracket-fail set_halt failed")
                        # Telemetry: if effective_sl diverged from strategy's sl_px,
                        # persist effective value so position_loop monitors the
                        # actually-active level (the strategy's intent stays in
                        # signals table for audit).
                        try:
                            if 'effective_sl' in locals() and abs(effective_sl - float(sig.sl_px)) > 1e-8:
                                self.conn.execute(
                                    "UPDATE trades SET sl_px=? WHERE cloid=?",
                                    (effective_sl, cloid),
                                )
                        except Exception:
                            log.exception("trades.sl_px sync failed")
        except Exception as e:
            log.exception("hl.market_open raised %s/%s", strategy.NAME, sig.coin)
            err = f"hl_raised:{e}"
        finally:
            # Always promote pending → open or open_failed, even on exception
            new_status = "open" if (live and err is None) or not live else "open_failed"
            if err:
                log.warning("trader.open FAILED %s/%s: %s (cloid=%s size_usd=%.2f live=%s)",
                            strategy.NAME, sig.coin, err, cloid, size_usd, live)
            try:
                self.conn.execute(
                    "UPDATE trades SET status=?, extras_json=? WHERE cloid=?",
                    (new_status,
                     json.dumps({"live": live, "fire_reason": sig.fire_reason,
                                 "extras": sig.extras, "open_error": err,
                                 "tp_cloid": tp_cloid, "sl_cloid": sl_cloid,
                                 "brackets": bracket_status}, default=str),
                     cloid),
                )
            except Exception:
                log.exception("CRITICAL: failed to UPDATE pending row %s — coin may stay locked until sweep",
                              cloid)
        return OpenResult(ok=(err is None), cloid=cloid, error=err)

    def sweep_stale_pending(self, max_age_s: int = 300) -> int:
        """Demote any 'pending' rows older than max_age_s seconds. A pending
        row only exists transiently inside open(); anything older is from a
        process crash mid-call or a stuck exception path.

        IMPORTANT: before demoting to 'open_failed' (which releases the coin
        lock), cross-check against HL. If HL has a position in this coin, the
        order DID succeed before the crash — promote to 'open' (we own it)
        rather than 'open_failed' (we don't). The latter would let a new fire
        through and open a second position, which is the exact failure mode
        that produced 10 duplicate APT shorts on 2026-05-17.

        Failure to query HL → safe default: leave as 'pending' for next pass
        rather than risk releasing the lock on an actually-open position.
        """
        cutoff = time.time() - max_age_s
        stale = self.conn.execute(
            "SELECT id, cloid, coin, strategy, open_ts FROM trades "
            "WHERE status='pending' AND open_ts < ?",
            (cutoff,),
        ).fetchall()
        if not stale:
            return 0
        # Query HL once; index by uppercased coin → size
        try:
            hl_pos = self.bus.hl_positions() or []
            hl_by_coin = {
                (p.get("coin") or "").upper(): float(p.get("size_coin", 0) or 0)
                for p in hl_pos
            }
            hl_available = True
        except Exception:
            log.exception("sweep_stale_pending: bus.hl_positions failed; "
                          "leaving %d pending rows untouched this pass", len(stale))
            return 0
        demoted = 0
        promoted = 0
        for r in stale:
            coin = (r["coin"] or "").upper()
            size = hl_by_coin.get(coin, 0.0)
            if abs(size) > 0:
                # HL has a position — order succeeded, we crashed before
                # marking 'open'. Recover the row.
                self.conn.execute(
                    "UPDATE trades SET status='open', "
                    "extras_json=json_set(COALESCE(extras_json,'{}'),"
                    "  '$.recovered','sweep_promoted_from_pending',"
                    "  '$.hl_size_at_recover',?) "
                    "WHERE id=?",
                    (size, r["id"]),
                )
                promoted += 1
                log.error("sweep_stale_pending: %s/%s cloid=%s RECOVERED — "
                          "HL has size=%g; promoted 'pending' → 'open'",
                          r["strategy"], coin, r["cloid"], size)
            else:
                self.conn.execute(
                    "UPDATE trades SET status='open_failed', "
                    "extras_json=json_set(COALESCE(extras_json,'{}'),"
                    "  '$.open_error','stale_pending_swept') "
                    "WHERE id=?",
                    (r["id"],),
                )
                demoted += 1
        if demoted or promoted:
            log.warning("sweep_stale_pending: demoted=%d promoted=%d "
                        "(stale=%d, hl_available=%s)",
                        demoted, promoted, len(stale), hl_available)
        return demoted + promoted

    def reconcile_with_hl(self, min_confirm_s: int = 60) -> int:
        """Cross-check every local 'open' row against HL's actual position
        list. Any local 'open' coin not present on HL (and not 'pending') is
        an off-book ghost — most likely the HL position closed by SL/TP that
        the bracket placed at open, but our position_loop never observed the
        close. These ghosts hold the coin lock and block new fires forever.

        TWO-PASS CONFIRMATION (sentinel council H2 fix, 2026-05-19):
        A single bus.hl_positions() snapshot can miss a real position during
        WS reconnect or transient lag. If we trust a single snapshot, we may
        release the lock for a coin that genuinely has an HL position open,
        allowing a second engine to fire and create a duplicate HL position.

        Defense: require TWO consecutive 'absent' observations separated by
        at least min_confirm_s (default 60s) before reconciling. First
        absence is recorded in kv_state. Re-seeing the coin clears the
        record. Only second-pass absence after the cool-down reconciles.

        Action: mark such rows status='reconciled_off_book' so the lock
        releases. The trade is NOT booked to closures — operator can review
        the reconciled rows in trades table to backfill PnL from HL fills
        history. Returns # reconciled.

        No-op if HL bus unavailable.
        """
        try:
            hl_pos = self.bus.hl_positions()
        except Exception:
            log.exception("reconcile_with_hl: bus.hl_positions failed; skip")
            return 0
        live_coins = {
            (p.get("coin") or "").upper()
            for p in (hl_pos or [])
            if (p.get("coin") and float(p.get("size_coin", 0) or 0) != 0)
        }
        local_open = self.conn.execute(
            "SELECT id, coin, strategy, open_ts FROM trades WHERE status='open'"
        ).fetchall()
        if not local_open:
            return 0
        # 5min safety window for HL WS lag on freshly-opened trades
        age_cutoff = time.time() - 300
        now = time.time()
        n = 0
        # Stash the set of absent-coins seen this pass for cleanup of stale
        # kv_state entries
        all_local_coins = {(r["coin"] or "").upper() for r in local_open}
        for r in local_open:
            coin = (r["coin"] or "").upper()
            if r["open_ts"] >= age_cutoff:
                continue
            if coin in live_coins:
                # coin reappeared on HL → clear pending-reconcile if any
                key = f"recon_pending:{coin}"
                if persistence.kv_get(self.conn, key) is not None:
                    self.conn.execute("DELETE FROM kv_state WHERE k=?", (key,))
                    log.info("reconcile_with_hl: %s reappeared on HL — pending cleared",
                             coin)
                continue
            # Coin absent on HL. First-pass: record and wait.
            key = f"recon_pending:{coin}"
            first_seen = persistence.kv_get(self.conn, key)
            if first_seen is None:
                persistence.kv_set(self.conn, key, str(now))
                log.warning("reconcile_with_hl: %s/%s id=%d absent on HL (pass 1/2) — "
                            "will reconcile if still absent after %ds",
                            r["strategy"], coin, r["id"], min_confirm_s)
                continue
            # Second-pass: only reconcile if min_confirm_s elapsed
            try:
                first_seen_ts = float(first_seen)
            except (TypeError, ValueError):
                first_seen_ts = now  # corrupt entry — reset
                persistence.kv_set(self.conn, key, str(now))
                continue
            if now - first_seen_ts < min_confirm_s:
                continue
            # Confirmed: row is a ghost. Mark and clear sentinel.
            self.conn.execute(
                "UPDATE trades SET status='reconciled_off_book', "
                "extras_json=json_set(COALESCE(extras_json,'{}'),"
                "  '$.reconcile_reason','hl_position_absent_2pass',"
                "  '$.reconcile_first_absent_ts',?,"
                "  '$.reconcile_confirm_ts',?) "
                "WHERE id=?",
                (first_seen_ts, now, r["id"]),
            )
            self.conn.execute("DELETE FROM kv_state WHERE k=?", (key,))
            n += 1
            log.error("reconcile_with_hl: %s/%s id=%d → reconciled_off_book "
                      "(absent on HL for %ds — confirmed off-book)",
                      r["strategy"], coin, r["id"], int(now - first_seen_ts))
            # Attempt to back-fill closure from HL fills so attribution sees the
            # PnL. If we can't (no matching fills found), the trade is still
            # reconciled; just no closure row. Logged either way.
            try:
                trade_row = self.conn.execute(
                    "SELECT * FROM trades WHERE id=?", (r["id"],)
                ).fetchone()
                booked = self.book_closure_from_fills(trade_row, reason="reconciled_off_book")
                if booked:
                    log.warning("reconcile_with_hl: %s/%s booked closure pnl=$%+.4f from HL fills",
                                r["strategy"], coin, booked)
            except Exception:
                log.exception("book_closure_from_fills failed for id=%d", r["id"])
        # Garbage-collect kv_state entries for coins whose local 'open' rows
        # are gone (closed/reconciled/etc) so they don't re-trigger spuriously.
        for k_row in self.conn.execute(
            "SELECT k FROM kv_state WHERE k LIKE 'recon_pending:%'"
        ).fetchall():
            coin_k = k_row["k"].split(":", 1)[1]
            if coin_k not in all_local_coins:
                self.conn.execute("DELETE FROM kv_state WHERE k=?", (k_row["k"],))
        return n

    def book_closure_from_fills(self, trade_row, reason: str = "from_fills") -> Optional[float]:
        """Reconstruct a closures row by matching HL fills to this trade.

        Method:
          1. Pull HL fills for this coin since open_ts (via signal-bus).
          2. Find the OPEN fill matching this trade's cloid (records true fill px).
          3. Find subsequent CLOSE fill(s) — opposite side, same coin, between
             open_ts and (close_ts hint OR now). Use HL's own closedPnl field
             which is exact.
          4. Insert closures row with summed close_px (qty-weighted), summed
             closedPnl, summed fees.

        Returns the booked net PnL (closedPnl - fees) if a closure row was
        inserted, else None. Idempotent: refuses to insert if closures already
        has a row with this cloid.
        """
        if trade_row is None:
            return None
        cloid = trade_row["cloid"]
        # Idempotency guard
        existing = self.conn.execute(
            "SELECT 1 FROM closures WHERE cloid=? LIMIT 1", (cloid,)
        ).fetchone()
        if existing:
            return None
        coin = (trade_row["coin"] or "").upper()
        open_ts = float(trade_row["open_ts"])
        is_long = bool(trade_row["is_long"])
        # HL fills since open_ts (with a 30s pad to catch the open fill itself)
        since_ms = int((open_ts - 30) * 1000)
        try:
            fills = self.bus.hl_fills(since_ms=since_ms) or []
        except Exception:
            log.exception("book_closure_from_fills: bus.hl_fills failed")
            return None
        # Match open fill by cloid; HL stores cloid in lowercase 0x hex
        cloid_norm = (cloid or "").lower()
        # On HL, side='A' = ask = SELL; side='B' = bid = BUY.
        # Open of a long position is side=B; open of a short is side=A.
        # Close fills are the opposite side AND have dir 'Close ...'.
        open_fill = None
        close_fills = []
        for f in fills:
            if (f.get("coin") or "").upper() != coin:
                continue
            f_cloid = (f.get("cloid") or "").lower()
            raw = f.get("raw") or {}
            direction = raw.get("dir") or ""
            if f_cloid == cloid_norm:
                open_fill = f
                continue
            # Subsequent close: must be after open_ts AND a Close direction
            f_ts = float(f.get("ts", 0)) / 1000.0
            if f_ts < open_ts:
                continue
            if not direction.startswith("Close"):
                continue
            # Match position direction: long open → close has dir 'Close Long', etc.
            if is_long and "Long" not in direction:
                continue
            if (not is_long) and "Short" not in direction:
                continue
            close_fills.append(f)
        if not close_fills:
            return None  # not closed yet on HL, or no fills visible
        # Compute closed_pnl + fees using HL's authoritative numbers
        total_pnl = 0.0
        total_fee = 0.0
        total_qty = 0.0
        weighted_px = 0.0
        last_close_ts = 0.0
        for f in close_fills:
            raw = f.get("raw") or {}
            try:
                total_pnl += float(raw.get("closedPnl") or 0)
                total_fee += float(raw.get("fee") or 0)
                qty = float(raw.get("sz") or f.get("qty") or 0)
                px = float(raw.get("px") or f.get("price") or 0)
                total_qty += qty
                weighted_px += qty * px
                f_ts = float(f.get("ts", 0)) / 1000.0
                if f_ts > last_close_ts:
                    last_close_ts = f_ts
            except (TypeError, ValueError):
                continue
        if total_qty <= 0 or last_close_ts <= 0:
            return None
        close_px = weighted_px / total_qty
        # Open fee — best-effort from open_fill if matched
        open_fee = 0.0
        open_px = float(trade_row["open_px"] or 0)
        if open_fill is not None:
            raw_o = open_fill.get("raw") or {}
            try:
                open_fee = float(raw_o.get("fee") or 0)
                # Prefer HL's actual fill px over our ref_price
                hp = float(raw_o.get("px") or 0)
                if hp > 0:
                    open_px = hp
            except (TypeError, ValueError):
                pass
        fees_usd = open_fee + total_fee
        # Insert closures row. status='closed' is still set in trades for clarity;
        # reconciled_off_book stays as the canonical lifecycle terminus but the
        # closure is booked for attribution.
        self.conn.execute(
            "INSERT INTO closures(cloid,strategy,coin,is_long,open_ts,close_ts,"
            "open_px,close_px,size_coin,pnl_usd,fees_usd,close_reason,extras_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cloid, trade_row["strategy"], coin, int(is_long),
                open_ts, last_close_ts, open_px, close_px,
                total_qty, total_pnl, fees_usd, reason,
                json.dumps({"booked_from_fills": True,
                            "n_close_fills": len(close_fills)}),
            ),
        )
        return total_pnl - fees_usd

    def backfill_reconciled_closures(self, since_ts: float = 0.0) -> dict:
        """One-shot retroactive: for every row in trades with status in
        ('reconciled_off_book','force_closed_unverified','closed') that has
        NO matching closures row and open_ts >= since_ts, attempt to book
        a closure from HL fills.

        Returns {'scanned': N, 'booked': K, 'no_fills': M, 'errors': E}.
        """
        rows = self.conn.execute(
            "SELECT t.* FROM trades t "
            "LEFT JOIN closures c ON c.cloid = t.cloid "
            "WHERE t.status IN ('reconciled_off_book','force_closed_unverified','closed') "
            "  AND c.id IS NULL "
            "  AND t.open_ts >= ?",
            (since_ts,),
        ).fetchall()
        scanned = booked = no_fills = errors = 0
        for r in rows:
            scanned += 1
            try:
                pnl = self.book_closure_from_fills(r, reason="backfill")
                if pnl is not None:
                    booked += 1
                    log.warning("backfill: %s/%s booked pnl=$%+.4f from HL fills",
                                r["strategy"], r["coin"], pnl)
                else:
                    no_fills += 1
            except Exception:
                errors += 1
                log.exception("backfill failed cloid=%s", r["cloid"])
        log.warning("backfill_reconciled_closures: scanned=%d booked=%d no_fills=%d errors=%d",
                    scanned, booked, no_fills, errors)
        return {"scanned": scanned, "booked": booked, "no_fills": no_fills, "errors": errors}

    def force_close_stale(self, age_multiplier: float = 3.0) -> int:
        """Local-only force-close for trades stuck 'open' way past their
        max_hold_bars × tf. Defends against the failure mode where HL close
        keeps erroring (size mismatch, position gone, rate limit) and
        position_loop's retry budget is exhausted but the trade row stays
        'open' indefinitely, holding the coin lock.

        HL CHECK FIRST (sentinel council H3 fix, 2026-05-19):
        Before marking closed locally, attempt to verify the HL state. If
        HL says no position exists OR HL close succeeds, mark closed cleanly.
        If HL is unreachable, fall back to 'force_closed_unverified' status
        (still releases the lock) AND halts the strategy so the operator
        must investigate before further trading.

        Returns # force-closed.
        """
        rows = self.conn.execute(
            "SELECT cloid, strategy, coin, is_long, open_ts, open_px, size_coin, "
            "max_hold_bars, extras_json FROM trades WHERE status='open'"
        ).fetchall()
        now = time.time()
        n = 0
        # One HL position snapshot per call (cheap) for the HL check
        try:
            hl_pos = self.bus.hl_positions()
            hl_by_coin = {
                (p.get("coin") or "").upper(): p for p in (hl_pos or [])
                if (p.get("coin") and float(p.get("size_coin", 0) or 0) != 0)
            }
            hl_reachable = True
        except Exception:
            log.exception("force_close_stale: bus.hl_positions failed")
            hl_by_coin = {}
            hl_reachable = False
        for r in rows:
            tf_secs = 3600
            try:
                ex = json.loads(r["extras_json"] or "{}")
                tf = (ex.get("extras", {}) or {}).get("tf") or ex.get("tf") or "1h"
                tf_secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                           "4h": 14400, "1d": 86400}.get(tf, 3600)
            except Exception:
                pass
            stale_at = r["open_ts"] + (r["max_hold_bars"] or 8) * tf_secs * age_multiplier
            if now < stale_at:
                continue
            coin = (r["coin"] or "").upper()
            # Get current mark for approximate close PnL
            try:
                m = self.bus.markprice(r["coin"])
                px = float(m.get("hl_mid") or m.get("binance_mid") or r["open_px"])
            except Exception:
                px = float(r["open_px"])  # 0-PnL fallback
            # HL state branch
            if not hl_reachable:
                # Last resort: mark force_closed_unverified + halt strategy
                self.conn.execute(
                    "UPDATE trades SET status='force_closed_unverified', "
                    "extras_json=json_set(COALESCE(extras_json,'{}'),"
                    "  '$.close_reason','stale_force_close_hl_unreachable',"
                    "  '$.close_px',?, '$.close_ts',?) "
                    "WHERE cloid=?",
                    (px, now, r["cloid"]),
                )
                # halt strategy — operator must reconcile
                try:
                    _halt.set_halt(self.conn, r["strategy"], halted=True, actor="force_close_stale", reason="force_close_unverified_hl_unreachable")
                except Exception:
                    log.exception("set_halt failed")
                n += 1
                log.error("force_close_stale: %s/%s cloid=%s HL UNREACHABLE — "
                          "force_closed_unverified + strategy HALTED",
                          r["strategy"], coin, r["cloid"])
                continue
            hl_p = hl_by_coin.get(coin)
            if hl_p is None:
                # HL has no position: clean local close
                self.conn.execute(
                    "UPDATE trades SET status='closed', "
                    "extras_json=json_set(COALESCE(extras_json,'{}'),"
                    "  '$.close_reason','stale_force_close_hl_absent',"
                    "  '$.close_px',?, '$.close_ts',?) "
                    "WHERE cloid=?",
                    (px, now, r["cloid"]),
                )
                n += 1
                log.warning("force_close_stale: %s/%s cloid=%s closed locally "
                            "(HL has no position; safe)",
                            r["strategy"], coin, r["cloid"])
                continue
            # HL position exists. Attempt actual close.
            if self.hl is None:
                # No HL client configured (paper mode or boot error). Mark
                # closed locally with a clear flag.
                self.conn.execute(
                    "UPDATE trades SET status='closed', "
                    "extras_json=json_set(COALESCE(extras_json,'{}'),"
                    "  '$.close_reason','stale_force_close_paper',"
                    "  '$.close_px',?, '$.close_ts',?) "
                    "WHERE cloid=?",
                    (px, now, r["cloid"]),
                )
                n += 1
                continue
            try:
                size_coin = float(r["size_coin"])
                res = self.hl.market_close(coin=coin, size_coin=size_coin,
                                            cloid=r["cloid"])
                ok = bool(res and getattr(res, "ok", False))
            except Exception:
                log.exception("force_close_stale: hl.market_close raised")
                ok = False
            if ok:
                self.conn.execute(
                    "UPDATE trades SET status='closed', "
                    "extras_json=json_set(COALESCE(extras_json,'{}'),"
                    "  '$.close_reason','stale_force_close_hl_ok',"
                    "  '$.close_px',?, '$.close_ts',?) "
                    "WHERE cloid=?",
                    (px, now, r["cloid"]),
                )
                n += 1
                log.warning("force_close_stale: %s/%s cloid=%s closed via HL",
                            r["strategy"], coin, r["cloid"])
            else:
                # HL close failed AND position exists on HL. Last resort:
                # mark unverified + halt strategy.
                self.conn.execute(
                    "UPDATE trades SET status='force_closed_unverified', "
                    "extras_json=json_set(COALESCE(extras_json,'{}'),"
                    "  '$.close_reason','stale_force_close_hl_refused',"
                    "  '$.close_px',?, '$.close_ts',?) "
                    "WHERE cloid=?",
                    (px, now, r["cloid"]),
                )
                try:
                    _halt.set_halt(self.conn, r["strategy"], halted=True, actor="force_close_stale", reason="force_close_unverified_hl_refused")
                except Exception:
                    log.exception("set_halt failed")
                n += 1
                log.error("force_close_stale: %s/%s cloid=%s HL close REFUSED — "
                          "force_closed_unverified + strategy HALTED. "
                          "Manual operator action required.",
                          r["strategy"], coin, r["cloid"])
        return n

    def position_loop_once(self, registry=None) -> int:
        """Scan every open trade and close those that hit SL/TP/timeout, OR
        whose strategy.should_close() returns True (e.g. Donchian's
        trail-exit). Returns # closed.

        `registry` is an optional list of StrategyBase classes (used for
        strategy-driven close checks). If None, only SL/TP/timeout is used.
        """
        rows = self.conn.execute(
            "SELECT cloid,strategy,coin,is_long,open_ts,open_px,size_coin,sl_px,tp_px,max_hold_bars,extras_json "
            "FROM trades WHERE status='open'"
        ).fetchall()
        closed = 0
        now = time.time()
        strat_by_name = {s.NAME: s for s in (registry or [])}
        # Refuse to evaluate SL/TP against marks older than this many seconds.
        # On a thin WS reconnect or signal-bus outage, get_mark returns the
        # last cached tick; without a freshness guard the bot would close on
        # arbitrarily-stale prices. Brackets remain the primary stop.
        max_mark_age_sec = config.get_int("MAX_MARK_AGE_SEC", 30)
        for r in rows:
            coin = r["coin"]
            try:
                m = self.bus.markprice(coin)
            except Exception:
                continue
            px = (m.get("hl_mid") or m.get("binance_mid"))
            if not px:
                continue
            # WS freshness — mark ts is in milliseconds from signal-bus.
            mark_ts_ms = m.get("ts") or 0
            try:
                age_sec = (time.time() * 1000 - float(mark_ts_ms)) / 1000.0
            except (TypeError, ValueError):
                age_sec = None
            if age_sec is None or age_sec > max_mark_age_sec:
                log.warning("stale mark for %s (age=%.1fs > %ds) — skipping SL/TP eval",
                            coin, age_sec if age_sec is not None else -1, max_mark_age_sec)
                continue
            px = float(px)
            is_long = bool(r["is_long"])
            hit_tp = (is_long and px >= r["tp_px"]) or (not is_long and px <= r["tp_px"])
            hit_sl = (is_long and px <= r["sl_px"]) or (not is_long and px >= r["sl_px"])
            # Timeout = max_hold_bars × seconds-per-bar (TF-aware).
            # Bug fix 2026-05-17: prior code used max_hold_bars * 3600 unconditionally,
            # which is correct only for 1h strategies. Daily engines holding "5 bars"
            # should mean 5 DAYS not 5 HOURS. Read tf from extras_json and convert.
            tf_secs = 3600  # default 1h
            try:
                ex = json.loads(r["extras_json"] or "{}")
                tf = (ex.get("extras", {}) or {}).get("tf") or ex.get("tf") or "1h"
                tf_secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                           "4h": 14400, "1d": 86400}.get(tf, 3600)
            except Exception:
                pass
            timed_out = (now - r["open_ts"]) > r["max_hold_bars"] * tf_secs

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
        # Concurrency: claim the row by transitioning open → closing in one
        # SQL UPDATE. If rowcount is 0, another path (operator force_close,
        # a sibling position_loop tick) has already claimed it. Return
        # silently rather than double-firing market_close on HL.
        cur = self.conn.execute(
            "UPDATE trades SET status='closing' WHERE cloid=? AND status='open'",
            (cloid,),
        )
        if cur.rowcount == 0:
            return
        coin = trade_row["coin"]
        strategy = trade_row["strategy"]
        is_long = bool(trade_row["is_long"])
        size_coin = float(trade_row["size_coin"])
        open_px = float(trade_row["open_px"])
        live = self.hl is not None and self._is_live(strategy)

        # Read bracket cloids stashed at entry time so we can cancel any
        # surviving trigger order after we close the position ourselves.
        # This is the council-mandated cleanup path for the orphan-trigger
        # race condition.
        tp_cloid_orphan: Optional[str] = None
        sl_cloid_orphan: Optional[str] = None
        try:
            extras = json.loads(trade_row["extras_json"] or "{}")
            tp_cloid_orphan = extras.get("tp_cloid")
            sl_cloid_orphan = extras.get("sl_cloid")
        except Exception:
            pass

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
                # Release the 'closing' claim so the next tick can retry,
                # and bump retry counter atomically.
                self.conn.execute(
                    "UPDATE trades SET status='open', "
                    "close_retries = close_retries + 1 WHERE cloid=?",
                    (cloid,),
                )
                cur = self.conn.execute(
                    "SELECT close_retries FROM trades WHERE cloid=?", (cloid,)
                ).fetchone()
                retries = int(cur["close_retries"]) if cur else 0
                log.error("hl close FAILED (retry %d/%d) %s/%s reason=%s err=%s",
                          retries, self.MAX_CLOSE_RETRIES, strategy, coin, reason, err)
                if retries >= self.MAX_CLOSE_RETRIES:
                    # Halt the strategy. Must go through halt.set_halt so the
                    # in-memory _HALTED set updates — raw INSERT only writes
                    # the row, leaving the next scan unaware until restart.
                    try:
                        _halt.set_halt(
                            self.conn, strategy, halted=True,
                            reason=f"close_failed cloid={cloid} after {retries} retries",
                            actor="trader",
                        )
                        log.critical("HALT %s — close failures (cloid=%s)", strategy, cloid)
                    except Exception:
                        log.exception("halt insert failed")
                return  # do NOT mark closed

            # Live close succeeded — cancel any surviving bracket triggers
            # so they don't fire spuriously on a future re-entry on same coin.
            # If either is already filled (which is fine — that's what closed
            # the position), the cancel will no-op or return an error we ignore.
            if reason != "tp" and tp_cloid_orphan:
                try:
                    self.hl.cancel_order(coin=coin, cloid=tp_cloid_orphan)
                except Exception:
                    log.exception("cancel TP orphan failed")
            if reason != "sl" and sl_cloid_orphan:
                try:
                    self.hl.cancel_order(coin=coin, cloid=sl_cloid_orphan)
                except Exception:
                    log.exception("cancel SL orphan failed")

        # PAPER (or live success): mark closed and write closure row
        # ─── Fee accounting (matches LIVE economics in paper mode) ───
        # HL standard taker fee: 0.045% per side. Maker: 0.015% per side.
        # If extras.maker_only_recommended is True, charge maker on entry, taker on exit
        # (since exits are unconditional market closes).
        TAKER_FEE_RATE = 0.00045
        MAKER_FEE_RATE = 0.00015
        try:
            extras_dict = json.loads(trade_row["extras_json"] or "{}") if trade_row["extras_json"] else {}
            inner_extras = extras_dict.get("extras", {}) if isinstance(extras_dict, dict) else {}
            maker_entry = bool(inner_extras.get("maker_only_recommended"))
        except (json.JSONDecodeError, KeyError, TypeError):
            maker_entry = False
        entry_fee_rate = MAKER_FEE_RATE if maker_entry else TAKER_FEE_RATE
        exit_fee_rate = TAKER_FEE_RATE   # closes are always market (TP/SL/timeout)
        entry_fee = abs(open_px * size_coin * entry_fee_rate)
        exit_fee = abs(close_px * size_coin * exit_fee_rate)
        total_fees = entry_fee + exit_fee
        gross_pnl = (close_px - open_px) * size_coin * (1 if is_long else -1)
        pnl = gross_pnl - total_fees
        self.conn.execute("UPDATE trades SET status='closed' WHERE cloid=?", (cloid,))
        self.conn.execute(
            "INSERT INTO closures(cloid,strategy,coin,is_long,open_ts,close_ts,open_px,close_px,size_coin,"
            "pnl_usd,fees_usd,close_reason,extras_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cloid, strategy, coin, int(is_long), float(trade_row["open_ts"]), ts,
             open_px, close_px, size_coin, pnl, total_fees, reason,
             json.dumps({"gross_pnl": round(gross_pnl, 4),
                          "entry_fee_rate": entry_fee_rate,
                          "maker_entry": maker_entry})),
        )
        log.info("closed %s/%s %s pnl=%.4f gross=%.4f fees=%.4f maker_entry=%s",
                 strategy, coin, reason, pnl, gross_pnl, total_fees, maker_entry)
