# sentinel-portfolio-manager

Algorithmic trading stack. Binance/OKX signals, Hyperliquid perps execution. Operator-curated engine registry, sentinel-audited.

**Single source of truth:** [`SPEC.md`](SPEC.md) (v2.1).
**Engine registry (authoritative):** `pm/pretrade.py:ENGINE_REGISTRY`.

## Services

The runtime is collapsed into two Render services (see `render.yaml`):

| Service | Role |
|---|---|
| `core` | signal-bus + strategy-runner + pm + monitor in one process. HL execution, scan loop, position loop, pre-trade gate, drawdown halt, daily Claude routines. |
| `sniper` | Hyperliquid listing-sniper micro-service. See [`DEPLOY_SNIPER.md`](DEPLOY_SNIPER.md). |

Historical 4-service split (`signal-bus / strategy-runner / pm / monitor`) is documented in [`WORKFLOW.md`](WORKFLOW.md); the code now lives in one process per `core/server.py`.

## Engines

The registry holds ~25 engines today across GREEN / WATCH / YELLOW / RED / UNTESTED / PROVISIONAL tiers. The full table — verdict, honest PF, n, cap_frac, affinity — lives in [`SPEC.md`](SPEC.md) §3 and mirrors `pm/pretrade.py:ENGINE_REGISTRY`. If the table and the code diverge, **the code wins**.

Live capital is allocated only to engines with `cap_frac > 0`. Paper engines (`cap_frac=0`) still scan and write signals; they do not place HL orders.

## Local dev

```bash
pip install -r requirements.txt
pytest tests/                # ~355 tests
```

Some tests are stale against the current registry shape and will fail until refreshed; see commits tagged `chore(tests):`.

## Promotion lifecycle

Engines are promoted GREEN → live one at a time via `STRATEGY_<NAME>_LIVE=1`. Demotion happens automatically on:

- 4 consecutive losses per coin or 6 per engine (1h cooldown)
- 12% drawdown (1h cooldown)
- live PF < 0.74× backtest PF (rolling 30-trade window, 1h cooldown)
- 10% peak drawdown ⇒ global halt via monitor

Promotion gate metadata and verdicts live in `pm/promotion_gate.py`; the run-time pre-trade gate is `pm/pretrade.py:check`.

## Halt control

```bash
# halt single strategy
curl -X POST $CORE_URL/halt/<engine> \
     -H "X-Halt-Token: $HALT_TOKEN" -d '{"reason":"manual","actor":"operator"}'

# halt all
curl -X POST $CORE_URL/halt/all \
     -H "X-Halt-Token: $HALT_TOKEN" -d '{"reason":"manual","actor":"operator"}'
```

`HALT_TOKEN` must be set; the boot path aborts if it is missing on `core`. See `common/halt.py`.

## Post-deploy gates (operator-owned)

1. Deploy via `render.yaml`; confirm `LIVE_TRADING=0` and all `STRATEGY_*_LIVE=0` until validated.
2. Verify 24h paper uptime + WS >95% liveness on `/health`.
3. Run honest backtests in `scripts/` and check `STRATEGY_GATES.md` / `STAGE1_GATES.md`.
4. Promote GREEN engines to live one-at-a-time via `STRATEGY_<NAME>_LIVE=1`.
