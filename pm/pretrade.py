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


# ── Low-liquidity guard (sentinel-born 2026-05-18) ─────────────────────────
# Council Q1 verdict (5/6 voters): 5 of 5 recent live losses (AVAX/INJ/WIF/
# TIA/OP) shared "mid-cap alt during thin liquidity" pattern. Add a global
# gate that rejects entries on coins below MIN_24H_VOL_USD (default $50M).
# Fail-open: if signal-bus volume unreachable, do NOT block (avoid bus-down
# being a trading halt).

import functools
import urllib.request as _urlreq
_VOL_CACHE: dict[str, tuple[float, float]] = {}   # coin -> (vol_usd, fetched_ts)
_VOL_TTL_SEC = 300

def _fetch_24h_vol_usd(coin: str) -> Optional[float]:
    """Compute 24h notional volume in USD from signal-bus 1h klines."""
    now = time.time()
    cached = _VOL_CACHE.get(coin)
    if cached and (now - cached[1]) < _VOL_TTL_SEC:
        return cached[0]
    bus_url = os.environ.get("SIGNAL_BUS_URL", "").rstrip("/")
    if not bus_url:
        return None
    try:
        url = f"{bus_url}/candles/{coin}/1h?n=24"
        with _urlreq.urlopen(url, timeout=3) as r:
            import json as _j
            bars = _j.loads(r.read())
        if not bars or len(bars) < 12:
            return None
        # volume × close = USD notional
        usd_vol = sum(float(b.get("volume", 0)) * float(b.get("close", 0)) for b in bars)
        _VOL_CACHE[coin] = (usd_vol, now)
        return usd_vol
    except Exception as e:
        log.debug("vol fetch failed for %s: %s", coin, e)
        return None

