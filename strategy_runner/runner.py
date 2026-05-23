"""Strategy registry and scan dispatcher per SPEC §6.2."""
from __future__ import annotations

import logging
import os
import time
from typing import Type

from common import config, halt
from common.bus_client import BusClient
from common.pm_client import PMClient

from .strategies._base import Signal, StrategyBase


log = logging.getLogger("runner")


REGISTRY: list[Type[StrategyBase]] = []

# Per-(engine, coin) most-recent signal fire timestamp. Used by scan_once to
# suppress duplicate signal rows when an engine's setup persists across many
# scan cycles. Lives in memory only (acceptable — restart clears cooldowns,
# which is the safe direction; we'd rather re-fire after restart than be
# silently muted from a stale cooldown).
_SIGNAL_FIRE_LAST: dict[tuple[str, str], float] = {}


def _tf_to_seconds(tf: str) -> int:
    """Convert engine TF tag ('1m','5m','15m','1h','4h','1d') to seconds.
    Used to scale signal-cooldown to engine timeframe.
    Unknown TF → conservative 5min default (so cooldown never goes below floor).
    """
    if not tf:
        return 300
    tf = tf.lower().strip()
    try:
        if tf.endswith("m"):
            return int(tf[:-1]) * 60
        if tf.endswith("h"):
            return int(tf[:-1]) * 3600
        if tf.endswith("d"):
            return int(tf[:-1]) * 86400
    except (ValueError, IndexError):
        pass
    return 300


def register(*classes: Type[StrategyBase]) -> None:
    for c in classes:
        if c.NAME and c not in REGISTRY:
            REGISTRY.append(c)


