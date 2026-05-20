"""poly-pm HTTP server.

Endpoints:
  GET  /health
  GET  /strategies                full registry with effective_capital_fraction
  GET  /recommendations           lifecycle eval (no side effects)
  POST /set_stage/<name>/<stage>  manual stage transition (admin only)
  GET  /pretrade_check?<query>    dry-run admit check
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from common.poly_persistence import init_poly_db
from poly_pm.lifecycle import apply_recommendation, evaluate_all
from poly_pm.pretrade_gate import check_admit
from poly_pm.registry import REGISTRY, effective_capital_fraction, is_live, set_stage


log = logging.getLogger("poly_pm")

HTTP_PORT = int(os.environ.get("HTTP_PORT", "10102"))
ADMIN_TOKEN = os.environ.get("HALT_TOKEN", "")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa
        if log.isEnabledFor(logging.DEBUG):
            log.debug(format % args)

    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        token = self.headers.get("X-Halt-Token") or self.headers.get("x-halt-token")
        return bool(ADMIN_TOKEN) and token == ADMIN_TOKEN

    def do_GET(self) -> None:  # noqa
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        if not parts:
            return self._send_json(200, {"service": "poly-pm"})

        if parts[0] == "health":
            return self._send_json(200, {"service": "poly-pm",
                                          "strategies": list(REGISTRY)})

        if parts[0] == "strategies":
            return self._send_json(200, {
                name: {
                    **info,
                    "effective_capital_fraction": effective_capital_fraction(name),
                    "is_live": is_live(name),
                }
                for name, info in REGISTRY.items()
            })

        if parts[0] == "recommendations":
            recs = [asdict(r) for r in evaluate_all()]
            return self._send_json(200, {"recommendations": recs, "n": len(recs)})

        if parts[0] == "pretrade_check":
            try:
                sig = {
                    "strategy": q.get("strategy", ""),
                    "size_usdc": float(q.get("size_usdc", "0")),
                    "pm_implied": float(q.get("pm_implied", "0.5")),
                }
                aum = float(q.get("aum_usdc", "1000"))
                ok, reason = check_admit(sig, aum)
                return self._send_json(200, {"ok": ok, "reason": reason})
            except Exception as e:
                return self._send_json(400, {"error": str(e)})

        self._send_json(404, {"error": "unknown path"})

    def do_POST(self) -> None:  # noqa
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]

        if len(parts) >= 3 and parts[0] == "set_stage":
            if not self._auth():
                return self._send_json(401, {"error": "bad halt token"})
            name = parts[1]; stage = parts[2]
            ok = set_stage(name, stage)
            if ok:
                return self._send_json(200, {"strategy": name, "stage": stage})
            return self._send_json(400, {"error": "set_stage failed"})

        if len(parts) >= 2 and parts[0] == "apply_recommendations":
            if not self._auth():
                return self._send_json(401, {"error": "bad halt token"})
            applied = []
            for r in evaluate_all():
                if r.auto_apply and apply_recommendation(r):
                    applied.append(asdict(r))
            return self._send_json(200, {"applied": applied, "n": len(applied)})

        self._send_json(404, {"error": "unknown path"})


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    init_poly_db()
    addr = ("0.0.0.0", HTTP_PORT)
    httpd = ThreadingHTTPServer(addr, Handler)
    log.info(f"poly-pm http listening on {addr}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
