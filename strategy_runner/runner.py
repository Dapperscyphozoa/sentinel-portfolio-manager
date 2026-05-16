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
    """Lazy-import strategy modules. PRODUCTION DEPLOYMENT: only OOS-validated
    engines are loaded by default. Legacy strategies remain importable for
    backtesting but are not registered automatically.

    Order matters: registry order = first-fire arbitration order. OOS engines
    are loaded in PF-priority order (highest bt_PF first → wins ties).
    """
    # PRODUCTION — 11 OOS-validated engines in PF-priority order
    try:
        from .strategies.oos_engines import OOS_ENGINES
        for cls in OOS_ENGINES:
            register(cls)
        log.info("Loaded %d OOS engines: %s", len(OOS_ENGINES), [c.NAME for c in OOS_ENGINES])
    except Exception:
        log.exception("CRITICAL: failed to load OOS engines")

    # LEGACY — only loaded if STRATEGY_LEGACY_LOAD=1 env (default off)
    import os
    if os.environ.get("STRATEGY_LEGACY_LOAD", "0") == "1":
        log.warning("Loading legacy strategies — production should keep STRATEGY_LEGACY_LOAD=0")
        for modname in ("fsp", "range_fade", "range_breakout", "vsq", "fd1",
                        "lh1", "precog", "liq_cascade", "cex_dex_arb", "donchian"):
            try:
                mod = __import__(f"strategy_runner.strategies.{modname}", fromlist=["*"])
                for attr in dir(mod):
                    obj = getattr(mod, attr)
                    if (isinstance(obj, type) and issubclass(obj, StrategyBase)
                            and obj is not StrategyBase and obj.NAME):
                        register(obj)
            except ImportError:
                pass
            except Exception:
                log.exception("failed to load legacy strategy %s", modname)


def scan_once(bus: BusClient, pm: PMClient, on_signal) -> int:
    """One pass through every enabled strategy × its universe.

    Calls on_signal(strategy_cls, signal, pm_decision) for each (allowed) signal.
    Returns count of allowed signals.
    """
    if not REGISTRY:
        _load_registered()
    n = 0
    for strat in REGISTRY:
        if not config.strategy_enabled(strat.NAME):
            continue
        if halt.is_halted(strat.NAME):
            continue
        for coin in strat.UNIVERSE:
            try:
                sig = strat.evaluate(coin, bus)
            except Exception:
                log.exception("evaluate error %s/%s", strat.NAME, coin)
                continue
            if sig is None:
                continue
            try:
                decision = pm.check(strat.NAME, sig.to_dict())
            except Exception:
                log.exception("pm.check error %s/%s", strat.NAME, coin)
                continue
            if not decision.allow:
                log.info("pm denied %s/%s: %s", strat.NAME, coin, decision.reason)
                continue
            try:
                on_signal(strat, sig, decision)
                n += 1
            except Exception:
                log.exception("on_signal handler error %s/%s", strat.NAME, coin)
    return n


def registry_info() -> list[dict]:
    if not REGISTRY:
        _load_registered()
    return [s.info() for s in REGISTRY]
