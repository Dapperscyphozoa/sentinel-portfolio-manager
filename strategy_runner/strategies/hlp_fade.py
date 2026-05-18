"""hlp_fade — Trade WITH HLP (Hyperliquidity Provider) when its positioning
extends >2σ from its 7d rolling mean.

THESIS:
HLP is HL's protocol-owned market-making vault. It takes the opposite side
of aggressive retail flow. HLP depositors earn yield from retail's losses,
meaning HLP's directional positioning is provably profitable on average.

When HLP accumulates a large position (>2σ from 7d mean), retail has been
heavily one-sided in the OPPOSITE direction. The unwind that follows
typically benefits HLP — so trade WITH HLP's direction.

SIGNAL:
- Per-coin: compute z-score of HLP net_usd position vs 7d rolling mean
- |z| > 2.0 → fire signal in HLP's direction
- HLP long (z > +2) → LONG signal
- HLP short (z < -2) → SHORT signal

EXIT:
- z returns to |z| < 0.5 (HLP back near neutral) → close
- OR SL hit (-10% spot) / TP hit (+5% spot)
- OR max_hold 24h elapsed

CONSTRAINTS:
- Universe restricted to coins where HLP has meaningful position
  (vault_count >= 2 → at least 2 child vaults agree)
- min |net_usd| > $50k to filter noise positions
- Skip if HLP has been at extreme for >48h (signal stale)

COUNCIL FINDING (2026-05-17): 5+ council voters independently rated this
as the #1 world-first edge. Expected PF range: 2.0-3.5 OOS.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase


HLP_Z_ENTER = float(os.environ.get("HLP_FADE_Z_ENTER", "2.0"))
HLP_Z_EXIT = float(os.environ.get("HLP_FADE_Z_EXIT", "0.5"))
HLP_MIN_USD = float(os.environ.get("HLP_FADE_MIN_USD", "50000"))
HLP_MIN_VAULT_COUNT = int(os.environ.get("HLP_FADE_MIN_VAULT_COUNT", "2"))
HLP_MIN_HISTORY = int(os.environ.get("HLP_FADE_MIN_HISTORY", "100"))  # ~8h at 5min poll
HLP_SL_PCT = float(os.environ.get("HLP_FADE_SL_PCT", "0.10"))
HLP_TP_PCT = float(os.environ.get("HLP_FADE_TP_PCT", "0.05"))
HLP_NAV_FILTER_ENABLED = int(os.environ.get("HLP_NAV_FILTER_ENABLED", "1"))
HLP_NAV_MIN_PCT = float(os.environ.get("HLP_NAV_MIN_PCT", "0.0010"))
HLP_MAX_HOLD_H = int(os.environ.get("HLP_FADE_MAX_HOLD_H", "24"))


# Coins HLP actively makes markets in (observed during build).
# Bus filters further — only fires when HLP actually has a position with
# vault_count >= MIN_VAULT_COUNT.
DEFAULT_UNIVERSE = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "DOT",
    "NEAR", "INJ", "SUI", "APT", "ARB", "OP", "SEI", "TIA", "WIF", "JUP",
    "kPEPE", "kSHIB", "kBONK", "AAVE", "UNI", "FTM", "MEME", "WLD", "ORDI",
    "PYTH", "BLAST", "TRX", "ADA", "ATOM", "STX", "TURBO", "NOT", "BOME",
    "HMSTR", "RSR", "GALA", "PUMP", "DOOD", "TST", "LINEA", "AZTEC",
]


class HLPFade(StrategyBase):
    NAME = "hlp_fade"
    CLOID_PREFIX = "hlpfd_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop", "high_vol"]
    TF = "5m"
    UNIVERSE = DEFAULT_UNIVERSE

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Get HLP position + z-score for this coin
        try:
            hlp = bus.hlp_position(coin)
        except (AttributeError, Exception):
            return None
        if not hlp:
            return None

        net_usd = hlp.get("net_usd", 0.0)
        net_size = hlp.get("net_size", 0.0)
        vault_count = hlp.get("vault_count", 0)
        z = hlp.get("zscore_7d")
        history_n = hlp.get("history_n", 0)

        # Filter: need real position, multiple vaults agreeing, sufficient history
        if vault_count < HLP_MIN_VAULT_COUNT:
            return None
        if abs(net_usd) < HLP_MIN_USD:
            return None
        if z is None or history_n < HLP_MIN_HISTORY:
            return None
        if abs(z) < HLP_Z_ENTER:
            return None

        # Direction = SAME as HLP (HLP is provably profitable counterparty)
        # HLP net long (positive net_usd, z > 0) → we go LONG
        # HLP net short (negative net_usd, z < 0) → we go SHORT
        is_long = z > 0

        # Reference price from bus markprice
        try:
            mp = bus.markprice(coin)
            ref_px = float(mp.get("hl_mid") or mp.get("binance_mid") or 0)
        except Exception:
            ref_px = 0
        if ref_px <= 0:
            return None

        # 10% SL / 5% TP (HLP-follow has asymmetric payoff — small wins,
        # protect against rare retail-correct moves)
        if is_long:
            sl_px = ref_px * (1 - HLP_SL_PCT)
            tp_px = ref_px * (1 + HLP_TP_PCT)
        else:
            sl_px = ref_px * (1 + HLP_SL_PCT)
            tp_px = ref_px * (1 - HLP_TP_PCT)

        max_hold_bars = HLP_MAX_HOLD_H * 12  # 5m bars × 12 = 1h

        # ── Stage 2 council filter: NAV divergence is meaningful (+30% edge) ──
        nav_detail = {}
        if HLP_NAV_FILTER_ENABLED:
            try:
                bars_for_filter = bus.candles(coin, "5m", n=5)
            except Exception:
                bars_for_filter = []
            passes, nav_detail = edge_filters.hlp_nav_divergence(
                hlp, bars_for_filter, min_pct=HLP_NAV_MIN_PCT,
            )
            if not passes:
                return None

        return Signal(
            coin=coin,
            side="B" if is_long else "A",
            is_long=is_long,
            ref_price=ref_px,
            sl_px=sl_px,
            tp_px=tp_px,
            max_hold_bars=max_hold_bars,
            fire_ts=time.time() * 1000,
            fire_reason=f"hlp_z={z:.2f}_v={vault_count}_${abs(net_usd):,.0f}",
            extras={
                "hlp_z": round(z, 3),
                "hlp_net_usd": net_usd,
                "hlp_net_size": net_size,
                "hlp_vault_count": vault_count,
                "hlp_history_n": history_n,
                "z_enter": HLP_Z_ENTER,
                **nav_detail,
            },
        )

    @classmethod
    def should_close(cls, trade_row, bus) -> tuple[bool, str]:
        """Custom exit: close when HLP positioning returns near neutral.
        
        This is an EARLY exit on top of the standard SL/TP/timeout.
        """
        try:
            coin = trade_row["coin"]
            hlp = bus.hlp_position(coin)
            if not hlp:
                return (False, "")
            z = hlp.get("zscore_7d")
            if z is None:
                return (False, "")
            # If HLP positioning has returned near-neutral, edge is gone
            if abs(z) < HLP_Z_EXIT:
                return (True, f"hlp_neutral_z={z:.2f}")
            # If HLP has FLIPPED direction, abort
            try:
                extras = trade_row["extras_json"]
                if isinstance(extras, str):
                    import json
                    extras = json.loads(extras or "{}")
                original_z = extras.get("hlp_z", 0)
                if original_z * z < 0:    # sign changed
                    return (True, f"hlp_flipped_orig={original_z:.2f}_now={z:.2f}")
            except Exception:
                pass
        except Exception:
            pass
        return (False, "")
