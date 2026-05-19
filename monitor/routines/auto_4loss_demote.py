"""auto_4loss_demote — handle the operator 2026-05-18 workflow:

  4 consec losses on engine  →  engine_demotions row written by cooldown.py
                              →  THIS routine detects new row
                              →  fires sentinel audit on engine code
                              →  flips STRATEGY_<NAME>_LIVE=0 via Render API
                              →  posts /strategy/halt/<name> for in-memory halt
                              →  saves audit report

  Engine in demoted state runs in paper mode (existing _LIVE=0 path).

  Every cycle, for each demoted engine:
    count last 4 paper closures (extras.live=false) for this engine
    if all 4 PnL > 0:
      flip STRATEGY_<NAME>_LIVE=1 (auto-promote)
      POST /reinstate/<engine> to clear cooldown demotion
      save promotion event

Severity:
  CRITICAL on first detection of a demotion (new event)
  HIGH on auto-promotion
  CLEAN otherwise
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Optional

import httpx


log = logging.getLogger("routine.auto_4loss_demote")


# Paths
COOLDOWN_DB = os.environ.get("COOLDOWN_DB", "/var/data/cooldowns.sqlite")
AUDIT_DIR = os.environ.get("AUDIT_DIR", "/var/data/audits")
SEEN_DEMOTIONS_TABLE = "seen_demotions"   # in monitor's own db
WIN_STREAK_FOR_PROMOTE = 4

# Render config (env)
RENDER_API_TOKEN = os.environ.get("RENDER_API_TOKEN", "")
RUNNER_SERVICE_ID = os.environ.get("STRATEGY_RUNNER_SERVICE_ID",
                                   "srv-d840lr99rddc7397jfgg")
PM_URL = os.environ.get("PM_URL", "https://spm-pm.onrender.com")
HALT_TOKEN = os.environ.get("HALT_TOKEN", "")


def _ensure_seen_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS seen_demotions (
        engine TEXT PRIMARY KEY,
        demoted_ts INTEGER NOT NULL,
        audit_fired INTEGER NOT NULL DEFAULT 0,
        env_flipped INTEGER NOT NULL DEFAULT 0,
        promoted_ts INTEGER,
        audit_path TEXT
    )
    """)
    conn.commit()


def _read_demotions() -> list[dict]:
    """Read current engine_demotions from PM cooldown DB.
    The DB lives on PM's disk; we read via PM API if no direct path.
    Fall back to local file if PM service shares /var/data with monitor."""
    # Strategy: PM exposes /demotions if reachable, else read local file
    pm_url = PM_URL.rstrip("/")
    try:
        r = httpx.get(f"{pm_url}/demotions", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    # Fallback: direct SQLite read (works only if monitor shares disk with PM)
    if not os.path.exists(COOLDOWN_DB):
        return []
    try:
        c = sqlite3.connect(COOLDOWN_DB)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT engine, demoted_ts, reason FROM engine_demotions"
        ).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("demotions read failed")
        return []


def _fire_sentinel_audit(engine: str, conn: sqlite3.Connection) -> Optional[str]:
    """Run sentinel council on the engine's strategy file. Return audit_path or None.

    Reads source via GitHub raw (no local repo dependency)."""
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    if not gh_token:
        log.warning("GITHUB_TOKEN not set; skipping sentinel audit code fetch")
        return None
    # Engine name maps to strategy file (lowercase + .py). For engines defined
    # in oos_engines.py (e01/e07/e08/e09/e16/e17 series), the file is oos_engines.py.
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

    # Call internal claude_client (uses Anthropic API budget).
    # Cheap haiku model for routine triage.
    try:
        from monitor.claude_client import call as claude_call
        prompt = (
            f"You are auditing an engine that just got auto-demoted after 4 "
            f"consecutive losses. The strategy code is below. Answer in <500 "
            f"words: what is the SINGLE most-likely failure mode that produced "
            f"the 4 losses, and ONE concrete code or parameter change that "
            f"would have prevented them? Be terse and specific.\n\n"
            f"ENGINE: {engine}\n\nCODE:\n{source[:18000]}"
        )
        result = claude_call(
            conn, routine="auto_4loss_demote_audit", prompt=prompt,
            max_tokens=800,
            daily_budget_usd=float(os.environ.get("DAILY_API_BUDGET_USD", "5")),
        )
    except Exception as e:
        log.exception("audit call failed")
        result = {"error": str(e)}

    os.makedirs(AUDIT_DIR, exist_ok=True)
    audit_path = os.path.join(AUDIT_DIR, f"{engine}_{int(time.time())}.md")
    try:
        with open(audit_path, "w") as f:
            f.write(f"# Auto-audit: {engine} (4-loss demote)\n\n")
            f.write(f"ts: {time.ctime()}\n\n")
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


