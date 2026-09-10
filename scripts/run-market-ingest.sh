#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_ROOT="$ROOT"
export PYTHONPATH="$ROOT/services/market-ingest/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m market_ingest "$@"
