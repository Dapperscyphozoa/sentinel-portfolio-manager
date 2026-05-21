"""Trail-stop helper — incremental ratchet stop.

Strategy fires with extras["trail"] = {start_pct, increment_pct} (both in
percent favorable terms — e.g. 0.30 for 0.30%, NOT 0.003 as a fraction).

For each open trade in position_loop_once, this module:
  1. Reads current peak_favorable from extras_json (persisted between polls).
  2. Updates peak from current mark price.
  3. If peak >= start_pct, computes the new stop level using incremental ratchet:
       steps     = int((peak - start_pct) / increment_pct)
       stop_lvl  = (start_pct - increment_pct) + steps * increment_pct
     i.e. stop sits one increment below the highest ratchet step reached.
  4. Converts stop_lvl (in % favorable) to an absolute price.
  5. Updates trades.sl_px ONLY if the new level is tighter (locks more profit)
     than the current sl_px. Never loosens.
  6. Persists peak_favorable back to extras_json for next poll.

The HL bracket SL placed at open() remains at the original wide SL — it's the
deep-gap safety net. The trail level rides on top via the local poll, and
hit_sl fires when mark price retraces to the trailed level.

Activation: only fires when trade.extras_json contains a 'trail' object.
Strategies that don't want trail behavior simply omit the key.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger(__name__)


def _favorable_pct(px: float, open_px: float, is_long: bool) -> float:
    """Percent favorable for the position direction. Positive = in profit."""
    if not open_px or open_px <= 0:
        return 0.0
    raw = (px - open_px) / open_px * 100.0
    return raw if is_long else -raw


def _trail_stop_px(open_px: float, is_long: bool, stop_lvl_pct: float) -> float:
    """Convert a stop level in % favorable to an absolute trigger price."""
    if is_long:
        return open_px * (1.0 + stop_lvl_pct / 100.0)
    return open_px * (1.0 - stop_lvl_pct / 100.0)


def apply_trail(conn, trade_row, px: float) -> Optional[float]:
    """Apply trail-stop ratchet for one trade row.

    Returns the new sl_px if it was tightened, else None.

    Side effects:
      - Updates trades.sl_px if trail level is tighter than current sl_px.
      - Updates trades.extras_json with peak_favorable_pct and trail metadata.

    No-op when extras_json doesn't contain a 'trail' config block.
    """
    if not trade_row or px is None:
        return None
    try:
        extras = json.loads(trade_row["extras_json"] or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(extras, dict):
        return None
    # Strategy-level config nested under extras.extras (where Signal.extras lands)
    cfg_root = extras.get("extras") if isinstance(extras.get("extras"), dict) else extras
    trail_cfg = cfg_root.get("trail") if isinstance(cfg_root.get("trail"), dict) else None
    if not trail_cfg:
        return None
    try:
        start_pct = float(trail_cfg.get("start_pct"))
        incr_pct = float(trail_cfg.get("increment_pct"))
    except (TypeError, ValueError):
        return None
    if start_pct <= 0 or incr_pct <= 0:
        return None

    open_px = float(trade_row["open_px"])
    is_long = bool(trade_row["is_long"])
    current_fav = _favorable_pct(px, open_px, is_long)

    # Read prior peak from extras (None = not yet seen)
    prior_peak = None
    raw_peak = extras.get("peak_favorable_pct")
    if raw_peak is not None:
        try:
            prior_peak = float(raw_peak)
        except (TypeError, ValueError):
            prior_peak = None

    new_peak = max(current_fav, prior_peak) if prior_peak is not None else current_fav

    # No ratchet update if we never crossed start_pct.
    # Use epsilon to avoid floating-point miss at exact threshold (e.g. 100.30
    # in a long at open_px=100 may compute to 0.29999... < 0.30 in float).
    _EPS = 1e-9
    if new_peak < start_pct - _EPS:
        # Persist peak only (so a later poll sees the running max even before activation)
        if prior_peak is None or new_peak > prior_peak:
            try:
                conn.execute(
                    "UPDATE trades SET extras_json = json_set("
                    "  COALESCE(extras_json, '{}'),"
                    "  '$.peak_favorable_pct', ?"
                    ") WHERE cloid = ?",
                    (new_peak, trade_row["cloid"]),
                )
            except Exception:
                log.exception("trail: failed to persist peak pre-activation")
        return None

    # Compute new stop level (incremental ratchet)
    steps = int((new_peak - start_pct) / incr_pct)
    stop_lvl_pct = (start_pct - incr_pct) + steps * incr_pct
    new_sl_px = _trail_stop_px(open_px, is_long, stop_lvl_pct)

    # Tighten only — never loosen. "Tighter" for a long = HIGHER sl_px.
    # For a short = LOWER sl_px.
    current_sl_px = float(trade_row["sl_px"])
    if is_long:
        tightened = new_sl_px > current_sl_px
    else:
        tightened = new_sl_px < current_sl_px

    if not tightened:
        # Still persist peak update so we don't lose progress
        if prior_peak is None or new_peak > prior_peak:
            try:
                conn.execute(
                    "UPDATE trades SET extras_json = json_set("
                    "  COALESCE(extras_json, '{}'),"
                    "  '$.peak_favorable_pct', ?"
                    ") WHERE cloid = ?",
                    (new_peak, trade_row["cloid"]),
                )
            except Exception:
                log.exception("trail: failed to persist peak post-activation no-tighten")
        return None

    # Commit the ratchet
    try:
        conn.execute(
            "UPDATE trades SET sl_px = ?, "
            "extras_json = json_set("
            "  COALESCE(extras_json, '{}'),"
            "  '$.peak_favorable_pct', ?,"
            "  '$.trail_active', 1,"
            "  '$.trail_stop_lvl_pct', ?"
            ") WHERE cloid = ?",
            (new_sl_px, new_peak, stop_lvl_pct, trade_row["cloid"]),
        )
    except Exception:
        log.exception("trail: failed to update sl_px")
        return None

    log.warning("trail ratchet: %s/%s peak=+%.3f%% stop=+%.3f%% (new_sl_px=%.6f)",
                trade_row["strategy"], trade_row["coin"], new_peak, stop_lvl_pct, new_sl_px)
    return new_sl_px
