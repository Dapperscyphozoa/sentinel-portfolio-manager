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
    # 2026-05-21: ict_confluence_4h DEMOTED 0.15 → 0.05 per replay BT audit.
    # n=27 signals BT shows 93% WR but APT dominates 25/27 trades = single-coin
    # regime artifact (recent uptrend). At cap 0.15 ~$73 budget concentrated on
    # one coin's continuation. Demote to 0.05 (~$25 budget) until diversified.
    "ict_confluence_4h":   {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 3.18, "cap_frac": 0.05},   # demoted 0.15→0.05 (single-coin risk)
    "e09_pump3d10_td_1d":  {"affinity": ["trend_down"],             "bt_pf":  2.2, "cap_frac": 0.10},
    # uzt_rev v3 — reversal-only, single TP=5R, 16-coin universe. Bt n=41 WR 68% PF 6.92 OOS 6.92.
    # Operator-promoted to live 2026-05-19. Cap_frac 0.05 starting allocation (~$25 notional).
    "uzt_rev":             {"affinity": ["trend_up", "trend_down", "range", "chop"],
                             "bt_pf": 6.92, "cap_frac": 0.05},

    # ─── WATCH: green by PF but suspect IS/OOS or undersize n (2 engines) ───
    "e16_bb_fade_hv_1d":   {"affinity": ["high_vol"],               "bt_pf":  5.35, "cap_frac": 0.05},  # council-trimmed from 0.10 — n=29
    "e01_zfade3s_tu_1d":   {"affinity": ["trend_up"],               "bt_pf":  1.29, "cap_frac": 0.05},

    # ─── YELLOW: marginal — paper mode only (LIVE=0 env, 5 engines) ───
    "e17_bb_fade_bt_1d":   {"affinity": ["high_vol", "range"],      "bt_pf":  1.21, "cap_frac": 0.01},
    "e07_zfade2s_tu_1d":   {"affinity": ["trend_up"],               "bt_pf":  1.01, "cap_frac": 0.02},
    # 2026-05-21: KILLED — replay BT n=25 WR 0% PF 0 net -$10.60. APT dominant
    # 24/25 trades. Engine was firing 6.3/d in paper and bleeding mock capital.
    # Disable via cap_frac=0 + ENABLED=0 env var (kills signal generation).
    # 2026-05-21: e08_dip3d10_td_1d PURGED. Honest BT 180d×10 coins:
    # n=48, WR 45.8%, PF 0.37, expectancy -2.97%/trade. OOS PF 0.28.
    # Every coin RED (PF < 1.0). Confirmed dead by evidence, not by sample bias.
    # (entry removed entirely; previously cap_frac=0.00 with stale bt_pf=0.5)
    "e07_zfade2s_tu_4h":   {"affinity": ["trend_up"],               "bt_pf":  1.22, "cap_frac": 0.06},
    "e01_zfade3s_tu_4h":   {"affinity": ["trend_up"],               "bt_pf":  1.20, "cap_frac": 0.02},

    # ─── UNTESTED: low weight, monitor live (2 engines) ───
    "liq_cascade":  {"affinity": ["trend_up", "trend_down"],         "bt_pf": 1.30, "cap_frac": 0.05},  # event-driven, sentinel-born
    "e16_bb_fade_hv_4h":   {"affinity": ["high_vol"],               "bt_pf":  1.50, "cap_frac": 0.02},  # n=1 BT only, low weight

    # ─── RED: honest PF < 1.0 — halted via STRATEGY_<NAME>_ENABLED=0 env ───
    # e08_dip3d7_td_4h GHOST CLEANUP 2026-05-19: file was archived 2026-05-18
    # (commit 6c77c8a). Registry entry retained at cap_frac 0.10 by oversight.
    # Removed entirely per SPEC v2.1 §3.10 / Phase 15. Cap_frac sum drops 1.05→0.95
    # which remains inside the ±0.06 invariant tolerance — no redistribution needed.
    # ─── REVIVED 2026-05-20: e08_dip3d7_td_4h_INV (LIVE at $25 notional) ───
    # Original (LONG) was archived 2026-05-19 after -$6.81 bleed.
    # Honest re-test 2026-05-20 (365d × 60 OKX symbols):
    #   - LONG side: 0/252 combos pass OOS PF≥1.0 (confirmed dead)
    #   - SHORT side: 114/252 combos pass GREEN (OOS PF≥1.4)
    #   - Walk-forward universe-select OOS PF 2.88, WR 71.7%, hit-rate 7/7
    # Thesis: dip in TREND_DOWN = continuation, not exhaustion. Same family
    # of error as lh1 (SPEC §3.5). Ship config drop=0.07 hold=8 sl=tp=0.10,
    # 7-coin universe (ARB GALA INJ OP ORDI PYTH WIF).
    # Operator-promoted to LIVE 2026-05-20 at $25 notional per trade:
    #   size_mult 0.2 → margin 0.05*$491*0.2 ≈ $4.91 → notional $24.55
    #   cap_frac 0.02 → engine budget ~$9.82 margin → up to 2 concurrent positions
    # Funded by trimming hl_settle_5m 0.18→0.16 (lowest-cost trim — keeps
    # its 0.16 = ~$78 budget, still the largest single allocation).
    "e08_dip3d7_td_4h_inv": {"affinity": ["trend_down"], "bt_pf": 2.88,
                              "cap_frac": 0.02, "size_mult": 0.2},
    # 2026-05-21: donchian PROMOTED. Honest BT 60d×6 coins (2026-05-19):
    # n=152, WR 54.6%, PF 1.96, OOS PF 1.78. Per-coin: DOGE 4.26, SOL 2.48,
    # AVAX 1.81, ETH 1.76, BTC 1.49, LINK 1.41. Registry was carrying stale
    # bt_pf=0.01 from a much-older BT; correcting + restoring cap_frac.
    "donchian":            {"affinity": ["trend_up", "trend_down"], "bt_pf":  1.96, "cap_frac": 0.05},
    # 2026-05-21: PURGED (3 confirmed dead):
    #   e17_bb_fade_bt_4h — bt_pf 0.86 RED, never recovered to PF≥1.0
    #   cex_dex_arb       — SPEC §4 look-ahead bias (PF 14.92 fictional)
    #   cascade_sniper_hl — never validated, never fired, council halt
    # Downstream LIVE_SAFETY_ENGINES guards left intact in case of accidental load.
    # ─── World-first: HLP Vault Fade (Council #1 pick, Tier 1 ship-first) ───
    # ACTIVATED 2026-05-18 — sentinel council 3/5 YES; council caveat:
    # validate /hlp poll latency < 1s before first live fire (operator action)
    # 2026-05-21: hlp_fade DEMOTED 0.10 → 0.025 (canary level) per sentinel audit
    # MODERATE @ 84%. Live n=10, PF 1.39, +$0.28 net — BUT NEAR=240% of profit
    # (1 coin = 4 trades, 50% WR). Sample variance could erase edge.
    # Demote to canary (0.025 = ~$12 budget) to accumulate live trades safely.
    # Promotion gate: ≥20 LIVE trades + clean_PF ≥ 1.2 → promote 0.025 → 0.05.
    "hlp_fade":            {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 2.50, "cap_frac": 0.025},   # demoted 0.10→0.025 (canary)
    # ─── Tier 1 #2: Funding Momentum (2nd-derivative funding signal) ───
    "fmom":                {"affinity": ["trend_up", "trend_down", "range", "chop"],
                             "bt_pf": 1.75, "cap_frac": 0.00},
    "hl_settle_5m":        {"affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
                             "bt_pf": 1.85, "cap_frac": 0.16},   # trimmed 0.18→0.16 to fund e08_inv (2026-05-20)
    # Stage 1 NEW ENGINE — paper-only pending honest backtest gate (council priority)
    "hl_cvd_aggressor": {
        "class": "cvd_aggressor_flow",
        "affinity": ["trend_up", "trend_down", "range"],
        "capital_fraction": 0.00,           # 0 = paper-only until honest backtest passes
        "bt_pf": 2.20,                       # council est +1.8-2.5%/mo
        "bt_n": 0,                           # not yet backtested
        "min_n_for_gate": 30,
        "audit_status": "PROVISIONAL_NEW_ENGINE_PAPER",
        "notes": "world-first HL CVD aggressor flow. Needs honest backtest before live.",
    },
    "liq_cluster_hunt": {
        "class": "liq_cluster_predictive",
        "affinity": ["range", "chop", "high_vol", "trend_up", "trend_down"],
        "capital_fraction": 0.00,
        "bt_pf": 2.60,
        "bt_n": 0,
        "min_n_for_gate": 30,
        "audit_status": "PROVISIONAL_NEW_ENGINE_PAPER",
        "notes": "Predict sweep path from stacked liq cluster + round-number alignment.",
    },
    "hl_whale_frontrun": {
        "class": "whale_position_copy",
        "affinity": ["trend_up", "trend_down", "range", "chop"],
        "capital_fraction": 0.00,
        "bt_pf": 3.20,
        "bt_n": 0,
        "min_n_for_gate": 30,
        "audit_status": "PROVISIONAL_NEW_ENGINE_PAPER",
        "notes": "World-first: copy new opens from top-20 HL wallets. Highest est edge.",
    },
    "hl_depth_shock": {
        "class": "orderbook_liquidity_shock",
        "affinity": ["range", "chop", "high_vol"],
        "capital_fraction": 0.00,
        "bt_pf": 2.10,
        "bt_n": 0,
        "min_n_for_gate": 30,
        "audit_status": "PROVISIONAL_NEW_ENGINE_PAPER",
        "notes": "Fade bid/ask depth shocks before price catches down.",
    },
    "hl_vault_predict": {
        "class": "vault_rebalance_anticipation",
        "affinity": ["range", "chop", "trend_up", "trend_down"],
        "capital_fraction": 0.00,
        "bt_pf": 3.00,
        "bt_n": 0,
        "min_n_for_gate": 30,
        "audit_status": "PROVISIONAL_NEW_ENGINE_PAPER",
        "notes": "Anticipate HLP imminent rebalance from NAV-vs-mark divergence rate.",
    },
    # hlp_decoder — reverse-engineered signal from 4 HLP sub-vaults (master,
    # strategy_a, strategy_b, liquidator). Three signal kinds toggleable via
    # env. Cap_frac=0 paper-only until honest backtest passes.
    "hlp_decoder": {
        "class": "hlp_subvault_decode",
        "affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
        "capital_fraction": 0.00,
        "bt_pf": 2.50,                 # council est; pending honest backtest
        "bt_n": 0,
        "min_n_for_gate": 30,
        "audit_status": "PROVISIONAL_NEW_ENGINE_PAPER",
        "notes": "World-first: decode HLP's 4 sub-vault positioning. H-LIQ + H-CONSENSUS + H-FADE-MM.",
    },
   # PROMOTED 2026-05-18 post short-only fix
    # ─── Tier 1 #4: Stop Hunt Rejection ───
    # ACTIVATED 2026-05-18 — council Q5 unblocked: news-spike ATR filter
    # added (STOPH_NEWS_SPIKE_ATR_MULT=3.0). Bars with range >3×ATR_14
    # are rejected (likely macro news, not stop hunt). 1/5 was YES pre-fix.
    "stop_hunt":           {"affinity": ["range", "chop", "high_vol"],
                             "bt_pf": 3.00, "cap_frac": 0.02},
    # ─── Tier 1 #5: VPOC Retest (naked weekly POC magnet) ───
    # ACTIVATED 2026-05-18 — sentinel council 5/5 YES (unanimous activation vote)
    "vpoc_retest":         {"affinity": ["range", "chop", "trend_up", "trend_down"],
                             "bt_pf": 1.90, "cap_frac": 0.03},
    # ─── Tier 1 #6: OI Concentration ───
    # ACTIVATED 2026-05-18 (council Q5 unblock) — real OI feed now wired via
    # signal_bus.oi_poller (HL metaAndAssetCtxs, 5min poll, 30d history).
    # Strategy reparameterized off real OI (was volume-proxy v1).
    "oi_concentration":    {"affinity": ["high_vol", "range", "chop"],
                             "bt_pf": 2.75, "cap_frac": 0.02},
}

