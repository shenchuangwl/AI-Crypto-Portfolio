# market-ingest (S2)

Binance **native** USDT-M REST + WebSocket ingest for selection engine.

## Features (v0.1)

- `GET /fapi/v1/exchangeInfo` → USDT PERPETUAL universe filter (exclude TRADIFI/INDEX)
- REST bootstrap: `premiumIndex`, `ticker/24hr`, sample `klines`
- WS: `!markPrice@arr@1s` + `!miniTicker@arr` with **silent-timeout reconnect**
- Hot store under `data/market-ingest/`
- Status HTTP: `http://127.0.0.1:18100/v1/status`

## Run

```bash
export HERMES_ROOT=/data120/GITHUB/Github/Hermes/Hermes_Binance_Web_Grok_02
cd $HERMES_ROOT
# one-shot bootstrap (no long WS)
PYTHONPATH=services/market-ingest/src python3 -m market_ingest --once

# long-running (WS + refresh)
PYTHONPATH=services/market-ingest/src python3 -m market_ingest
# optional: pip install websockets
```

## Env

| Var | Default |
|---|---|
| `BINANCE_FAPI_REST` | `https://fapi.binance.com` |
| `BINANCE_FAPI_WS` | `wss://fstream.binance.com` |
| `MARKET_INGEST_PORT` | `18100` |
| `MARKET_INGEST_ENABLE_WS` | `1` |
| `WS_SILENT_TIMEOUT_SEC` | `10` |