def _load_registered() -> None:
    """Lazy-import strategy modules. COMBINED DEPLOYMENT: 9 legacy strategies +
    11 OOS-validated engines load together (per operator: combine with already-live
    system). All routed through unified PM gate (coin lock + regime + cooldowns).

    Registry order = first-fire arbitration. OOS engines load first (higher PF),
    then legacy strategies fill remaining regimes.
    """
    # 1) OOS-validated engines first (PF-priority order)
    try:
        from .strategies.oos_engines import OOS_ENGINES
        for cls in OOS_ENGINES:
            register(cls)
        log.info("Loaded %d OOS engines: %s", len(OOS_ENGINES), [c.NAME for c in OOS_ENGINES])
    except Exception:
        log.exception("CRITICAL: failed to load OOS engines")

    # 1.5) ICT Confluence (live deploy with tightened safety per council)
    try:
        from .strategies.ict_confluence import ICT_Confluence_4h, ICT_Confluence_1d
        register(ICT_Confluence_4h)
        register(ICT_Confluence_1d)
        log.info("Loaded ICT confluence engines: %s",
                 [ICT_Confluence_4h.NAME, ICT_Confluence_1d.NAME])
    except Exception:
        log.exception("failed to load ICT confluence engines")

    # 1.6) Cascade Sniper — ARCHIVED 2026-05-19. Never validated (no backtest,
    # halted, PF 0.00). Module moved to _archived/. Do not reactivate without
    # honest backtest + paper validation.

    # 1.7) HLP Fade (council 5+ voters world-first pick — fade WITH HLP vault)
    try:
        from .strategies.hlp_fade import HLPFade
        register(HLPFade)
        log.info("Loaded hlp_fade: %s", HLPFade.NAME)
    except Exception:
        log.exception("failed to load hlp_fade")

    # 1.8) Funding Momentum (fmom) — REMOVED 2026-05-23 (operator kill directive,
    #       dashboard cleanup). Live n=20 WR 40% net -$0.86 over operational window.
    #       Module moved to _archived/fmom.py. Closures archived to
    #       legacy-data/fmom_closures.json. Do not reactivate without honest
    #       backtest + sentinel-validated edge.

    # 1.10) Stop Hunt Rejection (Tier 1 #4 — wick-reject at swept S/R)
    try:
        from .strategies.stop_hunt import StopHunt
        register(StopHunt)
        log.info("Loaded stop_hunt: %s", StopHunt.NAME)
    except Exception:
        log.exception("failed to load stop_hunt")

    # 1.11) VPOC Retest (Tier 1 #5 — naked weekly POC retest, top-5 only)
    try:
        from .strategies.vpoc_retest import VPOCRetest
        register(VPOCRetest)
        log.info("Loaded vpoc_retest: %s", VPOCRetest.NAME)
    except Exception:
        log.exception("failed to load vpoc_retest")

    # 1.12) OI Concentration — REMOVED 2026-05-23 (operator kill directive).
    #       Live n=2 WR 0% net -$0.03 — never produced a winning trade.
    #       Module moved to _archived/oi_concentration.py. Closures archived
    #       to legacy-data/oi_concentration_closures.json.

    # 1.9) HL Hourly Funding Boundary (Tier 1 #3 — only HL has hourly funding)
    try:
        from .strategies.hl_settle_5m import HLSettle5m
        register(HLSettle5m)
        log.info("Loaded hl_settle_5m: %s", HLSettle5m.NAME)
    except Exception:
        log.exception("failed to load hl_settle_5m")

    # 1.13) HL CVD Aggressor (Stage 1 — world-first HL CVD edge per council 4/4)
    try:
        from .strategies.hl_cvd_aggressor import HLCVDAggressor
        register(HLCVDAggressor)
        log.info("Loaded hl_cvd_aggressor: %s", HLCVDAggressor.NAME)
    except Exception:
        log.exception("failed to load hl_cvd_aggressor")

    # 1.14) Funding Triangulation — REMOVED 2026-05-22 (sentinel CRITICAL 66%,
    #       same 8h-settlement-lag structural flaw as killed fd1; live n=14 WR 29%
    #       net -$0.69 after threshold doubled + 6-coin denylist failed to fix.
    #       Closures archived to legacy-data/funding_triangulation_closures.json)

    # 1.15) Cross-Coin Z-Score — KILLED 2026-05-19 (sentinel CRITICAL unanimous,
    #       honest backtest PF 0.99 over 90d × 10 pairs, thesis broken, see SPEC §4)

    # 1.16) Liq Cluster Hunt — REMOVED 2026-05-23 (operator kill directive).
    #       Live n=1 WR 0% net -$0.17 — single trade losing. Paper status,
    #       never promoted. Module moved to _archived/liq_cluster_hunt.py.
    #       Closures archived to legacy-data/liq_cluster_hunt_closures.json.

    # 1.17) hl_whale_frontrun — REMOVED 2026-05-23 (operator kill: live n=6
    #       WR 16.7% net -$1.20, 5L 1W). World-first thesis didn't hold:
    #       whale moves were either too fast to frontrun or signals were noise.
    #       Module moved to _archived/. Closures archived to
    #       legacy-data/hl_whale_frontrun_closures.json.

    # 1.18) hl_depth_shock — REMOVED 2026-05-22. n=9 WR 22% PF 0.32 net -$0.69.
    #       Widening params to observe was rejected by operator: dead engines
    #       take up registry/cognitive space; remove rather than nurse.
    #       See legacy-data/hl_depth_shock_closures.json + BLEEDER_TRIAGE.md.

    # 1.19) HL Vault Predict (Stage 1 #7 — HLP rebalance anticipation)
    try:
        from .strategies.hl_vault_predict import HLVaultPredict
        register(HLVaultPredict)
        log.info("Loaded hl_vault_predict: %s", HLVaultPredict.NAME)
    except Exception:
        log.exception("failed to load hl_vault_predict")

    # 1.13) UZT — Unified Zone Trading (Lesson #2 framework).
    # STATUS: PROVISIONAL / DISABLED by default. v1 implementation failed §1.5
    # honest backtest gate (30d × 4 majors via OKX: n=21, WR 14.3%, PF 0.18,
    # 0 TP hits, 12 SL hits, 9 timeouts — RED per gate rules). Loaded so the
    # registry knows about it, but auto-skipped at scan time unless explicitly
    # re-enabled via STRATEGY_UZT_ENABLED=1 after parameter retuning + re-backtest.
    # See backtests/uzt_*.md and references/uzt_postmortem.md.
    import os as _os
    if _os.environ.get("STRATEGY_UZT_ENABLED", "0") == "1":
        try:
            from .strategies.uzt import UZT
            register(UZT)
            log.warning("Loaded uzt (PROVISIONAL, opt-in via STRATEGY_UZT_ENABLED=1)")
        except Exception:
            log.exception("failed to load uzt")
    else:
        log.info("uzt skipped (PROVISIONAL — set STRATEGY_UZT_ENABLED=1 to load)")

    # 1.14) UZT_REV — v3 ship config (REVERSAL-only, single 5R TP, Asia
    # filter, 16-coin tier-1 universe). Backtest 120d × 30 top-30: n=41,
    # WR 68.3%, PF 6.92, +1.707R/trade, +69.97R total. Consistency across
    # 90d×20 / 120d×20 / 120d×30 samples (PF 5.18 → 5.69 → 6.92).
    # Deploy paper-first: STRATEGY_UZT_REV_ENABLED=1, STRATEGY_UZT_REV_LIVE=0.
    if _os.environ.get("STRATEGY_UZT_REV_ENABLED", "0") == "1":
        try:
            from .strategies.uzt_rev import UZT_REV
            register(UZT_REV)
            log.warning("Loaded uzt_rev v3 (PROVISIONAL, "
                        "STRATEGY_UZT_REV_ENABLED=1, "
                        "live=%s)",
                        _os.environ.get("STRATEGY_UZT_REV_LIVE", "0"))
        except Exception:
            log.exception("failed to load uzt_rev")
    else:
        log.info("uzt_rev skipped (set STRATEGY_UZT_REV_ENABLED=1 to load)")

    # 1.15) hlp_decoder — reverse-engineered signal from 4 HLP sub-vaults.
    # Consumes hlp_decoder_poller's per-vault deltas. Three signal kinds
    # (H-LIQ, H-CONSENSUS, H-FADE-MM) selectable via env. Free-rate-cost
    # engine (poller uses 96 weight/min total). Cap_frac=0 (paper) until
    # honest backtest validates.
    try:
        from .strategies.hlp_decoder import HlpDecoder
        register(HlpDecoder)
        log.info("Loaded hlp_decoder: %s", HlpDecoder.NAME)
    except Exception:
        log.exception("failed to load hlp_decoder")

    # 2) Remaining keepers — donchian (post-sentinel build). liq_cascade
    #    REMOVED 2026-05-23 (operator kill: n=2 WR 50% net -$0.03 — too sparse
    #    to keep, closures archived to legacy-data/liq_cascade_closures.json).
    #    cex_dex_arb ARCHIVED 2026-05-19 (look-ahead bias, PF 0.00). The 7
    #    earlier legacy ports (fsp, vsq, range_fade, range_breakout, lh1, fd1,
    #    precog) live in _archived/ and are intentionally NOT imported here.
    import os
    if os.environ.get("STRATEGY_LEGACY_LOAD", "1") == "1":   # default ON for combined deployment
        legacy_loaded = []
        for modname in ("donchian",):  # liq_cascade REMOVED 2026-05-23 (n=2 WR 50% net -$0.03, operator kill)
            try:
                mod = __import__(f"strategy_runner.strategies.{modname}", fromlist=["*"])
                for attr in dir(mod):
                    obj = getattr(mod, attr)
                    if (isinstance(obj, type) and issubclass(obj, StrategyBase)
                            and obj is not StrategyBase and obj.NAME):
                        register(obj)
                        legacy_loaded.append(obj.NAME)
            except ImportError:
                pass
            except Exception:
                log.exception("failed to load legacy strategy %s", modname)
        log.info("Loaded %d legacy strategies: %s", len(legacy_loaded), legacy_loaded)
    log.info("REGISTRY total: %d strategies (first-fire order)", len(REGISTRY))

    # ── Boot-time enabled-vs-registered sanity check (council 2026-05-18) ──
    # Caught a 48h silent outage where 29/30 engines were disabled because
    # their STRATEGY_<NAME>_ENABLED env vars never got set. Defaulting to False
    # silently swallowed the misconfiguration. This check makes it loud.
    if REGISTRY:
        enabled_engines = [s.NAME for s in REGISTRY if config.strategy_enabled(s.NAME)]
        disabled_engines = [s.NAME for s in REGISTRY if not config.strategy_enabled(s.NAME)]
        log.info("BOOT_GATE registered=%d enabled=%d disabled=%d",
                 len(REGISTRY), len(enabled_engines), len(disabled_engines))
        if len(enabled_engines) == 0:
            log.critical(
                "BOOT_GATE CRITICAL: 0 engines enabled. Set STRATEGY_<NAME>_ENABLED=1 "
                "for at least one of: %s", [s.NAME for s in REGISTRY[:8]] + ["..."])
        elif len(disabled_engines) > 0:
            log.warning("BOOT_GATE %d engines registered but DISABLED via env: %s",
                        len(disabled_engines), disabled_engines)


