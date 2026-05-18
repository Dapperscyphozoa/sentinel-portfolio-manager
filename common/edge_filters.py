"""edge_filters — shared signal-quality filters per Stage 2 council consensus.

Each filter is a PURE function returning (passes: bool, detail: dict). Filters
return True (PASS) when the signal should proceed and False (BLOCK) when the
signal should be skipped. Detail dict goes into Signal.extras for telemetry.

These filters are imported by individual strategy modules and applied near the
top of evaluate() before any heavy computation, so they short-circuit cheaply.

Council Q3 mapping (defended by 4/4 voters):
  hl_settle_5m       → cvd_alignment            (+0.5% WR / +0.2 PF)
  ict_confluence     → oi_delta_on_trigger      (+22% PF)
  hlp_fade           → hlp_nav_divergence       (+30% edge)
  stop_hunt          → liquidity_at_target      (+20% WR)
  vpoc_retest        → vpoc_min_volume          (+0.5% WR)
  oi_concentration   → oi_divergence_3sigma     (+0.4% WR)

Additional council-recommended filters for ALL engines:
  asia_kill_window   — block 00:00-05:00 UTC (UZT_REV learning)
  funding_volatility — block fmom/funding engines when funding is flat
  spread_max_bps     — block when book spread is wide (slippage risk)
  cvd_alignment      — require trade-tape CVD aligns with signal direction
  oi_delta           — require open interest increase confirming participation
"""
from __future__ import annotations

import os
import time
from typing import Optional


def _f(k: str, d: float) -> float:
    try:
        return float(os.environ.get(k, d))
    except Exception:
        return d


# ─────────────────────────────────────────────────────────────────────────
# Time-of-day filter — Asia ultra-low-vol kills mean-rev signals
# ─────────────────────────────────────────────────────────────────────────

def asia_kill_window(start_h: int = 0, end_h: int = 5) -> tuple[bool, dict]:
    """Block 00:00-05:00 UTC by default (UZT_REV learning).
    Returns (pass, detail). PASS = outside kill window.
    """
    h = time.gmtime().tm_hour
    in_window = start_h <= h < end_h
    return not in_window, {"utc_hour": h, "kill_window": (start_h, end_h),
                           "in_kill_window": in_window}


# ─────────────────────────────────────────────────────────────────────────
# CVD alignment filter — require trade-tape direction matches signal
# Council recommended for: hl_settle_5m (+0.5% WR), ict_confluence (+5-15% WR)
# ─────────────────────────────────────────────────────────────────────────

def cvd_alignment(bus, coin: str, is_long: bool,
                  window_ms: int = 30_000,
                  min_z: float = 0.5,
                  min_ratio: float = 0.55) -> tuple[bool, dict]:
    """Require last `window_ms` CVD aligns with intended signal direction.

    For LONG: require buy_notional / total > min_ratio AND z > +min_z
    For SHORT: require sell_notional / total > min_ratio AND z < -min_z

    Fails open (returns True) on bus error — don't block engines if CVD feed down.
    """
    try:
        cvd = bus.cvd(coin, window_ms=window_ms)
    except Exception as e:
        return True, {"cvd_filter": "skip_bus_error", "error": str(e)[:80]}

    if not cvd or cvd.get("n_trades", 0) < 5:
        return True, {"cvd_filter": "skip_low_activity",
                      "n_trades": cvd.get("n_trades", 0) if cvd else 0}

    buy_ntl = float(cvd.get("buy_notional", 0))
    sell_ntl = float(cvd.get("sell_notional", 0))
    total = buy_ntl + sell_ntl
    if total <= 0:
        return True, {"cvd_filter": "skip_zero_notional"}

    z = float(cvd.get("z_score", 0))
    if is_long:
        ratio = buy_ntl / total
        passes = ratio > min_ratio and z > min_z
    else:
        ratio = sell_ntl / total
        passes = ratio > min_ratio and z < -min_z

    return passes, {
        "cvd_filter": "pass" if passes else "block",
        "cvd_z": z,
        "cvd_ratio": round(ratio, 3),
        "min_z": min_z,
        "min_ratio": min_ratio,
    }


# ─────────────────────────────────────────────────────────────────────────
# OI-delta filter — require open interest INCREASE on trigger (participation)
# Council recommended for: ict_confluence (+22% PF), vpoc_retest, stop_hunt
# ─────────────────────────────────────────────────────────────────────────

