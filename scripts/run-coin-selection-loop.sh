#!/usr/bin/env bash
# Production 15-minute coin-selection daemon (no SM_FAST).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_ROOT="$ROOT"
export PYTHONPATH="$ROOT/services/coin-selection/src${PYTHONPATH:+:$PYTHONPATH}"

# Production safety
unset SM_FAST 2>/dev/null || true
export COIN_SELECTION_ALLOW_SM_FAST="${COIN_SELECTION_ALLOW_SM_FAST:-0}"

# —— 代码身份：钉住**部署提交**，而不是 HEAD（ChatGpt_SOL5.6 文档A §13.1）——
#
# 若不钉：每提交一次 HEAD 就变，rule_identity.code_commit 跟着变，于是每个
# commit 都成了一个新规则段，复盘窗口会被切碎（文档B §3.1「身份不同即不同规则段」）。
# 正确做法是把身份钉在「这一次部署的是哪个提交」上，只在真正部署时更新。
#
# 事实源是 data/coin-selection/deployed-commit.txt —— 它在 data/ 下，本就不进版本控制：
# 部署产物不该是源码。若该文件不存在，才回落到 rule_manifest.resolve_code_commit()
# 的自动探测（读 .git），行为与改造前一致。
DEPLOY_COMMIT_FILE="${HERMES_DEPLOY_COMMIT_FILE:-$ROOT/data/coin-selection/deployed-commit.txt}"
if [[ -z "${HERMES_CODE_COMMIT:-}" && -f "$DEPLOY_COMMIT_FILE" ]]; then
  HERMES_CODE_COMMIT="$(tr -d '[:space:]' <"$DEPLOY_COMMIT_FILE")"
  [[ -n "$HERMES_CODE_COMMIT" ]] && export HERMES_CODE_COMMIT
fi

LOG_DIR="$ROOT/data/coin-selection/logs"
mkdir -p "$LOG_DIR" "$ROOT/data/coin-selection"
LOG_FILE="${COIN_SELECTION_LOG:-$LOG_DIR/loop.log}"
PID_FILE="${COIN_SELECTION_PID:-$ROOT/data/coin-selection/loop.pid}"
STATUS_FILE="$ROOT/data/coin-selection/loop_status.json"
CMD=(python3 -m coin_selection --loop --force)

action="${1:-fg}"
shift || true
# extra args after action
if [[ $# -gt 0 ]]; then
  CMD+=("$@")
fi

# Scanner processes only: comm must be a python interpreter AND the argv must
# carry `-m coin_selection --loop`. Never matches this script or its shell.
find_scanners() {
  local pid comm
  for pid in $(pgrep -x -f '.*' 2>/dev/null || ls /proc | grep -E '^[0-9]+$'); do
    [[ "$pid" == "$$" ]] && continue
    comm="$(cat "/proc/$pid/comm" 2>/dev/null || true)"
    case "$comm" in python*|Python*) ;; *) continue ;; esac
    if tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q -- '-m coin_selection'; then
      if tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q -- '--loop'; then
        echo "$pid"
      fi
    fi
  done
}

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null
}

case "$action" in
  stop)
    if is_running; then
      pid="$(cat "$PID_FILE")"
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.3
      done
      kill -9 "$pid" 2>/dev/null || true
      echo "stopped pid=$pid"
    else
      echo "not running"
    fi
    # Belt and braces: older starts recorded the wrapper subshell rather than the
    # scanner itself, so a stop could leave an orphaned `--loop` still writing
    # snapshots. Never leave two scanners racing on the same data dir.
    #
    # Match on the *interpreter* (comm), not just the command line: a plain
    # `pgrep -f 'coin_selection --loop'` also matches any shell whose own command
    # line happens to contain that string — including the one running this script.
    orphans="$(find_scanners)"
    if [[ -n "${orphans:-}" ]]; then
      echo "sweeping orphaned scanner(s): $(echo "$orphans" | tr '\n' ' ')"
      # shellcheck disable=SC2086
      kill $orphans 2>/dev/null || true
      sleep 1
      # shellcheck disable=SC2086
      kill -9 $(find_scanners) 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    exit 0
    ;;
  status)
    if is_running; then
      echo "running pid=$(cat "$PID_FILE")"
      [[ -f "$STATUS_FILE" ]] && cat "$STATUS_FILE"
      exit 0
    fi
    echo "not running"
    exit 1
    ;;
  restart)
    "$0" stop
    sleep 1
    exec "$0" start "$@"
    ;;
  start|bg|daemon)
    if is_running; then
      echo "already running pid=$(cat "$PID_FILE")"
      exit 0
    fi
    # write starter status
    python3 - <<PY
import json, time, os
from pathlib import Path
Path("$STATUS_FILE").write_text(json.dumps({
  "status": "starting",
  "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  "log": "$LOG_FILE",
  "cmd": " ".join(${CMD[@]@Q}),
  "sm_fast": os.environ.get("SM_FAST"),
}, indent=2), encoding="utf-8")
PY
    # background process; write pid after spawn
    (
      cd "$ROOT"
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting ${CMD[*]}" >>"$LOG_FILE"
      # `exec` replaces the subshell, so $! below is the scanner's own pid and
      # matches loop_status.json's `pid` field. Stopping it then actually stops it.
      exec "${CMD[@]}" >>"$LOG_FILE" 2>&1
    ) &
    pid=$!
    echo "$pid" >"$PID_FILE"
    sleep 0.8
    if kill -0 "$pid" 2>/dev/null; then
      echo "started pid=$pid log=$LOG_FILE"
      exit 0
    fi
    echo "failed to stay up; last log:" >&2
    tail -n 40 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    exit 1
    ;;
  fg|*)
    cd "$ROOT"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] foreground ${CMD[*]}" | tee -a "$LOG_FILE"
    echo "$$" >"$PID_FILE"
    trap 'rm -f "$PID_FILE"' EXIT
    exec "${CMD[@]}"
    ;;
esac
