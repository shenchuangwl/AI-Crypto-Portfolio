#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_ROOT="$ROOT"
export PYTHONPATH="$ROOT/services/coin-selection/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m coin_selection "$@"
