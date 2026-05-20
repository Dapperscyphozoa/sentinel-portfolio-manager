"""Closure-quality classification.

A 'clean' closure is one whose lifecycle reflects the strategy's own thesis
(entry signal → SL/TP/timeout/strategy-defined exit). A 'noisy' closure was
produced by operator override, bug recovery, or operational rescue and does
not represent strategy edge. Promotion/demotion gates and rolling-PF audits
should exclude noisy closures so they don't punish (or reward) strategies for
operator-driven outcomes.

Used by:
  - strategy_runner /attribution (?clean_only=1 flag)
  - monitor/routines/auto_demote (_compute_rolling_pf)
  - monitor/routines/auto_4loss_demote (_count_paper_wins_streak)
  - any future PF-based decision logic
"""
from __future__ import annotations

# Prefixes/values that mark a closure as operator-driven, bug recovery, or
# off-book reconstruction rather than the strategy's own exit. Match logic
# below is prefix-based to catch suffixed reasons like 'force_close:audit_red'.
NOISY_PREFIXES: tuple[str, ...] = (
    "force_close",                  # operator-initiated kill (any flavour)
    "stale_force_close",            # last-resort sweep — operator/system override
    "force_closed_unverified",      # HL state ambiguous — not a clean strategy exit
)

NOISY_EXACT: frozenset[str] = frozenset({
    "backfill",                     # PnL reconstructed from HL fills — bug-era rescue
    "reconciled_off_book",          # row reconciled by /reconcile path
    "manual",                       # any explicit manual record
    "operator_force_close",         # legacy alias
})


# Engines fully retired — file archived from strategies/, registry entry removed.
# Historical closures persist in the closures table but must NOT surface in
# dashboard attribution panels, promotion gates, or any live decision logic.
# Add to this set the moment an engine is decommissioned; never re-add a
# resurrected engine here (resurrection ⇒ new closures stream, no need to hide).
ARCHIVED_ENGINES: frozenset[str] = frozenset({
    "e08_dip3d7_td_4h",             # archived 2026-05-18 (8 force-closed losses, 0 clean exits)
})


def is_archived_engine(name: str | None) -> bool:
    """True if engine is in the archived set — exclude from dashboard/attribution."""
    return bool(name) and name in ARCHIVED_ENGINES


def is_clean_closure(close_reason: str | None) -> bool:
    """Return True if the closure represents a strategy-driven exit.

    Empty/None reason → True (legacy rows, assume clean; the closures table
    has historical entries without recorded reasons).
    """
    if not close_reason:
        return True
    r = close_reason.strip()
    if r in NOISY_EXACT:
        return False
    for p in NOISY_PREFIXES:
        if r.startswith(p):
            return False
    return True


def clean_metrics(closures: list[dict]) -> dict:
    """Compute n / wins / wr / pf / net_pnl over the clean subset of a
    closures list. Each closure dict must have keys: pnl_usd, fees_usd,
    close_reason. Missing fees treated as 0.

    Returns dict with both raw_n (all rows) and clean_n / wins / wr / pf /
    gross_pnl / fees / net_pnl (clean rows only).
    """
    raw_n = len(closures)
    clean = [c for c in closures if is_clean_closure(c.get("close_reason"))]
    n = len(clean)
    if n == 0:
        return {"raw_n": raw_n, "clean_n": 0, "wins": 0, "losses": 0,
                "wr": 0.0, "pf": None, "gross_pnl": 0.0, "fees": 0.0,
                "net_pnl": 0.0}
    wins = 0
    losses = 0
    gross_pnl = 0.0
    fees = 0.0
    win_pnl = 0.0
    loss_pnl = 0.0
    for c in clean:
        p = float(c.get("pnl_usd") or 0)
        f = float(c.get("fees_usd") or 0)
        net = p - f
        gross_pnl += p
        fees += f
        if net > 0:
            wins += 1
            win_pnl += net
        elif net < 0:
            losses += 1
            loss_pnl += -net
    pf = (win_pnl / loss_pnl) if loss_pnl > 0 else (float("inf") if win_pnl > 0 else None)
    return {
        "raw_n": raw_n,
        "clean_n": n,
        "wins": wins,
        "losses": losses,
        "wr": wins / n if n else 0.0,
        "pf": pf,
        "gross_pnl": gross_pnl,
        "fees": fees,
        "net_pnl": gross_pnl - fees,
    }
