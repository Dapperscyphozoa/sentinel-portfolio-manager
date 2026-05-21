"""Tests for common.trail_stop — incremental ratchet trail stop.

Ratchet logic spec:
  - Strategy fires Signal.extras["trail"] = {start_pct, increment_pct}
  - position_loop_once calls apply_trail() each minute with current mark px
  - apply_trail:
      1. Reads peak_favorable_pct from extras_json (init from current px if absent)
      2. Updates peak from current px (only grows)
      3. If peak < start_pct: persist peak; return None (no ratchet)
      4. Else: compute stop_lvl = (start_pct - increment_pct) + steps * increment_pct
              where steps = floor((peak - start_pct) / increment_pct)
              and store sl_px ONLY if tighter (locks more profit)
"""
import json
import sqlite3
import pytest

from common.trail_stop import apply_trail, _favorable_pct, _trail_stop_px


# ─── helpers ──────────────────────────────────────────────────────────────
def _make_db():
    """In-memory SQLite with minimal trades schema for trail tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE trades (
            cloid TEXT PRIMARY KEY,
            strategy TEXT, coin TEXT, is_long INTEGER,
            open_ts REAL, open_px REAL, size_coin REAL,
            sl_px REAL, tp_px REAL, max_hold_bars INTEGER,
            status TEXT DEFAULT 'open',
            extras_json TEXT
        )
    """)
    return conn


def _insert(conn, *, cloid, is_long, open_px, sl_px, tp_px, extras):
    conn.execute(
        "INSERT INTO trades(cloid,strategy,coin,is_long,open_ts,open_px,size_coin,"
        "sl_px,tp_px,max_hold_bars,status,extras_json) VALUES "
        "(?,'hl_settle_5m','SOL',?,1000000,?,1.0,?,?,1,'open',?)",
        (cloid, 1 if is_long else 0, open_px, sl_px, tp_px, json.dumps(extras)),
    )


def _row(conn, cloid):
    return conn.execute("SELECT * FROM trades WHERE cloid=?", (cloid,)).fetchone()


def _extras(conn, cloid):
    r = _row(conn, cloid)
    return json.loads(r["extras_json"] or "{}")


TRAIL_CONFIG = {"trail": {"start_pct": 0.30, "increment_pct": 0.02}}


# ─── _favorable_pct ───────────────────────────────────────────────────────
def test_favorable_pct_long():
    # LONG: price up 0.5% from 100 → +0.5% favorable
    assert _favorable_pct(100.5, 100.0, True) == pytest.approx(0.5)
    assert _favorable_pct(99.5, 100.0, True) == pytest.approx(-0.5)


def test_favorable_pct_short():
    # SHORT: price down 0.5% from 100 → +0.5% favorable for the short
    assert _favorable_pct(99.5, 100.0, False) == pytest.approx(0.5)
    assert _favorable_pct(100.5, 100.0, False) == pytest.approx(-0.5)


# ─── ratchet math ─────────────────────────────────────────────────────────
def test_trail_no_op_when_no_trail_config():
    """Without extras['trail'], apply_trail returns None and doesn't touch sl_px."""
    conn = _make_db()
    _insert(conn, cloid="a", is_long=True, open_px=100, sl_px=99.7, tp_px=100.4, extras={})
    row = _row(conn, "a")
    assert apply_trail(conn, row, 100.5) is None
    assert _row(conn, "a")["sl_px"] == pytest.approx(99.7)


def test_trail_no_ratchet_before_activation():
    """peak < start_pct → no ratchet, but peak is persisted."""
    conn = _make_db()
    _insert(conn, cloid="a", is_long=True, open_px=100, sl_px=99.7, tp_px=100.4,
            extras=TRAIL_CONFIG)
    row = _row(conn, "a")
    # +0.20% favorable, below 0.30% start
    assert apply_trail(conn, row, 100.20) is None
    # Peak should be persisted at 0.20
    assert _extras(conn, "a")["peak_favorable_pct"] == pytest.approx(0.20)
    # sl_px unchanged
    assert _row(conn, "a")["sl_px"] == pytest.approx(99.7)


