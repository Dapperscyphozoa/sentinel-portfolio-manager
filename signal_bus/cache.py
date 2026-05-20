"""In-memory ring buffers for klines / liq / markPrice / funding, with SQLite flush.

Ring buffers are thread-safe via per-key locks. Writers (WS thread) and readers
(HTTP threads) share these directly. SQLite is durable warm-state.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Optional


KLINE_CAP = 1000   # bars per (coin, tf)
LIQ_CAP   = 50000  # liq events total
MARK_CAP  = 300    # last 5min @ 1s


_DB_LOCK = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    coin       TEXT NOT NULL,
    tf         TEXT NOT NULL,
    open_ts    INTEGER NOT NULL,
    open_px    REAL NOT NULL,
    high_px    REAL NOT NULL,
    low_px     REAL NOT NULL,
    close_px   REAL NOT NULL,
    volume     REAL NOT NULL,
    PRIMARY KEY (coin, tf, open_ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_klines_coin_tf_ts ON klines(coin, tf, open_ts DESC);

CREATE TABLE IF NOT EXISTS liq_events (
    ts         INTEGER NOT NULL,
    coin       TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        REAL NOT NULL,
    price      REAL NOT NULL,
    usd        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liq_ts ON liq_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_liq_coin_ts ON liq_events(coin, ts DESC);

CREATE TABLE IF NOT EXISTS funding (
    coin       TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    rate       REAL NOT NULL,
    venue      TEXT NOT NULL DEFAULT 'binance',
    PRIMARY KEY (coin, ts, venue)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_funding_coin_ts ON funding(coin, ts DESC);

CREATE TABLE IF NOT EXISTS hl_fills (
    fill_id    TEXT PRIMARY KEY,
    ts         INTEGER NOT NULL,
    coin       TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        REAL NOT NULL,
    price      REAL NOT NULL,
    cloid      TEXT,
    raw_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_hl_fills_ts ON hl_fills(ts DESC);

CREATE TABLE IF NOT EXISTS hlp_history (
    coin       TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    net_usd    REAL NOT NULL,
    PRIMARY KEY (coin, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_hlp_coin_ts ON hlp_history(coin, ts DESC);
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    return conn


class Cache:
    """All state for the signal-bus. Single instance per process.

    SQLite access: each thread gets its own connection via the `db` property.
    WAL journal mode (set in connect()) means multiple readers and one writer
    can run concurrently without blocking. This replaces a previous shared
    connection that raised sqlite3.InterfaceError when an HTTP handler thread
    read while the WS flush thread wrote.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        # Initialise schema on the constructing thread; subsequent threads
        # get their own connections via the `db` property.
        _init = init_db(db_path)
        _init.close()
        self._tls = threading.local()
        self.klines: dict[tuple[str, str], Deque[dict]] = defaultdict(lambda: deque(maxlen=KLINE_CAP))
        self.liqs: Deque[dict] = deque(maxlen=LIQ_CAP)
        self.marks: dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=MARK_CAP))
        self.funding_latest: dict[str, dict] = {}
        # HL state
        self.hl_account: dict = {"value": 0.0, "margin_used": 0.0, "positions": []}
        self.hl_positions: list[dict] = []
        self.hl_fills: Deque[dict] = deque(maxlen=2000)  # was 3000 — OOM mitigation
        # HL public trades per-coin for CVD aggregator (council priority — world-first edge)
        # maxlen=600: ~4min of trades on a busy coin like BTC. Trimmed for OOM.
        self.hl_trades: dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=600))  # was 1500
        # Whale tracker (Stage 1 #5 — world-first edge per Qwen3 235B +3.2%/mo)
        # whale_events: detected new opens; consumed by hl_whale_frontrun engine
        self.whale_events: Deque[dict] = deque(maxlen=800)  # was 2000 — OOM mitigation
        self.whale_stats: dict = {"ts": 0, "n_whales": 0, "new_events": 0}
        # HLP decoder (per-vault state for the 4 known HLP sub-vaults)
        # Consumed by hlp_decoder strategy (different from hlp_fade which uses
        # the aggregate net via hlp_poller).
        self.hlp_vault_snapshots: dict[str, dict] = {}  # {label: {ts_ms, positions}}
        self.hlp_vault_events: Deque[dict] = deque(maxlen=600)
        # L2 order book per coin (Stage 1 #6 — depth shock detection)
        # Each: {ts, bids: [{px,sz,n}, ...20], asks: [{px,sz,n}, ...20]}
        self.l2book_latest: dict[str, dict] = {}
        self.l2book_history: dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=60))  # was 300
        self.last_update: dict[str, float] = {
            "binance_ws": 0.0, "hl_ws": 0.0, "binance_flush": 0.0, "liq_flush": 0.0,
            "okx_ws": 0.0, "bybit_ws": 0.0, "oi_poll": 0.0, "whale_poll": 0.0,
        }
        self.ws_alive: dict[str, bool] = {"binance": False, "hl": False, "okx": False, "bybit": False}
        # OI state (HL openInterest via metaAndAssetCtxs, 60s poll)
        self.oi_latest: dict[str, dict] = {}
        self.oi_history: dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=720))  # was 2880 — 12h @ 60s, OOM mitigation
        self._lock = threading.RLock()
        # Sentinel fix: flush cursor for liq events. Without this, each flush
        # re-wrote every event in the ring buffer (50k×12/hour duplicates).
        self._last_flushed_liq_ts: int = 0

    @property
    def db(self) -> sqlite3.Connection:
        """Thread-local SQLite connection. WAL mode means concurrent readers
        and one writer don't block each other. Replaces a previously shared
        connection that raised sqlite3.InterfaceError under thread contention.
        """
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = connect(self._db_path)
            self._tls.conn = conn
        return conn

    def update_oi(self, snap: dict) -> None:
        """Update oi_latest snapshot and append to per-coin history."""
        if not snap:
            return
        with self._lock:
            for coin, row in snap.items():
                self.oi_latest[coin] = row
                self.oi_history[coin].append({
                    "ts": row["ts"], "oi": row["oi"], "oi_usd": row["oi_usd"],
                })

    def get_oi(self, coin: str, n: int = 60) -> list[dict]:
        """Return last n OI snapshots for coin (oldest first)."""
        with self._lock:
            dq = self.oi_history.get(coin)
            if not dq:
                return []
            n = max(1, min(n, len(dq)))
            return list(dq)[-n:]


    # -------- writers --------

    def push_kline(self, coin: str, tf: str, k: dict) -> None:
        with self._lock:
            dq = self.klines[(coin, tf)]
            # dedupe on open_ts: replace if same bucket
            if dq and dq[-1]["open_ts"] == k["open_ts"]:
                dq[-1] = k
            else:
                dq.append(k)
            self.last_update["binance_ws"] = time.time()

    def push_liq(self, ev: dict) -> None:
        with self._lock:
            self.liqs.append(ev)
            self.last_update["binance_ws"] = time.time()

    def push_hl_trade(self, coin: str, ts_ms: int, side: str, sz: float, px: float) -> None:
        """HL public trade event. side='B'=buyer-taker, 'A'=seller-taker.
        Aggressor: 'B' → +sz (buy pressure), 'A' → -sz (sell pressure)."""
        signed_sz = sz if side == "B" else (-sz if side == "A" else 0.0)
        if signed_sz == 0.0:
            return
        with self._lock:
            self.hl_trades[coin].append({
                "ts": ts_ms, "side": side, "sz": sz, "px": px, "signed_sz": signed_sz,
                "notional": sz * px,
            })


    def push_whale_event(self, ev: dict) -> None:
        """Append whale-position-open event. Consumed by hl_whale_frontrun."""
        with self._lock:
            self.whale_events.append(ev)

    def add_hlp_vault_event(self, ev: dict) -> None:
        """Append an HLP sub-vault delta event for hlp_decoder strategy."""
        with self._lock:
            self.hlp_vault_events.append(ev)

    def set_hlp_vault_snapshot(self, label: str, positions: dict, ts_ms: int) -> None:
        """Persist a per-vault snapshot for hlp_decoder lookups."""
        with self._lock:
            self.hlp_vault_snapshots[label] = {
                "ts_ms": ts_ms, "positions": positions,
            }

    def get_hlp_vault_events(self, since_ms: int = 0,
                             coin: Optional[str] = None,
                             vault_label: Optional[str] = None) -> list[dict]:
        """Return HLP-vault events since timestamp; optionally filter by coin/vault."""
        with self._lock:
            evs = list(self.hlp_vault_events)
        out = []
        for ev in evs:
            if ev["ts"] < since_ms:
                continue
            if coin and ev["coin"].upper() != coin.upper():
                continue
            if vault_label and ev["vault_label"] != vault_label:
                continue
            out.append(ev)
        return out

    def get_hlp_vault_snapshot(self, label: str) -> dict:
        """Return latest snapshot for a vault label or empty dict."""
        with self._lock:
            return dict(self.hlp_vault_snapshots.get(label, {}))

    def get_whale_events(self, since_ms: int = 0, coin: Optional[str] = None) -> list[dict]:
        """Return whale events since timestamp, optionally filtered by coin."""
        with self._lock:
            evs = list(self.whale_events)
        out = []
        for ev in evs:
            if ev["ts"] < since_ms:
                continue
            if coin and ev["coin"].upper() != coin.upper():
                continue
            out.append(ev)
        return out


    def push_l2book(self, coin: str, ts_ms: int, bids: list, asks: list) -> None:
        """Store latest L2 snapshot per coin + append to history."""
        if not bids or not asks:
            return
        # Compute total depth at ±0.5% and ±1.0% bands
        best_bid = bids[0]["px"]
        best_ask = asks[0]["px"]
        mid = (best_bid + best_ask) / 2 if (best_bid > 0 and best_ask > 0) else 0
        if mid <= 0:
            return
        band_05 = mid * 0.005
        band_10 = mid * 0.010
        bid_depth_05 = sum(b["sz"] * b["px"] for b in bids if b["px"] >= mid - band_05)
        ask_depth_05 = sum(a["sz"] * a["px"] for a in asks if a["px"] <= mid + band_05)
        bid_depth_10 = sum(b["sz"] * b["px"] for b in bids if b["px"] >= mid - band_10)
        ask_depth_10 = sum(a["sz"] * a["px"] for a in asks if a["px"] <= mid + band_10)
        spread_bps = (best_ask - best_bid) / mid * 10_000 if mid > 0 else 0

        snap = {
            "ts": ts_ms,
            "mid": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_bps": spread_bps,
            "bid_depth_05pct_usd": bid_depth_05,
            "ask_depth_05pct_usd": ask_depth_05,
            "bid_depth_10pct_usd": bid_depth_10,
            "ask_depth_10pct_usd": ask_depth_10,
        }
        with self._lock:
            self.l2book_latest[coin] = snap
            self.l2book_history[coin].append(snap)
            self.last_update["hl_ws"] = time.time()

    def get_l2book(self, coin: str) -> dict:
        """Latest L2 snapshot for coin (compact form)."""
        with self._lock:
            return dict(self.l2book_latest.get(coin, {}))

    def get_depth_shock(self, coin: str, window_s: int = 5) -> dict:
        """Detect liquidity shock — bid or ask depth drop in last window_s seconds.
        Returns: {coin, mid, spread_bps, bid_shock_pct, ask_shock_pct, price_move_bps,
                  shock_kind: 'bid'|'ask'|None}
        """
        with self._lock:
            dq = self.l2book_history.get(coin)
            if not dq or len(dq) < 2:
                return {"coin": coin, "shock_kind": None}
            now_ms = int(time.time() * 1000)
            cutoff = now_ms - window_s * 1000
            recent = [s for s in dq if s["ts"] >= cutoff]
            if len(recent) < 2:
                return {"coin": coin, "shock_kind": None}
            first = recent[0]
            last = recent[-1]

        # Depth shock: percentage drop in bid or ask depth at 0.5% band
        b0 = first.get("bid_depth_05pct_usd", 0)
        b1 = last.get("bid_depth_05pct_usd", 0)
        a0 = first.get("ask_depth_05pct_usd", 0)
        a1 = last.get("ask_depth_05pct_usd", 0)
        bid_drop = (b0 - b1) / b0 if b0 > 0 else 0
        ask_drop = (a0 - a1) / a0 if a0 > 0 else 0
        mid0 = first.get("mid", 0)
        mid1 = last.get("mid", 0)
        price_move_bps = (mid1 - mid0) / mid0 * 10_000 if mid0 > 0 else 0

        shock_kind = None
        # Liquidity-eviction definition: depth drops >30% but price hasn't moved much (<10bps)
        if bid_drop > 0.30 and abs(price_move_bps) < 10 and bid_drop > ask_drop:
            shock_kind = "bid"
        elif ask_drop > 0.30 and abs(price_move_bps) < 10 and ask_drop > bid_drop:
            shock_kind = "ask"

        return {
            "coin": coin,
            "mid": mid1,
            "spread_bps": last.get("spread_bps", 0),
            "bid_shock_pct": round(bid_drop * 100, 2),
            "ask_shock_pct": round(ask_drop * 100, 2),
            "price_move_bps": round(price_move_bps, 2),
            "shock_kind": shock_kind,
            "bid_depth_now_usd": b1,
            "ask_depth_now_usd": a1,
            "samples": len(recent),
        }

    def get_cvd(self, coin: str, window_ms: int = 30_000) -> dict:
        """Cumulative Volume Delta for trailing window.
        Returns: {window_ms, n_trades, cvd_size, cvd_notional, buy_notional, sell_notional,
                  rolling_5m_sigma, z_score}"""
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - window_ms
        with self._lock:
            dq = self.hl_trades.get(coin)
            if not dq:
                return {"window_ms": window_ms, "n_trades": 0, "cvd_size": 0.0,
                        "cvd_notional": 0.0, "buy_notional": 0.0, "sell_notional": 0.0,
                        "rolling_5m_sigma": 0.0, "z_score": 0.0}
            recent = [t for t in dq if t["ts"] >= cutoff]
            # 5min sigma context for z-score normalization
            cutoff_5m = now_ms - 300_000
            window_5m = [t for t in dq if t["ts"] >= cutoff_5m]

        cvd_size = sum(t["signed_sz"] for t in recent)
        buy_ntl = sum(t["notional"] for t in recent if t["signed_sz"] > 0)
        sell_ntl = sum(t["notional"] for t in recent if t["signed_sz"] < 0)
        cvd_ntl = buy_ntl - sell_ntl

        # rolling 5m sigma of window_ms-sized CVDs (estimated by chunking 5m into N buckets)
        if len(window_5m) >= 10 and window_ms > 0:
            n_buckets = max(1, 300_000 // window_ms)
            bucket_size = 300_000 // n_buckets
            buckets = [0.0] * n_buckets
            base_ts = now_ms - 300_000
            for t in window_5m:
                idx = int((t["ts"] - base_ts) // bucket_size)
                if 0 <= idx < n_buckets:
                    buckets[idx] += t["signed_sz"]
            mean = sum(buckets) / n_buckets
            var = sum((b - mean) ** 2 for b in buckets) / max(1, n_buckets - 1)
            sigma = var ** 0.5
            z = (cvd_size - mean) / sigma if sigma > 0 else 0.0
        else:
            sigma = 0.0
            z = 0.0

        return {
            "window_ms": window_ms,
            "n_trades": len(recent),
            "cvd_size": round(cvd_size, 6),
            "cvd_notional": round(cvd_ntl, 2),
            "buy_notional": round(buy_ntl, 2),
            "sell_notional": round(sell_ntl, 2),
            "rolling_5m_sigma": round(sigma, 6),
            "z_score": round(z, 3),
        }

    def push_mark(self, coin: str, m: dict) -> None:
        with self._lock:
            self.marks[coin].append(m)
            self.last_update["binance_ws"] = time.time()

    def push_funding(self, coin: str, ts_ms: int, rate: float, venue: str = "binance") -> None:
        with self._lock:
            self.funding_latest[coin] = {"ts": ts_ms, "rate": rate, "venue": venue}
            with _DB_LOCK:
                self.db.execute(
                    "INSERT OR REPLACE INTO funding(coin,ts,rate,venue) VALUES(?,?,?,?)",
                    (coin, ts_ms, rate, venue),
                )

    # -------- readers --------

    def get_klines(self, coin: str, tf: str, n: int) -> list[dict]:
        with self._lock:
            dq = self.klines.get((coin, tf))
            if dq:
                return list(dq)[-n:]
        # cold-load from SQLite (thread-local connection — WAL allows concurrent
        # reads alongside the WS-thread writer)
        rows = self.db.execute(
            "SELECT open_ts,open_px,high_px,low_px,close_px,volume FROM klines "
            "WHERE coin=? AND tf=? ORDER BY open_ts DESC LIMIT ?",
            (coin, tf, n),
        ).fetchall()
        return [
            {"open_ts": r["open_ts"], "open": r["open_px"], "high": r["high_px"],
             "low": r["low_px"], "close": r["close_px"], "volume": r["volume"]}
            for r in reversed(rows)
        ]

    def get_liqs(self, since_ms: int = 0, coin: Optional[str] = None) -> list[dict]:
        with self._lock:
            out = [e for e in self.liqs if e["ts"] >= since_ms and (coin is None or e["coin"] == coin)]
        return out

    def get_mark(self, coin: str) -> dict:
        with self._lock:
            dq = self.marks.get(coin)
            latest = dq[-1] if dq else None
        return latest or {"coin": coin, "ts": 0, "binance_mid": None, "hl_mid": None}

    def get_funding(self, coin: str, hours: int) -> list[dict]:
        since = int((time.time() - hours * 3600) * 1000)
        rows = self.db.execute(
            "SELECT ts, rate, venue FROM funding WHERE coin=? AND ts>=? ORDER BY ts ASC",
            (coin, since),
        ).fetchall()
        return [dict(r) for r in rows]

    # -------- flushers --------

    def flush_klines(self) -> int:
        """Write all in-memory klines to SQLite. Called hourly by scheduler."""
        with self._lock:
            snapshot = {k: list(v) for k, v in self.klines.items()}
        n = 0
        with _DB_LOCK:
            for (coin, tf), bars in snapshot.items():
                for b in bars:
                    self.db.execute(
                        "INSERT OR REPLACE INTO klines(coin,tf,open_ts,open_px,high_px,low_px,close_px,volume) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (coin, tf, b["open_ts"], b["open"], b["high"], b["low"], b["close"], b["volume"]),
                    )
                    n += 1
        self.last_update["binance_flush"] = time.time()
        return n

    def flush_liqs(self) -> int:
        """Write liq events newer than last flush cursor to SQLite. Every 5min."""
        with self._lock:
            cursor = self._last_flushed_liq_ts
            # snapshot only NEW events
            snapshot = [e for e in self.liqs if e["ts"] > cursor]
            if snapshot:
                self._last_flushed_liq_ts = max(e["ts"] for e in snapshot)
        n = 0
        with _DB_LOCK:
            for e in snapshot:
                self.db.execute(
                    "INSERT INTO liq_events(ts,coin,side,qty,price,usd) VALUES(?,?,?,?,?,?)",
                    (e["ts"], e["coin"], e["side"], e["qty"], e["price"], e["usd"]),
                )
                n += 1
            # prune SQLite to last 7d
            cutoff = int((time.time() - 7 * 86400) * 1000)
            self.db.execute("DELETE FROM liq_events WHERE ts < ?", (cutoff,))
        self.last_update["liq_flush"] = time.time()
        return n

    def flush_hlp_history(self) -> int:
        """Persist HLP positioning history to SQLite (5min cadence from poller).
        Called by the poller on every successful poll; survives redeploys."""
        if not hasattr(self, "hlp_history"):
            return 0
        n = 0
        with _DB_LOCK:
            for coin, hist_deque in self.hlp_history.items():
                if not hist_deque:
                    continue
                # Only persist the latest entry — earlier entries already on disk
                ts, net_usd = hist_deque[-1]
                try:
                    self.db.execute(
                        "INSERT OR IGNORE INTO hlp_history(coin, ts, net_usd) VALUES(?,?,?)",
                        (coin, ts, net_usd),
                    )
                    n += 1
                except Exception:
                    pass
            # prune SQLite to last 30d
            cutoff = int((time.time() - 30 * 86400) * 1000)
            self.db.execute("DELETE FROM hlp_history WHERE ts < ?", (cutoff,))
        self.last_update["hlp_flush"] = time.time()
        return n

    def cold_load_hlp(self) -> int:
        """Rehydrate hlp_history deque from SQLite on boot. Returns total rows loaded."""
        if not hasattr(self, "hlp_history"):
            from collections import defaultdict, deque as _dq
            self.hlp_history = defaultdict(lambda: _dq(maxlen=2880))  # was 8640
        since = int((time.time() - 30 * 86400) * 1000)
        rows = self.db.execute(
            "SELECT coin, ts, net_usd FROM hlp_history WHERE ts >= ? ORDER BY ts ASC",
            (since,),
        ).fetchall()
        with self._lock:
            for r in rows:
                self.hlp_history[r["coin"]].append((r["ts"], r["net_usd"]))
        return len(rows)

    def cold_load(self, hours_klines: int = 24, hours_liqs: int = 24) -> None:
        """On boot: rehydrate ring buffers from SQLite."""
        since_kline = int((time.time() - hours_klines * 3600) * 1000)
        kline_rows = self.db.execute(
            "SELECT coin,tf,open_ts,open_px,high_px,low_px,close_px,volume FROM klines "
            "WHERE open_ts >= ? ORDER BY coin, tf, open_ts ASC",
            (since_kline,),
        ).fetchall()
        with self._lock:
            for r in kline_rows:
                self.klines[(r["coin"], r["tf"])].append({
                    "open_ts": r["open_ts"], "open": r["open_px"], "high": r["high_px"],
                    "low": r["low_px"], "close": r["close_px"], "volume": r["volume"],
                })
        since_liq = int((time.time() - hours_liqs * 3600) * 1000)
        liq_rows = self.db.execute(
            "SELECT ts,coin,side,qty,price,usd FROM liq_events WHERE ts>=? ORDER BY ts ASC",
            (since_liq,),
        ).fetchall()
        with self._lock:
            for r in liq_rows:
                self.liqs.append({"ts": r["ts"], "coin": r["coin"], "side": r["side"],
                                  "qty": r["qty"], "price": r["price"], "usd": r["usd"]})
            # Seed cursor so the cold-loaded events are not re-flushed
            if liq_rows:
                self._last_flushed_liq_ts = max(r["ts"] for r in liq_rows)

    def stats(self) -> dict:
        with self._lock:
            return {
                "ws_alive": dict(self.ws_alive),
                "last_update": dict(self.last_update),
                "kline_keys": len(self.klines),
                "kline_bars_total": sum(len(v) for v in self.klines.values()),
                "liq_events": len(self.liqs),
                "mark_coins": len(self.marks),
                "funding_coins": len(self.funding_latest),
                "hl_positions": len(self.hl_positions),
                "hl_fills_buffered": len(self.hl_fills),
            }
