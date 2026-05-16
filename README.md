# sentinel-portfolio-manager

Lean rebuild of MULTICA. Binance signals, Hyperliquid execution. 4 services. Sentinel-audited MODERATE 7/7.

**Single source of truth:** [`SPEC.md`](SPEC.md)
**Build plan:** [`WORKFLOW.md`](WORKFLOW.md)

## Services

| Service | Role |
|---|---|
| `signal-bus` | One Binance WS + one HL WS. HTTP API to the rest of the stack. |
| `strategy-runner` | 9 strategies, scan loop, position loop, HL orders. |
| `pm` | Pre-trade gate (Rule 5b), lifecycle, capital fractions. |
| `monitor` | Cron routines firing Claude API for autonomous health checks. |

## Local dev

```bash
pip install -r requirements.txt
pytest tests/
```

## Status

| Session | Status |
|---|---|
| 1 — scaffold + common/ | done |

Currently at: end of Session 1. Do not advance to Session 2 without operator approval.
