"""Tests for signal_bus.hlp_decoder_poller — including H5 regression."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from signal_bus import hlp_decoder_poller as P


def test_detect_events_new_open_above_threshold():
    """Position appearing > MIN_NEW_POSITION_USD → OPEN event."""
    prev = {}
    curr = {"ETH": {"szi": 100.0, "entry_px": 3000, "ntl_usd": 1_500_000}}
    events = P._detect_events(prev, curr, "liquidator")
    assert len(events) == 1
    assert events[0]["kind"] == "OPEN"
    assert events[0]["coin"] == "ETH"
    assert events[0]["is_long"] is True
    assert events[0]["vault_label"] == "liquidator"


def test_detect_events_skips_small_positions():
    """New position under threshold → no event."""
    prev = {}
    curr = {"ETH": {"szi": 100.0, "entry_px": 3000, "ntl_usd": 50_000}}
    events = P._detect_events(prev, curr, "liquidator")
    assert events == []


def test_detect_events_flip():
    """Long → short = FLIP."""
    prev = {"ETH": {"szi": 100.0, "entry_px": 3000, "ntl_usd": 300_000}}
    curr = {"ETH": {"szi": -100.0, "entry_px": 3000, "ntl_usd": 300_000}}
    events = P._detect_events(prev, curr, "strategy_a")
    assert len(events) == 1
    assert events[0]["kind"] == "FLIP"


def test_detect_events_grew():
    """Same direction, 50%+ size growth, delta ≥ $1M = GREW."""
    prev = {"ETH": {"szi": 100.0, "entry_px": 3000, "ntl_usd": 1_000_000}}
    curr = {"ETH": {"szi": 200.0, "entry_px": 3000, "ntl_usd": 2_100_000}}
    events = P._detect_events(prev, curr, "strategy_b")
    assert len(events) == 1
    assert events[0]["kind"] == "GREW"


def test_detect_events_close():
    """Position closed → CLOSE event."""
    prev = {"ETH": {"szi": 100.0, "entry_px": 3000, "ntl_usd": 1_000_000}}
    curr = {}
    events = P._detect_events(prev, curr, "liquidator")
    assert len(events) == 1
    assert events[0]["kind"] == "CLOSE"


def test_tick_skips_snapshot_update_on_none_result():
    """H5 regression: _fetch_vault_positions returning None must NOT
    overwrite the existing prev snapshot. Otherwise, every coin would
    appear as a new OPEN on the next successful poll."""
    from unittest.mock import MagicMock
    cache = MagicMock()
    poller = P.HlpDecoderPoller(cache)
    # Seed a real snapshot
    poller._prev_snapshots["liquidator"] = {
        "ETH": {"szi": 100.0, "entry_px": 3000, "ntl_usd": 1_500_000},
    }

    # First failure: _fetch returns None → snapshot must NOT change
    with patch.object(P, "_fetch_vault_positions", return_value=None):
        poller._tick()
    # Snapshot preserved
    assert poller._prev_snapshots["liquidator"] != {}
    assert "ETH" in poller._prev_snapshots["liquidator"]
    # No events emitted, no snapshot wiped
    assert cache.add_hlp_vault_event.call_count == 0

    # Second cycle: _fetch returns the SAME position. No deltas → no events.
    with patch.object(P, "_fetch_vault_positions",
                       return_value={"ETH": {"szi": 100.0, "entry_px": 3000,
                                              "ntl_usd": 1_500_000}}):
        poller._tick()
    # Critical: ZERO false events emitted from the no-change cycle
    assert cache.add_hlp_vault_event.call_count == 0


def test_tick_first_successful_poll_emits_no_events():
    """First successful poll establishes baseline, must NOT emit OPEN
    events for every existing position."""
    from unittest.mock import MagicMock
    cache = MagicMock()
    poller = P.HlpDecoderPoller(cache)
    # No prior snapshot for 'strategy_a'
    assert poller._prev_snapshots["strategy_a"] == {}

    with patch.object(P, "_fetch_vault_positions",
                       return_value={
                           "ETH": {"szi": 100.0, "entry_px": 3000, "ntl_usd": 5_000_000},
                           "BTC": {"szi": 10.0, "entry_px": 60000, "ntl_usd": 6_000_000},
                       }):
        poller._tick()
    # Baseline established
    assert "ETH" in poller._prev_snapshots["strategy_a"]
    assert "BTC" in poller._prev_snapshots["strategy_a"]
    # No events from any of the 4 vaults' first poll
    assert cache.add_hlp_vault_event.call_count == 0
    # Snapshot published for ALL 4 vaults (master, strategy_a, strategy_b, liquidator)
    assert cache.set_hlp_vault_snapshot.call_count == len(P.HLP_VAULTS)


def test_tick_empty_vault_baseline_does_not_repeat():
    """Vault that returns empty positions on every poll establishes baseline
    on first poll. After that, no events emitted and _has_baseline stays
    True (no false 'first snapshot' re-trigger). This was a live bug
    2026-05-21: master and liquidator vaults legitimately have {}
    positions; the OLD `if not prev:` check returned True forever for
    empty vaults, re-firing the 'first snapshot' path every 5s.
    """
    from unittest.mock import MagicMock
    cache = MagicMock()
    poller = P.HlpDecoderPoller(cache)
    # Initial state: all vaults need a baseline
    assert not any(poller._has_baseline.values())

    # All 4 vaults return empty positions
    with patch.object(P, "_fetch_vault_positions", return_value={}):
        poller._tick()
    # After one tick, ALL vaults have established baseline
    assert all(poller._has_baseline.values()), \
        f"some vaults still need baseline: {poller._has_baseline}"

    # Run two more ticks — no events should fire (vault stays empty)
    with patch.object(P, "_fetch_vault_positions", return_value={}):
        poller._tick()
        poller._tick()
    assert cache.add_hlp_vault_event.call_count == 0
    # _has_baseline must NOT have been reset
    assert all(poller._has_baseline.values())
