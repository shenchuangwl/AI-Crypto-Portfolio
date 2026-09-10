#!/usr/bin/env bash
# Pipeline: market-ingest once → coin-selection P1 (limited symbols) → gateway check → dmr paper
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_ROOT="$ROOT"
export PYTHONPATH="$ROOT/services/market-ingest/src:$ROOT/services/coin-selection/src:$ROOT/services/dmr-adapter/src:$ROOT/services/dmr-executor/src${PYTHONPATH:+:$PYTHONPATH}"

# Isolated scratch tree: a 40-symbol smoke must never overwrite the production
# board / state_machine.json / DMR inbox that the 15m loop is maintaining.
SMOKE_DIR="${SMOKE_DIR:-$ROOT/data/smoke/p1}"
mkdir -p "$SMOKE_DIR/inbox"
export COIN_SELECTION_DATA_DIR="$SMOKE_DIR/coin-selection"
export DMR_INBOX_DIR="$SMOKE_DIR/inbox"
export MARKET_INGEST_DATA_DIR="${MARKET_INGEST_DATA_DIR:-$SMOKE_DIR/market-ingest}"
# Port 0 = ephemeral, so this coexists with a running ingest daemon on 18100.
export MARKET_INGEST_PORT="${MARKET_INGEST_PORT:-0}"

echo "== 1 market-ingest --once =="
python3 -m market_ingest --once

echo "== 2 coin-selection P1 gate1 (max 40 symbols for smoke) =="
# Plain assignment, not a command prefix: under `set -u` the argument expansion
# below runs in this shell, where a prefix-only assignment would still be unset.
GATE1_MAX_SYMBOLS="${GATE1_MAX_SYMBOLS:-40}"
export GATE1_MAX_SYMBOLS
python3 -m coin_selection --force --max-symbols "${GATE1_MAX_SYMBOLS}"

echo "== 3 latest board summary =="
COIN_SELECTION_DATA_DIR="$COIN_SELECTION_DATA_DIR" python3 - <<'PY'
import json, os
from pathlib import Path
p=Path(os.environ["COIN_SELECTION_DATA_DIR"])/"latest.json"
d=json.loads(p.read_text())
print("scan", d["meta"]["scan_id"], "long", len(d["long_pool"]), "gate1", d["meta"].get("gate1"))
if d["long_pool"]:
  print("top", d["long_pool"][0]["symbol"], d["long_pool"][0]["liquidity_grade"], d["long_pool"][0]["aqv_6d_m"])
PY

echo "== 4 dmr-adapter seed + process =="
python3 -m dmr_adapter --seed-example --process-inbox | tail -c 400
echo

echo "== 5 dmr-executor paper =="
python3 -m dmr_executor --paper --limit 10 | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["counts"], d["dmr"]["exists"])'

echo "OK p1 pipeline smoke"
