#!/usr/bin/env bash
# Live smoke against a running gateway + ingest (no SM_FAST).
set -euo pipefail
GW="${GATEWAY_URL:-http://127.0.0.1:18080}"
ING="${MARKET_INGEST_URL:-http://127.0.0.1:18100}"

echo "== ingest health =="
curl -fsS "$ING/health"
echo
echo "== ingest universe stats =="
python3 - <<PY
import json, urllib.request
u = json.load(urllib.request.urlopen("$ING/v1/universe/stats", timeout=15))
print(json.dumps(u, ensure_ascii=False, indent=2)[:2000])
assert u.get("count", 0) > 100, u
stats = u.get("stats") or {}
print("selected", stats.get("selected_count"), "tradifi_ct", stats.get("tradifi_perpetual_contract_type"))
PY

echo "== gateway health =="
curl -fsS "$GW/api/v1/health" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"], "live", d.get("selection_latest_exists"), "web", d.get("web_dist_exists"))'

echo "== gateway universe =="
python3 - <<PY
import json, urllib.request
u = json.load(urllib.request.urlopen("$GW/api/v1/markets/universe", timeout=15))
print("count", u["count"], "source", u.get("source"), "sample", [s["symbol"] for s in u["symbols"][:5]])
assert u["count"] > 100
assert all(s.get("market_kind") != "tradifi_perp" for s in u["symbols"])
PY

echo "== live klines BTCUSDT 15m =="
python3 - <<PY
import json, urllib.request
u = json.load(urllib.request.urlopen("$GW/api/v1/market/BTCUSDT/klines?interval=15m&limit=20", timeout=20))
print("source", u.get("source"), "bars", len(u.get("bars") or []))
assert u.get("source") in ("binance_fapi", "market-ingest", "cache"), u.get("source")
assert len(u.get("bars") or []) >= 5
print("last close", u["bars"][-1]["close"], "time", u["bars"][-1]["time"])
PY

echo "== screener latest =="
python3 - <<PY
import json, urllib.request
u = json.load(urllib.request.urlopen("$GW/api/v1/screener/latest", timeout=15))
meta = u.get("meta") or {}
print("scan", meta.get("scan_id"), "source", meta.get("api_source"), "long", len(u.get("long_pool") or []))
PY

echo "== SSE hello =="
python3 - <<'PY'
import socket
from urllib.request import Request, urlopen
req = Request("http://127.0.0.1:18080/api/v1/screener/events", headers={"Accept": "text/event-stream"})
with urlopen(req, timeout=8) as resp:
    buf = b""
    while b"event: hello" not in buf and len(buf) < 4096:
        chunk = resp.read(256)
        if not chunk:
            break
        buf += chunk
print(buf.decode("utf-8", "replace")[:400])
assert b"event: hello" in buf
PY

echo "OK live smoke"