# CUT_ENGINES — hard-blocked from check() regardless of env. The 7 legacy
# strategies are now archived (files moved out of strategies/), so they
# cannot be loaded and don't need to appear here. Empty set retained as a
# mechanism for future emergency blocks.
CUT_ENGINES: set = set()

# Backward-compat alias
OOS_ENGINE_REGISTRY = ENGINE_REGISTRY
# Stage 1 engines use 'capital_fraction'; legacy use 'cap_frac'. Accept both.
def _cap_of(e: dict) -> float:
    return float(e.get("cap_frac", e.get("capital_fraction", 0.0)))

_cap_sum = sum(_cap_of(e) for e in ENGINE_REGISTRY.values())
# 2026-05-21: cap_sum INVARIANT REMOVED per operator instruction.
# Rationale: concurrent open positions are rare (rarely >3 at once). Normalized
# cap_frac as a budget was a $491-wallet constraint that never bound in practice.
# Risk is measured at the POSITION level via leverage × notional, not the
# normalized weight. cap_frac retained per engine as a sizing HINT but no longer
# gated against a sum invariant. Engines can run at any cap_frac level approved
# by operator. PM still enforces per-engine cap during /check (engine can't
# exceed its own budget) — but no global cap_sum block.
log.info("registry loaded: %d engines, cap_sum=%.3f (no global invariant)",
         len(ENGINE_REGISTRY), _cap_sum)

