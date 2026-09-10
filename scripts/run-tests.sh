#!/usr/bin/env bash
# All Python unit suites (stdlib runners — no pytest required) + the acceptance gate.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3
export PYTHONPATH="$ROOT/services/coin-selection/src:$ROOT/services/market-ingest/src:$ROOT/services/dmr-adapter/src:$ROOT/services/dmr-executor/src:$ROOT/contracts:$ROOT/contracts/python${PYTHONPATH:+:$PYTHONPATH}"

fail=0
# ChatGpt_SOL5.6 文档B §18.5：adapter / executor 的执行守卫套件必须进同一条 CI 流水线，
# 否则「两道边界共用同一个正向 allow-only 谓词」就没有机器证据。
for t in services/coin-selection/tests/*.py services/market-ingest/tests/*.py \
         services/dmr-adapter/tests/*.py services/dmr-executor/tests/*.py; do
  out="$("$PY" "$t" 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    printf 'PASS  %-32s %s\n' "$(basename "$t")" "$(printf '%s' "$out" | tail -1)"
  else
    fail=1
    printf 'FAIL  %-32s\n%s\n' "$(basename "$t")" "$out"
  fi
done

echo
echo "== CI 护栏 · 主板冻结 (scripts/check_main_frozen.py) =="
"$PY" scripts/check_main_frozen.py || fail=1

echo
echo "== CI 护栏 · 216 组合恒等式 (scripts/mcap_combo_zones.py --assert) =="
"$PY" scripts/mcap_combo_zones.py --assert || fail=1

# ChatGpt_SOL5.6 文档A 附录B/附录C：216/432 映射的不变量与 mapping_hash。
# 任何人手改 packages/config/mcap-216-v2.0.0-r1.json 都会在这里立刻红。
echo
echo "== CI 护栏 · 216/432 映射与 mapping_hash (scripts/gen_mcap_mapping_v2.py --check) =="
"$PY" scripts/gen_mcap_mapping_v2.py --check || fail=1

echo
echo "== acceptance gate · 选币榜 (scripts/verify_new_four_zone.py) =="
"$PY" scripts/verify_new_four_zone.py || fail=1

# 选币榜Y 是选币榜的 100% 复刻，必须过同一套硬约束闸。板面还没产出时跳过，
# 不算失败（比如刚 clone 完还没跑过扫描）。
if [ -f data/coin-selection-y/latest.json ]; then
  echo
  echo "== acceptance gate · 选币榜Y / v2.0.0 (--board y) =="
  "$PY" scripts/verify_new_four_zone.py --board y || fail=1
else
  echo "SKIP  选币榜Y 尚未产出（跑一轮扫描，或 scripts/replay_screener_y.py）"
fi

# 选币榜X v1.3.0 复刻 main v1.4.0：同一验收闸与独立复盘闭环；演进只改 X overrides。
if [ -f data/coin-selection-x/latest.json ]; then
  "$PY" scripts/verify_new_four_zone.py --board x || fail=1
else
  echo "SKIP  选币榜X 尚未产出（python3 scripts/replay_screener_y.py --board x）"
fi
if [ -f data/coin-selection-x/review/ledger.sqlite ]; then
  "$PY" scripts/verify_review_ledger.py --data-dir data/coin-selection-x --check-param-hash || fail=1
else
  echo "SKIP  X ledger not built (python3 scripts/replay_screener_y.py --board x)"
fi

# 选币榜X 的两个专用工具必须进聚合验收，否则「新增工具已实现并测试」只是口头承诺。
#   verify_screener_x.py    按 X 实际形态选口径：CLONE 逐字段等于 main；
#                           ADAPTED 验证 DMR⊆CONFIRMED + 方向 ceiling==DMR + 四区零触碰
#   audit_screener_x_v13.py 只读身份/账本审计；**不加 --require-legacy**：
#                           适配版不得冒充「旧 dual-path 严格历史认证」通过（该模式故意退 2）
echo
echo "== 选币榜X 形态验收 (scripts/verify_screener_x.py) =="
if [ -f data/coin-selection-x/latest.json ] && [ -f data/coin-selection/latest.json ]; then
  "$PY" scripts/verify_screener_x.py || fail=1
else
  echo "SKIP  X 或 main 尚未产出"
fi
echo
echo "== 选币榜X v1.3 身份/账本审计 (scripts/audit_screener_x_v13.py) =="
if [ -f data/coin-selection-x/latest.json ]; then
  "$PY" scripts/audit_screener_x_v13.py || fail=1
else
  echo "SKIP  X 尚未产出"
fi

echo
echo "== retention / disk (scripts/prune_data.py --plan, 只读) =="
"$PY" scripts/prune_data.py --plan || fail=1

echo
echo "== review ledger gate · v1.4.0 (scripts/verify_review_ledger.py) =="
if [ -f data/coin-selection/review/ledger.sqlite ]; then
  "$PY" scripts/verify_review_ledger.py || fail=1
else
  echo "SKIP  ledger not built (python3 scripts/build_review_ledger.py --reset)"
fi

# 「复盘选币」里的 v2.0.0 规则读的就是这本账本，同一套护栏必须也过。
echo
echo "== review ledger gate · v2.0.0 / 选币榜Y（含 param_hash 一致性 C14）=="
if [ -f data/coin-selection-y/review/ledger.sqlite ]; then
  "$PY" scripts/verify_review_ledger.py --data-dir data/coin-selection-y --check-param-hash || fail=1
else
  echo "SKIP  v2.0.0 ledger not built (python3 scripts/replay_screener_y.py)"
fi

# 文档A 附录 C 的读数清单 + 文档B §8 的阶段门。阶段门以 WARN 呈现，
# 不让 CI 变红 —— 它们说的是「现在还不能做什么」，不是「代码坏了」。
echo
echo "== 读数清单 + 阶段门 · 选币榜Y (scripts/check_y_readout.py) =="
if [ -f data/coin-selection-y/latest.json ]; then
  "$PY" scripts/check_y_readout.py || fail=1
else
  echo "SKIP  选币榜Y 尚未产出"
fi

# ChatGpt_SOL5.6 文档B §8.1 / §21.1：在线 ↔ 离线逐字段一致性 + 重放确定性 + 物理 coverage。
echo
echo "== 一致性与可回测验收器 · 选币榜Y ↔ 复盘选币 (scripts/verify_rule_consistency.py) =="
if [ -d data/coin-selection-y/snapshots ]; then
  "$PY" scripts/verify_rule_consistency.py --board y --sample 8 || fail=1
else
  echo "SKIP  选币榜Y 尚未产出快照"
fi

# ChatGpt_SOL5.6 文档A §17.1：三个 flags 关闭时 Y 业务输出与冻结基线逐字段一致。
echo
echo "== 影子投影零漂移取证 (scripts/shadow_project_y.py) =="
if [ -f data/coin-selection-y/latest.json ]; then
  "$PY" scripts/shadow_project_y.py --board y || fail=1
else
  echo "SKIP  选币榜Y 尚未产出"
fi

echo
echo "== frontend gates (apps/web) =="
if [ -d apps/web/node_modules ]; then
  ( cd apps/web && npm run --silent verify ) || fail=1
else
  echo "SKIP  apps/web/node_modules 未安装（cd apps/web && npm install）"
fi

exit $fail
