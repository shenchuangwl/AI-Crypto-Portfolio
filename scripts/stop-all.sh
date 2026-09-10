#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_ROOT="$ROOT"
python3 "$ROOT/scripts/daemonize.py" web-dev stop || true
python3 "$ROOT/scripts/daemonize.py" selection-loop stop || true
python3 "$ROOT/scripts/daemonize.py" gateway stop || true
python3 "$ROOT/scripts/daemonize.py" market-ingest stop || true
echo "stopped"
