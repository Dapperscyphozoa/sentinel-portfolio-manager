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
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    from common.cooldown import CooldownTracker
except Exception:
    CooldownTracker = None

try:
    from common.live_safety import get_safety
except Exception:
    get_safety = None


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


# Combined engine registry — 14 active = 11 OOS + 2 ict + sniper + 2 keepers
# 7 legacy archived (moved to strategy_runner/strategies/_archived/):
#   fsp, vsq, range_fade, range_bo, lh1, fd1, precog
# cex_dex_arb retained on paper.
# All routed through same PM gate (coin lock + regime + cooldown + sizing).
# cap_frac is advisory only; sizing is flat 5% margin per trade.
# Engine registry — honest_pf audited 2026-05-17 (180d 1d / 90d 4h, OKX data).
# 'bt_pf' below is HONEST out-of-sample PF, not the original inflated claims.
# 'cap_frac' redistributed post-audit: GREEN concentrated, halted set to 0.
# Actual sizing remains flat MARGIN_PCT_PER_TRADE × LEVERAGE per trade;
# cap_frac is advisory (displayed on dashboard, used for future
# size-by-weight refactor).
#
# Audit verdict legend:
#   GREEN  — honest PF ≥ 1.4 & OOS PF ≥ 1.0 — keep live
#   WATCH  — honest PF ≥ 1.4 but IS/OOS divergence or n<30 — keep live, monitor
#   YELLOW — honest PF 1.0-1.4 — paper mode (LIVE=0 env)
#   RED    — honest PF < 1.0 — halted (ENABLED=0 env)
#   UNTESTED — no honest backtest possible (needs custom harness)
ENGINE_REGISTRY: dict[str, dict] = {
    # ─── GREEN: real edge (3 engines) ───
    "ict_confluence_1d":   {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 3.35, "cap_frac": 0.00},   # PF 3.77 n=46 WR57%; live_safety controls sizing
    "ict_confluence_4h":   {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 3.18, "cap_frac": 0.00},   # PF 3.18 n=266 WR57%; live_safety
    "e09_pump3d10_td_1d":  {"affinity": ["trend_down"],             "bt_pf":  2.2, "cap_frac": 0.41},  # WR 81% — strongest non-ICT

    # ─── WATCH: green by PF but suspect IS/OOS or undersize n (2 engines) ───
    "e16_bb_fade_hv_1d":   {"affinity": ["high_vol"],               "bt_pf":  5.35, "cap_frac": 0.30},  # PROMOTED 2026-05-18: backtest_v2 confirms PF 2.70 n=37, walk-forward 1.38→22.96
    "e01_zfade3s_tu_1d":   {"affinity": ["trend_up"],               "bt_pf":  1.29, "cap_frac": 0.05},  # DEMOTED 2026-05-18: backtest_v2 n=8 WR 50% PF 0.78; walk-forward 1.07→0.49

    # ─── YELLOW: marginal — paper mode only (LIVE=0 env, 5 engines) ───
    "e17_bb_fade_bt_1d":   {"affinity": ["high_vol", "range"],      "bt_pf":  1.21, "cap_frac": 0.02},  # regime gate inverted 2026-05-18 per backtest_v2
    "e07_zfade2s_tu_1d":   {"affinity": ["trend_up"],               "bt_pf":  1.01, "cap_frac": 0.02},
    # Binance re-audit 2026-05-17: these 3 were OKX-false-positives. Paper-mode pending live evidence.
    "e08_dip3d10_td_1d":   {"affinity": ["trend_down"],             "bt_pf":  0.5, "cap_frac": 0.02},  # OKX 0.58 → Binance 1.61 (OOS 0.96)
    "e07_zfade2s_tu_4h":   {"affinity": ["trend_up"],               "bt_pf":  1.22, "cap_frac": 0.02},  # OKX 0.86 → Binance 1.22
    "e01_zfade3s_tu_4h":   {"affinity": ["trend_up"],               "bt_pf":  1.20, "cap_frac": 0.02},  # OKX 0.84 → Binance 1.20

    # ─── UNTESTED: low weight, monitor live (2 engines) ───
    "liq_cascade":  {"affinity": ["trend_up", "trend_down"],         "bt_pf": 1.30, "cap_frac": 0.07},  # event-driven, no honest BT yet
    "e16_bb_fade_hv_4h":   {"affinity": ["high_vol"],               "bt_pf":  1.50, "cap_frac": 0.07},  # only n=1 in 90d BT, retain at low weight

    # ─── RED: honest PF < 1.0 — halted via STRATEGY_<NAME>_ENABLED=0 env ───
    "e08_dip3d7_td_4h":    {"affinity": ["trend_down"],             "bt_pf":  0.93, "cap_frac": 0.00},  # Binance audit 0.93 (OOS 2.01 — recent strength)
    "e17_bb_fade_bt_4h":   {"affinity": ["high_vol", "range"],      "bt_pf":  0.86, "cap_frac": 0.00},  # regime gate inverted 2026-05-18; cap stays 0 pending 30+ trades under new gate
    "donchian":            {"affinity": ["trend_up", "trend_down"], "bt_pf":  0.01, "cap_frac": 0.00},  # confirmed: WR 4% Binance, WR 6.8% OKX
    "cex_dex_arb":  {"affinity": ["range", "chop"],                  "bt_pf": 0.00, "cap_frac": 0.00},  # halted: bt_pf=0, lookahead history
    "cascade_sniper_hl":   {"affinity": ["high_vol", "trend_up", "trend_down", "range", "chop"], "bt_pf": 0.00, "cap_frac": 0.00},  # halted: bt_pf=0, untested
    # ─── World-first: HLP Vault Fade (Council #1 pick, Tier 1 ship-first) ───
    # Trade WITH HLP positioning when z-score >2σ from 7d mean. Custom exit
    # when HLP returns near-neutral. No bt_pf yet — forward-validate live.
    "hlp_fade":            {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 2.50, "cap_frac": 0.00},   # 2.5 = expected midpoint (council 2.0-3.5)
    # ─── Tier 1 #2: Funding Momentum (2nd-derivative funding signal) ───
    # Z-score on funding-rate ROC vs price ROC divergence. Distinct from
    # dead fsp (level-based) and fd1 (1st-derivative divergence).
    "fmom":                {"affinity": ["trend_up", "trend_down", "range", "chop"],
                             "bt_pf": 1.75, "cap_frac": 0.00},   # 1.75 midpoint of council 1.5-2.0
    "hl_settle_5m":        {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 1.85, "cap_frac": 0.00},   # 1.85 midpoint of council 1.5-2.2, MAKER ONLY
    # ─── Tier 1 #4: Stop Hunt Rejection (S/R wick-sweep + reversal) ───
    # Detects algorithmic stop sweeps at horizontal S/R with strict wick
    # mechanics (≥50% bar, ≥20bps sweep, body >10bps). Expected WR 65-75%.
    "stop_hunt":           {"affinity": ["range", "chop", "high_vol"],
                             "bt_pf": 3.00, "cap_frac": 0.00},   # 3.0 midpoint of council 2.5-3.5
    # ─── Tier 1 #5: VPOC Retest (naked weekly POC magnet) ───
    # Universe restricted to BTC/ETH/SOL/BNB/XRP (institutional flow respects
    # VPOCs; memes do not). Distinct from ICT structure (uses volume, not OB).
    "vpoc_retest":         {"affinity": ["range", "chop", "trend_up", "trend_down"],
                             "bt_pf": 1.90, "cap_frac": 0.00},   # 1.9 midpoint of council 1.6-2.2
    # ─── Tier 1 #6: OI Concentration (pre-cascade detector, v1 vol-proxy) ───
    # Detects extreme volume + price near major S/R. Fires when conditions for
    # cascading liquidation are present. v1 uses volume percentile as OI proxy
    # (true wallet-level aggregation deferred to v2). Low frequency, asymmetric.
    "oi_concentration":    {"affinity": ["high_vol", "range", "chop"],
                             "bt_pf": 2.75, "cap_frac": 0.00},   # 2.75 midpoint of council 2.0-3.5
}

