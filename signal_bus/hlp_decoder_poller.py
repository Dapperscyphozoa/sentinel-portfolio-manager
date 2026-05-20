"""hlp_decoder_poller — observe each of 4 HLP sub-vaults individually.

Unlike hlp_poller (which aggregates net positions across all sub-vaults),
this poller tracks each sub-vault separately so we can extract signal from
position deltas of specific sub-strategies.

The 4 known HLP sub-vaults (public addresses, no auth required):
  HLP master       0xdfc24b077bc1425ad1dea75bcb6f8158e10df303   (net aggregate)
  HLP Strategy A   0x010461c14e146ac35fe42271bdc1134ee31c703a   (MM, short bias historically)
  HLP Strategy B   0x31ca8395cf837de08b24da3f660e77761dfb974b   (MM, long bias historically)
  HLP Liquidator   0x2e3d94f0562703b25c83308a05046ddaf9a8dd14   (takes whale-liq opposite side)

Detection rules for hlp_decoder strategy:

  H-LIQ: Liquidator vault opens a NEW position > $1M notional. A whale
         just got force-closed. Liquidations often cluster — join the
         Liquidator's side for the cascade continuation.

  H-CONSENSUS: Strategy A AND Strategy B both shift same direction within
               same 5-min window. This is rare — MMs normally hedge each
               other. Same-direction means they detected directional flow.

  H-FADE-MM: Strategy A or B reaches > 80th-percentile position size from
             rolling 7d distribution. MM is overloaded → mean reversion
             play AGAINST their direction.

Rate budget:
  - 4 vaults × 2 weight (clearinghouseState) × 12 cycles/min (5s cadence)
    = 96 weight/min
  - Leaves 1080 - 96 = 984 weight/min for other pollers + safety

Cache schema (added to Cache class via cache.set_hlp_vault):
  hlp_vault_snapshots: dict {
      vault_label: str ("liquidator"|"strategy_a"|"strategy_b"|"master"),
      ts_ms: int,
      positions: {coin: {szi, entry_px, ntl_usd}},
  }
  hlp_vault_events: deque [
      {ts, vault_label, kind: "OPEN"|"CLOSE"|"GREW"|"FLIP", coin, is_long,
       ntl_usd, prev_ntl, delta_ntl}
  ]   — last ~30 min

The hlp_decoder strategy consumes hlp_vault_events to fire trades.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Optional

import httpx

log = logging.getLogger("hlp_decoder_poller")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
POLL_INTERVAL_S = int(os.environ.get("HLP_DECODER_POLL_INTERVAL_S", "5"))
MIN_NEW_POSITION_USD = float(os.environ.get("HLP_DECODER_MIN_NEW_USD", "1000000"))
EVENT_MAXLEN = 2000
SNAPSHOT_HISTORY_MAX = 180        # 15 minutes at 5s cadence

# Known HLP sub-vault addresses (public, on-chain). Override via env if HL
# restructures HLP in future.
HLP_VAULTS = {
    "master":     os.environ.get("HLP_VAULT_MASTER",     "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"),
    "strategy_a": os.environ.get("HLP_VAULT_STRATEGY_A", "0x010461c14e146ac35fe42271bdc1134ee31c703a"),
    "strategy_b": os.environ.get("HLP_VAULT_STRATEGY_B", "0x31ca8395cf837de08b24da3f660e77761dfb974b"),
    "liquidator": os.environ.get("HLP_VAULT_LIQUIDATOR", "0x2e3d94f0562703b25c83308a05046ddaf9a8dd14"),
}


def _fetch_vault_positions(vault_addr: str) -> Optional[dict]:
    """Fetch clearinghouseState for one sub-vault. Returns {coin: {szi, entry_px, ntl_usd}}.

    Returns:
      - dict (possibly empty) on success
      - None on failure (budget exhausted, 429, network error, non-200, exception)

    The None vs {} distinction is critical: empty dict means the vault truly
    has no positions, while None means we couldn't determine state and must
    NOT overwrite the previous snapshot (would emit false OPEN events on
    recovery — sentinel H5 finding, 6/6 voters caught this).

    Costs 2 weight (clearinghouseState).
    """
    try:
        from common.weight_budget import get_budget, WEIGHT_CHEAP
        if not get_budget().spend(WEIGHT_CHEAP):
            log.warning("hlp_decoder_poller: weight budget exhausted; skip vault %s",
                        vault_addr[:10])
            return None
    except ImportError:
        pass
    try:
        r = httpx.post(HL_INFO_URL,
                       json={"type": "clearinghouseState", "user": vault_addr},
                       timeout=10.0)
        if r.status_code == 429:
            try:
                from common.weight_budget import get_budget
                get_budget().note_429()
            except ImportError: pass
            return None
        if r.status_code != 200:
            return None
        d = r.json()
        out: dict = {}
        for entry in d.get("assetPositions", []) or []:
            pos = entry.get("position", {}) or {}
            coin = pos.get("coin")
            if not coin:
                continue
            try:
                szi = float(pos.get("szi", 0) or 0)
                entry_px = float(pos.get("entryPx", 0) or 0)
                ntl = float(pos.get("positionValue", 0) or 0)
            except Exception:
                continue
            out[coin.upper()] = {
                "szi": szi, "entry_px": entry_px, "ntl_usd": ntl,
            }
        return out
    except Exception:
        return None


def _detect_events(prev_snap: dict, curr_snap: dict, vault_label: str) -> list[dict]:
    """Compare prev vs curr snapshots for one vault, emit position-change events.

    Event kinds:
      OPEN  — no prior position; new one >= MIN_NEW_POSITION_USD
      CLOSE — had position; now zero or near-zero
      FLIP  — sign changed (long → short or vice versa)
      GREW  — same sign, notional grew by ≥ 50%
    """
    events: list[dict] = []
    now_ms = int(time.time() * 1000)

    # Universe of coins seen in either snapshot
    coins = set(prev_snap.keys()) | set(curr_snap.keys())
    for coin in coins:
        p = prev_snap.get(coin, {})
        c = curr_snap.get(coin, {})
        p_sz = float(p.get("szi", 0) or 0)
        c_sz = float(c.get("szi", 0) or 0)
        p_ntl = abs(float(p.get("ntl_usd", 0) or 0))
        c_ntl = abs(float(c.get("ntl_usd", 0) or 0))

        # Skip near-zero noise (HLP MMs constantly micro-adjust)
        if p_ntl < 100_000 and c_ntl < 100_000:
            continue

        kind = None
        if p_sz == 0 and c_sz != 0 and c_ntl >= MIN_NEW_POSITION_USD:
            kind = "OPEN"
        elif c_sz == 0 and p_sz != 0:
            kind = "CLOSE"
        elif p_sz != 0 and c_sz != 0 and (p_sz > 0) != (c_sz > 0):
            kind = "FLIP"
        elif p_sz != 0 and c_sz != 0 and (p_sz > 0) == (c_sz > 0):
            if c_ntl > p_ntl * 1.5 and (c_ntl - p_ntl) >= MIN_NEW_POSITION_USD:
                kind = "GREW"

        if kind:
            events.append({
                "ts": now_ms,
                "vault_label": vault_label,
                "kind": kind,
                "coin": coin,
                "is_long": (c_sz > 0) if c_sz != 0 else (p_sz > 0),
                "ntl_usd": c_ntl if c_sz != 0 else p_ntl,
                "prev_ntl": p_ntl,
                "delta_ntl": c_ntl - p_ntl,
            })
    return events


class HlpDecoderPoller(threading.Thread):
    """Background thread polling each HLP sub-vault on POLL_INTERVAL_S cadence."""

    def __init__(self, cache):
        super().__init__(daemon=True, name="hlp_decoder_poller")
        self.cache = cache
        self._stop = threading.Event()
        # Per-vault prev snapshot for delta detection
        self._prev_snapshots: dict = {label: {} for label in HLP_VAULTS}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.warning("hlp_decoder_poller starting; vaults=%s interval=%ds",
                    list(HLP_VAULTS.keys()), POLL_INTERVAL_S)
        while not self._stop.wait(POLL_INTERVAL_S):
            try:
                self._tick()
            except Exception:
                log.exception("hlp_decoder_poller tick failed")

    def _tick(self) -> None:
        for label, addr in HLP_VAULTS.items():
            curr = _fetch_vault_positions(addr)
            if curr is None:
                # Fetch failed (budget/429/error). Do NOT overwrite the
                # previous snapshot — would emit false OPEN events for
                # every existing position on next successful poll
                # (sentinel H5 — Qwen3 235B 95% CRITICAL, 6/6 voters).
                continue
            prev = self._prev_snapshots.get(label, {})
            # If this is the first successful poll (prev is empty), don't
            # emit events for every current position — that's the baseline,
            # not new opens. Only persist the snapshot and let future ticks
            # detect deltas against it.
            if not prev:
                self._prev_snapshots[label] = curr
                self.cache.set_hlp_vault_snapshot(label, curr,
                                                  int(time.time() * 1000))
                log.info("hlp_decoder: first snapshot for vault=%s (%d positions)",
                         label, len(curr))
                continue
            events = _detect_events(prev, curr, label)
            for ev in events:
                log.warning("hlp_decoder: vault=%s kind=%s coin=%s long=%s ntl=$%.0f",
                            ev["vault_label"], ev["kind"], ev["coin"],
                            ev["is_long"], ev["ntl_usd"])
                self.cache.add_hlp_vault_event(ev)
            # Persist current snapshot for next tick
            self._prev_snapshots[label] = curr
            self.cache.set_hlp_vault_snapshot(label, curr,
                                              int(time.time() * 1000))


def start(cache) -> Optional[HlpDecoderPoller]:
    if os.environ.get("HLP_DECODER_POLLER_ENABLED", "1") == "0":
        log.info("hlp_decoder_poller disabled via env")
        return None
    t = HlpDecoderPoller(cache)
    t.start()
    return t
