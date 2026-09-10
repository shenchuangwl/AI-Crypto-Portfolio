#!/usr/bin/env bash
# End-to-end S2→S4 smoke (stdlib + optional websockets)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_ROOT="$ROOT"
export PYTHONPATH="$ROOT/services/market-ingest/src:$ROOT/services/coin-selection/src:$ROOT/services/dmr-adapter/src${PYTHONPATH:+:$PYTHONPATH}"

# Same isolation rule as smoke-p1: a smoke run must not overwrite the live board
# or the production state_machine.json that the 15m loop owns.
SMOKE_DIR="${SMOKE_DIR:-$ROOT/data/smoke/s2s4}"
mkdir -p "$SMOKE_DIR/inbox"
export COIN_SELECTION_DATA_DIR="$SMOKE_DIR/coin-selection"
export DMR_INBOX_DIR="$SMOKE_DIR/inbox"
export MARKET_INGEST_DATA_DIR="${MARKET_INGEST_DATA_DIR:-$SMOKE_DIR/market-ingest}"
export MARKET_INGEST_PORT="${MARKET_INGEST_PORT:-0}"

echo "== S2 market-ingest --once =="
python3 -m market_ingest --once

echo "== S3 coin-selection P0 =="
python3 -m coin_selection --force | head -c 800
echo

echo "== S4 dmr-adapter probe + seed example =="
python3 -m dmr_adapter --probe-dmr | head -c 600
echo
python3 -m dmr_adapter --seed-example --process-inbox | head -c 1200
echo

echo "== artifacts =="
ls -la "$MARKET_INGEST_DATA_DIR" 2>/dev/null | head -20 || true
ls -la "$COIN_SELECTION_DATA_DIR/snapshots" 2>/dev/null | head -10 || true
ls -la "$ROOT/data/dmr-adapter/accepted" 2>/dev/null | head -10 || true
echo "OK smoke finished"
