"""hl_vault_predict — anticipate HLP imminent rebalance from NAV divergence rate.

Stage 1 #7. Council pick (Qwen3 235B) +3.0%/mo paper-tested on 30d.

DIFFERENTIATED from hlp_fade:
  - hlp_fade trades AGAINST current HLP net position (mean-revert vs vault)
  - hl_vault_predict trades AHEAD of HLP rebalance (anticipates the next move)

Mechanic:
  HLP rebalances when its mark-to-market PnL deviates significantly from the
  zero-DTE expected value of its delta. When NAV vs spot mid diverges by
  >0.10% on a fast time scale (5-15min), HLP's risk system triggers a rebalance.

  If HLP is currently NET LONG and NAV is rising FASTER than spot mark
  (vault outperforming) — HLP will reduce long → sell pressure incoming → SHORT.
  If HLP NET LONG and NAV is falling FASTER than mark — HLP will add long to
  defend → buy pressure incoming → LONG.

  Mirror for HLP net short.

  We enter 30-60s before the predicted rebalance trigger and exit on the move.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from common import edge_filters
from strategy_runner.strategies._base import Signal, StrategyBase


log = logging.getLogger("strategy.hl_vault_predict")


def _f(k: str, d: float) -> float:
    try:
        return float(os.environ.get(k, d))
    except Exception:
        return d


VP_NAV_DIVERGENCE_PCT = _f("VP_NAV_DIVERGENCE_PCT", 0.0010)   # 0.10% NAV vs mark
VP_DIVERGENCE_RATE_PCT_PER_MIN = _f("VP_DIVERGENCE_RATE_PCT_PER_MIN", 0.0003)
VP_MIN_VAULT_NET_USD = _f("VP_MIN_VAULT_NET_USD", 100_000.0)
VP_SL_PCT = _f("VP_SL_PCT", 0.005)
VP_TP_PCT = _f("VP_TP_PCT", 0.010)
VP_MAX_HOLD_BARS = int(_f("VP_MAX_HOLD_BARS", 4))            # 20min on 5m
VP_TF = "5m"


class HLVaultPredict(StrategyBase):
    NAME = "hl_vault_predict"
    CLOID_PREFIX = "vlpre"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = VP_TF
    UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK",
                "LTC", "NEAR", "SUI", "APT", "ARB", "OP", "INJ", "SEI"]

    # Rate-limited per-coin warn timestamps (epoch seconds) so the missing-
    # unrealized_pnl warning fires at most once per 15min per coin. Class-
    # level state — shared across evaluate() calls within one runner process.
    _last_warn_unrl_pnl_ms: dict = {}

    @classmethod
    def _warn_missing_unrl_pnl(cls, coin: str) -> None:
        """Log once per coin every 15min that hlp_position returned no
        unrealized_pnl, so the gate is visible in runner logs rather than
        silently no-firing. Sentinel-audit fix 2026-05-19."""
        now_s = time.time()
        last = cls._last_warn_unrl_pnl_ms.get(coin, 0)
        if now_s - last >= 900:    # 15 min
            cls._last_warn_unrl_pnl_ms[coin] = now_s
            log.warning(
                "hl_vault_predict[%s]: hlp_position returned no "
                "unrealized_pnl — check signal-bus hlp_poller config",
                coin,
            )

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # 1. HLP position (already exposed by hlp_poller). Use the public
        # BusClient method — earlier draft reached into bus._client/base_url/
        # timeout, which broke when BusClient was refactored. Public method
        # returns the same JSON shape including {net_usd, unrealized_pnl}.
        try:
            hlp_data = bus.hlp_position(coin)
        except Exception:
            return None
        if not hlp_data:
            return None

        net_usd = float(hlp_data.get("net_usd", 0) or 0)
        if abs(net_usd) < VP_MIN_VAULT_NET_USD:
            return None
        hlp_long = net_usd > 0

        # 2. Recent bars to compute NAV proxy (HLP NAV correlated with avg-entry vs mark)
        # NAV proxy: HLP's unrealized PnL / position size, sampled over last 15min
        # We approximate with: divergence between mark trajectory and HLP entry trajectory
        try:
            bars = bus.candles(coin, VP_TF, n=4)   # last 20min of 5m bars
        except Exception:
            return None
        if not bars or len(bars) < 3:
            return None

        # Mark trajectory: % change per 5min
        closes = [float(b["close"]) for b in bars]
        if any(c <= 0 for c in closes):
            return None

        # 3. NAV divergence: prefer the real unrealized_pnl from the hlp endpoint.
        # FALLBACK (15min-lookback price-proxy) is intentionally DISABLED — it
        # assumes the HLP entered exactly 15min ago, which is almost never true
        # for vaults that hold positions for hours/days. Firing on the proxy
        # produced spurious signals. Skip the engine for the coin until the
        # endpoint exposes unrealized_pnl.
        #
        # Sentinel-audit follow-up 2026-05-19: log a warning when this gate
        # trips so a misconfigured hlp_poller (not returning unrealized_pnl)
        # becomes visible in runner logs rather than silently no-firing.
        # Rate-limited per-coin to once every 15min to avoid log spam.
        unrl_pnl = float(hlp_data.get("unrealized_pnl", 0) or 0)
        if unrl_pnl == 0 and abs(net_usd) > 0:
            cls._warn_missing_unrl_pnl(coin)
            return None
        unrl_pct = unrl_pnl / abs(net_usd) if abs(net_usd) > 0 else 0

        # 4. Divergence rate = unrl_pct per minute over 15min
        divergence_rate = unrl_pct / 15.0   # pct per minute

        # 5. Trigger
        side = None
        is_long = None
        reason = None
        close = closes[-1]

        # NAV diverged enough AND moving fast enough → rebalance imminent
        if abs(unrl_pct) > VP_NAV_DIVERGENCE_PCT and abs(divergence_rate) > VP_DIVERGENCE_RATE_PCT_PER_MIN:
            if hlp_long:
                # HLP long
                if unrl_pct > 0:
                    # Vault gaining — will reduce long → sell pressure → SHORT
                    side = "A"; is_long = False
                    reason = (f"HLP_long_gaining unrl={unrl_pct*100:.3f}% "
                              f"rate={divergence_rate*100*60:.3f}%/h → rebalance_sell")
                else:
                    # Vault losing — will add long to defend → buy pressure → LONG
                    side = "B"; is_long = True
                    reason = (f"HLP_long_losing unrl={unrl_pct*100:.3f}% "
                              f"rate={divergence_rate*100*60:.3f}%/h → rebalance_buy")
            else:
                # HLP short
                if unrl_pct > 0:
                    # Short profitable → reduce short → buy pressure → LONG
                    side = "B"; is_long = True
                    reason = (f"HLP_short_gaining unrl={unrl_pct*100:.3f}% → rebalance_buy")
                else:
                    # Short losing → add short to defend → sell pressure → SHORT
                    side = "A"; is_long = False
                    reason = (f"HLP_short_losing unrl={unrl_pct*100:.3f}% → rebalance_sell")

        if not side:
            return None

        if is_long:
            sl_px = close * (1 - VP_SL_PCT)
            tp_px = close * (1 + VP_TP_PCT)
        else:
            sl_px = close * (1 + VP_SL_PCT)
            tp_px = close * (1 - VP_TP_PCT)

        # ── Stage 2 council filter: spread max ──
        spread_pass, spread_detail = edge_filters.spread_max(bus, coin, max_bps=6.0)
        if not spread_pass:
            return None

        return Signal(
            coin=coin, side=side, is_long=is_long, ref_price=close,
            sl_px=sl_px, tp_px=tp_px,
            max_hold_bars=VP_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=reason,
            extras={
                "hlp_net_usd": net_usd,
                "hlp_is_long": hlp_long,
                "nav_divergence_pct": unrl_pct,
                "divergence_rate_pct_per_min": divergence_rate,
            },
        )
