"""hlp_decoder — Reverse-engineered signal from HLP's 4 sub-vaults.

Unlike hlp_fade (which uses HLP's aggregate NET position via z-score),
this strategy decodes the BEHAVIOR of each sub-vault separately.

The 4 known HLP sub-vaults (public addresses):
  master       0xdfc24b07…  net aggregate (info only)
  strategy_a   0x010461c1…  MM, short bias historically
  strategy_b   0x31ca8395…  MM, long bias historically
  liquidator   0x2e3d94f0…  takes whale-liq opposite side

SIGNAL HYPOTHESES (each toggleable via env):

  H-LIQ (most informative, default-on):
    The Liquidator opens a NEW position > $1M notional. This means a major
    whale just got force-closed. Liquidations cluster — fire trades in the
    SAME direction as the Liquidator (joining the post-cascade momentum).
    Env: HLP_DECODER_H_LIQ_ENABLED=1
         HLP_DECODER_H_LIQ_MIN_USD=1000000

  H-CONSENSUS (medium-frequency, default-on):
    Strategy A AND Strategy B BOTH shift in the same direction within a
    short window (default 5min). MMs normally hedge each other — same-
    direction shifts mean both detected directional flow worth exposure.
    Trade WITH their consensus direction.
    Env: HLP_DECODER_H_CONSENSUS_ENABLED=1
         HLP_DECODER_H_CONSENSUS_WINDOW_MS=300000

  H-FADE-MM (low-frequency, default-off):
    Strategy A or B's position notional > 80th percentile of its 7d
    distribution → MM is loaded; fade their direction. EXPERIMENTAL —
    requires per-vault history before enabling.
    Env: HLP_DECODER_H_FADE_MM_ENABLED=0

EXIT RULES (uniform across all three signal kinds):
  SL: -8% spot (close to HLP_FADE_SL_PCT but slightly tighter since these
       are higher-conviction signals)
  TP: +4% spot (faster targets — cascade-momentum doesn't sustain long)
  Max hold: 12h

UNIVERSE: same liquid universe as hl_settle_5m so we have execution depth.

PROMOTION PATH: cap_frac=0 paper-only until n>=20 trades with PF>=1.5.
After paper validation, operator promotes via registry edit.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


HD_LOOKBACK_MS = int(os.environ.get("HLP_DECODER_LOOKBACK_MS", "120000"))   # 2 min
HD_H_LIQ_ENABLED = os.environ.get("HLP_DECODER_H_LIQ_ENABLED", "1") == "1"
HD_H_LIQ_MIN_USD = float(os.environ.get("HLP_DECODER_H_LIQ_MIN_USD", "1000000"))
HD_H_CONSENSUS_ENABLED = os.environ.get("HLP_DECODER_H_CONSENSUS_ENABLED", "1") == "1"
HD_H_CONSENSUS_WINDOW_MS = int(os.environ.get("HLP_DECODER_H_CONSENSUS_WINDOW_MS", "300000"))
HD_H_FADE_MM_ENABLED = os.environ.get("HLP_DECODER_H_FADE_MM_ENABLED", "0") == "1"
HD_SL_PCT = float(os.environ.get("HLP_DECODER_SL_PCT", "0.08"))
HD_TP_PCT = float(os.environ.get("HLP_DECODER_TP_PCT", "0.04"))
HD_MAX_HOLD_H = int(os.environ.get("HLP_DECODER_MAX_HOLD_H", "12"))
HD_TF = os.environ.get("HLP_DECODER_TF", "5m")


class HlpDecoder(StrategyBase):
    NAME = "hlp_decoder"
    CLOID_PREFIX = "hpdec"
    AFFINITY = ["trend_up", "trend_down", "range", "chop", "high_vol"]
    TF = HD_TF
    # Liquid majors — needed for cascade momentum to translate to executable fills
    UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK",
                "LTC", "NEAR", "SUI", "APT", "ARB", "INJ"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Pull recent vault events
        try:
            since = int(time.time() * 1000) - HD_LOOKBACK_MS
            events = bus.hlp_vault_events(since_ms=since, coin=coin)
        except Exception:
            return None
        if not events:
            return None

        # Sort newest-first
        events.sort(key=lambda e: -e["ts"])

        # Reference price from recent candles
        try:
            bars = bus.candles(coin, HD_TF, n=4)
        except Exception:
            return None
        if not bars or len(bars) < 1:
            return None
        ref_px = float(bars[-1]["close"])
        if ref_px <= 0:
            return None

        # ─── H-LIQ ───────────────────────────────────────────────────────
        if HD_H_LIQ_ENABLED:
            for ev in events:
                if ev["vault_label"] != "liquidator":
                    continue
                if ev["kind"] not in ("OPEN", "GREW"):
                    continue
                if ev["ntl_usd"] < HD_H_LIQ_MIN_USD:
                    continue
                # Join the Liquidator's side. Direction of the absorbed-liq
                # position: Liquidator went LONG ETH means a SHORT whale got
                # liquidated → continuation is more longs absorbed → join long.
                is_long = bool(ev["is_long"])
                side = "B" if is_long else "A"
                sl_px = ref_px * (1 - HD_SL_PCT) if is_long else ref_px * (1 + HD_SL_PCT)
                tp_px = ref_px * (1 + HD_TP_PCT) if is_long else ref_px * (1 - HD_TP_PCT)
                max_hold_bars = HD_MAX_HOLD_H * 12  # 5m bars per hour = 12
                return Signal(
                    coin=coin, side=side, is_long=is_long,
                    ref_price=ref_px, sl_px=sl_px, tp_px=tp_px,
                    max_hold_bars=max_hold_bars,
                    fire_reason=f"hlp_h_liq:{ev['kind']}:liquidator_ntl=${ev['ntl_usd']:.0f}",
                    fire_ts=int(time.time() * 1000),
                    extras={
                        "hypothesis": "H-LIQ",
                        "vault_label": "liquidator",
                        "vault_kind": ev["kind"],
                        "vault_ntl_usd": ev["ntl_usd"],
                    },
                )

        # ─── H-CONSENSUS ─────────────────────────────────────────────────
        # Need at least one event each from strategy_a and strategy_b in
        # the consensus window, both in same direction.
        if HD_H_CONSENSUS_ENABLED:
            consensus_cutoff = int(time.time() * 1000) - HD_H_CONSENSUS_WINDOW_MS
            a_evs = [e for e in events
                     if e["vault_label"] == "strategy_a"
                     and e["ts"] >= consensus_cutoff
                     and e["kind"] in ("OPEN", "GREW")]
            b_evs = [e for e in events
                     if e["vault_label"] == "strategy_b"
                     and e["ts"] >= consensus_cutoff
                     and e["kind"] in ("OPEN", "GREW")]
            if a_evs and b_evs:
                # Newest event of each
                a = a_evs[0]
                b = b_evs[0]
                if a["is_long"] == b["is_long"]:
                    is_long = bool(a["is_long"])
                    side = "B" if is_long else "A"
                    sl_px = ref_px * (1 - HD_SL_PCT) if is_long else ref_px * (1 + HD_SL_PCT)
                    tp_px = ref_px * (1 + HD_TP_PCT) if is_long else ref_px * (1 - HD_TP_PCT)
                    return Signal(
                        coin=coin, side=side, is_long=is_long,
                        ref_price=ref_px, sl_px=sl_px, tp_px=tp_px,
                        max_hold_bars=HD_MAX_HOLD_H * 12,
                        fire_reason=f"hlp_h_consensus:a_ntl=${a['ntl_usd']:.0f}_b_ntl=${b['ntl_usd']:.0f}",
                        fire_ts=int(time.time() * 1000),
                        extras={
                            "hypothesis": "H-CONSENSUS",
                            "a_event": {"kind": a["kind"], "ntl": a["ntl_usd"]},
                            "b_event": {"kind": b["kind"], "ntl": b["ntl_usd"]},
                        },
                    )

        # ─── H-FADE-MM ───────────────────────────────────────────────────
        # EXPERIMENTAL — needs per-vault 7d history before reliable. For
        # now: if both MM vaults are overloaded same direction and the
        # position size is in extreme percentile (rough proxy: > $5M per
        # vault), fade against them. Operator opt-in only.
        if HD_H_FADE_MM_ENABLED:
            try:
                snap_a = bus.hlp_vault_snapshot("strategy_a")
                snap_b = bus.hlp_vault_snapshot("strategy_b")
            except Exception:
                return None
            pos_a = (snap_a.get("positions") or {}).get(coin, {})
            pos_b = (snap_b.get("positions") or {}).get(coin, {})
            ntl_a = abs(float(pos_a.get("ntl_usd", 0) or 0))
            ntl_b = abs(float(pos_b.get("ntl_usd", 0) or 0))
            szi_a = float(pos_a.get("szi", 0) or 0)
            szi_b = float(pos_b.get("szi", 0) or 0)
            # Proxy: each vault has >$5M same-direction position
            if (ntl_a > 5_000_000 and ntl_b > 5_000_000
                    and szi_a != 0 and szi_b != 0
                    and (szi_a > 0) == (szi_b > 0)):
                # Fade their direction
                mm_long = szi_a > 0
                is_long = not mm_long
                side = "B" if is_long else "A"
                sl_px = ref_px * (1 - HD_SL_PCT) if is_long else ref_px * (1 + HD_SL_PCT)
                tp_px = ref_px * (1 + HD_TP_PCT) if is_long else ref_px * (1 - HD_TP_PCT)
                return Signal(
                    coin=coin, side=side, is_long=is_long,
                    ref_price=ref_px, sl_px=sl_px, tp_px=tp_px,
                    max_hold_bars=HD_MAX_HOLD_H * 12,
                    fire_reason=f"hlp_h_fade_mm:ntl_a=${ntl_a:.0f}_ntl_b=${ntl_b:.0f}",
                    fire_ts=int(time.time() * 1000),
                    extras={
                        "hypothesis": "H-FADE-MM",
                        "mm_long": mm_long,
                        "ntl_a": ntl_a, "ntl_b": ntl_b,
                    },
                )

        return None
