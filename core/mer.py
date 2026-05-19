"""
Macro Economic Report (MER) — self-contained port from legacy portfolio-manager.

Owns its own sqlite at $STATE_DIR/mer.sqlite (default /var/data/mer.sqlite).
No deps on the slim PM's persistence layer.

Pulls free RSS feeds + Forex Factory weekly calendar. Classifies each item
into NATIONAL / GLOBAL by keyword. Ranks by recency + source weight + boost
keywords. Dedupes by normalised-title hash. Persists daily JSON snapshots.

Public surface used by core/server.py:
    get_today_snapshot() -> dict        # {day, generated_ts, internal[],
                                        #  national[], global[], next_event, version}
    get_snapshot(day_iso) -> dict
    get_recent_raw(limit, category) -> list
    pull_all() -> stats dict
    build_snapshot(day_iso=None) -> dict
    get_blackout_status() -> dict       # {active, active_event, next_event, checked_ts}
    is_blackout_active() -> bool
    poller_loop(pull_interval_sec=3600) # background thread entrypoint

Stdlib only — urllib, xml.etree, sqlite3, threading.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional
from xml.etree import ElementTree as ET

# ============================================================
# CONFIG
# ============================================================
_STATE_DIR = os.environ.get("STATE_DIR", "/var/data")
_DB_PATH = os.environ.get("MER_DB", os.path.join(_STATE_DIR, "mer.sqlite"))
USER_AGENT = "Mozilla/5.0 (compatible; PSYCHO-MER/1.0)"

RSS_FEEDS = [
    # Crypto
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "src": "CoinDesk",     "wt": 1.0, "tag": "crypto"},
    {"url": "https://www.theblock.co/rss.xml",                 "src": "The Block",    "wt": 1.0, "tag": "crypto"},
    {"url": "https://cointelegraph.com/rss",                   "src": "CoinTelegraph","wt": 0.7, "tag": "crypto"},
    {"url": "https://decrypt.co/feed",                         "src": "Decrypt",      "wt": 0.8, "tag": "crypto"},
    # World / macro
    {"url": "https://feeds.apnews.com/rss/apf-topnews",        "src": "AP",           "wt": 1.0, "tag": "world"},
    {"url": "http://feeds.bbci.co.uk/news/world/rss.xml",      "src": "BBC",          "wt": 1.0, "tag": "world"},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml",       "src": "Al Jazeera",   "wt": 0.9, "tag": "world"},
    # Markets
    {"url": "https://feeds.marketwatch.com/marketwatch/topstories/",       "src": "MarketWatch", "wt": 1.0, "tag": "markets"},
    {"url": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "src": "WSJ Markets", "wt": 0.9, "tag": "markets"},
]

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

GLOBAL_KEYWORDS = {
    "fomc", "powell", "fed ", "federal reserve", "ecb", "lagarde",
    "bank of japan", "boj ", "ueda", "bank of england", "boe ", "bailey",
    "pboc", "people's bank of china", "china policy",
    "iran", "russia", "ukraine", "taiwan", "war ", "missile", "strike",
    "sanction", "oil shock", "opec", "spr ", "gold spike",
    "btc etf", "ethereum etf", "spot etf", "etf approval", "etf flow",
    "sec ", "regulation",
    "interest rate", "rate cut", "rate hike", "rate decision",
    "inflation", "cpi", "core cpi", "ppi",
    "non-farm", "nonfarm", "nfp", "unemployment",
    "hyperliquid", "binance", "coinbase",
}

NATIONAL_KEYWORDS = {
    "rba ", "reserve bank of australia", "asx ",
    "aud cpi", "australian inflation", "australia gdp",
    "ism manufacturing", "ism services", "us retail sales",
    "uk gdp", "uk cpi",
    "japan tankan",
}

PENALIZE_KEYWORDS = {
    "moon", "x100", "gem", "altcoin season", "just bought",
    "diamond hands", "shitcoin", "memecoin pump", "100x",
}

BOOST_KEYWORDS = {
    "fomc": 5, "powell": 5, "rate cut": 5, "rate hike": 5,
    "ecb": 4, "boj": 4, "rba": 3, "pboc": 4, "boe": 3,
    "cpi": 4, "nfp": 4, "non-farm": 4, "nonfarm": 4,
    "iran": 4, "war": 5, "missile": 4, "sanctions": 4, "sanction": 3,
    "etf": 4, "spot etf": 5, "btc etf": 5, "ethereum etf": 5,
    "hack": 5, "exploit": 5, "drained": 4, "compromised": 3,
    "sec": 3, "regulation": 3, "lawsuit": 2,
    "liquidation": 3, "leverage": 2,
    "btc": 1, "ethereum": 1, "eth": 1, "solana": 1, "sol ": 1,
    "hyperliquid": 4, "perp": 2, "perps": 2,
    "oil": 2, "crude": 2, "gold": 2, "dxy": 3,
}

# Tier-1 blackout config — engines hard-blocked from firing in the window
# [event - 30min, event + 60min] when impact='high' AND title matches a
# currency's tier-1 keyword list.
TIER_1_KEYWORDS = {
    "USD": ["fomc", "federal funds", "cpi", "core cpi", "non-farm",
            "nonfarm", "nfp", "gdp", "retail sales", "ppi",
            "unemployment claims", "jobless claims"],
    "EUR": ["ecb", "deposit facility", "main refinancing", "rate decision"],
    "GBP": ["boe", "bank rate", "monetary policy"],
    "JPY": ["boj", "policy rate"],
    "AUD": ["rba", "cash rate"],
    "CNY": ["lpr", "loan prime rate"],
}
PRE_BLACKOUT_MIN = 30
POST_BLACKOUT_MIN = 60


# ============================================================
# SQLITE — own connection, own file, no deps on PM persistence
# ============================================================
_lock = threading.RLock()
_conn_holder: dict = {"conn": None}


def _open_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    c = sqlite3.connect(_DB_PATH, check_same_thread=False, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


@contextmanager
def _conn():
    """Yield the shared mer.sqlite connection. Thread-safe via RLock."""
    with _lock:
        if _conn_holder["conn"] is None:
            _conn_holder["conn"] = _open_conn()
        yield _conn_holder["conn"]


def init_schema():
    with _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS mer_items (
            hash TEXT PRIMARY KEY,
            ts_ms INTEGER NOT NULL,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT,
            summary TEXT,
            score REAL NOT NULL,
            ingested_ts INTEGER NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mer_ts ON mer_items(ts_ms DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mer_cat ON mer_items(category, score DESC)")
        c.execute("""
        CREATE TABLE IF NOT EXISTS mer_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            currency TEXT NOT NULL,
            title TEXT NOT NULL,
            impact TEXT NOT NULL,
            forecast TEXT,
            actual TEXT,
            previous TEXT,
            ingested_ts INTEGER NOT NULL,
            UNIQUE(ts_ms, currency, title)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mer_evt_ts ON mer_events(ts_ms ASC)")
        c.execute("""
        CREATE TABLE IF NOT EXISTS mer_snapshots (
            day TEXT PRIMARY KEY,
            generated_ts INTEGER NOT NULL,
            payload TEXT NOT NULL
        )""")