def _flip_env(engine: str, value: str) -> bool:
    """Set STRATEGY_<NAME>_LIVE env via Render API on the runner service."""
    if not RENDER_API_TOKEN:
        log.warning("RENDER_API_TOKEN not set; skipping env flip")
        return False
    key = f"STRATEGY_{engine.upper()}_LIVE"
    try:
        # Get current env vars (paginated would be ideal; for now limit=200)
        r = httpx.get(
            f"https://api.render.com/v1/services/{RUNNER_SERVICE_ID}/env-vars?limit=200",
            headers={"Authorization": f"Bearer {RENDER_API_TOKEN}"},
            timeout=20,
        )
        if r.status_code != 200:
            return False
        env_vars = r.json()
        existing = next((ev.get("envVar", ev) for ev in env_vars
                         if ev.get("envVar", ev).get("key") == key), None)
        if existing is None:
            # Create
            cr = httpx.post(
                f"https://api.render.com/v1/services/{RUNNER_SERVICE_ID}/env-vars",
                headers={"Authorization": f"Bearer {RENDER_API_TOKEN}",
                         "Content-Type": "application/json"},
                json={"key": key, "value": value},
                timeout=20,
            )
            return cr.status_code in (200, 201)
        else:
            # Update via PUT to /env-vars (Render's update method varies; use replace-all approach)
            updated_list = []
            for ev in env_vars:
                ev_obj = ev.get("envVar", ev)
                if ev_obj.get("key") == key:
                    updated_list.append({"key": key, "value": value})
                elif "value" in ev_obj:
                    updated_list.append({"key": ev_obj["key"], "value": ev_obj["value"]})
            ur = httpx.put(
                f"https://api.render.com/v1/services/{RUNNER_SERVICE_ID}/env-vars",
                headers={"Authorization": f"Bearer {RENDER_API_TOKEN}",
                         "Content-Type": "application/json"},
                json=updated_list,
                timeout=20,
            )
            return ur.status_code == 200
    except Exception:
        log.exception("env flip failed")
        return False


def _count_paper_wins_streak(closures_conn: sqlite3.Connection, engine: str,
                             since_ts: float, n: int = 4) -> int:
    """Count consecutive CLEAN paper closures with PnL > 0 since `since_ts`.
    Operator force-closes and bug-recovery backfills are skipped (they don't
    reflect strategy edge)."""
    from common.closures import is_clean_closure
    rows = closures_conn.execute(
        "SELECT pnl_usd, extras_json, close_reason FROM closures "
        "WHERE strategy=? AND close_ts>=? "
        "ORDER BY close_ts DESC LIMIT ?",
        (engine, since_ts, n * 3),  # over-fetch to allow filtering
    ).fetchall()
    paper_rows = []
    for r in rows:
        if not is_clean_closure(r["close_reason"]):
            continue
        try:
            e = json.loads(r["extras_json"] or "{}")
            if isinstance(e, dict) and e.get("live") is False:
                paper_rows.append(r)
        except Exception:
            pass
        if len(paper_rows) >= n:
            break
    # rows are DESC, so paper_rows[0] is most recent paper trade
    if len(paper_rows) < n:
        return 0
    streak = 0
    for r in paper_rows[:n]:
        if r["pnl_usd"] > 0:
            streak += 1
        else:
            return 0
    return streak


def _reinstate(engine: str) -> bool:
    """POST /reinstate/<engine> to PM."""
    if not HALT_TOKEN:
        return False
    try:
        r = httpx.post(f"{PM_URL.rstrip('/')}/reinstate/{engine}",
                       headers={"X-Halt-Token": HALT_TOKEN},
                       timeout=15)
        return r.status_code == 200
    except Exception:
        log.exception("reinstate POST failed")
        return False


