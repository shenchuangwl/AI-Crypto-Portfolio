#!/usr/bin/env bash
# 数据保留策略的定时入口（cron 调这个，不要直接调 python）。
#
#   ./scripts/run-retention.sh            # 执行策略（--apply）
#   DRY_RUN=1 ./scripts/run-retention.sh  # 只报告
#
# 策略本身在 packages/config/retention.json；护栏与单测见
# services/coin-selection/tests/test_retention.py。
#
# 退出码 2 = 磁盘可用空间低于 min_free_gb。cron 会把输出寄给你，
# 但真正要盯的是这个退出码。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3

LOG_DIR="$ROOT/data/coin-selection/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/retention.log"

MODE="--apply"
[ "${DRY_RUN:-0}" = "1" ] && MODE=""

{
  echo "───────────────────────────────────────────────"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] retention ${MODE:---dry-run}"
  "$PY" scripts/prune_data.py $MODE
  rc=$?
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] exit=$rc"
  exit $rc
} 2>&1 | tee -a "$LOG"

exit "${PIPESTATUS[0]}"
