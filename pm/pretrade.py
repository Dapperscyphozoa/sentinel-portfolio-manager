"""Pre-trade gate v2 — OOS engine production deployment.

NEW SPEC (operator confirmed):
  - 1 position per coin GLOBALLY (across all engines)
  - First-fire wins (engines evaluated in PF-priority order by runner)
  - 5x leverage on perp
  - 5% margin per new position (notional = 25% wallet)
  - 20 max concurrent positions globally
  - 10% spot stop loss (set in strategy.evaluate)
  - Auto-cooldowns: 4 consec loss/coin (1h), 6 consec loss/engine (1h),
    12% DD (1h), live PF < 0.74×bt (1h)
  - Promotion: 20 trades + live PF within 20% of bt → live
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

try:
    from common.cooldown import CooldownTracker
except Exception:
    CooldownTracker = None


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
    size_usd: float          # margin USD (notional = size × leverage)
    reason: str
    bt_pf: float = 0.0


# Production OOS engine registry — 11 engines, cap_fracs sum to 1.0
OOS_ENGINE_REGISTRY: dict[str, dict] = {
    "e01_zfade3s_tu_1d":   {"affinity": ["trend_up"],               "bt_pf": 10.05, "cap_frac": 0.10},
    "e07_zfade2s_tu_1d":   {"affinity": ["trend_up"],               "bt_pf":  2.12, "cap_frac": 0.08},
    "e08_dip3d10_td_1d":   {"affinity": ["trend_down"],             "bt_pf":  1.93, "cap_frac": 0.10},
    "e09_pump3d10_td_1d":  {"affinity": ["trend_down"],             "bt_pf":  1.87, "cap_frac": 0.08},
    "e16_bb_fade_hv_1d":   {"affinity": ["high_vol"],               "bt_pf":  1.47, "cap_frac": 0.08},
    "e17_bb_fade_bt_1d":   {"affinity": ["trend_up", "trend_down"], "bt_pf":  1.41, "cap_frac": 0.08},
    "e01_zfade3s_tu_4h":   {"affinity": ["trend_up"],               "bt_pf":  5.00, "cap_frac": 0.08},
    "e07_zfade2s_tu_4h":   {"affinity": ["trend_up"],               "bt_pf":  2.50, "cap_frac": 0.12},
    "e08_dip3d7_td_4h":    {"affinity": ["trend_down"],             "bt_pf":  1.50, "cap_frac": 0.08},
    "e16_bb_fade_hv_4h":   {"affinity": ["high_vol"],               "bt_pf":  1.50, "cap_frac": 0.08},
    "e17_bb_fade_bt_4h":   {"affinity": ["trend_up", "trend_down"], "bt_pf":  1.30, "cap_frac": 0.12},
}
assert abs(sum(e["cap_frac"] for e in OOS_ENGINE_REGISTRY.values()) - 1.0) < 0.01

# Singleton cooldown tracker
_cooldown: Optional[object] = None


def _get_cooldown():
    global _cooldown
    if _cooldown is None and CooldownTracker is not None:
        try:
            db_path = os.environ.get("COOLDOWN_DB", "/var/data/cooldowns.sqlite")
            _cooldown = CooldownTracker(db_path)
        except Exception as e:
            log.warning("CooldownTracker init failed: %s", e)
            _cooldown = None
    return _cooldown


def check(conn, strategy: str, signal: dict, regime: dict,
          account_value_usd: float, open_positions: list[dict]) -> CheckResult:
    """Pre-trade gate for OOS engine production deployment."""
    coin = signal.get("coin", "").upper()
    if not coin:
        return CheckResult(False, 0.0, "no_coin")

    if os.environ.get(f"STRATEGY_{strategy.upper()}_ENABLED", "1") not in ("1", "true", "yes"):
        return CheckResult(False, 0.0, "strategy_disabled")
    if os.environ.get(f"PM_FORCE_HALT_{strategy.upper()}", "0") == "1":
        return CheckResult(False, 0.0, "halt_forced")

    # 1) 1_GLOBAL COIN LOCK — 1 position per coin across all engines
    for p in open_positions:
        if p.get("coin", "").upper() == coin:
            return CheckResult(False, 0.0, "coin_locked")

    # 2) Global cap
    max_global = _i("MAX_OPEN_POSITIONS", 20)
    if len(open_positions) >= max_global:
        return CheckResult(False, 0.0, "max_open_global")

    # 3) Engine config
    eng_cfg = OOS_ENGINE_REGISTRY.get(strategy, {})
    bt_pf = eng_cfg.get("bt_pf", 0.0)
    affinity = eng_cfg.get("affinity", [])

    # 4) Regime affinity
    if affinity:
        reg_name = (regime.get("regime") or "unknown").lower()
        conf = float(regime.get("confidence", 0.0))
        if reg_name not in affinity and conf > 0.7:
            return CheckResult(False, 0.0, f"regime_mismatch:{reg_name}")

    # 5) Cooldown checks
    cd = _get_cooldown()
    if cd is not None:
        blocked, reason = cd.is_engine_blocked(strategy)
        if blocked:
            return CheckResult(False, 0.0, reason)
        blocked, reason = cd.is_coin_blocked(strategy, coin)
        if blocked:
            return CheckResult(False, 0.0, reason)

    if account_value_usd <= 0:
        return CheckResult(False, 0.0, "no_account_value")

    # 6) Sizing — 5% margin × 5x leverage
    leverage = _f("LEVERAGE", 5.0)
    margin_pct = _f("MARGIN_PCT_PER_TRADE", 0.05)
    margin_usd = margin_pct * account_value_usd

    max_margin_frac = _f("MAX_MARGIN_FRAC", 1.0)
    current_margin = sum(abs(float(p.get("margin", p.get("notional", 0) / leverage)))
                         for p in open_positions)
    if current_margin + margin_usd > max_margin_frac * account_value_usd:
        return CheckResult(False, 0.0, "no_margin_headroom")

    notional = margin_usd * leverage
    if notional < _f("MIN_TRADE_USD", 10.0):
        return CheckResult(False, 0.0, "size_below_min")

    return CheckResult(True, round(margin_usd, 2), "ok", bt_pf=bt_pf)


def record_close(strategy: str, coin: str, pnl_usd: float) -> dict:
    cd = _get_cooldown()
    if cd is None:
        return {"triggered_cooldowns": []}
    bt_pf = OOS_ENGINE_REGISTRY.get(strategy, {}).get("bt_pf", 0.0)
    return cd.record_close(strategy, coin, pnl_usd, bt_pf)