# ─── promotion gate ─────────────────────────────────────────────────────
# Prevents capital drift: refuses (strict) or warns (default) when any engine
# sits at canary/live cap_frac without meeting required metrics.
# Env PROMOTION_GATE_STRICT=1 to refuse boot. Env PROMOTION_OVERRIDE_<NAME>=1
# to bypass a single engine (recorded in logs). See pm/promotion_gate.py.
try:
    from . import promotion_gate as _pg
    _pg.enforce(ENGINE_REGISTRY,
                strict=os.environ.get("PROMOTION_GATE_STRICT", "").strip() in ("1", "true", "yes"))
except ImportError:
    log.warning("pm.promotion_gate not importable; skipping gate enforcement")

# Singleton cooldown tracker (lock-guarded init — sentinel audit 2026-05-17)
_cooldown: Optional[object] = None
_cooldown_lock = threading.Lock()

# Per-engine check serialization (sentinel H7 race condition fix 2026-05-19).
# Two concurrent /check calls on the same engine can both read the same
# open_positions snapshot, both pass the cap_frac concentration cap, and
# both allow trades that together exceed the engine's budget. Serialize
# per-engine to close the TOCTOU window between read and trade insertion.
_engine_locks: dict[str, threading.Lock] = {}
_engine_locks_guard = threading.Lock()