def oi_delta_increasing(bus, coin: str,
                        lookback_n: int = 6,
                        min_pct_delta: float = 0.002) -> tuple[bool, dict]:
    """OI must be growing (participation increasing) over last `lookback_n` samples.

    Fails open on bus error or insufficient history.
    """
    try:
        oi_history = bus.oi(coin, n=lookback_n)
    except Exception as e:
        return True, {"oi_filter": "skip_bus_error", "error": str(e)[:80]}

    if not oi_history or len(oi_history) < 2:
        return True, {"oi_filter": "skip_insufficient_history",
                      "samples": len(oi_history) if oi_history else 0}

    try:
        oi_now = float(oi_history[-1].get("oi_usd", 0) or oi_history[-1].get("oi", 0))
        oi_prev = float(oi_history[0].get("oi_usd", 0) or oi_history[0].get("oi", 0))
    except Exception:
        return True, {"oi_filter": "skip_parse_error"}

    if oi_prev <= 0:
        return True, {"oi_filter": "skip_zero_oi"}

    pct_delta = (oi_now - oi_prev) / oi_prev
    passes = pct_delta >= min_pct_delta
    return passes, {
        "oi_filter": "pass" if passes else "block",
        "oi_pct_delta": round(pct_delta * 100, 4),
        "oi_now_usd": oi_now,
        "oi_prev_usd": oi_prev,
        "min_pct_delta": min_pct_delta,
        "samples": len(oi_history),
    }


# ─────────────────────────────────────────────────────────────────────────
# Spread filter — block when book spread is too wide (slippage protection)
# Council recommended for: all 5m-and-faster engines
# ─────────────────────────────────────────────────────────────────────────

def spread_max(bus, coin: str, max_bps: float = 5.0) -> tuple[bool, dict]:
    """Block if current HL spread > max_bps. Fails open on missing book."""
    try:
        book = bus.l2book(coin)
    except Exception as e:
        return True, {"spread_filter": "skip_bus_error", "error": str(e)[:80]}

    if not book or not book.get("spread_bps"):
        return True, {"spread_filter": "skip_no_book"}

    spread_bps = float(book["spread_bps"])
    passes = spread_bps <= max_bps
    return passes, {
        "spread_filter": "pass" if passes else "block",
        "spread_bps": round(spread_bps, 2),
        "max_bps": max_bps,
    }


# ─────────────────────────────────────────────────────────────────────────
# Liquidity-at-target filter — for stop_hunt: confirm depth WHERE we expect sweep
# Council recommended for: stop_hunt (+20% WR)
# ─────────────────────────────────────────────────────────────────────────

def liquidity_at_target(bus, coin: str, is_long: bool,
                        min_far_side_depth_usd: float = 50_000) -> tuple[bool, dict]:
    """For stop_hunt: when we're trading WITH the expected sweep direction,
    the OPPOSITE side of the book (the side that holds the stops we're hunting)
    must have meaningful depth — otherwise it's already been swept.

    For LONG (expecting sweep up): require ASK depth (sellers above) ≥ threshold.
    For SHORT: require BID depth ≥ threshold.
    """
    try:
        book = bus.l2book(coin)
    except Exception as e:
        return True, {"liq_target_filter": "skip_bus_error", "error": str(e)[:80]}

    if not book:
        return True, {"liq_target_filter": "skip_no_book"}

    if is_long:
        depth = float(book.get("ask_depth_05pct_usd", 0))
        side_label = "ask"
    else:
        depth = float(book.get("bid_depth_05pct_usd", 0))
        side_label = "bid"

    passes = depth >= min_far_side_depth_usd
    return passes, {
        "liq_target_filter": "pass" if passes else "block",
        "target_side": side_label,
        "depth_usd": round(depth, 0),
        "min_depth_usd": min_far_side_depth_usd,
    }


# ─────────────────────────────────────────────────────────────────────────
# HLP NAV divergence filter — for hlp_fade: only when divergence is meaningful
# Council recommended for: hlp_fade (+30% edge)
# ─────────────────────────────────────────────────────────────────────────

