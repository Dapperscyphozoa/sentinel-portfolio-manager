"""Pre-trade gate (SPEC §7.1, Rule 5b).

Inputs from /check:
  strategy: str
  signal: dict (Signal.to_dict())

Allow logic:
  1. strategy enabled + not halted
  2. coin concentration: existing notional in this coin × proposed ≤ COIN_CONC_MAX (default 2.0×)
  3. account headroom: proposed notional fits within (account_value × MAX_NOTIONAL_FRAC)
  4. regime affinity: if regime ∈ AFFINITY of strategy (or strategy.AFFINITY empty)
  5. open-position cap per strategy (MAX_OPEN_PER_STRATEGY)
  6. global open-position cap (MAX_OPEN_GLOBAL)

Capital fractions:
  size_usd = min(
    risk_capital_per_trade,
    account_value × MAX_NOTIONAL_FRAC / LEVERAGE,
    PER_STRATEGY_CAP * account_value
  )
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger("pretrade")


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass
class CheckResult:
    allow: bool
    size_usd: float
    reason: str


# Universe of registered strategies (NAME → AFFINITY list); kept in sync via
# /register_cloid pings from the runner. Fallback to in-code constants for safety.
KNOWN_AFFINITIES: dict[str, list[str]] = {
    "fsp": ["range", "chop", "trend_up", "trend_down"],
    "vsq": ["trend_up", "trend_down"],
    "range_fade": ["range", "chop"],
    "range_bo": ["trend_up", "trend_down"],
    "lh1": ["range", "chop", "trend_up", "trend_down"],
    "fd1": ["range", "chop", "trend_up", "trend_down"],
    "precog": ["range", "chop", "trend_up", "trend_down"],
    "liq_cascade": ["range", "chop", "trend_up", "trend_down"],
    "cex_dex_arb": ["range", "chop", "trend_up", "trend_down"],
}


def check(conn, strategy: str, signal: dict, regime: dict,
          account_value_usd: float, open_positions: list[dict]) -> CheckResult:
    coin = signal.get("coin", "").upper()
    if not coin:
        return CheckResult(False, 0.0, "no_coin")

    # 1) strategy enabled + not halted
    if os.environ.get(f"STRATEGY_{strategy.upper()}_ENABLED", "1") not in ("1", "true", "yes"):
        return CheckResult(False, 0.0, "strategy_disabled")
    # halts are checked on the runner side; PM trusts that. We still respect a
    # PM_FORCE_HALT_<strategy> override.
    if os.environ.get(f"PM_FORCE_HALT_{strategy.upper()}", "0") == "1":
        return CheckResult(False, 0.0, "halt_forced")

    # 5) global + per-strategy open-position caps
    max_global = _i("MAX_OPEN_POSITIONS", 6)
    max_per = _i(f"MAX_OPEN_{strategy.upper()}", 2)
    n_open = len(open_positions)
    n_strat = sum(1 for p in open_positions if p.get("strategy") == strategy)
    if n_open >= max_global:
        return CheckResult(False, 0.0, "max_open_global")
    if n_strat >= max_per:
        return CheckResult(False, 0.0, f"max_open_{strategy}")

    # 4) regime affinity
    aff = KNOWN_AFFINITIES.get(strategy, [])
    if aff:
        reg_name = regime.get("regime", "unknown")
        conf = float(regime.get("confidence", 0.0))
        if reg_name not in aff and conf > 0.7:
            return CheckResult(False, 0.0, f"regime_mismatch:{reg_name}")

    # 2) coin concentration cap (notional-weighted)
    coin_conc_max = _f("PRETRADE_COIN_CONC_MAX", 2.0)
    leverage = _f("LEVERAGE", 5)
    risk_pct = _f("RISK_PCT_PER_TRADE", 0.02)
    max_notional_frac = _f("MAX_NOTIONAL_FRAC", 0.50)  # total notional ≤ 50% of equity
    per_strat_cap = _f(f"PER_STRATEGY_CAP_{strategy.upper()}", _f("PER_STRATEGY_CAP", 0.20))

    if account_value_usd <= 0:
        return CheckResult(False, 0.0, "no_account_value")

    proposed = account_value_usd * risk_pct * leverage  # naive notional sizing
    # cap by per-strategy fraction × account
    proposed = min(proposed, per_strat_cap * account_value_usd)
    # cap so total notional after adding ≤ MAX_NOTIONAL_FRAC × account × leverage
    current_notional = sum(abs(float(p.get("notional", 0))) for p in open_positions)
    headroom = (max_notional_frac * account_value_usd * leverage) - current_notional
    if headroom <= 0:
        return CheckResult(False, 0.0, "no_notional_headroom")
    proposed = min(proposed, headroom)

    # coin concentration: after adding `proposed`, total this-coin notional
    # must not exceed coin_conc_max × current this-coin notional.
    #   (coin_notional + add) ≤ coin_conc_max · coin_notional
    #   add ≤ coin_notional · (coin_conc_max - 1)
    coin_notional = sum(abs(float(p.get("notional", 0))) for p in open_positions if p.get("coin") == coin)
    min_trade = _f("MIN_TRADE_USD", 25.0)
    if coin_notional > 0:
        if coin_conc_max <= 1.0:
            return CheckResult(False, 0.0, "coin_concentration_full")
        max_additional = coin_notional * (coin_conc_max - 1.0)
        if max_additional < min_trade:
            return CheckResult(False, 0.0, "coin_concentration")
        proposed = min(proposed, max_additional)

    if proposed < min_trade:
        return CheckResult(False, 0.0, "size_below_min")

    return CheckResult(True, round(proposed, 2), "ok")
