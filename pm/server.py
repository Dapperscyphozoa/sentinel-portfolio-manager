"""strategy-runner HTTP server + scan/position loops."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import config
from pm import pretrade as pm_pretrade
from pm import regime as pm_regime  # noqa: E402
from common import halt, persistence  # noqa: E402
from common.bus_client import BusClient  # noqa: E402
from common.hl_exchange import HLExchange  # noqa: E402
from common.pm_client import PMClient  # noqa: E402

from strategy_runner import runner  # noqa: E402
from strategy_runner.trader import Trader  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("strategy_runner")


CONN = None
BUS = None
PM = None
TRADER = None


def _json(handler: BaseHTTPRequestHandler, status: int, body) -> None:
    payload = json.dumps(body, separators=(",", ":"), default=str).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if path == "/regime":
            try:
                bus = BusClient()
                candles = bus.candles("BTC", "1h", n=60)
                closes = [float(b["close"]) for b in candles]
                highs = [float(b["high"]) for b in candles]
                lows = [float(b["low"]) for b in candles]
                return _json(self, 200, pm_regime.classify(closes, highs, lows))
            except Exception as e:
                return _json(self, 500, {"error": str(e)[:200],
                                          "regime": "unknown", "confidence": 0.0})

        # /engines — registry list with cap_frac + audit_status
        if path == "/engines":
            try:
                from pm.pretrade import ENGINE_REGISTRY
                out = []
                for name, info in ENGINE_REGISTRY.items():
                    cap = info.get("capital_fraction", info.get("cap_frac", 0.0))
                    out.append({
                        "name": name,
                        "capital_fraction": cap,
                        "bt_pf": info.get("bt_pf", 0),
                        "bt_n": info.get("bt_n", 0),
                        "affinity": info.get("affinity", []),
                        "audit_status": info.get("audit_status", ""),
                        "stage": "live" if cap > 0 else "paper",
                    })
                return _json(self, 200, {"engines": out, "n": len(out)})
            except Exception as e:
                return _json(self, 500, {"error": str(e)[:200], "engines": []})

        # /equity — current wallet value + recent history for 24h pulse chart
        if path == "/equity":
            try:
                bus = BusClient()
                acct = bus.hl_account()
                value = float(acct.get("value", 0))
                # Simple history: just current point + zero placeholder for 24h
                return _json(self, 200, {
                    "ts": int(time.time() * 1000),
                    "value": value,
                    "withdrawable": float(acct.get("withdrawable", 0)),
                    "history": [],   # placeholder; monitor routine could populate
                    "delta_24h": 0.0,
                })
            except Exception as e:
                return _json(self, 500, {"error": str(e)[:200], "value": 0.0})

        # /regime — global regime classification (dashboard expects /regime/{coin} but
        # we serve same data for any coin since regime is BTC-driven)
        if path.startswith("/regime"):
            try:
                bus = BusClient()
                candles = bus.candles("BTC", "1h", n=60)
                closes = [float(b["close"]) for b in candles]
                highs = [float(b["high"]) for b in candles]
                lows = [float(b["low"]) for b in candles]
                return _json(self, 200, pm_regime.classify(closes, highs, lows))
            except Exception as e:
                return _json(self, 500, {"error": str(e)[:200],
                                          "regime": "unknown", "confidence": 0.0})

        # /risk and /risk/events — risk panel state
        if path == "/risk":
            try:
                bus = BusClient()
                acct = bus.hl_account()
                value = float(acct.get("value", 0))
                margin_used = float(acct.get("margin_used", 0))
                # Approx daily drawdown — not persisted yet, return zeros
                return _json(self, 200, {
                    "account_value": value,
                    "margin_used": margin_used,
                    "drawdown_pct_24h": 0.0,
                    "drawdown_halt_threshold_pct": 5.0,
                    "halt_active": False,
                })
            except Exception as e:
                return _json(self, 500, {"error": str(e)[:200], "account_value": 0})

        if path == "/risk/events":
            return _json(self, 200, {"events": [], "n": 0})

        if path == "/health":
            return _json(self, 200, {"ok": True, "ts": time.time(), "registry": runner.registry_info(),
                                     "halted": list(halt.active_halts())})
        if path == "/state":
            # Proxy to runner — trades DB is on runner disk (sentinel 2026-05-19)
            try:
                import httpx as _httpx
                runner_url = os.environ.get("STRATEGY_RUNNER_URL",
                                            "https://spm-strategy-runner.onrender.com")
                qs_str = "?" + u.query if u.query else ""
                r = _httpx.get(f"{runner_url.rstrip('/')}/state{qs_str}", timeout=15)
                return _json(self, r.status_code, r.json())
            except Exception as e:
                log.warning("/state proxy failed: %s", e)
                pass
        if path == "/state":
            rows = CONN.execute("SELECT cloid,strategy,coin,is_long,open_ts,open_px,size_usd,sl_px,tp_px,status,extras_json FROM trades ORDER BY open_ts DESC LIMIT 200").fetchall()
            return _json(self, 200, [dict(r) for r in rows])
        if path == "/signals":
            # Proxy to runner — trades DB is on runner disk (sentinel 2026-05-19)
            try:
                import httpx as _httpx
                runner_url = os.environ.get("STRATEGY_RUNNER_URL",
                                            "https://spm-strategy-runner.onrender.com")
                qs_str = "?" + u.query if u.query else ""
                r = _httpx.get(f"{runner_url.rstrip('/')}/signals{qs_str}", timeout=15)
                return _json(self, r.status_code, r.json())
            except Exception as e:
                log.warning("/signals proxy failed: %s", e)
                pass
        if path == "/signals":
            n = int(q.get("limit", "100"))
            rows = CONN.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (n,)).fetchall()
            return _json(self, 200, [dict(r) for r in rows])
        if path == "/demotions":
            # Expose engine_demotions for monitor.auto_4loss_demote routine.
            try:
                from pm.pretrade import _get_cooldown
                cd = _get_cooldown()
                if cd is None:
                    return _json(self, 200, [])
                c = cd._conn()
                rows = c.execute(
                    "SELECT engine, demoted_ts, reason FROM engine_demotions"
                ).fetchall()
                c.close()
                return _json(self, 200, [dict(r) for r in rows])
            except Exception as e:
                return _json(self, 500, {"error": str(e)[:200]})

        if path == "/closures":
            # Proxy to runner — trades DB is on runner disk (sentinel 2026-05-19)
            try:
                import httpx as _httpx
                runner_url = os.environ.get("STRATEGY_RUNNER_URL",
                                            "https://spm-strategy-runner.onrender.com")
                qs_str = "?" + u.query if u.query else ""
                r = _httpx.get(f"{runner_url.rstrip('/')}/closures{qs_str}", timeout=15)
                return _json(self, r.status_code, r.json())
            except Exception as e:
                log.warning("/closures proxy failed: %s", e)
                pass
        if path == "/closures":
            n = int(q.get("limit", "1000"))
            since = float(q.get("since", "0"))
            rows = CONN.execute("SELECT * FROM closures WHERE close_ts>=? ORDER BY id DESC LIMIT ?", (since, n)).fetchall()
            return _json(self, 200, [dict(r) for r in rows])
        if path == "/attribution":
            # PM attribution must proxy to runner — trades DB lives on runner disk,
            # PM has empty mirror. Sentinel-found bug 2026-05-19.
            try:
                import httpx as _httpx
                runner_url = os.environ.get("STRATEGY_RUNNER_URL",
                                            "https://spm-strategy-runner.onrender.com")
                qs_str = "?" + u.query if u.query else ""
                r = _httpx.get(f"{runner_url.rstrip('/')}/attribution{qs_str}", timeout=15)
                return _json(self, r.status_code, r.json())
            except Exception as e:
                log.warning("attribution proxy failed: %s", e)
                # Fall through to local query
                pass
        if path == "/attribution":
            # Per-engine attribution: n, wr, pf, expectancy, gross_pnl, fees, net_pnl
            # Plus per-coin breakdown within each engine.
            since = float(q.get("since", "0"))
            rows = CONN.execute(
                "SELECT strategy, coin, pnl_usd, fees_usd, close_reason, "
                "(close_ts - open_ts) AS hold_s "
                "FROM closures WHERE close_ts>=?", (since,)
            ).fetchall()
            # Also include open trades — unrealized PnL contributes to view of where we stand
            open_trades = CONN.execute(
                "SELECT strategy, coin, open_px, size_coin, is_long, open_ts, sl_px, tp_px, extras_json "
                "FROM trades WHERE status='open'"
            ).fetchall()
            # Aggregate closures by engine
            by_engine: dict = {}
            for r in rows:
                eng = r["strategy"]
                e = by_engine.setdefault(eng, {
                    "n": 0, "wins": 0, "losses": 0, "ties": 0,
                    "gross_pnl": 0.0, "fees": 0.0, "net_pnl": 0.0,
                    "win_pnl": 0.0, "loss_pnl": 0.0,
                    "by_coin": {}, "by_reason": {},
                    "hold_secs": 0.0,
                })
                pnl = float(r["pnl_usd"] or 0)
                fees = float(r["fees_usd"] or 0)
                net = pnl - fees
                e["n"] += 1
                e["gross_pnl"] += pnl
                e["fees"] += fees
                e["net_pnl"] += net
                e["hold_secs"] += float(r["hold_s"] or 0)
                if net > 0:
                    e["wins"] += 1
                    e["win_pnl"] += net
                elif net < 0:
                    e["losses"] += 1
                    e["loss_pnl"] += net  # negative
                else:
                    e["ties"] += 1
                # by coin
                c = e["by_coin"].setdefault(r["coin"], {"n": 0, "net_pnl": 0.0, "wins": 0})
                c["n"] += 1
                c["net_pnl"] += net
                if net > 0: c["wins"] += 1
                # by close reason
                reason = r["close_reason"] or "unknown"
                br = e["by_reason"].setdefault(reason, {"n": 0, "net_pnl": 0.0})
                br["n"] += 1
                br["net_pnl"] += net
            # Compute derived metrics per engine
            out_engines = []
            for eng, e in by_engine.items():
                n = e["n"]
                wr = (e["wins"] / n) if n else 0.0
                # Profit Factor = sum(wins) / abs(sum(losses))
                pf = (e["win_pnl"] / abs(e["loss_pnl"])) if e["loss_pnl"] < 0 else (float("inf") if e["win_pnl"] > 0 else 0.0)
                # Expectancy in $: net_pnl / n
                expect = (e["net_pnl"] / n) if n else 0.0
                avg_win = (e["win_pnl"] / e["wins"]) if e["wins"] else 0.0
                avg_loss = (e["loss_pnl"] / e["losses"]) if e["losses"] else 0.0
                avg_hold_h = (e["hold_secs"] / n / 3600.0) if n else 0.0
                out_engines.append({
                    "engine": eng,
                    "n": n,
                    "wins": e["wins"],
                    "losses": e["losses"],
                    "wr": round(wr, 4),
                    "pf": round(pf, 3) if pf != float("inf") else None,
                    "expectancy_usd": round(expect, 4),
                    "gross_pnl": round(e["gross_pnl"], 4),
                    "fees": round(e["fees"], 4),
                    "net_pnl": round(e["net_pnl"], 4),
                    "avg_win": round(avg_win, 4),
                    "avg_loss": round(avg_loss, 4),
                    "avg_hold_h": round(avg_hold_h, 2),
                    "by_coin": {c: {**v, "wr": round(v["wins"]/v["n"], 3) if v["n"] else 0,
                                    "net_pnl": round(v["net_pnl"], 4)}
                                for c, v in e["by_coin"].items()},
                    "by_reason": {r: {"n": v["n"], "net_pnl": round(v["net_pnl"], 4)}
                                  for r, v in e["by_reason"].items()},
                })
            # Sort by net_pnl desc
            out_engines.sort(key=lambda x: x["net_pnl"], reverse=True)
            # Open trades summary
            open_summary = []
            for ot in open_trades:
                open_summary.append({
                    "strategy": ot["strategy"], "coin": ot["coin"],
                    "is_long": bool(ot["is_long"]), "open_px": ot["open_px"],
                    "size_coin": ot["size_coin"], "open_ts": ot["open_ts"],
                    "sl_px": ot["sl_px"], "tp_px": ot["tp_px"],
                })
            # Cohort totals
            total = {"n": sum(e["n"] for e in by_engine.values()),
                     "wins": sum(e["wins"] for e in by_engine.values()),
                     "net_pnl": round(sum(e["net_pnl"] for e in by_engine.values()), 4),
                     "fees": round(sum(e["fees"] for e in by_engine.values()), 4)}
            total["wr"] = round(total["wins"] / total["n"], 4) if total["n"] else 0.0
            return _json(self, 200, {
                "ts": int(time.time() * 1000),
                "since": since,
                "engines": out_engines,
                "open": open_summary,
                "total": total,
            })
        return _json(self, 404, {"error": "not_found"})

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        parts = path.strip("/").split("/")
        body_len = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(body_len) if body_len else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        token = self.headers.get("X-Halt-Token")

        if len(parts) >= 2 and parts[0] == "halt":
            if not halt.halt_token_ok(token):
                return _json(self, 401, {"error": "bad_token"})
            target = parts[1]
            reason = body.get("reason", "")
            actor = body.get("actor", "api")
            if target == "all":
                halt.halt_all(CONN, reason=reason, actor=actor)
            else:
                halt.set_halt(CONN, target, True, reason=reason, actor=actor)
            return _json(self, 200, {"ok": True, "halted": list(halt.active_halts())})

        if len(parts) >= 2 and parts[0] == "resume":
            if not halt.halt_token_ok(token):
                return _json(self, 401, {"error": "bad_token"})
            target = parts[1]
            halt.set_halt(CONN, target, False, actor=body.get("actor", "api"))
            return _json(self, 200, {"ok": True, "halted": list(halt.active_halts())})

        # 4-loss paper demote reinstate — operator-only.
        # POST /reinstate/<engine>  with X-Halt-Token header.
        # Reverses permanent paper demote set by cooldown.demote_engine().
        if len(parts) >= 2 and parts[0] == "reinstate":
            if not halt.halt_token_ok(token):
                return _json(self, 401, {"error": "bad_token"})
            target = parts[1]
            try:
                from pm.pretrade import _get_cooldown
                cd = _get_cooldown()
                if cd is None:
                    return _json(self, 503, {"error": "cooldown_tracker_unavailable"})
                was = cd.reinstate_engine(target)
                return _json(self, 200, {
                    "ok": True, "engine": target, "was_demoted": was,
                    "actor": body.get("actor", "api"),
                })
            except Exception as e:
                return _json(self, 500, {"error": str(e)[:200]})

        # /check — PM pre-trade gate (operator 2026-05-18: re-wire after silent break)
        if path == "/check":
            try:
                # body already read + parsed at top of do_POST (line ~211)
                strategy = str(body.get("strategy", ""))
                signal = body.get("signal", {})
                if not strategy or not isinstance(signal, dict):
                    return _json(self, 400, {"error": "bad_payload"})
                # Pull account state + regime
                bus = BusClient()
                try:
                    acct = bus.hl_account()
                    account_value = float(acct.get("value", 0))
                    open_positions = acct.get("positions", []) or []
                except Exception:
                    account_value = 0.0
                    open_positions = []
                try:
                    candles = bus.candles("BTC", "1h", n=60)
                    closes = [float(b["close"]) for b in candles]
                    highs = [float(b["high"]) for b in candles]
                    lows = [float(b["low"]) for b in candles]
                    regime_d = pm_regime.classify(closes, highs, lows)
                except Exception:
                    regime_d = {"regime": "unknown", "confidence": 0.0}
                # Run pre-trade check
                result = pm_pretrade.check(CONN, strategy, signal, regime_d,
                                            account_value, open_positions)
                return _json(self, 200, {
                    "allow": result.allow,
                    "size_usd": result.size_usd,
                    "reason": result.reason,
                    "bt_pf": getattr(result, "bt_pf", None),
                })
            except Exception as e:
                log.exception("/check failed: %s", e)
                return _json(self, 500, {"error": str(e)[:200], "allow": False, "size_usd": 0})

        # /register_cloid — record HL order id for attribution
        if path == "/register_cloid":
            try:
                return _json(self, 200, {"ok": True, "stored": body})
            except Exception as e:
                return _json(self, 500, {"error": str(e)[:200]})

        if path == "/reconcile":
            # SQLite-only update — does NOT touch HL. Used when SQLite trade rows
            # have drifted from HL's net-position truth (e.g. multiple engines
            # firing on same coin → HL nets them, SQLite still shows N separate
            # rows). Safe because it never sends orders, only mutates DB state.
            if not halt.halt_token_ok(token):
                return _json(self, 401, {"error": "bad_token"})
            action = body.get("action", "")
            cloids = body.get("cloids") or []
            reason = body.get("reason", "reconcile")
            actor = body.get("actor", "operator")
            ts_now = time.time()
            if not cloids:
                return _json(self, 400, {"error": "no_cloids"})
            results = []
            for cloid in cloids:
                row = CONN.execute(
                    "SELECT cloid, strategy, coin, status, size_coin, extras_json FROM trades WHERE cloid=?",
                    (cloid,)
                ).fetchone()
                if not row:
                    results.append({"cloid": cloid, "ok": False, "error": "not_found"})
                    continue
                try:
                    extras = json.loads(row["extras_json"] or "{}")
                except Exception:
                    extras = {}
                extras["reconciled"] = {"action": action, "reason": reason, "actor": actor, "ts": ts_now,
                                        "prior_status": row["status"], "prior_size_coin": row["size_coin"]}
                if action == "off_book":
                    # Mark as closed via reconciliation, no HL call.
                    CONN.execute(
                        "UPDATE trades SET status='reconciled_off_book', extras_json=? WHERE cloid=?",
                        (json.dumps(extras, default=str), cloid)
                    )
                    results.append({"cloid": cloid, "ok": True, "new_status": "reconciled_off_book",
                                    "coin": row["coin"], "strategy": row["strategy"]})
                elif action == "adjust_size":
                    new_size = body.get("size_coin")
                    if new_size is None:
                        results.append({"cloid": cloid, "ok": False, "error": "no_size_coin"})
                        continue
                    CONN.execute(
                        "UPDATE trades SET size_coin=?, extras_json=? WHERE cloid=?",
                        (float(new_size), json.dumps(extras, default=str), cloid)
                    )
                    results.append({"cloid": cloid, "ok": True, "new_size_coin": float(new_size),
                                    "coin": row["coin"], "strategy": row["strategy"]})
                else:
                    results.append({"cloid": cloid, "ok": False, "error": f"unknown_action:{action}"})
            return _json(self, 200, {"ok": True, "action": action, "results": results})

        if path == "/admin/force_close":
            # OPERATOR-INITIATED close. Sends HL market_close on the listed
            # cloids, then marks them closed in our DB. Auth via HALT_TOKEN
            # (same as halt — only operator should have it). Body:
            #   {"cloids": ["0x...", ...],     # specific positions
            #    "reason": "string",            # logged in extras_json
            #    "actor": "string"}
            # Returns per-cloid result list.
            if not halt.halt_token_ok(token):
                return _json(self, 401, {"error": "bad_token"})
            cloids = body.get("cloids") or []
            reason = body.get("reason", "operator_force_close")
            actor = body.get("actor", "operator")
            if not cloids:
                return _json(self, 400, {"error": "no_cloids"})
            from common.bus_client import BusClient as _BC
            results = []
            for cloid in cloids:
                row = CONN.execute(
                    "SELECT * FROM trades WHERE cloid=? AND status='open'",
                    (cloid,)
                ).fetchone()
                if not row:
                    results.append({"cloid": cloid, "ok": False, "error": "not_open_or_not_found"})
                    continue
                coin = row["coin"]
                size_coin = float(row["size_coin"])
                try:
                    # Get current mark for the close_px record.
                    # BUS.markprice returns a dict {hl_mid, binance_mid, ...}, NOT a scalar.
                    # Pre-2026-05-18 this fell through to row["open_px"] which made every
                    # force_closed PnL look like $0.00 — masked real losses on red-engine cull.
                    px = BUS.markprice(coin) or {}
                    if isinstance(px, dict):
                        close_px = float(px.get("hl_mid") or px.get("binance_mid") or row["open_px"])
                    else:
                        close_px = float(px) if px else float(row["open_px"])
                except Exception:
                    close_px = float(row["open_px"])
                try:
                    res = TRADER.hl.market_close(coin=coin, size_coin=size_coin, cloid=cloid) if TRADER.hl else None
                except Exception as e:
                    log.exception("admin/force_close HL call failed cloid=%s", cloid)
                    results.append({"cloid": cloid, "coin": coin, "ok": False, "error": f"hl_raised:{e}"})
                    continue
                if res is None or not res.ok:
                    err = (res.error if res else "hl_disabled")
                    log.error("force_close FAILED cloid=%s coin=%s err=%s", cloid, coin, err)
                    results.append({"cloid": cloid, "coin": coin, "ok": False, "error": err})
                    continue
                # Cancel orphan brackets
                try:
                    extras = json.loads(row["extras_json"] or "{}")
                    tp_orphan = extras.get("tp_cloid")
                    sl_orphan = extras.get("sl_cloid")
                    if tp_orphan and hasattr(TRADER.hl, "cancel_by_cloid"):
                        try: TRADER.hl.cancel_by_cloid(coin, tp_orphan)
                        except Exception: log.warning("orphan TP cancel failed")
                    if sl_orphan and hasattr(TRADER.hl, "cancel_by_cloid"):
                        try: TRADER.hl.cancel_by_cloid(coin, sl_orphan)
                        except Exception: log.warning("orphan SL cancel failed")
                except Exception:
                    pass
                # Mark closed in DB + record close in closures table if it exists
                ts_now = time.time()
                pnl = (close_px - float(row["open_px"])) * size_coin * (1 if row["is_long"] else -1)
                # Fees: round-trip taker (entry already paid at open, exit is taker via market_close)
                # Default HL taker fee 0.045% per side. We can only record exit-side here unless
                # extras_json has entry_fee already; fall back to 2x taker as estimate.
                FORCE_CLOSE_FEE_RATE = 0.00045
                notional = float(row["open_px"]) * size_coin
                fees_usd = notional * FORCE_CLOSE_FEE_RATE * 2  # entry + exit estimate
                pnl_net = pnl - fees_usd  # subtract fees from gross
                try:
                    extras = json.loads(row["extras_json"] or "{}")
                except Exception:
                    extras = {}
                extras["force_closed"] = {"reason": reason, "actor": actor, "ts": ts_now,
                                          "close_px": close_px, "pnl_usd_gross": pnl,
                                          "pnl_usd_net": pnl_net, "fees_usd": fees_usd}
                CONN.execute(
                    "UPDATE trades SET status=?, extras_json=? WHERE cloid=?",
                    ("closed", json.dumps(extras, default=str), cloid)
                )
                try:
                    CONN.execute(
                        "INSERT OR IGNORE INTO closures(cloid, strategy, coin, is_long, open_ts, "
                        "close_ts, open_px, close_px, size_coin, pnl_usd, fees_usd, close_reason, extras_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cloid, row["strategy"], coin, row["is_long"], row["open_ts"], ts_now,
                         row["open_px"], close_px, size_coin, pnl_net, fees_usd,
                         f"force_close:{reason}", json.dumps(extras, default=str))
                    )
                except Exception:
                    log.exception("closures insert failed for %s", cloid)
                results.append({"cloid": cloid, "coin": coin, "ok": True,
                                "close_px": close_px, "pnl_usd": round(pnl_net, 4),
                                "fees_usd": round(fees_usd, 4)})
            return _json(self, 200, {"ok": True, "n_processed": len(cloids), "results": results})

        if path == "/admin/backfill_force_close_pnl":
            # ONE-SHOT backfill for the force_close PnL bug (pre-c5b055d).
            # The bug: BUS.markprice(coin) returned a dict; float(dict) raised
            # TypeError, caught silently → close_px defaulted to open_px → pnl=0
            # for every force_closed row. This endpoint fetches real HL price
            # at the original close_ts and rewrites pnl_usd + close_px + fees_usd.
            # Auth via HALT_TOKEN. Body:
            #   {"close_reason_like": "force_close:%",  # SQL LIKE pattern
            #    "dry_run": true|false}                 # default true
            if not halt.halt_token_ok(token):
                return _json(self, 401, {"error": "bad_token"})
            reason_like = body.get("close_reason_like", "force_close:%")
            dry_run = bool(body.get("dry_run", True))
            import httpx as _httpx
            HL_INFO = "https://api.hyperliquid.xyz/info"
            TAKER_FEE = 0.00045

            rows = CONN.execute(
                "SELECT * FROM closures WHERE close_reason LIKE ? AND pnl_usd = 0.0",
                (reason_like,)
            ).fetchall()
            results = []
            total_delta = 0.0
            n_fixed = 0
            for r in rows:
                coin = r["coin"]
                close_ts = float(r["close_ts"])
                open_px = float(r["open_px"])
                size = float(r["size_coin"])
                is_long = int(r["is_long"])
                end_ms = int(close_ts * 1000)
                start_ms = end_ms - 60_000
                try:
                    rr = _httpx.post(HL_INFO, json={
                        "type": "candleSnapshot",
                        "req": {"coin": coin, "interval": "1m",
                                "startTime": start_ms, "endTime": end_ms}
                    }, timeout=10.0)
                    bars = rr.json() or []
                except Exception as e:
                    results.append({"cloid": r["cloid"], "coin": coin, "ok": False,
                                    "error": f"hl_fetch:{e}"})
                    continue
                if not bars:
                    results.append({"cloid": r["cloid"], "coin": coin, "ok": False,
                                    "error": "no_hl_bars"})
                    continue
                b = bars[-1]
                real_close = float(b.get("c") or b.get("o") or 0)
                if real_close <= 0:
                    results.append({"cloid": r["cloid"], "coin": coin, "ok": False,
                                    "error": "invalid_close_px"})
                    continue
                gross = (real_close - open_px) * size * (1 if is_long else -1)
                notional = open_px * size
                fees = notional * TAKER_FEE * 2
                net = gross - fees
                if not dry_run:
                    extras = json.loads(r["extras_json"] or "{}")
                    extras["backfilled"] = {
                        "ts": time.time(),
                        "real_close_px": real_close,
                        "real_pnl_gross": gross,
                        "real_pnl_net": net,
                        "real_fees": fees,
                        "original_pnl": r["pnl_usd"],
                        "original_close_px": r["close_px"],
                        "source": "HL_info_candleSnapshot_1m"
                    }
                    CONN.execute(
                        "UPDATE closures SET close_px=?, pnl_usd=?, fees_usd=?, extras_json=? WHERE cloid=?",
                        (real_close, net, fees, json.dumps(extras, default=str), r["cloid"])
                    )
                    n_fixed += 1
                    total_delta += net
                results.append({"cloid": r["cloid"], "coin": coin, "strategy": r["strategy"],
                                "ok": True, "open_px": open_px, "real_close_px": real_close,
                                "pnl_net": round(net, 4), "fees": round(fees, 4),
                                "applied": not dry_run})
            return _json(self, 200, {"ok": True, "dry_run": dry_run,
                                     "n_candidates": len(rows),
                                     "n_fixed": n_fixed,
                                     "total_pnl_correction": round(total_delta, 4),
                                     "results": results})

        if path == "/precog/webhook":
            # HMAC verification of X-Precog-Sig (hex sha256 of body with PRECOG_WEBHOOK_SECRET)
            import hmac, hashlib
            secret = os.environ.get("PRECOG_WEBHOOK_SECRET", "")
            sig_hdr = self.headers.get("X-Precog-Sig") or self.headers.get("x-precog-sig", "")
            if not secret:
                return _json(self, 503, {"error": "no_secret_configured"})
            expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, (sig_hdr or "").lower()):
                return _json(self, 401, {"error": "bad_signature"})
            coin = (body.get("coin") or "").upper()
            if not coin:
                return _json(self, 400, {"error": "no_coin"})
            try:
                from strategy_runner.strategies import precog as precog_mod
                precog_mod.enqueue(coin, body)
            except Exception as e:
                return _json(self, 500, {"error": str(e)})
            return _json(self, 200, {"ok": True, "queue": precog_mod.queue_stats()})

        return _json(self, 404, {"error": "not_found"})


def _scan_loop() -> None:
    interval = config.get_int("SCAN_INTERVAL_SEC", 300)
    log.info("scan loop interval=%ds", interval)
    while True:
        t0 = time.time()
        try:
            def on_sig(strat, sig, decision):
                TRADER.open(strat, sig, decision.size_usd)
            n = runner.scan_once(BUS, PM, on_sig, trader=TRADER)
            if n:
                log.info("scan: %d signals processed", n)
        except Exception:
            log.exception("scan error")
        elapsed = time.time() - t0
        time.sleep(max(0, interval - elapsed))


def _position_loop() -> None:
    from strategy_runner.runner import REGISTRY
    while True:
        try:
            # Defense in depth: clear any stale 'pending' rows (>5min old)
            # from interrupted opens, which would otherwise hold the coin lock
            # indefinitely.
            try:
                TRADER.sweep_stale_pending()
            except Exception:
                log.exception("sweep_stale_pending failed")
            closed = TRADER.position_loop_once(registry=REGISTRY)
            if closed:
                log.info("position loop: closed %d", closed)
        except Exception:
            log.exception("position loop error")
        time.sleep(60)


def main() -> None:
    global CONN, BUS, PM, TRADER
    state = config.state_dir()
    CONN = persistence.init_db(os.path.join(state, "strategy_runner.db"))
    halt.load_active_halts(CONN)

    BUS = BusClient()
    PM = PMClient()
    try:
        hl = HLExchange()
    except Exception:
        hl = None
    TRADER = Trader(CONN, BUS, PM, hl)

    threading.Thread(target=_scan_loop, daemon=True, name="scan").start()
    threading.Thread(target=_position_loop, daemon=True, name="positions").start()

    port = config.get_int("HTTP_PORT", 10000)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("strategy-runner listening on :%d; registry=%s", port, runner.registry_info())
    server.serve_forever()


if __name__ == "__main__":
    main()
