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
# 在仓库根目录执行（HERMES_ROOT 默认即仓库根目录，可省略）
cd /path/to/AI-Crypto-Portfolio
# one-shot bootstrap (no long WS)
PYTHONPATH=services/market-ingest/src python3 -m market_ingest --once

# long-running (WS + refresh)
PYTHONPATH=services/market-ingest/src python3 -m market_ingest
# REST-only loop
PYTHONPATH=services/market-ingest/src python3 -m market_ingest --no-ws
# requires: pip install -r requirements.txt (websockets)
```

## Env

| Var | Default |
|---|---|
| `BINANCE_FAPI_REST` | `https://fapi.binance.com` |
| `BINANCE_FUTURES_WS` | `wss://fstream.binance.com` |
| `BINANCE_FAPI_WS_PATHS` | `/stream`（不要改成 `/ws`，可能连上后静默） |
| `MARKET_INGEST_HOST` | `127.0.0.1` |
| `MARKET_INGEST_PORT` | `18100`（`0` = 随机端口） |
| `MARKET_INGEST_ENABLE_WS` | `1` |
| `MARKET_INGEST_DATA_DIR` | `data/market-ingest` |
| `WS_SILENT_TIMEOUT_SEC` | `12` |
| `REST_TIMEOUT_SEC` | `20` |