# ============================================================
# CLASSIFY + SCORE + HASH
# ============================================================
def _classify(title: str, source_tag: str) -> str:
    """Tag-aware classification.

    GLOBAL = events that move all markets simultaneously (Fed/ECB/BoJ rate
             decisions, CPI/NFP prints, sanctions, ETF flows, BTC-systemic).
    NATIONAL = country-scoped events with limited cross-border spillover
               (regional CB decisions, regional inflation, world-events news
               that doesn't trip a global keyword).

    Default policy by source tag:
      crypto  → GLOBAL  (crypto markets are global)
      markets → GLOBAL  (broad-market financial news)
      world   → NATIONAL (general world/regional reporting — falls back here
                          unless a tier-1 GLOBAL keyword fires)
    """
    t = (title or "").lower()
    # Explicit GLOBAL keyword match always wins
    for kw in GLOBAL_KEYWORDS:
        if kw in t:
            return "global"
    # Explicit NATIONAL keyword match
    for kw in NATIONAL_KEYWORDS:
        if kw in t:
            return "national"
    # Tag-based fallback
    tag = (source_tag or "").lower()
    if tag == "world":
        return "national"
    # markets / crypto / unknown → global
    return "global"


def _score(title: str, source_wt: float, age_hours: float) -> float:
    t = (title or "").lower()
    boost = 0.0
    for kw, w in BOOST_KEYWORDS.items():
        if kw in t:
            boost += w
    for kw in PENALIZE_KEYWORDS:
        if kw in t:
            boost -= 3
    recency = 0.5 ** (max(age_hours, 0) / 12.0)
    return (1.0 + boost) * source_wt * recency


