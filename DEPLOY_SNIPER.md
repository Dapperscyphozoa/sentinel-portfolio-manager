# 🎯 SNIPER DEPLOY — precog-i8c3.onrender.com

## Acknowledgment

The Render URL `precog-i8c3.onrender.com` is being repurposed for the **sniper bot**.
The service NAME is "sniper", not "precog". (Precog the strategy is dead — see SPEC §4.)
I named the prior service "precog" off the URL slug on my own. That was wrong.
This deploy fixes it.

## What the sniper does

Council-validated edge (3/3 voters): **1.5-2.1 years from $491 → $50M**.

```
1) LISTING DETECTOR
   Polls HL /info {type:"meta"} every 10s. When a coin appears that wasn't
   there last poll → fires a listing event. Survives restarts via SQLite.

2) ORACLE-LAG ENGINE
   For each new listing:
     • Fetch HL mark price (allMids endpoint)
     • Fetch Binance + Bybit ticker (USDT perps)
     • If |HL - CEX_avg| / CEX_avg > 5% → fire trade TOWARD CEX consensus
   Direction: HL below CEX → long HL.  HL above CEX → short HL.

3) EXECUTOR
   Paper mode: simulated fills with 0.3% slippage (no real orders).
   Live mode: HL market_open via SDK with 5% slippage tolerance.
   Auto-exit at +5% (TP), -5% (SL), or 30min timeout.

4) RISK CONTROLLER
   • Max 1 sniper trade per day
   • Kill switch: 3 consecutive losses (SQLite-backed)
   • Position size: 25% wallet for first 5 trades, 50% after
   • First 10 LIVE trades require operator approval (POST /approve)
   • Per-trade min $10
```

## Deploy steps on Render

### 1. Repoint precog-i8c3 service

```
Settings → Build & Deploy:
  Repo:           Dapperscyphozoa/sentinel-portfolio-manager
  Branch:         main
  Root directory: sniper        ← CHANGED from strategy_runner
  Build command:  pip install -r ../requirements.txt
  Start command:  python3 server.py
  Auto-deploy:    ON
  Health check:   /health
  Service NAME:   can change to "sniper" in Render settings (optional; URL stays)
```

### 2. Persistent disk

```
Name:       sniper-state
Mount path: /var/data
Size:       1 GB
```

### 3. Env vars (paste-block)

```bash
HL_AGENT_WALLET=0xD27751815dBD5373629D0064bE85aedA349E0eD5
HL_PRIVATE_KEY=<the Sentinel agent key as secret>

# Paper mode — KEEP 0 until first 5 paper events go well
SNIPER_LIVE_TRADING=0

# Listing detection
SNIPER_POLL_INTERVAL_S=10
SNIPER_SETTLE_DELAY_S=5
SNIPER_LISTING_DB=/var/data/sniper_listings.sqlite

# Oracle-lag threshold (5% divergence to fire)
SNIPER_DIVERGENCE_THRESHOLD=0.05

# Risk controls (council spec)
SNIPER_RISK_DB=/var/data/sniper_risk.sqlite
SNIPER_MAX_PER_DAY=1
SNIPER_CB_CONSEC_LOSSES=3
SNIPER_SIZE_PCT=0.50
SNIPER_LEVERAGE=5.0
SNIPER_MIN_TRADE_USD=10.0
SNIPER_MIN_ACCOUNT_USD=50.0
SNIPER_REQUIRE_APPROVAL=1
SNIPER_APPROVAL_TRADES=10
SNIPER_FORCE_KILL=0

# Auth + state
SNIPER_AUTH_TOKEN=<set as secret, used for POST /kill, /approve, /reset>
STATE_DIR=/var/data
HTTP_PORT=10000
```

### 4. Verify deployment

```bash
# Health
curl https://precog-i8c3.onrender.com/health
# Expected: { "status": "ok", "killed": false, "open_positions": 0, ... }

# Recent listings (empty on first boot)
curl https://precog-i8c3.onrender.com/listings

# Service state
curl https://precog-i8c3.onrender.com/state
```

### 5. Operator workflow when a listing fires

```bash
# 1. Service detects listing, logs:
#    "NEW LISTING DETECTED: <COIN> (universe_index=...)"
#    "Snipe decision for <COIN>: fire=True div=+8.2% ..."
#    "Risk gate blocked <COIN>: needs_operator_approval"   ← waiting for you

# 2. You approve via HTTP:
curl -X POST https://precog-i8c3.onrender.com/approve \
  -H "X-Sniper-Auth: $SNIPER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"coin":"NEWCOIN"}'
# Approval valid for 10 minutes

# 3. On next poll cycle (within 10s), sniper retries and fires the trade
# 4. Position auto-exits at TP/SL/timeout
```

### 6. Manual kill

```bash
curl -X POST https://precog-i8c3.onrender.com/kill \
  -H "X-Sniper-Auth: $SNIPER_AUTH_TOKEN" \
  -d '{"reason":"manual"}'
# All future trades blocked until /reset
```

## Expected first 30 days

```
Council median estimates:
  Listings detected:     5-15
  Trades fired:          1-3 (most listings won't have CEX equivalent
                              or divergence below 5%)
  Expected return/event: 5-15% on margin (council: 5-50% per event)
  Account 30d:           $491 → $520-650
  Max DD:                25-50% (sniper is HIGH variance, 1 bad fill stings)
```

## The honest math

```
Council validated path:    1.5-2.1 years to $50M
But these are FIRST-ORDER estimates from 3 voters.
Real-world:
  - Slower if HL listings dry up
  - Slower if institutional bots eat the same edge
  - Faster if HL lists many obscure coins with thin CEX coverage
  - Fastest path requires WS pre-announcement detection (operator must provide)
```

## What the operator must provide next

```
For phase 2 acceleration:
  1. HL Discord/Telegram listing-announcement channel access
     → would catch listings BEFORE they hit /info meta endpoint
  2. Binance new-listings WebSocket subscription
     → would catch CEX-side listings for opposite-direction snipes
  3. Decision: when to flip SNIPER_LIVE_TRADING=1
     → recommend: after 5 paper events with positive expectancy
```

---

🟢 **Service ready. Operator: repoint precog-i8c3 → rootDir=sniper, paste env, deploy.**