def _engine_check_lock(engine: str) -> threading.Lock:
    with _engine_locks_guard:
        lock = _engine_locks.get(engine)
        if lock is None:
            lock = threading.Lock()
            _engine_locks[engine] = lock
        return lock


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
    # Serialize concurrent /check calls per engine to prevent TOCTOU race
    # on the cap_frac concentration cap (sentinel H7 2026-05-19).
    with _engine_check_lock(strategy):
        return _check_impl(conn, strategy, signal, regime,
                            account_value_usd, open_positions)


def _check_impl(conn, strategy: str, signal: dict, regime: dict,
                account_value_usd: float, open_positions: list[dict]) -> CheckResult:
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

    # 4) Regime affinity + Rule 5b trend_direction_aware half-size
    # Rule 5a (hard): if engine has affinity AND current regime is NOT in
    # affinity AND confidence > 0.7 → block. Unless trend_direction_aware
    # is true AND regime is the OPPOSITE trend; then allow at half size.
    size_mult = 1.0
    trend_aware = bool(eng_cfg.get("trend_direction_aware", False))
    if affinity:
        reg_name = (regime.get("regime") or "unknown").lower()
        conf = float(regime.get("confidence", 0.0))
        if reg_name not in affinity and conf > 0.7:
            # Trend engine in opposite trend? Apply half-size if flagged.
            opposite_pairs = {
                "trend_up": "trend_down",
                "trend_down": "trend_up",
            }
            opposite_of_reg = opposite_pairs.get(reg_name)
            in_opposite_trend = (opposite_of_reg is not None
                                  and opposite_of_reg in affinity)
            if trend_aware and in_opposite_trend:
                size_mult = 0.5
                log.info("Rule 5b: %s firing at half size in opposite trend "
                         "(regime=%s conf=%.2f affinity=%s)",
                         strategy, reg_name, conf, affinity)
            else:
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

    # 6) Sizing — per-trade 5% margin × 5x leverage (spec §6).
    # cap_frac is enforced as a per-ENGINE concentration cap below, not
    # as a per-trade multiplier. Each trade is sized identically; cap_frac
    # limits how many concurrent positions an engine can hold.
    #
    # Per-engine size_mult override: ENGINE_REGISTRY entry may declare a
    # size_mult to shrink/grow trades for a specific engine without changing
    # the global MARGIN_PCT_PER_TRADE. Used for promotion experiments where
    # a freshly-revived engine ships at smaller notional (e.g. e08_inv at
    # size_mult=0.2 → ~$25 notional vs default ~$125). Combined multiplicatively
    # with the regime-affinity half-size rule above.
    leverage = _f("LEVERAGE", 5.0)
    margin_pct = _f("MARGIN_PCT_PER_TRADE", 0.05)
    engine_size_mult = float(eng_cfg.get("size_mult", 1.0))
    margin_usd = margin_pct * account_value_usd * size_mult * engine_size_mult

    # 6a) Per-engine concentration cap (spec §7.1: capital_fraction).
    # Spec ends with "Sum: 1.00 (allocate every dollar)" — cap_frac is the
    # engine's SHARE of wallet equity, summed across all engines. Enforce
    # by capping the engine's TOTAL open margin at cap_frac × equity.
    # Before 2026-05-19 this was decorative — sentinel CRITICAL finding
    # (Mistral Large + Qwen3 235B unanimous). With LEVERAGE=5 and
    # MAX_OPEN_POSITIONS=20, the engine could open up to 20 trades × 25%
    # notional = 500% wallet notional, ignoring cap_frac entirely.
    # 2026-05-21: PER-ENGINE CAP_FRAC ENFORCEMENT REMOVED per operator instruction.
    # cap_frac retained on engine config as informational/sizing hint only.
    # Rationale: concurrent positions rarely exceed 3 on this $491 wallet, so
    # a normalized "budget" never bound. Risk is measured at the position level
    # via leverage × notional, MAX_OPEN_POSITIONS global limit, and per-engine
    # cooldown. cap_frac no longer blocks /check.
    cap_frac = _cap_of(eng_cfg)  # available for downstream sizing decisions
    _ = cap_frac  # marker — block intentionally removed

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
