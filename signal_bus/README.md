# signal-bus

One Binance WS connection + one HL WS connection. Serves all data over HTTP to the rest of the stack.
Eliminates the N×HL polling that throttled the legacy stack.

## Endpoints

| Path | Notes |
|---|---|
| `GET /health` | WS liveness, cache sizes, last-update timestamps |
| `GET /candles/{coin}/{tf}?n=200` | OHLCV from in-memory ring buffer (cold-loads from SQLite) |
| `GET /liq?since=<ms>&coin=<optional>` | Binance forceOrder events |
| `GET /funding/{coin}?hours=12` | Funding rate history (from markPrice stream, venue=binance) |
| `GET /markprice/{coin}` | `{ts, binance_mid, hl_mid}` |
| `GET /hl/account` | HL account view (value, margin, positions) |
| `GET /hl/fills?since=<ms>` | HL fills since timestamp |
| `GET /hl/positions` | Open HL positions |

## Streams (Binance Futures combined)

- klines: 1m, 5m, 15m, 1h for every configured symbol
- `!forceOrder@arr` — all-symbol liquidation feed
- `<sym>@markPrice@1s` — 1s mark + funding rate

Symbols configured via `BINANCE_SYMBOLS` env (CSV, e.g. `BTCUSDT,ETHUSDT,...`).

## Persistence

- In-memory: 1000 bars per (coin, tf); 50000 liq events; 300 mark snapshots per coin
- SQLite (`/var/data/signal_bus.db`): klines flushed hourly, liqs every 5min, funding on every push
- Cold-load on boot pulls last 24h from SQLite back into ring buffers

## Failure modes

- WS disconnect → exponential backoff (1s → 60s cap)
- After 30s WS down, `/health.ws_alive.binance=false` (monitor alerts)