def test_trail_first_ratchet_at_start():
    """Peak hits exactly start_pct → stop ratchets to (start - increment)."""
    conn = _make_db()
    _insert(conn, cloid="a", is_long=True, open_px=100, sl_px=99.7, tp_px=100.4,
            extras=TRAIL_CONFIG)
    row = _row(conn, "a")
    # +0.30% favorable → activate; steps=0; stop_lvl = 0.30 - 0.02 = +0.28%
    new_sl = apply_trail(conn, row, 100.30)
    assert new_sl is not None
    # +0.28% favorable on long → sl_px = 100 * 1.0028 = 100.28
    assert new_sl == pytest.approx(100.28)
    assert _row(conn, "a")["sl_px"] == pytest.approx(100.28)
    ex = _extras(conn, "a")
    assert ex["trail_active"] == 1
    assert ex["trail_stop_lvl_pct"] == pytest.approx(0.28)


def test_trail_incremental_ratchet():
    """Peak +0.45% → steps = int((0.45 - 0.30) / 0.02) = 7 → stop_lvl = 0.28 + 7*0.02 = 0.42%."""
    conn = _make_db()
    _insert(conn, cloid="a", is_long=True, open_px=100, sl_px=99.7, tp_px=100.4,
            extras=TRAIL_CONFIG)
    row = _row(conn, "a")
    new_sl = apply_trail(conn, row, 100.45)
    assert new_sl == pytest.approx(100.42, abs=1e-6)


def test_trail_never_loosens():
    """If price retreats, sl_px does NOT move back down."""
    conn = _make_db()
    _insert(conn, cloid="a", is_long=True, open_px=100, sl_px=99.7, tp_px=100.4,
            extras=TRAIL_CONFIG)
    # Ratchet up to +0.50% → stop at +0.48%
    apply_trail(conn, _row(conn, "a"), 100.50)
    assert _row(conn, "a")["sl_px"] == pytest.approx(100.48, abs=1e-6)
    # Now price drops back to +0.35% favorable. Ratchet must NOT loosen.
    apply_trail(conn, _row(conn, "a"), 100.35)
    assert _row(conn, "a")["sl_px"] == pytest.approx(100.48, abs=1e-6)


def test_trail_short_position():
    """Short side: peak favorable = price drops; stop = open_px * (1 - stop_lvl/100)."""
    conn = _make_db()
    # SHORT at 100, initial SL at 100.3 (above entry), TP at 99.6 (below)
    _insert(conn, cloid="a", is_long=False, open_px=100, sl_px=100.3, tp_px=99.6,
            extras=TRAIL_CONFIG)
    # Price drops to 99.65 = +0.35% favorable for short
    new_sl = apply_trail(conn, _row(conn, "a"), 99.65)
    # steps = int((0.35 - 0.30) / 0.02) = 2 → stop_lvl = 0.28 + 2*0.02 = 0.32
    # For short, sl_px = open * (1 - stop_lvl/100) = 100 * 0.9968 = 99.68
    assert new_sl == pytest.approx(99.68, abs=1e-4)


def test_trail_short_never_loosens():
    """Short: ratchet down only — sl_px must decrease monotonically."""
    conn = _make_db()
    _insert(conn, cloid="a", is_long=False, open_px=100, sl_px=100.3, tp_px=99.6,
            extras=TRAIL_CONFIG)
    apply_trail(conn, _row(conn, "a"), 99.50)  # +0.50% favorable → stop_lvl 0.48 → sl_px 99.52
    sl_after_deep = _row(conn, "a")["sl_px"]
    assert sl_after_deep < 100.0
    apply_trail(conn, _row(conn, "a"), 99.80)  # only +0.20% favorable now — must not loosen
    assert _row(conn, "a")["sl_px"] == sl_after_deep


