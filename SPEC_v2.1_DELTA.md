# SPEC v2.1 DELTA — 4-loss workflow + auto-audit + paper-win promotion

**Patch over SPEC v2.0 (commit 13ae4ea).** Operator directive 2026-05-18.

This document amends §7.3 (auto-cooldown rules) and §11 (migration plan) of
SPEC v2.0 with the full 4-loss workflow. Apply at the next SPEC consolidation.

---

## §7.3 — Auto-cooldown rules (REVISED)

The 4-consec-loss-per-engine rule now triggers a workflow, not just a flag:

```
  4 consec losses on engine
   ↓
  cooldown.demote_engine()  (PM-internal flag, paper_demoted=true)
   ↓
  monitor.auto_4loss_demote routine detects new row (polls every 5 min)
   ↓
  Step 1: fires sentinel audit on the engine's source code
          - Pulls source from GitHub raw
          - Uses haiku model via internal claude_client (~$0.005/audit)
          - Writes audit report to /var/data/audits/<engine>_<ts>.md
          - Records audit_path in monitor's seen_demotions table
   ↓
  Step 2: flips STRATEGY_<NAME>_LIVE=0 via Render API on strategy-runner
   ↓
  Engine now runs in PAPER MODE (existing _LIVE=0 path)
  - Signals continue to fire and are recorded
  - Trader does NOT place live HL orders for this engine
  - Paper closures land in `closures` table with extras_json.live=false
   ↓
  Auto-promote watch (every 5 min, same routine):
    For each demoted engine:
      Count last N=4 PAPER closures since demoted_ts
      If all 4 have pnl_usd > 0:
        - Flip STRATEGY_<NAME>_LIVE=1 via Render API
        - POST /reinstate/<engine> with X-Halt-Token (clears cooldown flag)
        - Record promotion in seen_demotions.promoted_ts
```

### Rule constants

| Constant | Value | Where |
|---|---|---|
| `CONSEC_LOSS_ENGINE` (lose-trigger) | 4 | `common/cooldown.py` |
| `WIN_STREAK_FOR_PROMOTE` | 4 | `monitor/routines/auto_4loss_demote.py` |
| Monitor poll interval | 300s (5 min) | `monitor/server.py` |
| Sentinel audit model | `claude-haiku-4-5-20251001` | `monitor/claude_client.py` DEFAULT_MODEL |
| Daily API budget | $5 | `DAILY_API_BUDGET_USD` env, monitor service |

### Manual override

Operator can always:
- `POST /reinstate/<engine>` (X-Halt-Token auth) — clears cooldown flag immediately
- Flip `STRATEGY_<NAME>_LIVE=1` env on strategy-runner — restore live trading
  (the monitor routine will not re-demote unless 4 new live losses accrue)

### Failure modes & fail-soft semantics

| Failure | Effect |
|---|---|
| Render API down / no token | env flip skipped, engine stays at current _LIVE state |
| GitHub source fetch fails | sentinel audit skipped, demote/promote still proceeds |
| claude_client budget exceeded | audit skipped (logged), demote/promote still proceeds |
| Monitor routine crashes | next 5-min cycle retries |
| PM /demotions endpoint unreachable | falls back to direct SQLite read (works only on shared disk) |

---

## §11 — Migration plan (additions)

Phase 11 (new, **completed 2026-05-18**):
- [x] Lazy `PMClient.base_url` (fixes PM service transient PM_URL crash)
- [x] `monitor/routines/auto_4loss_demote.py` implementing full workflow
- [x] `pm/server.py` exposes `GET /demotions` for monitor to read
- [x] `monitor/server.py` schedules routine every 300s
- [x] Tests: `tests/test_4loss_demote.py` extended (4 tests, all pass)

Phase 12 (open):
- [ ] Strategy-runner emits paper closures with `extras_json.live=false` — verify this is already happening for engines with `STRATEGY_<NAME>_LIVE=0`
- [ ] Monitor needs `RENDER_API_TOKEN`, `HALT_TOKEN`, `STRATEGY_RUNNER_SERVICE_ID`, `GITHUB_TOKEN` envs configured

---

## File touchpoints (v2.1 commit set)

```
common/pm_client.py          — lazy base_url property (sentinel-cleared)
monitor/server.py            — schedule auto_4loss_demote
monitor/routines/auto_4loss_demote.py  — NEW: 348 lines
pm/server.py                 — GET /demotions endpoint
tests/test_4loss_demote.py   — extended (4 tests)
SPEC_v2.1_DELTA.md           — THIS document
```

---

## Required envs on `spm-monitor` for v2.1 to function

```
RENDER_API_TOKEN              — needed to flip STRATEGY_<X>_LIVE
STRATEGY_RUNNER_SERVICE_ID    — default srv-d840lr99rddc7397jfgg
PM_URL                        — https://spm-pm.onrender.com
HALT_TOKEN                    — for POST /reinstate
GITHUB_TOKEN                  — to fetch engine source for audit
CLAUDE_CODE_API_KEY           — for sentinel audit calls
DAILY_API_BUDGET_USD          — default 5
AUDIT_DIR                     — default /var/data/audits
COOLDOWN_DB                   — default /var/data/cooldowns.sqlite
```

If any are missing, the workflow degrades to the documented fail-soft behaviour
(no system halt, just less functionality).

---

**END OF SPEC v2.1 DELTA**
