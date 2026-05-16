# sentinel-portfolio-manager

Lean rebuild of MULTICA. Binance signals, Hyperliquid execution. 4 services. Sentinel-audited MODERATE 7/7.

**Single source of truth:** [`SPEC.md`](SPEC.md)
**Build plan:** [`WORKFLOW.md`](WORKFLOW.md)

## Services (4)

| Service | Role |
|---|---|
| `signal-bus` | One Binance WS + one HL WS + optional OKX/Bybit funding WS. HTTP API to the rest of the stack. |
| `strategy-runner` | 9 strategies, scan loop, position loop, HL orders. |
| `pm` | Pre-trade gate (Rule 5b), regime detector, capital fractions, attribution. |
| `monitor` | Drawdown halt + health-check + daily report cron, with $5/day Claude API budget. |

## Strategies (9, per SPEC §3)

| Name | TF | Universe | Type |
|---|---|---|---|
| `fsp` | 1h | 30 alts | Funding Spike Predator — fresh sustained-funding entry |
| `vsq` | 1h | 24 majors | Volatility Squeeze Breakout — BB inside KC then break |
| `range_fade` | 15m | 18 mid-caps | RSI<25 + BB lower fade |
| `range_bo` | 15m | BTC/ETH/SOL/XRP/BNB | Range break with 2× vol confirmation |
| `lh1` | 1h | 24 majors+alts | Liquidation heatmap, INVERTED (sweep → continuation) |
| `fd1` | 1h | 19 majors+alts | Funding-price divergence |
| `precog` | 5m | 20 majors | Webhook consumer (HMAC) for HL Precog signals |
| `liq_cascade` | 1m | 20 majors | Forced-order cascade fader |
| `cex_dex_arb` | 1h | 20 majors | HL vs CEX funding spread (HL leg only) |

## Local dev

```bash
pip install -r requirements.txt
pytest tests/                # 97 unit tests
```

## Build sessions (per WORKFLOW.md, all complete)

| Session | Scope | Status |
|---|---|---|
| 1 | scaffold + common/ | done — 14 tests |
| 2 | signal-bus Binance side | done — 11 tests |
| 3 | signal-bus HL side | done — 4 tests |
| 4 | strategy-runner + fsp | done — 7 tests |
| 5 | range_fade + range_bo | done — 7 tests |
| 6 | vsq + backtest harness | done — 4 tests |
| 7 | fd1 + lh1 inverted | done — 10 tests |
| 8 | precog webhook + HL confluence | done — 7 tests |
| 9 | liq_cascade | done — 6 tests |
| 10 | cex_dex_arb + OKX/Bybit WS | done — 6 tests |
| 11 | pm rewrite | done — 12 tests |
| 12 | monitor + Claude routines | done — 9 tests |
| 13 | decommission legacy services | operator only — post-deploy |

## Post-deploy gates (operator-owned)

1. Deploy via render.yaml; set `LIVE_TRADING=0` everywhere
2. Verify 24h paper uptime + WS >95% liveness
3. Run `python3 scripts/honest_backtest.py` → check `STRATEGY_GATES.md`
4. Promote GREEN strategies to live one-at-a-time via `STRATEGY_<NAME>_LIVE=1`
5. Decommission legacy `trend-rider-v1` + `vol-squeeze-fade` (already suspended)

## Halt control

```bash
# halt single strategy
curl -X POST https://spm-strategy-runner.onrender.com/halt/fsp \
     -H "X-Halt-Token: $HALT_TOKEN" -d '{"reason":"manual","actor":"operator"}'

# halt all
curl -X POST https://spm-strategy-runner.onrender.com/halt/all \
     -H "X-Halt-Token: $HALT_TOKEN" -d '{"reason":"manual","actor":"operator"}'
```
