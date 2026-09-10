# api-gateway

Live screener API + CONFIRMED feed + SSE push.

## Run (detached)

```bash
# 在仓库根目录执行（HERMES_ROOT 默认即仓库根目录，可省略）
cd /path/to/AI-Crypto-Portfolio
python3 scripts/daemonize.py gateway start
python3 scripts/daemonize.py gateway status
python3 scripts/daemonize.py gateway stop
```

Default: `http://127.0.0.1:18080`

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/health` | includes `loop_status` |
| GET | `/api/v1/screener/latest` | live `data/coin-selection/latest.json` |
| GET | `/api/v1/screener/confirmed` | LONG/SHORT CONFIRMED only + dmr inbox msgs |
| GET | `/api/v1/screener/events` | **SSE** `screener.updated` / `confirmed.changed` |
| GET | `/api/v1/dmr/candidates` | CONFIRMED messages from inbox |
| GET | `/api/v1/selection/loop-status` | 15m daemon heartbeat |

SSE watches `latest.json` mtime every 2s and fans out to browsers.

## Frontend wiring

Vite proxies `/api` → `:18080`. Screener page:

- EventSource `/api/v1/screener/events`
- fallback poll 15s
- `ConfirmedBanner` pulses on `confirmed.changed`