def scan_once(bus: BusClient, pm: PMClient, on_signal, trader=None) -> int:
    """One pass through every enabled strategy × its universe.

    Calls on_signal(strategy_cls, signal, pm_decision) for each (allowed) signal.
    Returns count of allowed signals.

    If `trader` is provided, a synchronous local coin-lock pre-check runs
    BEFORE pm.check. This is the 1_GLOBAL coin lock's fast path — query the
    local SQLite trades table for any open/pending position (or recent
    open_failed within cooldown) on the candidate coin and skip silently if
    found. Eliminates the rapid re-fire / HL-hammer failure mode that the
    sentinel council flagged (2026-05-17 audit).
    """
    if not REGISTRY:
        _load_registered()
    n = 0
    # Per-engine counters: name → {eval, none, sig, locked, denied, err}
    stats: dict[str, dict] = {}
    enabled_count = 0
    disabled_count = 0
    halted_count = 0
    for strat in REGISTRY:
        if not config.strategy_enabled(strat.NAME):
            disabled_count += 1
            continue
        if halt.is_halted(strat.NAME):
            halted_count += 1
            continue
        enabled_count += 1
        s = stats.setdefault(strat.NAME, {"eval": 0, "none": 0, "sig": 0,
                                            "locked": 0, "denied": 0, "err": 0})
        # 2026-05-22: per-engine coin denylist via env var <NAME_UPPER>_COIN_DENYLIST
        # Comma-separated coin symbols. Lets operator deny bleeding coins per engine
        # at runtime without code changes. Read once per scan cycle for efficiency.
        # Empty string = no denylist (default). Coins matched case-insensitively.
        _deny_env = os.environ.get(f"{strat.NAME.upper()}_COIN_DENYLIST", "")
        _denyset = (
            {c.strip().upper() for c in _deny_env.split(",") if c.strip()}
            if _deny_env else set()
        )
        for coin in strat.UNIVERSE:
            s["eval"] += 1
            if _denyset and coin.upper() in _denyset:
                s["none"] += 1
                continue
            try:
                sig = strat.evaluate(coin, bus)
            except Exception:
                s["err"] += 1
                log.exception("evaluate error %s/%s", strat.NAME, coin)
                continue
            if sig is None:
                s["none"] += 1
                continue

            # ─── Per-(engine, coin) signal cooldown (2026-05-21) ───
            # Operator: don't generate duplicate signals on the same (engine,
            # coin) when the setup persists across multiple scan cycles. Without
            # this, e17_bb_fade_bt_4h/SUI was producing 5 signal rows over 25min
            # while the BB-break condition held, polluting the signals table
            # and the dashboard.
            #
            # Cooldown = max(4 bars of engine TF, 5 min). On a 4h-TF engine,
            # this is 16 hours; on 1m engine, 5 min minimum floor. Cooldown is
            # purely about signal-row dedup — coin-lock at trader.open already
            # blocks the actual order. This just stops repeated row writes.
            now_s = time.time()
            cd_key = (strat.NAME, coin)
            tf_sec = _tf_to_seconds(getattr(strat, "TF", "5m"))
            cooldown_s = max(tf_sec * 4, 300)
            last_fire = _SIGNAL_FIRE_LAST.get(cd_key, 0.0)
            if now_s - last_fire < cooldown_s:
                s.setdefault("cooldown", 0)
                s["cooldown"] += 1
                continue
            _SIGNAL_FIRE_LAST[cd_key] = now_s

            s["sig"] += 1
            # ─── 1_GLOBAL coin-lock pre-check (synchronous, in-process) ───
            # Skip the (HTTP) pm.check round-trip entirely if coin is already
            # locked locally. Cheap indexed SQLite lookup.
            if trader is not None:
                try:
                    locked, reason = trader.is_coin_locked(coin)
                except Exception:
                    locked, reason = False, ""
                    log.exception("is_coin_locked check failed %s/%s", strat.NAME, coin)
                if locked:
                    s["locked"] += 1
                    continue
            try:
                decision = pm.check(strat.NAME, sig.to_dict())
            except Exception:
                s["err"] += 1
                log.exception("pm.check error %s/%s", strat.NAME, coin)
                continue
            if not decision.allow:
                s["denied"] += 1
                log.info("pm denied %s/%s: %s", strat.NAME, coin, decision.reason)
                continue
            try:
                on_signal(strat, sig, decision)
                n += 1
            except Exception:
                s["err"] += 1
                log.exception("on_signal handler error %s/%s", strat.NAME, coin)

    # Per-scan summary
    log.info(
        "scan_summary registry=%d enabled=%d disabled=%d halted=%d allowed_fires=%d",
        len(REGISTRY), enabled_count, disabled_count, halted_count, n,
    )
    # Per-engine breakdown — one line each, easy to grep
    for name, s in stats.items():
        log.info(
            "scan_engine %s eval=%d none=%d sig=%d locked=%d denied=%d err=%d",
            name, s["eval"], s["none"], s["sig"], s["locked"], s["denied"], s["err"],
        )
    # Stash last-scan stats for admin endpoint visibility (2026-05-21).
    # Goes-silent post-fix debugging — without this we have no way to tell
    # if engines are evaluating but returning None vs not running at all.
    global LAST_SCAN_STATS
    LAST_SCAN_STATS = {
        "ts": time.time(),
        "registry": len(REGISTRY),
        "enabled": enabled_count,
        "disabled": disabled_count,
        "halted": halted_count,
        "allowed_fires": n,
        "per_engine": stats,
    }
    return n


# Module-level tracker for /admin/scan_stats endpoint
LAST_SCAN_STATS: dict = {"ts": 0, "registry": 0, "enabled": 0, "per_engine": {}}


def registry_info() -> list[dict]:
    if not REGISTRY:
        _load_registered()
    return [s.info() for s in REGISTRY]