def hlp_nav_divergence(hlp_data: dict, bars: list,
                       min_pct: float = 0.0010) -> tuple[bool, dict]:
    """Only fade HLP when its position has meaningful unrealized PnL.

    Strong rebalance signal = NAV moved > min_pct against HLP's position.
    """
    if not hlp_data or not bars or len(bars) < 4:
        return True, {"hlp_nav_filter": "skip_insufficient_data"}

    net_usd = float(hlp_data.get("net_usd", 0) or 0)
    if net_usd == 0:
        return True, {"hlp_nav_filter": "skip_no_position"}

    hlp_long = net_usd > 0
    try:
        c_now = float(bars[-1]["close"])
        c_prev = float(bars[-4]["close"])
    except Exception:
        return True, {"hlp_nav_filter": "skip_parse_error"}

    if c_prev <= 0:
        return True, {"hlp_nav_filter": "skip_invalid_price"}

    mark_pct = (c_now - c_prev) / c_prev
    # If HLP long, gain pct = mark_pct; HLP short, gain pct = -mark_pct
    hlp_pnl_pct = mark_pct if hlp_long else -mark_pct
    abs_div = abs(hlp_pnl_pct)
    passes = abs_div >= min_pct
    return passes, {
        "hlp_nav_filter": "pass" if passes else "block",
        "hlp_long": hlp_long,
        "hlp_pnl_pct": round(hlp_pnl_pct * 100, 4),
        "min_pct": min_pct,
    }


# ─────────────────────────────────────────────────────────────────────────
# Funding volatility filter — block fmom-style engines when funding is flat
# Council recommended for: fmom (already shipped — kept here for future reuse)
# ─────────────────────────────────────────────────────────────────────────

def funding_volatility(bus, coin: str, hours: int = 8,
                       min_stdev: float = 0.00005) -> tuple[bool, dict]:
    """Funding must have measurable volatility over `hours` lookback."""
    try:
        recent = bus.funding(coin, hours=hours)
    except Exception:
        return True, {"funding_vol_filter": "skip_bus_error"}

    if not recent or len(recent) < 3:
        return True, {"funding_vol_filter": "skip_insufficient_history"}

    rates = []
    for r in recent:
        try:
            rates.append(float(r["rate"]))
        except Exception:
            continue
    if len(rates) < 3:
        return True, {"funding_vol_filter": "skip_parse"}

    mean = sum(rates) / len(rates)
    var = sum((r - mean) ** 2 for r in rates) / max(1, len(rates) - 1)
    stdev = var ** 0.5
    passes = stdev >= min_stdev
    return passes, {
        "funding_vol_filter": "pass" if passes else "block",
        "funding_stdev": round(stdev, 8),
        "min_stdev": min_stdev,
        "samples": len(rates),
    }


# ─────────────────────────────────────────────────────────────────────────
# VPOC volume filter — for vpoc_retest: only retest meaningful POCs
# Council recommended for: vpoc_retest (+0.5% WR)
# ─────────────────────────────────────────────────────────────────────────

def vpoc_min_volume(bars: list, vpoc_bar_idx: int,
                    min_volume_ratio: float = 1.5) -> tuple[bool, dict]:
    """VPOC bar volume must be at least min_volume_ratio × average volume."""
    if not bars or vpoc_bar_idx < 0 or vpoc_bar_idx >= len(bars):
        return True, {"vpoc_vol_filter": "skip_invalid_idx"}

    volumes = []
    for b in bars:
        try:
            volumes.append(float(b.get("volume", 0)))
        except Exception:
            volumes.append(0.0)
    if len(volumes) < 5:
        return True, {"vpoc_vol_filter": "skip_short_history"}

    avg_vol = sum(volumes) / len(volumes)
    vpoc_vol = volumes[vpoc_bar_idx]
    if avg_vol <= 0:
        return True, {"vpoc_vol_filter": "skip_zero_avg"}

    ratio = vpoc_vol / avg_vol
    passes = ratio >= min_volume_ratio
    return passes, {
        "vpoc_vol_filter": "pass" if passes else "block",
        "vpoc_vol_ratio": round(ratio, 3),
        "min_ratio": min_volume_ratio,
    }


# ─────────────────────────────────────────────────────────────────────────
# Filter chain helper — apply multiple filters, short-circuit on first block
# ─────────────────────────────────────────────────────────────────────────

def apply_chain(filters: list) -> tuple[bool, dict]:
    """Apply a list of (pass: bool, detail: dict) tuples. Returns (all_pass, merged_detail).

    Usage:
        passes, detail = apply_chain([
            asia_kill_window(),
            cvd_alignment(bus, coin, is_long),
            oi_delta_increasing(bus, coin),
        ])
        if not passes:
            return None  # signal blocked
    """
    merged = {}
    all_pass = True
    block_reason = None
    for passes, detail in filters:
        merged.update(detail)
        if not passes and all_pass:
            all_pass = False
            # Try to identify the blocking filter from detail keys
            for k, v in detail.items():
                if k.endswith("_filter") and v == "block":
                    block_reason = k
                    break
    if block_reason:
        merged["chain_blocked_by"] = block_reason
    return all_pass, merged
