# Render service archive — 2026-05-25

Operator directive: cut all bloat services from the stack. Keep only:
- sentinel-trader (live DCA + FSP_v2 executor)
- precog/core (dashboard at core-o21t.onrender.com)
- sentinel (graph + council infrastructure)
- sentinel-api (frontier audit council)

## Services archived + deleted

Endpoint snapshots taken at 2026-05-25 ~20:50 UTC just before deletion.
The Render service config is in `_render_service.json` per service.

### sniper (pre-list arbitrage, srv-d84af94vikkc73912i9g)
- Operator gate: "delete and archive if not WR above 70 and making money"
- /state showed: `total_live_trades=0, trades_today=0, live_trading=0`
- 9 days since deploy (2026-05-16), zero production fires
- Verdict: FAIL — no WR (undefined, n=0), zero revenue → DELETE
- Recoverable: yes, archive includes /state + Render config

### SPM stack (7 services, srv-d840*, srv-d86d*, srv-d87g*)
- spm-bus, spm-signal-bus, spm-strategy-runner, spm-pm, spm-monitor,
  spm-sentinel-pm
- Superseded by sentinel-trader since 2026-05-22 sessions
- All running HTTP 200 but serving zero traffic from live stack
- Operator directive: cut. Verdict: DELETE

### quant-stack-dashboard (srv-d7vq5k50lvsc7384g7o0)
- Suspended pre-session; alternate dashboard, replaced by core-o21t
- Verdict: DELETE

## What's kept

```
🟢 sentinel-trader   srv-d86hb7ugvqtc73dn1aqg   live DCA + FSP_v2 executor
🟢 precog (core)     srv-d84af2n7f7vs739u6qcg   dashboard core-o21t.onrender.com
🟢 sentinel          srv-d8428e8g4nts73ep2nvg   graph + council infrastructure
🟢 sentinel-api      srv-d88npr28qa3s73de5920   17-model frontier audit council
```

## Cost impact (Render plans, monthly)

Before: 12 services (mix of starter + standard + pro)
After:  4 services
Estimated saving: ~$50-70/mo (5 standard plans × $25 = $125 less standard; 2 starter × $7 = $14 less starter; ~$140 gross delta, minus the still-running pro/standard)
