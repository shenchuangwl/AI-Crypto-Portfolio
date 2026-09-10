#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_ROOT="$ROOT"
export PYTHONPATH="$ROOT/services/dmr-executor/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m dmr_executor "$@"
