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

from common import config, halt, persistence  # noqa: E402
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
        if path == "/health":
            return _json(self, 200, {"ok": True, "ts": time.time(), "registry": runner.registry_info(),
                                     "halted": list(halt.active_halts())})
        if path == "/state":
            rows = CONN.execute("SELECT cloid,strategy,coin,is_long,open_ts,open_px,size_usd,sl_px,tp_px,status,extras_json FROM trades ORDER BY open_ts DESC LIMIT 200").fetchall()
            return _json(self, 200, [dict(r) for r in rows])
        if path == "/signals":
            n = int(q.get("limit", "100"))
            rows = CONN.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (n,)).fetchall()
            return _json(self, 200, [dict(r) for r in rows])
        if path == "/closures":
            n = int(q.get("limit", "1000"))
            since = float(q.get("since", "0"))
            rows = CONN.execute("SELECT * FROM closures WHERE close_ts>=? ORDER BY id DESC LIMIT ?", (since, n)).fetchall()
            return _json(self, 200, [dict(r) for r in rows])
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
    from .runner import REGISTRY
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