def _hash_title(title: str) -> str:
    norm = re.sub(r'[^a-z0-9 ]+', '', (title or "").lower()).strip()
    norm = re.sub(r'\s+', ' ', norm)[:200]
    return hashlib.md5(norm.encode()).hexdigest()[:16]


# ============================================================
# RSS PARSING
# ============================================================
_PUB_FORMATS = (
    '%a, %d %b %Y %H:%M:%S %z',
    '%a, %d %b %Y %H:%M:%S %Z',
    '%a, %d %b %Y %H:%M:%S GMT',
    '%Y-%m-%dT%H:%M:%S%z',
    '%Y-%m-%dT%H:%M:%S.%f%z',
    '%Y-%m-%dT%H:%M:%SZ',
    '%Y-%m-%dT%H:%M:%S.%fZ',
    '%Y-%m-%dT%H:%M:%S',
)


def _parse_pub(pub: str) -> int:
    if not pub:
        return int(time.time() * 1000)
    pub = pub.strip()
    for fmt in _PUB_FORMATS:
        try:
            dt = datetime.strptime(pub, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return int(time.time() * 1000)


def _strip_tags(s: str) -> str:
    if not s:
        return ''
    return re.sub(r'<[^>]+>', '', s)[:500]


def _fetch_rss(url: str, timeout: int = 10) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise RuntimeError(f"fetch failed: {e}")

    items = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise RuntimeError(f"xml parse: {e}")

    for elem in root.iter():
        tag = elem.tag.lower().split('}')[-1]
        if tag not in ('item', 'entry'):
            continue
        title = ''
        link = ''
        pub = ''
        summary = ''
        for child in elem:
            ctag = child.tag.lower().split('}')[-1]
            text = (child.text or '').strip()
            if ctag == 'title':
                title = text
            elif ctag == 'link':
                link = text or child.attrib.get('href', '')
            elif ctag in ('pubdate', 'published', 'updated', 'date'):
                if not pub:
                    pub = text
            elif ctag in ('description', 'summary', 'content'):
                if not summary:
                    summary = _strip_tags(text)
        if title:
            items.append({
                "title": title,
                "link": link,
                "ts_ms": _parse_pub(pub),
                "summary": summary,
            })
    return items


# ============================================================
# PULL CYCLE
# ============================================================
def pull_all() -> dict:
    """Pull all RSS + FF calendar; insert ranked items. Returns stats."""
    init_schema()
    now_ms = int(time.time() * 1000)
    inserted = 0
    skipped_old = 0
    errors = []
    reclassified = _reclassify_existing()

    for feed in RSS_FEEDS:
        try:
            items = _fetch_rss(feed["url"])
        except Exception as e:
            errors.append({"feed": feed["src"], "err": str(e)[:200]})
            continue

        for it in items:
            age_h = (now_ms - it["ts_ms"]) / 3600_000.0
            if age_h > 72 or age_h < -2:
                skipped_old += 1
                continue
            cat = _classify(it["title"], feed.get("tag", ""))
            sc = _score(it["title"], feed["wt"], max(age_h, 0))
            h = _hash_title(it["title"])
            try:
                with _conn() as c:
                    cur = c.execute("""
                        INSERT OR IGNORE INTO mer_items
                        (hash, ts_ms, source, category, title, link, summary, score, ingested_ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (h, it["ts_ms"], feed["src"], cat,
                          it["title"][:300], (it["link"] or "")[:500],
                          (it.get("summary") or "")[:500], sc, now_ms))
                    inserted += cur.rowcount
            except Exception as e:
                errors.append({"feed": feed["src"], "err": f"insert: {e}"})

    try:
        events_added = pull_ff_calendar()
    except Exception as e:
        events_added = 0
        errors.append({"feed": "ForexFactory", "err": str(e)[:200]})

    # Trim: keep only last 7 days of items
    try:
        cutoff = now_ms - 7 * 86400_000
        with _conn() as c:
            c.execute("DELETE FROM mer_items WHERE ts_ms < ?", (cutoff,))
    except Exception:
        pass

    return {
        "items_inserted": inserted,
        "events_inserted": events_added,
        "skipped_old": skipped_old,
        "reclassified": reclassified,
        "errors": errors,
        "ts": now_ms,
    }


def _reclassify_existing() -> int:
    """Re-run classification on all existing mer_items. Used to migrate
    rows ingested under prior (broken) classification rules — e.g. when
    everything fell through to 'global' because the world-tag fallback
    wasn't differentiating regional from cross-market news. Cheap to run
    every pull cycle (~hundreds of rows max thanks to the 7-day retention)."""
    # Source-tag lookup from RSS_FEEDS so we can re-apply tag-aware rules.
    src_tag = {f["src"]: f.get("tag", "") for f in RSS_FEEDS}
    updated = 0
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT hash, title, source, category FROM mer_items"
            ).fetchall()
            for h, title, source, current_cat in rows:
                new_cat = _classify(title, src_tag.get(source, ""))
                if new_cat != current_cat:
                    c.execute(
                        "UPDATE mer_items SET category=? WHERE hash=?",
                        (new_cat, h),
                    )
                    updated += 1
    except Exception as e:
        print(f"[mer.reclassify] err: {e}", flush=True)
    return updated


def pull_ff_calendar() -> int:
    """Pull weekly FF calendar; insert High and Medium impact events."""
    req = urllib.request.Request(FF_CALENDAR_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        events = json.loads(resp.read())
    now_ms = int(time.time() * 1000)
    added = 0
    for e in events:
        try:
            impact = (e.get("impact", "") or "").lower()
            if impact not in ("high", "medium"):
                continue
            try:
                dt = datetime.fromisoformat(e["date"])
            except Exception:
                dt = datetime.strptime(e["date"][:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
            ts_ms = int(dt.timestamp() * 1000)
            with _conn() as c:
                cur = c.execute("""
                    INSERT OR IGNORE INTO mer_events
                    (ts_ms, currency, title, impact, forecast, actual, previous, ingested_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (ts_ms, e.get("country", ""), e.get("title", "")[:200], impact,
                      e.get("forecast", ""), e.get("actual", ""), e.get("previous", ""), now_ms))
                if cur.rowcount > 0:
                    added += 1
        except Exception:
            continue
    return added


# ============================================================
# SNAPSHOT GENERATION
# ============================================================
def build_snapshot(day_iso: Optional[str] = None) -> dict:
    init_schema()
    if day_iso is None:
        day_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    cutoff_ms = int(time.time() * 1000) - 24 * 3600_000

    snapshot = {
        "day": day_iso,
        "generated_ts": int(time.time() * 1000),
        "internal": _build_internal_section(),
        "national": _build_news_section("national", cutoff_ms, limit=8),
        "global":   _build_news_section("global",   cutoff_ms, limit=12),
        "next_event": _next_high_impact_event(),
        "version": "1.0",
    }

    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO mer_snapshots (day, generated_ts, payload)
            VALUES (?, ?, ?)
        """, (day_iso, snapshot["generated_ts"], json.dumps(snapshot)))
    return snapshot


def _build_news_section(cat: str, cutoff_ms: int, limit: int = 10) -> list:
    with _conn() as c:
        rows = c.execute("""
            SELECT title, link, source, ts_ms, score, summary
            FROM mer_items
            WHERE category=? AND ts_ms >= ?
            ORDER BY score DESC, ts_ms DESC
            LIMIT ?
        """, (cat, cutoff_ms, limit)).fetchall()
    return [
        {"title": r[0], "link": r[1], "source": r[2],
         "ts_ms": r[3], "score": round(r[4], 2),
         "summary": (r[5] or "")[:240]}
        for r in rows
    ]


def _build_internal_section() -> list:
    """Stack-internal macro audit data.

    Returns rows shaped {label, value} that renderInternal() draws as a
    two-column table. The whole point of this section is: 'what is the
    stack doing right now in the context of the macro picture?'

    Includes:
      - Regime classification + confidence + volatility
      - Live HL account (value, margin, unrealized PnL, withdrawable)
      - Live position book (count, net notional)
      - Paper book (engines' view of what they'd be holding)
      - 24h realized PnL (net, n trades, win rate)
      - Top live exposure (biggest single position)
      - Engines by stage (live / paper / halted)
      - Capital aligned with current regime
      - Next tier-1 macro event countdown
      - Macro blackout status
    """
    import httpx

    items: list = []
    # Port assignments per core/server.py (single source of truth):
    #   SIGNAL_BUS_PORT = 10001
    #   STRATEGY_PORT   = 10002
    #   PM_PORT         = 10003
    sb_port = int(os.environ.get("SIGNAL_BUS_PORT", "10001") or 10001)
    sr_port = int(os.environ.get("STRATEGY_PORT",   "10002") or 10002)
    pm_port = int(os.environ.get("PM_PORT",         "10003") or 10003)

    def _get(url: str, timeout: float = 3.0):
        try:
            with httpx.Client(timeout=timeout) as cli:
                r = cli.get(url)
                if r.status_code == 200:
                    return r.json()
        except Exception:
            return None
        return None

    # ─── 1. Regime ─────────────────────────────────────────────
    regime = _get(f"http://localhost:{pm_port}/regime") or {}
    if regime:
        rg = regime.get("regime", "?")
        conf = regime.get("confidence", 0) or 0
        atr_pct = (regime.get("atr_pct", 0) or 0) * 100
        slope = regime.get("ema20_slope", 0) or 0
        arrow = "↑" if slope > 0 else ("↓" if slope < 0 else "→")
        items.append({
            "label": "Regime",
            "value": f"{rg.upper()} · conf {conf*100:.0f}% · ATR {atr_pct:.2f}% · slope {arrow}",
        })

    # ─── 2. Live HL account ────────────────────────────────────
    acct = _get(f"http://localhost:{sb_port}/hl/account") or {}
    if acct:
        val = float(acct.get("value", 0) or 0)
        margin = float(acct.get("margin_used", 0) or 0)
        ntl = float(acct.get("ntl_pos", 0) or 0)
        withdrawable = float(acct.get("withdrawable", 0) or 0)
        items.append({
            "label": "Account",
            "value": f"${val:,.2f} · withdrawable ${withdrawable:,.2f}",
        })
        items.append({
            "label": "Margin / notional",
            "value": f"${margin:.2f} margin · ${ntl:.2f} notional",
        })

        # Live positions + unrealized PnL
        live_positions = acct.get("positions") or []
        if live_positions:
            upnl = sum(float(p.get("unrealized_pnl", 0) or 0) for p in live_positions)
            sign = "+" if upnl >= 0 else "-"
            items.append({
                "label": "Live positions",
                "value": f"{len(live_positions)} open · uPnL {sign}${abs(upnl):.2f}",
            })

    # ─── 3. Paper book (strategy_runner view) ──────────────────
    state = _get(f"http://localhost:{sr_port}/state", timeout=5.0) or []
    if isinstance(state, list) and state:
        long_usd = sum(float(p.get("size_usd", 0) or 0) for p in state if p.get("is_long"))
        short_usd = sum(float(p.get("size_usd", 0) or 0) for p in state if not p.get("is_long"))
        net = long_usd - short_usd
        gross = long_usd + short_usd
        net_sign = "long" if net >= 0 else "short"
        items.append({
            "label": "Paper book",
            "value": f"{len(state)} open · gross ${gross:,.0f} · net ${abs(net):,.0f} {net_sign}",
        })
        # Top exposure by coin
        by_coin: dict = {}
        for p in state:
            c = p.get("coin", "?")
            by_coin[c] = by_coin.get(c, 0) + float(p.get("size_usd", 0) or 0)
        if by_coin:
            top_coin, top_usd = max(by_coin.items(), key=lambda x: x[1])
            pct = (top_usd / gross * 100) if gross else 0
            items.append({
                "label": "Top exposure",
                "value": f"{top_coin} ${top_usd:,.0f} · {pct:.0f}% of book",
            })

    # ─── 4. 24h realized PnL (paper) ───────────────────────────
    since_24h = time.time() - 86400
    attr = _get(f"http://localhost:{sr_port}/attribution?since={since_24h}", timeout=5.0) or {}
    if attr and attr.get("engines"):
        engines_24h = attr["engines"]
        net_pnl = sum(float(e.get("net_pnl", 0) or 0) for e in engines_24h)
        n = sum(int(e.get("n", 0) or 0) for e in engines_24h)
        wins = sum(int(e.get("wins", 0) or 0) for e in engines_24h)
        wr_pct = (wins / n * 100) if n else 0
        sign = "+" if net_pnl >= 0 else "-"
        items.append({
            "label": "24h PnL",
            "value": f"{sign}${abs(net_pnl):.2f} · {n} trades · {wr_pct:.0f}% WR",
        })

    # ─── 5. Engines by stage ───────────────────────────────────
    eng_data = _get(f"http://localhost:{pm_port}/engines") or {}
    engines_list = eng_data.get("engines", []) or []
    if engines_list:
        stages: dict = {}
        for e in engines_list:
            st = e.get("stage", "unknown")
            stages[st] = stages.get(st, 0) + 1
        items.append({
            "label": "Engines",
            "value": " · ".join(f"{k} {v}" for k, v in sorted(stages.items())),
        })

        # Regime alignment — fraction of capital_fraction whose affinity
        # includes the current regime
        current_regime = regime.get("regime") if regime else None
        if current_regime:
            aligned_cap = 0.0
            total_cap = 0.0
            for e in engines_list:
                cf = float(e.get("capital_fraction", 0) or 0)
                total_cap += cf
                aff = e.get("affinity") or []
                if current_regime in aff:
                    aligned_cap += cf
            if total_cap > 0:
                pct = aligned_cap / total_cap * 100
                items.append({
                    "label": "Regime aligned",
                    "value": f"{pct:.0f}% of capital in engines tuned for {current_regime}",
                })

    # ─── 6. Next tier-1 event (from mer_events) ────────────────
    try:
        init_schema()
        now_ms = int(time.time() * 1000)
        with _conn() as c:
            rows = c.execute("""
                SELECT ts_ms, currency, title
                FROM mer_events
                WHERE ts_ms > ? AND impact='high'
                ORDER BY ts_ms ASC
                LIMIT 20
            """, (now_ms,)).fetchall()
        # Find first row matching tier-1 currency keywords
        for ts_ms, currency, title in rows:
            if is_tier_1(currency, title):
                mins = int((ts_ms - now_ms) / 60_000)
                if mins < 60:
                    when = f"{mins}m"
                elif mins < 1440:
                    h = mins // 60
                    m = mins % 60
                    when = f"{h}h {m}m" if m else f"{h}h"
                else:
                    d = mins // 1440
                    h = (mins % 1440) // 60
                    when = f"{d}d {h}h" if h else f"{d}d"
                items.append({
                    "label": "Next tier-1",
                    "value": f"{currency} · {title} · in {when}",
                })
                break
    except Exception:
        pass

    # ─── 7. Blackout state ─────────────────────────────────────
    try:
        bl = get_blackout_status()
        if bl.get("active"):
            ev = bl.get("active_event") or {}
            ends = ev.get("ends_min_from_now", "?")
            items.append({
                "label": "Blackout",
                "value": f"⛔ ACTIVE · {ev.get('currency','?')} {ev.get('title','?')[:50]} · {ends}m left",
            })
        else:
            items.append({"label": "Blackout", "value": "inactive"})
    except Exception:
        pass

    return items


def _next_high_impact_event() -> Optional[dict]:
    now_ms = int(time.time() * 1000)
    with _conn() as c:
        row = c.execute("""
            SELECT ts_ms, currency, title, impact
            FROM mer_events
            WHERE ts_ms > ? AND impact = 'high'
            ORDER BY ts_ms ASC
            LIMIT 1
        """, (now_ms,)).fetchone()
    if not row:
        return None
    return {
        "ts_ms": row[0],
        "currency": row[1],
        "title": row[2],
        "impact": row[3],
        "in_hours": round((row[0] - now_ms) / 3600_000.0, 1),
        "in_min": int((row[0] - now_ms) / 60_000.0),
    }


# ============================================================
# READ ACCESSORS
# ============================================================
def get_today_snapshot() -> dict:
    """Return today's snapshot, rebuilding if missing or stale (>2h)."""
    init_schema()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as c:
        row = c.execute(
            "SELECT generated_ts, payload FROM mer_snapshots WHERE day=?", (day,)
        ).fetchone()
    if row:
        if int(time.time() * 1000) - row[0] < 2 * 3600_000:
            return json.loads(row[1])
    return build_snapshot(day)


def get_snapshot(day_iso: str) -> dict:
    init_schema()
    with _conn() as c:
        row = c.execute(
            "SELECT payload FROM mer_snapshots WHERE day=?", (day_iso,)
        ).fetchone()
    if not row:
        return {"error": "no_snapshot", "day": day_iso}
    return json.loads(row[0])


def get_recent_raw(limit: int = 50, category: Optional[str] = None) -> list:
    init_schema()
    with _conn() as c:
        if category:
            rows = c.execute("""
                SELECT title, link, source, category, ts_ms, score
                FROM mer_items WHERE category=?
                ORDER BY ts_ms DESC LIMIT ?
            """, (category, limit)).fetchall()
        else:
            rows = c.execute("""
                SELECT title, link, source, category, ts_ms, score
                FROM mer_items ORDER BY ts_ms DESC LIMIT ?
            """, (limit,)).fetchall()
    return [
        {"title": r[0], "link": r[1], "source": r[2], "category": r[3],
         "ts_ms": r[4], "score": round(r[5], 2)}
        for r in rows
    ]


# ============================================================
# BLACKOUT (Tier-1 macro events)
# ============================================================
def is_tier_1(currency: str, title: str) -> bool:
    if not currency or not title:
        return False
    kws = TIER_1_KEYWORDS.get(currency.upper(), [])
    t = title.lower()
    return any(k in t for k in kws)


def get_blackout_status() -> dict:
    """Compute current blackout state from mer_events.

    Active blackout: there exists a tier-1 event such that
        ts - 30min  ≤  now  ≤  ts + 60min
    """
    now_ms = int(time.time() * 1000)
    pre_window_start = now_ms - POST_BLACKOUT_MIN * 60_000
    forward_window = now_ms + 24 * 3600_000

    try:
        init_schema()
        with _conn() as c:
            rows = c.execute("""
                SELECT ts_ms, currency, title, impact
                FROM mer_events
                WHERE ts_ms >= ? AND ts_ms <= ? AND impact = 'high'
                ORDER BY ts_ms ASC
                LIMIT 100
            """, (pre_window_start, forward_window)).fetchall()
    except Exception as e:
        # Fail-OPEN — never block on infra issues
        return {
            "active": False,
            "active_event": None,
            "next_event": None,
            "checked_ts": now_ms,
            "error": f"db: {str(e)[:120]}",
        }

    active_event = None
    next_event = None

    for ts_ms, currency, title, impact in rows:
        if not is_tier_1(currency, title):
            continue
        in_pre = (ts_ms - PRE_BLACKOUT_MIN * 60_000) <= now_ms
        in_post = now_ms <= (ts_ms + POST_BLACKOUT_MIN * 60_000)
        if in_pre and in_post and active_event is None:
            active_event = {
                "ts_ms": ts_ms,
                "currency": currency,
                "title": title,
                "started_min_ago": int((now_ms - (ts_ms - PRE_BLACKOUT_MIN * 60_000)) / 60_000),
                "ends_min_from_now": int(((ts_ms + POST_BLACKOUT_MIN * 60_000) - now_ms) / 60_000),
            }
        if ts_ms > now_ms and next_event is None:
            next_event = {
                "ts_ms": ts_ms,
                "currency": currency,
                "title": title,
                "in_min": int((ts_ms - now_ms) / 60_000),
            }
        if active_event and next_event:
            break

    return {
        "active": active_event is not None,
        "active_event": active_event,
        "next_event": next_event,
        "checked_ts": now_ms,
        "pre_window_min": PRE_BLACKOUT_MIN,
        "post_window_min": POST_BLACKOUT_MIN,
    }


def is_blackout_active() -> bool:
    return get_blackout_status().get("active", False)


# ============================================================
# BACKGROUND POLLER
# ============================================================
_poller_thread: Optional[threading.Thread] = None


def poller_loop(pull_interval_sec: int = 3600):
    """Pull every hour. Build snapshot once per UTC day boundary."""
    last_snapshot_day = None
    first_run = True
    time.sleep(20)  # let the rest of core finish booting
    while True:
        try:
            stats = pull_all()
            print(
                f"[mer.poller] pull: items={stats['items_inserted']} "
                f"events={stats['events_inserted']} "
                f"reclassified={stats.get('reclassified', 0)} "
                f"errs={len(stats['errors'])}",
                flush=True,
            )
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Force snapshot on first run after restart — guarantees the
            # served snapshot reflects current classification rules and any
            # code changes deployed in this process.
            if day != last_snapshot_day or first_run:
                build_snapshot(day)
                last_snapshot_day = day
                first_run = False
                print(f"[mer.poller] snapshot built for {day}", flush=True)
        except Exception as e:
            print(f"[mer.poller] err: {e}", flush=True)
        time.sleep(pull_interval_sec)


def start_poller():
    """Idempotent — call once from server.py startup."""
    global _poller_thread
    if _poller_thread is not None and _poller_thread.is_alive():
        return
    _poller_thread = threading.Thread(target=poller_loop, name="mer-poller", daemon=True)
    _poller_thread.start()
    print("[mer] poller thread started", flush=True)
