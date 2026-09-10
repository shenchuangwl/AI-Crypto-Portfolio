#!/usr/bin/env bash
# Dev frontend (Vite) for the in-repo terminal. Proxies /api → :18080.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/apps/web"
if [[ ! -d node_modules ]]; then
  npm install --no-audit --no-fund
fi
exec npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