ENGINE_REGISTRY: dict[str, dict] = {
    # ─── GREEN: real edge (3 engines) ───
    # cap_frac REBALANCED 2026-05-18 per council promotion audit:
    #   ict_confluence_4h: 0.00 → 0.15 (council trim from 0.25 — diversification
    #     ethos for $491 wallet; OOS PF 1.37 on longs means asymmetry is real
    #     but longs are still profitable, so monitor not ban — kept SHORT_ONLY=0)
    #   hl_settle_5m:      0.00 → 0.20 (most-tested live engine, n=55; promoted
    #     after short-only + denylist fix, fee-cleanup TP 0.4%)
    #   e08_dip3d7_td_4h:  0.00 → 0.10 (OOS PF 2.01 n=191; force_close bug fixed)
    #   ict_confluence_1d: 0.00 → 0.05 (paper-only via live_safety)
    #   e16_bb_fade_hv_1d: 0.30 → 0.05 (council trim — n=29 too thin for 0.10)
    #   e09_pump3d10_td_1d: 0.41 → 0.10 (n=26 over-allocated)
    # Verdict: 7-voter council MODERATE — over-concentration risk addressed.
    "ict_confluence_1d":   {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 3.35, "cap_frac": 0.05},
    "ict_confluence_4h":   {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 3.18, "cap_frac": 0.15},   # council-trimmed from 0.25
    "e09_pump3d10_td_1d":  {"affinity": ["trend_down"],             "bt_pf":  2.2, "cap_frac": 0.10},

    # ─── WATCH: green by PF but suspect IS/OOS or undersize n (2 engines) ───
    "e16_bb_fade_hv_1d":   {"affinity": ["high_vol"],               "bt_pf":  5.35, "cap_frac": 0.05},  # council-trimmed from 0.10 — n=29
    "e01_zfade3s_tu_1d":   {"affinity": ["trend_up"],               "bt_pf":  1.29, "cap_frac": 0.05},

    # ─── YELLOW: marginal — paper mode only (LIVE=0 env, 5 engines) ───
    "e17_bb_fade_bt_1d":   {"affinity": ["high_vol", "range"],      "bt_pf":  1.21, "cap_frac": 0.01},
    "e07_zfade2s_tu_1d":   {"affinity": ["trend_up"],               "bt_pf":  1.01, "cap_frac": 0.02},
    # Binance re-audit 2026-05-17: these 3 were OKX-false-positives.
    "e08_dip3d10_td_1d":   {"affinity": ["trend_down"],             "bt_pf":  0.5, "cap_frac": 0.06},  # OOS 1.85
    "e07_zfade2s_tu_4h":   {"affinity": ["trend_up"],               "bt_pf":  1.22, "cap_frac": 0.06},
    "e01_zfade3s_tu_4h":   {"affinity": ["trend_up"],               "bt_pf":  1.20, "cap_frac": 0.02},

    # ─── UNTESTED: low weight, monitor live (2 engines) ───
    "liq_cascade":  {"affinity": ["trend_up", "trend_down"],         "bt_pf": 1.30, "cap_frac": 0.05},  # event-driven, sentinel-born
    "e16_bb_fade_hv_4h":   {"affinity": ["high_vol"],               "bt_pf":  1.50, "cap_frac": 0.02},  # n=1 BT only, low weight

    # ─── RED: honest PF < 1.0 — halted via STRATEGY_<NAME>_ENABLED=0 env ───
    # e08_dip3d7_td_4h PROMOTED 2026-05-18: OOS PF 2.01 n=191; force_close PnL
    # bug masked real performance. Re-graded after bug fix c5b055d.
    "e08_dip3d7_td_4h":    {"affinity": ["trend_down"],             "bt_pf":  0.93, "cap_frac": 0.10},
    "e17_bb_fade_bt_4h":   {"affinity": ["high_vol", "range"],      "bt_pf":  0.86, "cap_frac": 0.00},
    "donchian":            {"affinity": ["trend_up", "trend_down"], "bt_pf":  0.01, "cap_frac": 0.00},
    "cex_dex_arb":  {"affinity": ["range", "chop"],                  "bt_pf": 0.00, "cap_frac": 0.00},
    "cascade_sniper_hl":   {"affinity": ["high_vol", "trend_up", "trend_down", "range", "chop"], "bt_pf": 0.00, "cap_frac": 0.00},
    # ─── World-first: HLP Vault Fade (Council #1 pick, Tier 1 ship-first) ───
    # ACTIVATED 2026-05-18 — sentinel council 3/5 YES; council caveat:
    # validate /hlp poll latency < 1s before first live fire (operator action)
    "hlp_fade":            {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 2.50, "cap_frac": 0.03},
    # ─── Tier 1 #2: Funding Momentum (2nd-derivative funding signal) ───
    "fmom":                {"affinity": ["trend_up", "trend_down", "range", "chop"],
                             "bt_pf": 1.75, "cap_frac": 0.00},
    "hl_settle_5m":        {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 1.85, "cap_frac": 0.20},   # PROMOTED 2026-05-18 post short-only fix
    # ─── Tier 1 #4: Stop Hunt Rejection (S/R wick-sweep + reversal) ───
    "stop_hunt":           {"affinity": ["range", "chop", "high_vol"],
                             "bt_pf": 3.00, "cap_frac": 0.00},
    # ─── Tier 1 #5: VPOC Retest (naked weekly POC magnet) ───
    # ACTIVATED 2026-05-18 — sentinel council 5/5 YES (unanimous activation vote)
    "vpoc_retest":         {"affinity": ["range", "chop", "trend_up", "trend_down"],
                             "bt_pf": 1.90, "cap_frac": 0.03},
    # ─── Tier 1 #6: OI Concentration (pre-cascade detector, v1 vol-proxy) ───
    "oi_concentration":    {"affinity": ["high_vol", "range", "chop"],
                             "bt_pf": 2.75, "cap_frac": 0.00},
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

    # 1b) Low-liquidity guard (sentinel-born 2026-05-18, council Q1).
    # Skip-on-missing: if SIGNAL_BUS unreachable, do NOT block. The filter is
    # an edge improvement, not a safety gate — fail-open preserves uptime.
    min_vol_usd = _f("MIN_24H_VOL_USD", 50_000_000.0)
    if min_vol_usd > 0:
        v = _fetch_24h_vol_usd(coin)
        if v is not None and v < min_vol_usd:
            return CheckResult(False, 0.0, f"low_liquidity:{v/1e6:.1f}M<{min_vol_usd/1e6:.0f}M")

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
        # 5a) Permanent paper-demote check (operator 2026-05-18: 4-loss rule)
        # Survives restarts. Reverses only via POST /reinstate/<engine>.
        demoted, reason = cd.is_engine_demoted(strategy)
        if demoted:
            return CheckResult(False, 0.0, reason)
        # 5b) Rolling 1h cooldowns
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
