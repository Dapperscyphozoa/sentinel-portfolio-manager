"""Engine cooldown tracker — per operator spec.

Rules:
  - 4 consecutive losses on same COIN → 1h coin cooldown (engine still runs other coins)
  - 6 consecutive losses on any ENGINE → 1h engine cooldown (all coins blocked)
  - Engine DD > 12% → 1h engine cooldown
  - Live PF < 0.74 × backtest PF after 22+ trades → 1h engine cooldown
  - Cooldowns are ROLLING (not permanent halts). Engines resume after 1h.

Persisted to /var/data/cooldowns.sqlite for survival across restarts.
"""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from typing import Optional


COOLDOWN_SECS = 3600   # 1h
CONSEC_LOSS_COIN = 4
CONSEC_LOSS_ENGINE = 6
MAX_DD_PCT = 0.12
MIN_TRADES_FOR_PF_CHECK = 22
MIN_PF_RATIO = 0.74    # live PF / backtest PF


class CooldownTracker:
    """SQLite-backed cooldown state.

    Tables:
      coin_cooldowns(engine TEXT, coin TEXT, until_ts INTEGER, reason TEXT)
      engine_cooldowns(engine TEXT, until_ts INTEGER, reason TEXT)
      consec_losses(engine TEXT, coin TEXT, count INTEGER, updated_ts INTEGER)
      engine_consec_losses(engine TEXT, count INTEGER, updated_ts INTEGER)
      engine_pnl(engine TEXT, ts INTEGER, pnl REAL)   -- for DD + live PF calc
    """

    def __init__(self, db_path: str = "/var/data/cooldowns.sqlite") -> None:
        self.db_path = db_path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        c = self._conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS coin_cooldowns (
            engine TEXT, coin TEXT, until_ts INTEGER, reason TEXT,
            PRIMARY KEY (engine, coin)
        );
        CREATE TABLE IF NOT EXISTS engine_cooldowns (
            engine TEXT PRIMARY KEY, until_ts INTEGER, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS consec_losses (
            engine TEXT, coin TEXT, count INTEGER, updated_ts INTEGER,
            PRIMARY KEY (engine, coin)
        );
        CREATE TABLE IF NOT EXISTS engine_consec_losses (
            engine TEXT PRIMARY KEY, count INTEGER, updated_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS engine_pnl (
            engine TEXT, ts INTEGER, pnl REAL,
            PRIMARY KEY (engine, ts)
        );
        """)
        c.commit()
        c.close()

    def is_coin_blocked(self, engine: str, coin: str, now_ts: Optional[int] = None) -> tuple[bool, str]:
        now = now_ts or int(time.time())
        c = self._conn()
        row = c.execute(
            "SELECT until_ts, reason FROM coin_cooldowns WHERE engine=? AND coin=?",
            (engine, coin),
        ).fetchone()
        c.close()
        if row and row["until_ts"] > now:
            return (True, f"coin_cooldown:{row['reason']}:{row['until_ts']}")
        return (False, "")

    def is_engine_blocked(self, engine: str, now_ts: Optional[int] = None) -> tuple[bool, str]:
        now = now_ts or int(time.time())
        c = self._conn()
        row = c.execute(
            "SELECT until_ts, reason FROM engine_cooldowns WHERE engine=?", (engine,)
        ).fetchone()
        c.close()
        if row and row["until_ts"] > now:
            return (True, f"engine_cooldown:{row['reason']}:{row['until_ts']}")
        return (False, "")

    def record_close(self, engine: str, coin: str, pnl_usd: float,
                     backtest_pf: float, now_ts: Optional[int] = None) -> dict:
        """Update consecutive loss counters, record PnL, check thresholds.

        Returns dict with 'triggered_cooldowns' list."""
        now = now_ts or int(time.time())
        triggered = []
        is_loss = pnl_usd <= 0
        c = self._conn()
        c.isolation_level = None  # autocommit mode

        # Update per-coin consecutive losses
        if is_loss:
            row = c.execute(
                "SELECT count FROM consec_losses WHERE engine=? AND coin=?",
                (engine, coin),
            ).fetchone()
            new_count = (row["count"] if row else 0) + 1
            c.execute(
                "INSERT OR REPLACE INTO consec_losses VALUES (?, ?, ?, ?)",
                (engine, coin, new_count, now),
            )
            if new_count >= CONSEC_LOSS_COIN:
                # Trigger coin cooldown
                c.execute(
                    "INSERT OR REPLACE INTO coin_cooldowns VALUES (?, ?, ?, ?)",
                    (engine, coin, now + COOLDOWN_SECS, f"consec_loss_coin={new_count}"),
                )
                triggered.append({"type": "coin", "engine": engine, "coin": coin,
                                  "until_ts": now + COOLDOWN_SECS,
                                  "reason": f"consec_loss_coin={new_count}"})
                # Reset counter after triggering
                c.execute(
                    "INSERT OR REPLACE INTO consec_losses VALUES (?, ?, ?, ?)",
                    (engine, coin, 0, now),
                )
        else:
            # Win — reset coin counter
            c.execute(
                "INSERT OR REPLACE INTO consec_losses VALUES (?, ?, ?, ?)",
                (engine, coin, 0, now),
            )

        # Update per-engine consecutive losses
        if is_loss:
            row = c.execute(
                "SELECT count FROM engine_consec_losses WHERE engine=?", (engine,)
            ).fetchone()
            new_count = (row["count"] if row else 0) + 1
            c.execute(
                "INSERT OR REPLACE INTO engine_consec_losses VALUES (?, ?, ?)",
                (engine, new_count, now),
            )
            if new_count >= CONSEC_LOSS_ENGINE:
                c.execute(
                    "INSERT OR REPLACE INTO engine_cooldowns VALUES (?, ?, ?)",
                    (engine, now + COOLDOWN_SECS, f"consec_loss_engine={new_count}"),
                )
                triggered.append({"type": "engine", "engine": engine,
                                  "until_ts": now + COOLDOWN_SECS,
                                  "reason": f"consec_loss_engine={new_count}"})
                c.execute(
                    "INSERT OR REPLACE INTO engine_consec_losses VALUES (?, ?, ?)",
                    (engine, 0, now),
                )
        else:
            c.execute(
                "INSERT OR REPLACE INTO engine_consec_losses VALUES (?, ?, ?)",
                (engine, 0, now),
            )

        # Record PnL for DD + PF tracking
        c.execute("INSERT OR REPLACE INTO engine_pnl VALUES (?, ?, ?)", (engine, now, pnl_usd))

        # Check engine drawdown over recent trades (last ~50)
        pnls = c.execute(
            "SELECT pnl FROM engine_pnl WHERE engine=? ORDER BY ts DESC LIMIT 100",
            (engine,),
        ).fetchall()
        c.close()
        if len(pnls) >= 10:
            cum = 0
            peak = 0
            max_dd = 0
            for r in reversed(pnls):
                cum += r["pnl"]
                peak = max(peak, cum)
                dd = (peak - cum) / max(1.0, abs(peak) + 1.0) if peak > 0 else 0
                max_dd = max(max_dd, dd)
            if max_dd > MAX_DD_PCT:
                c = self._conn()
                c.execute(
                    "INSERT OR REPLACE INTO engine_cooldowns VALUES (?, ?, ?)",
                    (engine, now + COOLDOWN_SECS, f"max_dd={max_dd:.1%}"),
                )
                c.commit()
                c.close()
                triggered.append({"type": "engine", "engine": engine,
                                  "until_ts": now + COOLDOWN_SECS,
                                  "reason": f"max_dd={max_dd:.1%}"})

            # Check live PF vs backtest after MIN_TRADES_FOR_PF_CHECK trades
            if len(pnls) >= MIN_TRADES_FOR_PF_CHECK and backtest_pf > 0:
                wins = sum(r["pnl"] for r in pnls if r["pnl"] > 0)
                losses = -sum(r["pnl"] for r in pnls if r["pnl"] <= 0)
                live_pf = wins / losses if losses > 0 else float("inf")
                if live_pf < MIN_PF_RATIO * backtest_pf:
                    c = self._conn()
                    c.execute(
                        "INSERT OR REPLACE INTO engine_cooldowns VALUES (?, ?, ?)",
                        (engine, now + COOLDOWN_SECS, f"live_pf={live_pf:.2f}<{MIN_PF_RATIO}×bt"),
                    )
                    c.commit()
                    c.close()
                    triggered.append({"type": "engine", "engine": engine,
                                      "until_ts": now + COOLDOWN_SECS,
                                      "reason": f"live_pf_fail:{live_pf:.2f}"})

        return {"triggered_cooldowns": triggered}

    def engine_stats(self, engine: str) -> dict:
        c = self._conn()
        pnls = c.execute(
            "SELECT pnl FROM engine_pnl WHERE engine=? ORDER BY ts DESC LIMIT 200",
            (engine,),
        ).fetchall()
        c.close()
        pnls = [r["pnl"] for r in pnls]
        n = len(pnls)
        if n == 0:
            return {"n": 0, "wr": 0, "pf": 0, "total_pnl": 0, "live": False}
        wins = [p for p in pnls if p > 0]
        gw = sum(wins) if wins else 0
        gl = -sum(p for p in pnls if p <= 0)
        pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0)
        return {"n": n, "wr": len(wins) / n, "pf": pf, "total_pnl": sum(pnls),
                "live": n >= 20}
