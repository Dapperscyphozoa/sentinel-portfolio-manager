#!/usr/bin/env python3
"""Backfill e08 force_close PnL from raw HL candles at force-close timestamps.

The bug: BUS.markprice(coin) returned a dict, server.py:287 did float(dict)
which raised TypeError → caught → close_px defaulted to open_px → pnl=0 for
all 8 e08 force-closures on the session_audit_red_engines cull.

This script fetches real HL price at the force_close ts and rewrites the
closures row with proper pnl_usd + fees_usd.

Run from inside the live core service container:
  python3 scripts/backfill_e08_force_close_pnl.py
"""
import json, sqlite3, time, os, sys
import httpx

DB = os.environ.get("CORE_DB", "/var/data/state.db")
HL_INFO = "https://api.hyperliquid.xyz/info"

def fetch_hl_px_at(coin, ts_seconds):
    """Get HL spot mid at a given timestamp via /info candleSnapshot."""
    # 1-minute candle covering the ts
    end_ms = int(ts_seconds * 1000)
    start_ms = end_ms - 60_000
    try:
        r = httpx.post(HL_INFO, json={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1m",
                    "startTime": start_ms, "endTime": end_ms}
        }, timeout=10)
        bars = r.json() or []
        if not bars: return None
        b = bars[-1]
        # HL returns {t, T, s, i, o, h, l, c, v, n}
        return float(b.get("c") or b.get("o") or 0) or None
    except Exception as e:
        print(f"  fetch failed {coin}@{ts_seconds}: {e}")
        return None

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT * FROM closures
    WHERE close_reason LIKE 'force_close:session_audit_red_engines%'
      AND pnl_usd = 0.0
""").fetchall()

print(f"Found {len(rows)} corrupted force_close rows to backfill")
fixed = 0
total_pnl_delta = 0.0
TAKER_FEE = 0.00045

for r in rows:
    coin = r["coin"]
    close_ts = r["close_ts"]
    open_px = float(r["open_px"])
    size = float(r["size_coin"])
    is_long = int(r["is_long"])
    
    real_close = fetch_hl_px_at(coin, close_ts)
    if not real_close:
        print(f"  SKIP {coin} cloid={r["cloid"]} — could not fetch HL price")
        continue
    
    gross = (real_close - open_px) * size * (1 if is_long else -1)
    notional = open_px * size
    fees = notional * TAKER_FEE * 2
    net = gross - fees
    
    print(f"  {coin:<6} open={open_px:.4f} close_real={real_close:.4f} "
          f"size={size:.4f} gross=${gross:+.3f} fees=${fees:.3f} net=${net:+.3f}")
    
    # Update closures row
    extras = json.loads(r["extras_json"] or "{}")
    extras["backfilled"] = {
        "ts": time.time(),
        "real_close_px": real_close,
        "real_pnl_gross": gross,
        "real_pnl_net": net,
        "real_fees": fees,
        "original_pnl": r["pnl_usd"],
        "source": "HL_info_candleSnapshot_1m"
    }
    conn.execute("""
        UPDATE closures
        SET close_px=?, pnl_usd=?, fees_usd=?, extras_json=?
        WHERE cloid=?
    """, (real_close, net, fees, json.dumps(extras, default=str), r["cloid"]))
    fixed += 1
    total_pnl_delta += net  # delta vs prior $0

conn.commit()
print()
print(f"Backfilled {fixed}/{len(rows)} rows")
print(f"Total PnL correction: ${total_pnl_delta:+.2f} (these were silently absorbed by wallet earlier)")
