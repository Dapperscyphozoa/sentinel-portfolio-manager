"""reflexivity_emitter — PM extremes → SPM signal feed.

Does NOT place PM trades. Watches PM 5m markets for sustained extremes
(>0.85 or <0.15 with ≥90s remaining) and surfaces them via a server
endpoint. The SPM strategy-runner subscribes to this feed and trades the
follow-through on HL perps.

The bus already exposes /reflex_signal/{asset} — this strategy module's
purpose is to (a) confirm the sustained-S threshold has been met for log
correlation analysis later, and (b) publish events into the poly DB for
audit trail.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import StrategyBase
from common.poly_persistence import connect_poly


class ReflexivityEmitter(StrategyBase):
    NAME = "reflexivity_emitter"
    CAPITAL_FRACTION = 0.0   # never trades PM directly
    REQUIRES_LIVE_CL = False

    PROB_HI = float(os.environ.get("RE_PROB_EXTREME_HIGH", "0.85"))
    PROB_LO = float(os.environ.get("RE_PROB_EXTREME_LOW", "0.15"))
    TIME_REM_MIN = int(os.environ.get("RE_TIME_REMAINING_MIN", "90"))
    SUSTAINED_S = int(os.environ.get("RE_SUSTAINED_S", "5"))

    # Class-level "state since" tracker per (asset, state)
    _state_start: dict[str, tuple[str, float]] = {}

    @classmethod
    def evaluate(cls, market: dict, bus) -> None:
        """No Signal returned (this strategy doesn't trade PM).

        Side effect: writes a poly_signals row of type=reflex for each
        sustained extreme detected. SPM's strategy-runner reads from the
        bus's /reflex_signal endpoint at scan time.
        """
        asset = market.get("asset")
        if asset not in ("BTC", "ETH"):
            return None
        end_ts = market.get("end_ts")
        if not end_ts:
            return None
        tr = end_ts - time.time()
        if tr < cls.TIME_REM_MIN:
            return None

        ip = bus.implied_prob(market["market_id"]) or {}
        ym = ip.get("yes_mid")
        if ym is None:
            return None

        if ym >= cls.PROB_HI:
            new_state = "extreme_up"
        elif ym <= cls.PROB_LO:
            new_state = "extreme_down"
        else:
            cls._state_start.pop(asset, None)
            return None

        prev = cls._state_start.get(asset)
        now = time.time()
        if not prev or prev[0] != new_state:
            cls._state_start[asset] = (new_state, now)
            return None
        elapsed = now - prev[1]
        if elapsed < cls.SUSTAINED_S:
            return None

        # Sustained extreme; log it
        try:
            conn = connect_poly()
            conn.execute(
                "INSERT INTO poly_signals(ts, strategy, market_id, asset, token, side,"
                " price, size_usdc, pm_implied, fire_reason, extras_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (now, cls.NAME, market["market_id"], asset, "YES", "REFLEX",
                 ym, 0.0, ym, f"{new_state} sustained {elapsed:.1f}s",
                 f'{{"state":"{new_state}","time_remaining":{tr:.0f},"elapsed":{elapsed:.1f}}}'),
            )
            conn.close()
        except Exception:
            pass
        return None
