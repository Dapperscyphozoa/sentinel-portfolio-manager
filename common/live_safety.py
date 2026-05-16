"""Phase 1 live-safety controls per council convergence.

Council-mandated tightening before live ICT deploy:
  - 0.25% RISK per trade (not 5% margin) — ATR-defined SL distance
  - 3x leverage (not 5x)
  - Circuit breaker: pause after 3 consec losses OR 10% DD in 7d
  - Position cap: 1 open at a time (override engine-level via MAX_CONCURRENT_LIVE=1)
  - Daily loss limit: 2% wallet → auto-halt for the day
  - Kill switch: PM_FORCE_KILL_ALL=1 env stops everything immediately

All controls toggleable via env. Defaults reflect council spec.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger("live_safety")


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass
class SafetyResult:
    allow: bool
    margin_usd: float
    reason: str
    risk_pct: float = 0.0
    leverage: float = 0.0


class LiveSafetyController:
    """Council-spec safety controls. SQLite-backed for survival across restarts."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or os.environ.get(
            "LIVE_SAFETY_DB", "/var/data/live_safety.sqlite"
        )
        self._init()

    def _conn(self) -> sqlite3.Connection:
        # WAL mode for concurrent writer safety (council fix)
        c = sqlite3.connect(self.db_path, timeout=10, isolation_level="DEFERRED")
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def _utc_day(self) -> str:
        """Return current UTC day as 'YYYY-MM-DD'. Council fix: enforce UTC."""
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _utc_day_start_ms(self) -> int:
        """Return UTC day start as ms epoch. Council fix: don't use time.mktime
        (which uses LOCAL timezone)."""
        # Get current UTC tuple, zero out hours/min/sec, convert to epoch via calendar
        import calendar
        tup = time.gmtime()
        midnight_utc = (tup.tm_year, tup.tm_mon, tup.tm_mday, 0, 0, 0, 0, 0, 0)
        return calendar.timegm(midnight_utc) * 1000

    @staticmethod
    def _validate_equity(equity: float) -> bool:
        """Sanity check: equity must be > 0 and < $10M (catches reporting bugs)."""
        return 0 < equity < 10_000_000

    def _init(self) -> None:
        c = self._conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS closes (
            ts INTEGER PRIMARY KEY, pnl REAL, equity_after REAL
        );
        CREATE TABLE IF NOT EXISTS daily_halts (
            day TEXT PRIMARY KEY, reason TEXT, halt_until_ts INTEGER
        );
        """)
        c.commit()
        c.close()

    # ─────────────────────── State queries ───────────────────────
    def consecutive_losses(self, n: int = 10) -> int:
        c = self._conn()
        rows = c.execute("SELECT pnl FROM closes ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        c.close()
        count = 0
        for r in rows:
            if r["pnl"] <= 0:
                count += 1
            else:
                break
        return count

    def drawdown_7d(self, current_equity: float) -> float:
        c = self._conn()
        since = int(time.time() * 1000) - 7 * 86400_000
        rows = c.execute(
            "SELECT equity_after FROM closes WHERE ts >= ?", (since,)
        ).fetchall()
        c.close()
        if not rows:
            return 0.0
        peak = max(max(r["equity_after"] for r in rows), current_equity)
        return max(0.0, (peak - current_equity) / max(peak, 1.0))

    def daily_pnl_pct(self, current_equity: float) -> float:
        """Return today's realized PnL as fraction of equity (negative if losing).
        Council fix: use UTC day boundary, not local."""
        if not self._validate_equity(current_equity):
            return 0.0
        day_start = self._utc_day_start_ms()
        c = self._conn()
        rows = c.execute(
            "SELECT pnl FROM closes WHERE ts >= ?", (day_start,)
        ).fetchall()
        c.close()
        total = sum(r["pnl"] for r in rows)
        return total / max(current_equity, 1.0)

    def is_daily_halted(self) -> tuple[bool, str]:
        today = self._utc_day()   # UTC enforced
        c = self._conn()
        row = c.execute(
            "SELECT reason, halt_until_ts FROM daily_halts WHERE day=?", (today,)
        ).fetchone()
        c.close()
        if row and row["halt_until_ts"] > int(time.time() * 1000):
            return True, row["reason"]
        return False, ""

    def set_daily_halt(self, reason: str) -> None:
        today = self._utc_day()
        tomorrow = int(time.time() * 1000) + 86400_000
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO daily_halts VALUES (?, ?, ?)",
            (today, reason, tomorrow),
        )
        c.commit()
        c.close()

    def record_close(self, pnl_usd: float, equity_after: float) -> None:
        """Council fix: validate equity before recording."""
        if not self._validate_equity(equity_after):
            log.warning("record_close skipped: implausible equity_after=%s", equity_after)
            return
        ts = int(time.time() * 1000)
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO closes VALUES (?, ?, ?)",
                  (ts, pnl_usd, equity_after))
        c.commit()
        c.close()

    # ─────────────────────── Main gate ───────────────────────
    def check(self, signal: dict, account_value: float,
              open_positions: list[dict]) -> SafetyResult:
        """Apply ALL council safety constraints. Returns SafetyResult."""
        # 0) Kill switch
        if os.environ.get("PM_FORCE_KILL_ALL", "0") == "1":
            return SafetyResult(False, 0.0, "kill_switch_active")

        # 1) Daily halt check
        halted, reason = self.is_daily_halted()
        if halted:
            return SafetyResult(False, 0.0, f"daily_halt:{reason}")

        # 2) Concurrent position cap (council: 1 at a time during phase 1)
        max_concurrent = _i("MAX_CONCURRENT_LIVE", 1)
        if len(open_positions) >= max_concurrent:
            return SafetyResult(False, 0.0, f"max_concurrent:{max_concurrent}")

        # 3) Consecutive-losses circuit breaker
        consec_limit = _i("CB_CONSEC_LOSSES", 3)
        consec = self.consecutive_losses(consec_limit + 1)
        if consec >= consec_limit:
            self.set_daily_halt(f"consec_losses={consec}")
            return SafetyResult(False, 0.0, f"cb_consec:{consec}")

        # 4) Drawdown 7d circuit breaker
        dd_limit = _f("CB_DD_7D_PCT", 0.10)
        dd = self.drawdown_7d(account_value)
        if dd >= dd_limit:
            self.set_daily_halt(f"dd_7d={dd:.1%}")
            return SafetyResult(False, 0.0, f"cb_dd_7d:{dd:.1%}")

        # 5) Daily loss limit
        daily_loss_limit = _f("DAILY_LOSS_LIMIT_PCT", 0.02)
        daily_pnl_pct = self.daily_pnl_pct(account_value)
        if daily_pnl_pct <= -daily_loss_limit:
            self.set_daily_halt(f"daily_loss={daily_pnl_pct:.1%}")
            return SafetyResult(False, 0.0, f"cb_daily_loss:{daily_pnl_pct:.1%}")

        # 6) Sizing — ATR-based RISK (not flat margin)
        risk_pct = _f("RISK_PCT_PER_TRADE", 0.0025)   # 0.25% council spec
        leverage = _f("LEVERAGE_LIVE", 3.0)            # 3x council spec
        max_margin_cap = _f("MAX_MARGIN_PCT", 0.03)    # 3% wallet max margin safety

        # Pull SL distance from signal (ICT stores extras.risk_pct_of_price or
        # we compute from sl_px / ref_price)
        entry = float(signal.get("ref_price", 0))
        sl = float(signal.get("sl_px", 0))
        if entry <= 0 or sl <= 0 or entry == sl:
            return SafetyResult(False, 0.0, "no_valid_sl")
        sl_distance_pct = abs(entry - sl) / entry
        if sl_distance_pct < 0.003:
            return SafetyResult(False, 0.0, "sl_too_tight")

        # position_notional = risk_usd / sl_distance_pct
        risk_usd = risk_pct * account_value
        notional = risk_usd / sl_distance_pct
        margin = notional / leverage
        margin = min(margin, max_margin_cap * account_value)

        min_trade = _f("MIN_TRADE_USD", 10.0)
        if notional < min_trade:
            return SafetyResult(False, 0.0, "size_below_min")

        return SafetyResult(True, round(margin, 2), "ok",
                           risk_pct=risk_pct, leverage=leverage)


# Singleton
_safety: Optional[LiveSafetyController] = None


def get_safety() -> LiveSafetyController:
    global _safety
    if _safety is None:
        _safety = LiveSafetyController()
    return _safety