# CUT_ENGINES — hard-blocked from check() regardless of env. The 7 legacy
# strategies are now archived (files moved out of strategies/), so they
# cannot be loaded and don't need to appear here. Empty set retained as a
# mechanism for future emergency blocks.
CUT_ENGINES: set = set()

# Backward-compat alias
OOS_ENGINE_REGISTRY = ENGINE_REGISTRY
assert abs(sum(e["cap_frac"] for e in ENGINE_REGISTRY.values()) - 1.0) < 0.02, \
    f"cap_fracs sum to {sum(e['cap_frac'] for e in ENGINE_REGISTRY.values())}"

# Singleton cooldown tracker (lock-guarded init — sentinel audit 2026-05-17)
_cooldown: Optional[object] = None
_cooldown_lock = threading.Lock()


def _get_cooldown():
    global _cooldown
    # Fast path: already initialized
    if _cooldown is not None:
        return _cooldown
    if CooldownTracker is None:
        return None
    with _cooldown_lock:
        # Re-check under lock
        if _cooldown is None:
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

    # 0) Hard-block: CUT engines (audit verdict — see CUT_ENGINES set)
    if strategy in CUT_ENGINES:
        return CheckResult(False, 0.0, "engine_cut_by_audit")

    # 0b) Operator-controlled coin blocklist — env var BLOCKED_COINS=BTC,INJ,OP,...
    # Used to halt bleeders mid-run without code changes or per-strategy halts.
    # Comma-separated; case-insensitive; whitespace-tolerant.
    # Empty / unset → no coins blocked.
    blocked_raw = os.environ.get("BLOCKED_COINS", "")
    if blocked_raw:
        blocked = {c.strip().upper() for c in blocked_raw.split(",") if c.strip()}
        if coin in blocked:
            return CheckResult(False, 0.0, "coin_blocked_operator")

    # 1) 1_GLOBAL COIN LOCK — 1 position per coin across all engines
    for p in open_positions:
        if p.get("coin", "").upper() == coin:
            return CheckResult(False, 0.0, "coin_locked")

    # 2) Global cap
    max_global = _i("MAX_OPEN_POSITIONS", 20)
    if len(open_positions) >= max_global:
        return CheckResult(False, 0.0, "max_open_global")

    # 3) Engine config (combined registry: 9 legacy + 11 OOS)
    eng_cfg = ENGINE_REGISTRY.get(strategy, {})
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

    # 8) Live-safety gate for ICT signals + cascade sniper (council Phase 1 spec)
    # ICT + cascade sniper route through live_safety; OOS/legacy engines use
    # flat 5% margin × 5x lev as before.
    LIVE_SAFETY_ENGINES = {"ict_confluence_4h", "ict_confluence_1d", "cascade_sniper_hl"}
    if strategy in LIVE_SAFETY_ENGINES and get_safety is not None:
        try:
            safety = get_safety()
            sr = safety.check(signal, account_value_usd, open_positions)
            if not sr.allow:
                return CheckResult(False, 0.0, f"live_safety:{sr.reason}")
            # Override sizing with safety-controlled value (smaller, ATR-aware)
            margin_usd = sr.margin_usd
        except Exception as e:
            log.exception("live_safety check failed: %s", e)
            return CheckResult(False, 0.0, "live_safety_error")

    return CheckResult(True, round(margin_usd, 2), "ok", bt_pf=bt_pf)


def record_close(strategy: str, coin: str, pnl_usd: float) -> dict:
    cd = _get_cooldown()
    if cd is None:
        return {"triggered_cooldowns": []}
    bt_pf = ENGINE_REGISTRY.get(strategy, {}).get("bt_pf", 0.0)
    return cd.record_close(strategy, coin, pnl_usd, bt_pf)
