#!/usr/bin/env bash
# Detach ingest + gateway + optional 15m selection loop + optional Vite.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_ROOT="$ROOT"
cd "$ROOT"

python3 "$ROOT/scripts/daemonize.py" market-ingest start
python3 "$ROOT/scripts/daemonize.py" gateway start

if [[ "${START_SELECTION_LOOP:-0}" == "1" ]]; then
  python3 "$ROOT/scripts/daemonize.py" selection-loop start
fi

if [[ "${START_VITE:-0}" == "1" ]]; then
  python3 "$ROOT/scripts/daemonize.py" web-dev start
fi

PORT="${PORT:-18080}"
echo
echo "工作台  http://127.0.0.1:${PORT}/terminal"
echo "选币榜  http://127.0.0.1:${PORT}/screener      (param-v1.4.0)"
# X v1.3.0 复刻 main v1.4.0，复盘独立；未来只经 X overrides 演进，不启动执行层。
echo "选币榜X http://127.0.0.1:${PORT}/screener-x    (param-v1.3.0，主扫描顺带投影)"
echo "选币榜Y http://127.0.0.1:${PORT}/screener-y    (param-v2.0.0，主扫描顺带投影)"
echo "复盘    http://127.0.0.1:${PORT}/review"
echo "合约    http://127.0.0.1:${PORT}/markets"
echo "K线     http://127.0.0.1:${PORT}/market/BTCUSDT"
echo "板面    http://127.0.0.1:${PORT}/api/v1/boards"
echo "健康    http://127.0.0.1:${PORT}/api/v1/health"
echo "ingest  http://127.0.0.1:18100/v1/status"
echo "开发UI  ./scripts/run-web.sh  → http://127.0.0.1:5173/terminal"
echo "停服务  ./scripts/stop-all.sh"
