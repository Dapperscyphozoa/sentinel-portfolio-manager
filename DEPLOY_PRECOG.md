# 🚀 PRECOG-I8C3 DEPLOY INSTRUCTIONS

## What's been done (this session)

- ✅ ICT Confluence engine built + walk-forward validated (5/5 profitable, PF 1.21, DD 17%)
- ✅ Live safety controller: kill switch, 3 circuit breakers, ATR sizing, UTC, WAL-mode SQLite
- ✅ Kronos confirmation gate built + validated (60% directional acc) — but DISABLED for v1
- ✅ Council audit: 7/7 GO_WITH_CONDITIONS, 30d survival 85-99%, 6mo 52-95%
- ✅ Concurrency stress test: 1000 ops × 20 threads, 0 errors, 3143 ops/sec
- ✅ render.yaml: new `precog` service config added
- ✅ All committed + pushed to main (30143ec)

## Council-mandated NOT-TO-DO

```
🚫 Do NOT flip KRONOS_GATE_ENABLED=1 yet
   Council 0/7 said HELPS; 4/7 NEUTRAL; 2/7 HURTS
   60% directional acc too weak to overcome cost of filtering valid ICT signals.
   Revisit after 30d paper trial of ICT alone shows positive expectancy.
```

## Deploy steps — operator does these on Render dashboard

### Step 1 — Point precog-i8c3 service at the new repo

```
1. Open Render dashboard → find "precog" service (URL precog-i8c3.onrender.com)
2. Settings → Build & Deploy:
     Repo:           Dapperscyphozoa/sentinel-portfolio-manager
     Branch:         main
     Root directory: strategy_runner
     Build command:  pip install -r ../requirements.txt
     Start command:  python3 server.py
     Auto-deploy:    ON
     Health check:   /health
3. Disk: Create persistent disk if not present
     Name:       precog-state
     Mount path: /var/data
     Size:       1 GB
```

### Step 2 — Set env vars (paste-block ready)

```bash
# Wiring into existing infra
SIGNAL_BUS_URL=https://spm-signal-bus.onrender.com
PM_URL=https://spm-pm.onrender.com
PM_AUTH_TOKEN=<set as secret, same as spm-strategy-runner>
HL_AGENT_WALLET=0xD27751815dBD5373629D0064bE85aedA349E0eD5
HL_PRIVATE_KEY=<set as secret, the Sentinel agent key>

# PAPER MODE FOR INITIAL 14-DAY TRIAL
LIVE_TRADING=0

# Only ICT engines on this service
STRATEGY_LEGACY_LOAD=0
STRATEGY_ICT_CONFLUENCE_4H_ENABLED=1
STRATEGY_ICT_CONFLUENCE_1D_ENABLED=1

# Disable all OOS engines on this service (they run on spm-strategy-runner)
STRATEGY_E01_ZFADE3S_TU_1D_ENABLED=0
STRATEGY_E07_ZFADE2S_TU_1D_ENABLED=0
STRATEGY_E08_DIP3D10_TD_1D_ENABLED=0
STRATEGY_E09_PUMP3D10_TD_1D_ENABLED=0
STRATEGY_E16_BB_FADE_HV_1D_ENABLED=0
STRATEGY_E17_BB_FADE_BT_1D_ENABLED=0
STRATEGY_E01_ZFADE3S_TU_4H_ENABLED=0
STRATEGY_E07_ZFADE2S_TU_4H_ENABLED=0
STRATEGY_E08_DIP3D7_TD_4H_ENABLED=0
STRATEGY_E16_BB_FADE_HV_4H_ENABLED=0
STRATEGY_E17_BB_FADE_BT_4H_ENABLED=0
STRATEGY_FSP_ENABLED=0
STRATEGY_LIQ_CASCADE_ENABLED=0

# Council-mandated safety
PM_FORCE_KILL_ALL=0
LIVE_SAFETY_DB=/var/data/live_safety.sqlite
COOLDOWN_DB=/var/data/cooldowns.sqlite
RISK_PCT_PER_TRADE=0.0025
LEVERAGE_LIVE=3.0
MAX_CONCURRENT_LIVE=1
CB_CONSEC_LOSSES=3
CB_DD_7D_PCT=0.10
DAILY_LOSS_LIMIT_PCT=0.02
MAX_MARGIN_PCT=0.03
MIN_TRADE_USD=10.0

# Kronos OFF for now (council verdict)
KRONOS_GATE_ENABLED=0

# Halt + state
HALT_TOKEN=<set as secret>
STATE_DIR=/var/data
HTTP_PORT=10000
```

### Step 3 — Manual deploy + smoke test

```
1. Trigger manual deploy from Render dashboard
2. Wait for build to complete (~3-5 min)
3. Verify health:
     curl https://precog-i8c3.onrender.com/health
     → should return JSON with status fields, engines registered count >= 2
4. Verify ICT engines loaded:
     curl https://precog-i8c3.onrender.com/state | jq '.engines'
     → should list ict_confluence_4h and ict_confluence_1d
5. Verify safety active:
     Render logs should show "LiveSafetyController initialized"
```

### Step 4 — Manual halt-condition tests (council requirement)

```bash
# Test 1: Kill switch
# Set PM_FORCE_KILL_ALL=1 → wait 60s → verify all new signals blocked
# Restore to 0

# Test 2: Daily halt simulation
# SSH/console into Render service or use Render shell:
sqlite3 /var/data/live_safety.sqlite "INSERT OR REPLACE INTO daily_halts 
  VALUES ('$(date -u +%Y-%m-%d)', 'manual_test', $(date +%s)000 + 3600000);"
# Verify no signals fire
# Clear: DELETE FROM daily_halts WHERE reason='manual_test';

# Test 3: Drawdown trigger (skip for safety; only test in paper)
```

## Monitoring during 14-day paper trial

```
Daily checks:
  curl precog-i8c3.onrender.com/closures?limit=50    → review fills
  curl precog-i8c3.onrender.com/state                → check halts/positions
  
Decision gate at day 14:
  IF live_PF > 0.9 AND max_DD < 15% AND n_trades >= 10:
     → flip LIVE_TRADING=1, start with $50 wallet risk
  ELSE:
     → keep paper, investigate drag (likely slippage > backtest assumption)
     → or kill ICT, revisit
```

## Rollback (one-liner if anything goes wrong)

```bash
# Instant kill: set on Render dashboard
PM_FORCE_KILL_ALL=1

# Or fully disable both ICT engines
STRATEGY_ICT_CONFLUENCE_4H_ENABLED=0
STRATEGY_ICT_CONFLUENCE_1D_ENABLED=0
```

## What to expect (council median estimates)

```
30 days:
  Account:   $491 → $520-565 expected
  Max DD:    12-17%
  Profit prob: ~85%
  Trade rate: 0.3-1/day

If profitable + survives → flip to $50 real risk
If breakeven/loss → investigate before any real money
```

## Single most dangerous assumption (council unanimous)

> Backtest PF 1.21 may drop 20-30% in live to ~0.85-1.0 due to slippage,
> fees, latency. **The 14-day paper trial exists specifically to measure
> this drag.** Do not skip it.

---

🟢 **System is GO for paper deploy. Council green-lit. Operator's call to push button.**
