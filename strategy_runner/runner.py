"""Strategy registry and scan dispatcher per SPEC §6.2."""
from __future__ import annotations

import logging
import time
from typing import Type

from common import config, halt
from common.bus_client import BusClient
from common.pm_client import PMClient

from .strategies._base import Signal, StrategyBase


log = logging.getLogger("runner")


REGISTRY: list[Type[StrategyBase]] = []


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

    # 1.6) Cascade Sniper (council 5/6 pick — Binance liq → HL execution)
    try:
        from .strategies.cascade_sniper import CascadeSniperHL
        register(CascadeSniperHL)
        log.info("Loaded cascade sniper: %s", CascadeSniperHL.NAME)
    except Exception:
        log.exception("failed to load cascade sniper")

    # 1.7) HLP Fade (council 5+ voters world-first pick — fade WITH HLP vault)
    try:
        from .strategies.hlp_fade import HLPFade
        register(HLPFade)
        log.info("Loaded hlp_fade: %s", HLPFade.NAME)
    except Exception:
        log.exception("failed to load hlp_fade")

    # 1.8) Funding Momentum (Tier 1 #2 — 2nd-derivative funding signal)
    try:
        from .strategies.fmom import FundingMomentum
        register(FundingMomentum)
        log.info("Loaded fmom: %s", FundingMomentum.NAME)
    except Exception:
        log.exception("failed to load fmom")

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

    # 1.12) OI Concentration (Tier 1 #6 — pre-cascade detector, v1 volume proxy)
    try:
        from .strategies.oi_concentration import OIConcentration
        register(OIConcentration)
        log.info("Loaded oi_concentration: %s", OIConcentration.NAME)
    except Exception:
        log.exception("failed to load oi_concentration")

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

    # 1.14) Funding Triangulation (Stage 1 #2 — HL vs CEX funding divergence)
    try:
        from .strategies.funding_triangulation import FundingTriangulation
        register(FundingTriangulation)
        log.info("Loaded funding_triangulation: %s", FundingTriangulation.NAME)
    except Exception:
        log.exception("failed to load funding_triangulation")

    # 1.15) Cross-Coin Z-Score (Stage 1 #3 — pair ratio mean-revert)
    try:
        from .strategies.cross_coin_zscore import CrossCoinZScore
        register(CrossCoinZScore)
        log.info("Loaded cross_coin_zscore: %s", CrossCoinZScore.NAME)
    except Exception:
        log.exception("failed to load cross_coin_zscore")

    # 1.16) Liq Cluster Hunt (Stage 1 #4 — predict sweep path from stacked clusters)
    try:
        from .strategies.liq_cluster_hunt import LiqClusterHunt
        register(LiqClusterHunt)
        log.info("Loaded liq_cluster_hunt: %s", LiqClusterHunt.NAME)
    except Exception:
        log.exception("failed to load liq_cluster_hunt")

    # 1.17) HL Whale Frontrun (Stage 1 #5 — world-first, highest single-engine edge)
    try:
        from .strategies.hl_whale_frontrun import HLWhaleFrontrun
        register(HLWhaleFrontrun)
        log.info("Loaded hl_whale_frontrun: %s", HLWhaleFrontrun.NAME)
    except Exception:
        log.exception("failed to load hl_whale_frontrun")

    # 1.18) HL Depth Shock (Stage 1 #6 — orderbook liquidity-eviction fade)
    try:
        from .strategies.hl_depth_shock import HLDepthShock
        register(HLDepthShock)
        log.info("Loaded hl_depth_shock: %s", HLDepthShock.NAME)
    except Exception:
        log.exception("failed to load hl_depth_shock")

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

    # 2) Remaining keepers — liq_cascade (sentinel-born), cex_dex_arb (paper),
    #    donchian (post-sentinel build). The 7 legacy ports (fsp, vsq,
    #    range_fade, range_breakout, lh1, fd1, precog) live in _archived/
    #    and are intentionally NOT imported here.
    import os
    if os.environ.get("STRATEGY_LEGACY_LOAD", "1") == "1":   # default ON for combined deployment
        legacy_loaded = []
        for modname in ("liq_cascade", "cex_dex_arb", "donchian"):
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
        for coin in strat.UNIVERSE:
            s["eval"] += 1
            try:
                sig = strat.evaluate(coin, bus)
            except Exception:
                s["err"] += 1
                log.exception("evaluate error %s/%s", strat.NAME, coin)
                continue
            if sig is None:
                s["none"] += 1
                continue
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
    return n


def registry_info() -> list[dict]:
    if not REGISTRY:
        _load_registered()
    return [s.info() for s in REGISTRY]
