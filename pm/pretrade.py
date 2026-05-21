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
    # ═══════════════════════════════════════════════════════════════════════
    # SLASHED 2026-05-21 per operator instruction:
    #   "I don't want anything paper. If it can't be fixed it needs to be
    #    killed. They should all be live or they should never have been
    #    sitting there."
    #
    # Every engine in this registry is OPERATOR-APPROVED LIVE with real
    # capital deployed via STRATEGY_<NAME>_LIVE=1 on Render.
    #
    # KILL DECISIONS (engines removed from registry on 2026-05-21):
    #   hl_settle_5m       (cap 0.16) -$2.54 net paper, trail-stop alone -$0.011/trade BT
    #   ict_confluence_4h  (cap 0.05) BT n=27 93% WR but 25/27 trades = APT-only regime artifact
    #   ict_confluence_1d  (cap 0.05) zero fires in 4.6d window
    #   e09_pump3d10_td_1d (cap 0.10) zero fires in 4.6d window
    #   e16_bb_fade_hv_1d  (cap 0.05) zero fires
    #   e01_zfade3s_tu_1d  (cap 0.05) zero fires
    #   e17_bb_fade_bt_1d  (cap 0.01) BT n=3 all-loss
    #   e07_zfade2s_tu_1d  (cap 0.02) zero fires
    #   e08_dip3d10_td_1d  (cap 0.00 killed earlier) BT -$10.60
    #   e07_zfade2s_tu_4h  (cap 0.06) zero fires
    #   e01_zfade3s_tu_4h  (cap 0.02) zero fires
    #   liq_cascade        (cap 0.05) n=2 thin
    #   e16_bb_fade_hv_4h  (cap 0.02) zero fires
    #   e17_bb_fade_bt_4h  (cap 0.00) BT n=7 all-loss
    #   donchian           (cap 0.00 stage-0) zero fires
    #   cex_dex_arb        (cap 0.00 dead) per SPEC v1.0 §4
    #   cascade_sniper_hl  (cap 0.00 stage-0) zero fires
    #   fmom               (cap 0.00) PF 0.59 negative paper
    #   hl_cvd_aggressor, funding_triangulation, liq_cluster_hunt,
    #   hl_whale_frontrun, hl_depth_shock, hl_vault_predict, hlp_decoder
    #     (all stage-0 world-first, never backtested, never fired)
    #   stop_hunt          (cap 0.02) zero fires
    #   vpoc_retest        (cap 0.03) zero fires
    #   oi_concentration   (cap 0.02) zero fires
    #
    # 27 engines killed.  3 engines remain.  cap_sum = 0.50 (~$245 of $491
    # wallet actively deployed across high-conviction strategies).
    # ═══════════════════════════════════════════════════════════════════════

    # ─── hlp_fade — only engine with LIVE positive PF ───
    # n=10 live paper, WR 40%, PF 1.39, net +$0.28.  Per-coin: NEAR drives
    # 240% of profit but engine fires across 7+ coins (NEAR, SEI, SUI, APT,
    # UNI, ATOM, JUP).  Operator-approved scale 0.025 → 0.20.
    "hlp_fade": {
        "affinity": ["trend_up", "trend_down", "range", "chop", "high_vol"],
        "bt_pf": 2.50, "cap_frac": 0.20,
    },

    # ─── uzt_rev — operator world-first, locked-ship config ───
    # BT n=41, WR 68%, PF 6.92, +1.71R/trade.  Single TP at 5R, signal SL,
    # 40-bar time stop, Asia hours blocked.  16-coin universe.
    # Operator-promoted LIVE 2026-05-21 (env var was previously unset).
    "uzt_rev": {
        "affinity": ["trend_up", "trend_down", "range", "chop"],
        "bt_pf": 6.92, "cap_frac": 0.20,
    },

    # ─── e08_dip3d7_td_4h_inv — inverted dip in trend_down ───
    # Honest re-test 2026-05-20: SHORT side 114/252 combos GREEN, walk-forward
    # OOS PF 2.88 WR 71.7%.  Operator-promoted LIVE 2026-05-20.
    # 7-coin universe (ARB GALA INJ OP ORDI PYTH WIF).
    "e08_dip3d7_td_4h_inv": {
        "affinity": ["trend_down"],
        "bt_pf": 2.88, "cap_frac": 0.10, "size_mult": 0.2,
    },
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
# Block OVER-allocation strictly at 1.0 (with 0.005 float-tolerance).
# Under-allocation is safe (idle capital); over-allocation puts the wallet
# at risk via leveraged notional. On a $491 wallet × 5x lev, cap_sum=1.0
# already permits ~$2,455 max notional — tightening further would limit
# legitimate full-allocation engines.
# 2026-05-19: tightened from 1.06 → 1.005 per operator decision (council
# Qwen3 Coder 480B A4 flag on the previous 1.06 upper bound).
assert _cap_sum < 1.005, f"cap_fracs sum to {_cap_sum:.3f} (over-allocated; hard cap 1.0)"

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
        # 5a) Permanent paper-demote check REMOVED 2026-05-21 (operator instruction
        # "remove the demote authority from pm"). Engine state is operator-controlled
        # only via STRATEGY_<NAME>_LIVE / _ENABLED env vars. The is_engine_demoted
        # check is no longer consulted — auto-demote can never fire because the
        # underlying counter no longer writes to engine_demotions (see common/cooldown.py).
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
    cap_frac = _cap_of(eng_cfg)
    if cap_frac > 0:
        # Engine's existing open margin from positions tagged with this engine.
        # open_positions records typically include 'strategy' or 'engine' tag;
        # fall back to coin-name match-free counting (yields 0 if untagged).
        engine_open_margin = 0.0
        for p in open_positions:
            tag = (p.get("strategy") or p.get("engine") or "").lower()
            if tag != strategy.lower():
                continue
            engine_open_margin += abs(float(
                p.get("margin", p.get("notional", 0) / leverage)
            ))
        engine_budget = cap_frac * account_value_usd
        if engine_open_margin + margin_usd > engine_budget:
            return CheckResult(
                False, 0.0,
                f"engine_cap_frac_exhausted:"
                f"{engine_open_margin:.2f}+{margin_usd:.2f}>"
                f"{engine_budget:.2f}",
            )

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
