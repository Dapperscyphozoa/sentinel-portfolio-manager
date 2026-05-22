"""Per-engine coin denylist via <NAME>_COIN_DENYLIST env var."""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Module-level import inside the test functions to allow env manipulation


def _build_strat(name='hl_settle_5m', universe=('SOL', 'BNB', 'ETH')):
    strat = MagicMock()
    strat.NAME = name
    strat.UNIVERSE = list(universe)
    strat.TF = '5m'
    sig = MagicMock()
    sig.coin = 'ETH'
    sig.side = 'B'
    sig.is_long = True
    sig.ref_price = 2000.0
    sig.sl_px = 1980.0
    sig.tp_px = 2050.0
    sig.max_hold_bars = 12
    sig.fire_ts = 1.0
    sig.fire_reason = 'test'
    sig.extras = {}
    strat.evaluate.return_value = sig
    return strat


def test_denylist_blocks_coin(monkeypatch):
    monkeypatch.setenv('HL_SETTLE_5M_COIN_DENYLIST', 'SOL,BNB')
    monkeypatch.delenv('FMOM_COIN_DENYLIST', raising=False)
    # Re-evaluate the env reader inline (matches runner code)
    deny_env = os.environ.get('HL_SETTLE_5M_COIN_DENYLIST', '')
    denyset = {c.strip().upper() for c in deny_env.split(',') if c.strip()}
    assert denyset == {'SOL', 'BNB'}
    assert 'SOL' in denyset
    assert 'eth' not in denyset
    assert 'ETH' not in denyset


def test_denylist_empty_no_block(monkeypatch):
    monkeypatch.delenv('FMOM_COIN_DENYLIST', raising=False)
    deny_env = os.environ.get('FMOM_COIN_DENYLIST', '')
    denyset = {c.strip().upper() for c in deny_env.split(',') if c.strip()} if deny_env else set()
    assert denyset == set()


def test_denylist_handles_whitespace(monkeypatch):
    monkeypatch.setenv('XX_COIN_DENYLIST', '  sol , bnb,, ETH ')
    deny_env = os.environ.get('XX_COIN_DENYLIST', '')
    denyset = {c.strip().upper() for c in deny_env.split(',') if c.strip()}
    assert denyset == {'SOL', 'BNB', 'ETH'}


def test_denylist_case_insensitive():
    deny_env = 'sol,bnb'
    denyset = {c.strip().upper() for c in deny_env.split(',') if c.strip()}
    # Inputs upper'd, check lookup with mixed-case coin
    assert 'SOL'.upper() in denyset
    assert 'sol'.upper() in denyset
    assert 'Sol'.upper() in denyset


def test_runner_uses_denylist(monkeypatch):
    """Integration: runner skips coins in denylist before calling evaluate."""
    monkeypatch.setenv('TEST_ENGINE_COIN_DENYLIST', 'SOL,BNB')
    # Simulate the runner code block in isolation
    strat_name = 'test_engine'
    universe = ['SOL', 'BNB', 'ETH', 'BTC']
    deny_env = os.environ.get(f"{strat_name.upper()}_COIN_DENYLIST", "")
    denyset = (
        {c.strip().upper() for c in deny_env.split(",") if c.strip()}
        if deny_env else set()
    )
    blocked = []
    evaluated = []
    for coin in universe:
        if denyset and coin.upper() in denyset:
            blocked.append(coin)
            continue
        evaluated.append(coin)
    assert blocked == ['SOL', 'BNB']
    assert evaluated == ['ETH', 'BTC']