def test_trail_peak_grows_across_polls():
    """Peak monotonically grows across multiple poll cycles."""
    conn = _make_db()
    _insert(conn, cloid="a", is_long=True, open_px=100, sl_px=99.7, tp_px=100.4,
            extras=TRAIL_CONFIG)
    apply_trail(conn, _row(conn, "a"), 100.20)
    apply_trail(conn, _row(conn, "a"), 100.30)
    apply_trail(conn, _row(conn, "a"), 100.25)
    apply_trail(conn, _row(conn, "a"), 100.45)
    ex = _extras(conn, "a")
    assert ex["peak_favorable_pct"] == pytest.approx(0.45, abs=1e-4)


def test_trail_zero_or_negative_config_disables():
    """Bad config values must disable trail (no crash, no ratchet)."""
    conn = _make_db()
    for bad in [{"trail": {"start_pct": 0, "increment_pct": 0.02}},
                {"trail": {"start_pct": 0.30, "increment_pct": 0}},
                {"trail": {"start_pct": -0.1, "increment_pct": 0.02}},
                {"trail": {"start_pct": "bad", "increment_pct": "bad"}}]:
        cl = f"c{id(bad)}"
        _insert(conn, cloid=cl, is_long=True, open_px=100, sl_px=99.7, tp_px=100.4, extras=bad)
        result = apply_trail(conn, _row(conn, cl), 101.0)  # +1% favorable
        assert result is None, f"bad config should disable trail: {bad}"
        assert _row(conn, cl)["sl_px"] == pytest.approx(99.7)


def test_trail_persists_peak_when_not_tightened():
    """If new ratchet level doesn't tighten current sl_px (e.g. already higher),
    peak should still be persisted."""
    conn = _make_db()
    # Insert with manually-elevated sl_px (tighter than initial trail level)
    _insert(conn, cloid="a", is_long=True, open_px=100, sl_px=100.50, tp_px=100.4,
            extras=TRAIL_CONFIG)
    # +0.35% favorable would give stop_lvl 0.32 → sl_px 100.32. But current sl_px is 100.50.
    # 100.32 < 100.50 → NOT a tightening → don't update sl_px, but persist peak.
    result = apply_trail(conn, _row(conn, "a"), 100.35)
    assert result is None
    assert _row(conn, "a")["sl_px"] == pytest.approx(100.50)
    assert _extras(conn, "a")["peak_favorable_pct"] == pytest.approx(0.35, abs=1e-4)


# ─── strategy integration spec ────────────────────────────────────────────
def test_signal_carries_trail_config_when_enabled(monkeypatch):
    """When HL_SETTLE_TRAIL_ENABLED is set, Signal.extras must include trail config."""
    import importlib
    monkeypatch.setenv("HL_SETTLE_TRAIL_ENABLED", "1")
    monkeypatch.setenv("HL_SETTLE_TRAIL_START_PCT", "0.30")
    monkeypatch.setenv("HL_SETTLE_TRAIL_INCREMENT_PCT", "0.02")
    import strategy_runner.strategies.hl_settle_5m as m
    importlib.reload(m)
    assert m.HL_SETTLE_TRAIL_ENABLED is True
    assert m.HL_SETTLE_TRAIL_START_PCT == 0.30
    assert m.HL_SETTLE_TRAIL_INCREMENT_PCT == 0.02


def test_signal_omits_trail_when_disabled(monkeypatch):
    """When HL_SETTLE_TRAIL_ENABLED=0, trail config should NOT appear in extras."""
    import importlib
    monkeypatch.setenv("HL_SETTLE_TRAIL_ENABLED", "0")
    import strategy_runner.strategies.hl_settle_5m as m
    importlib.reload(m)
    assert m.HL_SETTLE_TRAIL_ENABLED is False