def run(conn: sqlite3.Connection) -> dict:
    """Main entry — monitor scheduler calls this every 5min."""
    _ensure_seen_table(conn)
    now = time.time()
    new_demotions = []
    promotions = []
    pending = []

    current = _read_demotions()
    current_engines = {d["engine"] for d in current}

    # Pre-fetch all closures (monitor has its own DB; PM has the live closures DB).
    # closures live in strategy_runner's db, queried via PM /closures endpoint.
    try:
        r = httpx.get(f"{PM_URL.rstrip('/')}/closures?limit=2000", timeout=20)
        closures_data = r.json() if r.status_code == 200 else []
    except Exception:
        closures_data = []

    def _closures_for(engine_name: str, since_ts: float, n: int) -> int:
        """Inline streak counter using closures_data list. Excludes
        operator-driven and bug-recovery closures via is_clean_closure."""
        from common.closures import is_clean_closure
        paper = []
        for row in closures_data:
            if row.get("strategy") != engine_name:
                continue
            if float(row.get("close_ts", 0)) < since_ts:
                continue
            if not is_clean_closure(row.get("close_reason")):
                continue
            try:
                e = json.loads(row.get("extras_json") or "{}")
                if not (isinstance(e, dict) and e.get("live") is False):
                    continue
            except Exception:
                continue
            paper.append(row)
        # Most recent first by close_ts desc
        paper.sort(key=lambda r: float(r.get("close_ts", 0)), reverse=True)
        if len(paper) < n:
            return 0
        for r in paper[:n]:
            if float(r.get("pnl_usd", 0)) <= 0:
                return 0
        return n

    # Process each currently-demoted engine
    for d in current:
        engine = d["engine"]
        demoted_ts = int(d.get("demoted_ts", 0))
        seen = conn.execute(
            "SELECT audit_fired, env_flipped, promoted_ts FROM seen_demotions WHERE engine=?",
            (engine,),
        ).fetchone()

        # First-time detection of this demotion
        if seen is None or int(seen["demoted_ts"]) != demoted_ts:
            log.error("NEW 4-LOSS DEMOTION detected: %s (demoted_ts=%s)", engine, demoted_ts)
            audit_path = _fire_sentinel_audit(engine, conn)
            env_ok = _flip_env(engine, "0")
            conn.execute("""
                INSERT OR REPLACE INTO seen_demotions
                (engine, demoted_ts, audit_fired, env_flipped, promoted_ts, audit_path)
                VALUES (?, ?, ?, ?, NULL, ?)
            """, (engine, demoted_ts, 1 if audit_path else 0, 1 if env_ok else 0, audit_path))
            conn.commit()
            new_demotions.append({"engine": engine, "audit_fired": bool(audit_path),
                                  "env_flipped": env_ok, "audit_path": audit_path})
            continue

        # Already-seen demoted engine — check for 4-paper-win promotion
        win_streak = _closures_for(engine, demoted_ts, WIN_STREAK_FOR_PROMOTE)
        pending.append({"engine": engine, "paper_win_streak": win_streak,
                        "need": WIN_STREAK_FOR_PROMOTE})
        if win_streak >= WIN_STREAK_FOR_PROMOTE:
            log.warning("AUTO-PROMOTE %s: %d consec paper wins", engine, win_streak)
            env_ok = _flip_env(engine, "1")
            reinstate_ok = _reinstate(engine)
            conn.execute(
                "UPDATE seen_demotions SET promoted_ts=? WHERE engine=?",
                (int(now), engine),
            )
            conn.commit()
            promotions.append({"engine": engine, "win_streak": win_streak,
                               "env_flipped": env_ok, "reinstate_ok": reinstate_ok})

    # Cleanup stale rows: engines no longer in cooldown.engine_demotions
    stale = conn.execute("SELECT engine FROM seen_demotions").fetchall()
    for row in stale:
        if row["engine"] not in current_engines:
            conn.execute("DELETE FROM seen_demotions WHERE engine=?", (row["engine"],))
    conn.commit()

    severity = "CRITICAL" if new_demotions else ("HIGH" if promotions else "CLEAN")
    return {
        "ts": now,
        "severity": severity,
        "new_demotions": new_demotions,
        "promotions": promotions,
        "pending": pending,
        "current_demoted_engines": list(current_engines),
    }
