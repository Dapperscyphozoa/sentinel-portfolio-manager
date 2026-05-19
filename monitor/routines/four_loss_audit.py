"""four_loss_audit — informational audit on 4 consec clean losses.

Operator 2026-05-19: PF gate replaces the 4-loss demote action. The
sentinel-audit hook is preserved here as a passive early-warning. When an
engine accumulates 4 consecutive clean losses, this routine fires a Claude
Haiku code audit and writes a report to AUDIT_DIR — no env flip, no demote,
no auto-promotion. Engine fate is determined solely by the PF gate
(common/cooldown.py:MIN_TRADES_FOR_PF_CHECK / MIN_PF_RATIO).

Dedupes via a small SQLite table so each consec-loss streak fires at most
one audit per engine until the streak resets (a clean win zeroes the
counter).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Optional

import httpx


log = logging.getLogger("routine.four_loss_audit")


AUDIT_DIR = os.environ.get("AUDIT_DIR", "/var/data/audits")
PM_URL = os.environ.get("PM_URL", "https://spm-pm.onrender.com")
TRIGGER_THRESHOLD = 4


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS seen_4loss_alerts (
        engine TEXT PRIMARY KEY,
        last_alert_ts INTEGER NOT NULL,
        last_streak INTEGER NOT NULL,
        audit_path TEXT
    )
    """)
    conn.commit()


def _consec_loss_engines() -> list[dict]:
    """Read engine_consec_losses from PM. Returns engines with count >= 4."""
    pm_url = PM_URL.rstrip("/")
    try:
        r = httpx.get(f"{pm_url}/cooldown/engine_consec_losses", timeout=10)
        if r.status_code == 200:
            return [row for row in r.json() if int(row.get("count", 0)) >= TRIGGER_THRESHOLD]
    except Exception:
        pass
    return []


def _fire_sentinel_audit(engine: str, streak: int, conn: sqlite3.Connection) -> Optional[str]:
    """Run sentinel audit on the engine's strategy file. Return audit_path or None."""
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    if not gh_token:
        log.warning("GITHUB_TOKEN not set; skipping audit code fetch")
        return None
    OOS_ENGINES = {"e01", "e07", "e08", "e09", "e16", "e17"}
    prefix = engine.split("_")[0].lower()
    if prefix in OOS_ENGINES:
        filename = "oos_engines.py"
    else:
        filename = f"{engine.lower()}.py"
    url = (f"https://raw.githubusercontent.com/Dapperscyphozoa/sentinel-portfolio-manager/"
           f"main/strategy_runner/strategies/{filename}")
    try:
        r = httpx.get(url, headers={"Authorization": f"token {gh_token}"}, timeout=30)
        if r.status_code != 200:
            log.warning("source fetch %s -> %d", url, r.status_code)
            return None
        source = r.text
    except Exception:
        log.exception("source fetch failed")
        return None

    try:
        from monitor.claude_client import call as claude_call
        prompt = (
            f"You are auditing an engine that just hit {streak} consecutive "
            f"clean losses. The engine has NOT been demoted — this is an "
            f"informational audit only. Answer in <500 words: what is the "
            f"SINGLE most-likely failure mode that produced these losses, "
            f"and ONE concrete code or parameter change that would have "
            f"prevented them? Be terse and specific.\n\n"
            f"ENGINE: {engine}\nSTREAK: {streak}\n\nCODE:\n{source[:18000]}"
        )
        result = claude_call(
            conn, routine="four_loss_audit", prompt=prompt,
            max_tokens=800,
            daily_budget_usd=float(os.environ.get("DAILY_API_BUDGET_USD", "5")),
        )
    except Exception as e:
        log.exception("audit call failed")
        result = {"error": str(e)}

    os.makedirs(AUDIT_DIR, exist_ok=True)
    audit_path = os.path.join(AUDIT_DIR, f"{engine}_4loss_{int(time.time())}.md")
    try:
        with open(audit_path, "w") as f:
            f.write(f"# Four-loss audit: {engine}\n\n")
            f.write(f"ts: {time.ctime()}\n")
            f.write(f"streak: {streak}\n")
            f.write(f"action: AUDIT_ONLY (no demote — PF gate decides engine fate)\n\n")
            f.write(f"file: {filename}\n\n")
            f.write("## Sentinel finding\n\n")
            if isinstance(result, dict):
                f.write(f"```json\n{json.dumps(result, indent=2)}\n```\n")
            else:
                f.write(str(result))
        return audit_path
    except Exception:
        log.exception("audit write failed")
        return None


def run(conn: sqlite3.Connection) -> dict:
    """Monitor scheduler entrypoint. Runs every 5min."""
    _ensure_table(conn)
    now = int(time.time())
    new_audits: list[dict] = []
    skipped: list[dict] = []

    current = _consec_loss_engines()
    for row in current:
        engine = row.get("engine")
        streak = int(row.get("count", 0))
        if not engine:
            continue
        seen = conn.execute(
            "SELECT last_streak FROM seen_4loss_alerts WHERE engine=?", (engine,)
        ).fetchone()
        # Dedupe: fire once per streak. Re-fire only when streak grows past last alert.
        if seen and int(seen["last_streak"]) >= streak:
            skipped.append({"engine": engine, "streak": streak, "reason": "already_audited"})
            continue
        audit_path = _fire_sentinel_audit(engine, streak, conn)
        conn.execute("""
            INSERT OR REPLACE INTO seen_4loss_alerts (engine, last_alert_ts, last_streak, audit_path)
            VALUES (?, ?, ?, ?)
        """, (engine, now, streak, audit_path))
        conn.commit()
        new_audits.append({"engine": engine, "streak": streak,
                           "audit_path": audit_path, "action": "audit_only"})

    # Drop stale rows for engines whose streak has reset (no longer >=4)
    current_engines = {row.get("engine") for row in current}
    stale = conn.execute("SELECT engine FROM seen_4loss_alerts").fetchall()
    for r in stale:
        if r["engine"] not in current_engines:
            conn.execute("DELETE FROM seen_4loss_alerts WHERE engine=?", (r["engine"],))
    conn.commit()

    severity = "HIGH" if new_audits else "CLEAN"
    return {
        "ts": now,
        "severity": severity,
        "new_audits": new_audits,
        "skipped": skipped,
        "current_engines_at_threshold": list(current_engines),
    }
