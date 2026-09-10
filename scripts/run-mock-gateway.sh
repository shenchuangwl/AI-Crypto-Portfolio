#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_ROOT="$ROOT"
# Prefer 18080: host :8080 is frequently occupied by docker-proxy in this env
PORT="${PORT:-18080}"
exec python3 "$ROOT/services/api-gateway/mock_server.py" --host 127.0.0.1 --port "$PORT"
