"""Sniper risk controller — separate from live_safety, dedicated to sniper.

Per council spec (3/3 voters approved sniper path):
  - Max 1 sniper trade per day
  - Hard kill after 3 consecutive losses (50% wallet lost in worst case)
  - First 10 live trades require operator approval (SNIPER_REQUIRE_APPROVAL=1
    blocks fire until row inserted in approvals table)
  - Position size: 50% of wallet per event (high conviction, low frequency)

State persisted to SQLite (WAL mode). Survives restarts.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("sniper_risk")


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass
class RiskResult:
    allow: bool
    margin_usd: float
    reason: str


class SniperRiskController:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or os.environ.get(
            "SNIPER_RISK_DB", "/var/data/sniper_risk.sqlite"
        )
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def _init(self) -> None:
        c = self._conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sniper_trades (
            ts INTEGER PRIMARY KEY,
            coin TEXT NOT NULL,
            margin_usd REAL,
            pnl_usd REAL,
            equity_after REAL,
            divergence_pct REAL,
            closed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sniper_approvals (
            ts INTEGER NOT NULL,
            coin TEXT NOT NULL,
            approved_by TEXT,
            PRIMARY KEY (ts, coin)
        );
        CREATE TABLE IF NOT EXISTS sniper_kill (
            id INTEGER PRIMARY KEY,
            killed INTEGER DEFAULT 0,
            kill_reason TEXT,
            kill_ts INTEGER
        );
        INSERT OR IGNORE INTO sniper_kill (id, killed) VALUES (1, 0);
        """)
        c.commit()
        c.close()

    def _utc_day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _utc_day_start_ms(self) -> int:
        import calendar
        tup = time.gmtime()
        return calendar.timegm((tup.tm_year, tup.tm_mon, tup.tm_mday, 0, 0, 0, 0, 0, 0)) * 1000

    def is_killed(self) -> tuple[bool, str]:
        c = self._conn()
        row = c.execute("SELECT killed, kill_reason FROM sniper_kill WHERE id=1").fetchone()
        c.close()
        if row and row["killed"]:
            return True, row["kill_reason"] or "unknown"
        return False, ""

    def set_killed(self, reason: str) -> None:
        c = self._conn()
        c.execute(
            "UPDATE sniper_kill SET killed=1, kill_reason=?, kill_ts=? WHERE id=1",
            (reason, int(time.time() * 1000)),
        )
        c.commit()
        c.close()
        log.warning("SNIPER KILLED: %s", reason)

    def reset_kill(self) -> None:
        """Manual reset — operator-only after investigation."""
        c = self._conn()
        c.execute("UPDATE sniper_kill SET killed=0, kill_reason=NULL, kill_ts=NULL WHERE id=1")
        c.commit()
        c.close()
        log.info("sniper kill reset (operator override)")

    def trades_today(self) -> int:
        day_start = self._utc_day_start_ms()
        c = self._conn()
        row = c.execute(
            "SELECT COUNT(*) AS n FROM sniper_trades WHERE ts >= ?", (day_start,)
        ).fetchone()
        c.close()
        return row["n"] if row else 0

    def consecutive_losses(self, limit: int = 10) -> int:
        c = self._conn()
        rows = c.execute(
            "SELECT pnl_usd FROM sniper_trades WHERE closed=1 ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        c.close()
        count = 0
        for r in rows:
            if r["pnl_usd"] is not None and r["pnl_usd"] <= 0:
                count += 1
            else:
                break
        return count

    def total_live_trades(self) -> int:
        c = self._conn()
        row = c.execute("SELECT COUNT(*) AS n FROM sniper_trades").fetchone()
        c.close()
        return row["n"] if row else 0

    def requires_approval(self) -> bool:
        if os.environ.get("SNIPER_REQUIRE_APPROVAL", "1") != "1":
            return False
        # First 10 live trades require approval per council spec
        threshold = _i("SNIPER_APPROVAL_TRADES", 10)
        return self.total_live_trades() < threshold

    def has_approval_for(self, coin: str, within_ms: int = 600_000) -> bool:
        """Approval valid within last `within_ms` (default 10 min)."""
        since = int(time.time() * 1000) - within_ms
        c = self._conn()
        row = c.execute(
            "SELECT ts FROM sniper_approvals WHERE coin=? AND ts >= ? ORDER BY ts DESC LIMIT 1",
            (coin, since),
        ).fetchone()
        c.close()
        return row is not None

    def grant_approval(self, coin: str, by: str = "operator") -> None:
        c = self._conn()
        c.execute(
            "INSERT OR IGNORE INTO sniper_approvals (ts, coin, approved_by) VALUES (?, ?, ?)",
            (int(time.time() * 1000), coin, by),
        )
        c.commit()
        c.close()

    def record_trade(self, coin: str, margin_usd: float,
                     divergence_pct: float) -> None:
        c = self._conn()
        c.execute(
            "INSERT INTO sniper_trades (ts, coin, margin_usd, divergence_pct) VALUES (?, ?, ?, ?)",
            (int(time.time() * 1000), coin, margin_usd, divergence_pct),
        )
        c.commit()
        c.close()

    def record_close(self, coin: str, pnl_usd: float, equity_after: float) -> None:
        c = self._conn()
        # Update last open trade for this coin
        row = c.execute(
            "SELECT ts FROM sniper_trades WHERE coin=? AND closed=0 ORDER BY ts DESC LIMIT 1",
            (coin,),
        ).fetchone()
        if row:
            c.execute(
                "UPDATE sniper_trades SET pnl_usd=?, equity_after=?, closed=1 WHERE ts=?",
                (pnl_usd, equity_after, row["ts"]),
            )
            c.commit()
        c.close()
        # Check 3-strike kill
        if self.consecutive_losses(limit=10) >= _i("SNIPER_CB_CONSEC_LOSSES", 3):
            self.set_killed(f"3_consec_losses_after_{coin}")

    def check(self, coin: str, account_value: float,
              divergence_pct: float) -> RiskResult:
        # Kill switches
        if os.environ.get("SNIPER_FORCE_KILL", "0") == "1":
            return RiskResult(False, 0.0, "sniper_force_kill_env")
        killed, kreason = self.is_killed()
        if killed:
            return RiskResult(False, 0.0, f"sniper_killed:{kreason}")
        # Daily cap (default 1/day per council)
        daily_max = _i("SNIPER_MAX_PER_DAY", 1)
        if self.trades_today() >= daily_max:
            return RiskResult(False, 0.0, f"daily_cap:{daily_max}")
        # Operator approval gate (first N live trades)
        if self.requires_approval() and not self.has_approval_for(coin):
            return RiskResult(False, 0.0, "needs_operator_approval")
        # Account sanity
        if account_value < _f("SNIPER_MIN_ACCOUNT_USD", 50.0):
            return RiskResult(False, 0.0, "account_too_small")
        # Sizing — council: 50% per event for first 5, scale if WR > 60%
        size_pct = _f("SNIPER_SIZE_PCT", 0.50)
        # Scale down if early trades (more conservative on first few)
        n_total = self.total_live_trades()
        if n_total < 5:
            size_pct = min(size_pct, 0.25)   # half-size for first 5 trades
        margin = size_pct * account_value
        if margin < _f("SNIPER_MIN_TRADE_USD", 10.0):
            return RiskResult(False, 0.0, "size_below_min")

        return RiskResult(True, round(margin, 2), "ok")


_sniper_risk: Optional[SniperRiskController] = None


def get_risk() -> SniperRiskController:
    global _sniper_risk
    if _sniper_risk is None:
        _sniper_risk = SniperRiskController()
    return _sniper_risk
