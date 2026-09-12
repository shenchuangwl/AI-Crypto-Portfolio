#!/usr/bin/env python3
"""API gateway: live screener latest + CONFIRMED feed + SSE push.

Endpoints:
  GET /api/v1/health
  GET /api/v1/boards                    (board variant registry: 选币榜 / 选币榜X / 选币榜Y)
  GET /api/v1/screener-x/latest|confirmed|events (选币榜X · param-v1.3.0-screener-x)
  GET /api/v1/screener/latest           (选币榜   · param-v1.4.0-staircase-confirm-dmr)
  GET /api/v1/screener/confirmed
  GET /api/v1/screener/events   (SSE: screener.updated / confirmed.changed)
  GET /api/v1/screener-y/latest         (选币榜Y · param-v2.0.0-screener-y)
  GET /api/v1/screener-y/confirmed
  GET /api/v1/screener-y/events (SSE, 独立通道)
  GET /api/v1/markets/universe
  GET /api/v1/market/{symbol}/klines   (proxies market-ingest / Binance fapi)
  GET /api/v1/dmr/candidates
  GET /api/v1/selection/loop-status
  GET /api/v1/review/coverage|summary|trades   (?board=main|x|y 选账本，默认 main)
  GET /api/v1/review/symbols/{symbol}

板面变体（选币榜 / 选币榜Y）的路径、参数版本、数据目录全部来自
``packages/config/board-variants.json``；本文件不硬编码第二块板面的任何常量。
选币榜X v1.3.0 复刻 main v1.4.0，独立复盘闭环；后续仅 X overrides 同步 YAML 演进。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
import traceback
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from review_api import review_payload
except Exception:  # gateway must still serve screener if review import fails
    review_payload = None  # type: ignore

ROOT = Path(os.environ.get("HERMES_ROOT", Path(__file__).resolve().parents[2]))
EXAMPLES = ROOT / "contracts" / "examples"
SELECTION_LATEST = Path(
    os.environ.get(
        "SELECTION_LATEST",
        str(ROOT / "data" / "coin-selection" / "latest.json"),
    )
)
SELECTION_DIR = ROOT / "data" / "coin-selection"

#: 板面被判定为「陈旧」的秒数。扫描节奏是 15 分钟，两个节点没更新就是真出事了。
BOARD_STALE_AFTER_SEC = 1800


def board_health(v) -> dict:
    """单块板面的运行健康：数据在不在、有多旧、身份对不对。

    受约束板面（选币榜Y）在身份漂移 / 未授权时会**拒绝出数**，此时 latest.json
    仍然躺在磁盘上 —— 只报 ``latest_exists: true`` 会把「板面已经黑了」说成健康。
    因此这里必须同时给出 age 与 identity_status（本轮裁决四）。
    """
    latest = board_latest_path(v.key)
    info = {
        "key": v.key,
        "label": v.label,
        "parameter_version": v.parameter_version,
        "api_prefix": v.api_prefix,
        "web_route": v.web_route,
        "latest_exists": latest.is_file(),
        "ledger_exists": v.ledger_path(ROOT).is_file(),
        "dmr_executable": v.dmr_executable,
    }
    if not latest.is_file():
        info["status"] = "no_data"
        return info
    try:
        age = max(0, int(time.time() - latest.stat().st_mtime))
    except OSError:
        age = None
    info["age_sec"] = age
    info["stale"] = bool(age is not None and age > BOARD_STALE_AFTER_SEC)
    try:
        meta = (load_json_file(latest) or {}).get("meta") or {}
        ri = meta.get("rule_identity") or {}
        info["scan_id"] = meta.get("scan_id")
        info["rule_revision"] = ri.get("rule_revision")
        info["identity_status"] = ri.get("identity_status")
        if ri.get("identity_drift"):
            info["identity_drift"] = ri["identity_drift"]
        if ri.get("identity_drift_acknowledged"):
            info["identity_drift_acknowledged"] = ri["identity_drift_acknowledged"]
    except Exception:  # noqa: BLE001
        info["identity_status"] = "unreadable"
    # 陈旧优先：板面停更是运维要先看到的事实，身份漂移是它最可能的原因。
    if info.get("stale"):
        info["status"] = "stale"
    elif info.get("identity_status") == "DRIFT":
        info["status"] = "drift"
    else:
        info["status"] = "ok"
    return info


DMR_INBOX = ROOT / "data" / "dmr-adapter" / "inbox"
WEB_DIST = Path(os.environ.get("WEB_DIST", str(ROOT / "apps" / "web" / "dist")))
MARKET_INGEST_URL = os.environ.get("MARKET_INGEST_URL", "http://127.0.0.1:18100").rstrip("/")
FAPI_REST = os.environ.get("BINANCE_FAPI_REST", "https://fapi.binance.com").rstrip("/")
ALLOWED_KLINE_INTERVALS = frozenset({"15m", "30m", "1h", "2h", "4h", "6h", "1d"})
USE_LIVE_SELECTION = os.environ.get("API_GATEWAY_USE_LIVE", "1") not in (
    "0",
    "false",
    "no",
)
SERVE_WEB = os.environ.get("API_GATEWAY_SERVE_WEB", "1") not in ("0", "false", "no")

# —— 板面变体（选币榜 / 选币榜Y）——
#
# 注册表是 packages/config/board-variants.json。网关自己不认识「Y」这个字，
# 它只是把每个变体的 api_prefix 映射到该变体的 data_dir。新增第三块板面
# 只需要往 JSON 里加一条，这里一行都不用改。
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))
try:
    from coin_selection.board_variants import (  # noqa: E402
        MAIN_KEY,
        BoardVariant,
        load_variants,
    )
except Exception as _e:  # 注册表模块坏了也只能少一块板面，不能让网关躺下
    sys.stderr.write(f"board variants unavailable, serving 选币榜 only: {_e}\n")
    MAIN_KEY = "main"
    BoardVariant = None  # type: ignore

    def load_variants():  # type: ignore
        return ()


def board_variants() -> list[Any]:
    """已启用的板面变体；注册表不可用时退化成「只有主板面」。"""
    try:
        return [v for v in load_variants() if v.is_enabled()]
    except Exception:
        return []


def board_by_key(key: str) -> Optional[Any]:
    for v in board_variants():
        if v.key == key:
            return v
    return None


def board_latest_path(board_key: str) -> Path:
    """该板面的 latest.json。

    主板面保留 ``SELECTION_LATEST`` 环境变量覆盖（冒烟脚本在用），行为与本次改动
    之前逐字节一致；其余板面一律走注册表里的 data_dir。
    """
    if board_key == MAIN_KEY:
        return SELECTION_LATEST
    v = board_by_key(board_key)
    return (v.data_path(ROOT) / "latest.json") if v is not None else SELECTION_LATEST


def board_data_dir(board_key: str) -> Path:
    if board_key == MAIN_KEY:
        return SELECTION_DIR
    v = board_by_key(board_key)
    return v.data_path(ROOT) if v is not None else SELECTION_DIR


def board_inbox_dir(board_key: str) -> Path:
    if board_key == MAIN_KEY:
        return DMR_INBOX
    v = board_by_key(board_key)
    return v.inbox_path(ROOT) if v is not None else DMR_INBOX


def resolve_board_path(path: str) -> tuple[Optional[str], str]:
    """``/api/v1/screener-y/latest`` → ``("y", "/latest")``。

    按 api_prefix 长度倒序匹配，``/api/v1/screener-y`` 才不会被
    ``/api/v1/screener`` 抢先吃掉（虽然当前两者不互为前缀，但别把正确性押在这上面）。
    """
    prefixes = sorted(
        ((v.api_prefix, v.key) for v in board_variants() if v.api_prefix),
        key=lambda x: -len(x[0]),
    )
    if not prefixes:
        prefixes = [("/api/v1/screener", MAIN_KEY)]
    for prefix, key in prefixes:
        if path == prefix:
            return key, "/"
        if path.startswith(prefix + "/"):
            return key, path[len(prefix):]
    return None, ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json_file(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_example(name: str) -> Any:
    return load_json_file(EXAMPLES / name)


#: 解析后的 latest.json。键 = board_key，值 = (fingerprint, snap, source)。
#: 15 分钟一扫，同一指纹下 /latest + /confirmed + SSE + health 不应各 parse 一遍 3MB。
_SNAP_LOCK = threading.Lock()
_SNAP: dict[str, tuple[str, dict, str]] = {}

#: 已编码的 /latest 响应。键 = board_key，值含 fp / raw / gz。
_WIRE_LOCK = threading.Lock()
_WIRE: dict[str, dict[str, Any]] = {}
_WIRE_BUILD_LOCKS: dict[str, threading.Lock] = {}

#: gzip 门槛：小于此的 JSON 压完可能更大，原样发送。
_GZIP_MIN_BYTES = 512


def dumps_wire(body: object) -> bytes:
    """线上 JSON：紧凑、不 pretty。indent=2 会把 Y 快照从 ~2.4MB 撑到 ~3.5MB。"""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def gzip_wire(raw: bytes) -> bytes:
    return gzip.compress(raw, compresslevel=4)


def load_screener_latest(board_key: str = MAIN_KEY) -> tuple[dict, str]:
    """某块板面的最新快照。

    次级板面（选币榜Y）没有产出时**不回落到 example** —— 那会把一份 v1.4.0 的
    示例板面冒充成 v2.0.0 的实时结果。主板面的 example 回落是既有行为，保留。
    """
    path = board_latest_path(board_key)
    fp = file_fingerprint(path)
    with _SNAP_LOCK:
        hit = _SNAP.get(board_key)
        if hit and hit[0] == fp:
            return hit[1], hit[2]
    snap: dict = {}
    source = "missing"
    if USE_LIVE_SELECTION and path.is_file():
        try:
            loaded = load_json_file(path)
            if isinstance(loaded, dict) and "meta" in loaded and "long_pool" in loaded:
                snap, source = loaded, str(path)
        except Exception as e:
            sys.stderr.write(f"live latest read failed ({board_key}): {e}\n")
    if not snap:
        if board_key != MAIN_KEY:
            return {}, "missing"
        snap, source = load_example("screener-latest.example.json"), "example"
        fp = f"example:{len(json.dumps(snap, ensure_ascii=False))}"
    with _SNAP_LOCK:
        _SNAP[board_key] = (fp, snap, source)
    return snap, source


def annotate_latest(snap: dict, source: str, board_key: str) -> dict:
    """给 /latest 补 api_source / board_key / confirmed_count，不改磁盘上的快照对象。"""
    body = dict(snap)
    meta = dict(body.get("meta") or {})
    meta["api_source"] = source
    meta["board_key"] = board_key
    meta["confirmed_count"] = extract_confirmed(snap)["count"]
    body["meta"] = meta
    return body


def _wire_build_lock(board_key: str) -> threading.Lock:
    with _WIRE_LOCK:
        lk = _WIRE_BUILD_LOCKS.get(board_key)
        if lk is None:
            lk = threading.Lock()
            _WIRE_BUILD_LOCKS[board_key] = lk
        return lk


def latest_wire(board_key: str) -> Optional[tuple[bytes, bytes]]:
    """``/latest`` 的紧凑 JSON + gzip。快照没产出时返回 None。

    同指纹只编一次；扫完立刻预热，避免公网第一击在 GIL 里 parse+gzip 2MB。
    """
    path = board_latest_path(board_key)
    fp = file_fingerprint(path)
    with _WIRE_LOCK:
        hit = _WIRE.get(board_key)
        if hit and hit.get("fp") == fp and "raw" in hit:
            return hit["raw"], hit["gz"]
    with _wire_build_lock(board_key):
        with _WIRE_LOCK:
            hit = _WIRE.get(board_key)
            if hit and hit.get("fp") == fp and "raw" in hit:
                return hit["raw"], hit["gz"]
        snap, source = load_screener_latest(board_key)
        if not snap:
            return None
        raw = dumps_wire(annotate_latest(snap, source, board_key))
        gz = gzip_wire(raw)
        with _WIRE_LOCK:
            _WIRE[board_key] = {"fp": fp, "raw": raw, "gz": gz}
        return raw, gz


def warm_latest_wires() -> None:
    """启动 / 扫完后把三块板面的 gzip 包编好，请求路径只 memcpy。"""
    keys = [v.key for v in board_variants()] or [MAIN_KEY]
    if MAIN_KEY not in keys:
        keys.insert(0, MAIN_KEY)
    for key in keys:
        try:
            latest_wire(key)
        except Exception as e:
            sys.stderr.write(f"warm latest ({key}): {e}\n")


def latest_meta(board_key: str) -> Optional[dict[str, Any]]:
    """``/meta``：页头/工作台/SSE 轮询只要 scan_id 与计数，不要 2MB 池。"""
    snap, source = load_screener_latest(board_key)
    if not snap:
        return None
    meta = snap.get("meta") or {}
    long_n = len(snap.get("long_pool") or [])
    short_n = len(snap.get("short_pool") or [])
    counts = meta.get("state_counts") or {}
    confirmed = counts.get("CONFIRMED")
    if confirmed is None:
        confirmed = extract_confirmed(snap)["count"]
    return {
        "scan_id": meta.get("scan_id"),
        "scan_timestamp_utc": meta.get("scan_timestamp_utc"),
        "parameter_version": meta.get("parameter_version"),
        "board_key": board_key,
        "api_source": source,
        "state_counts": counts,
        "occupancy": meta.get("occupancy") or {},
        "daily_unique": meta.get("daily_unique") or {},
        "control": meta.get("control") or {},
        "dmr": meta.get("dmr") or {},
        "alerts": meta.get("alerts") or [],
        "confirmed_count": confirmed,
        "long_count": long_n,
        "short_count": short_n,
        "generated_at_utc": snap.get("generated_at_utc"),
    }


def file_fingerprint(path: Path) -> str:
    if not path.is_file():
        return "missing"
    st = path.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def extract_confirmed(snap: dict) -> dict[str, Any]:
    long_c = [r for r in (snap.get("long_pool") or []) if r.get("state") == "CONFIRMED"]
    short_c = [r for r in (snap.get("short_pool") or []) if r.get("state") == "CONFIRMED"]
    meta = snap.get("meta") or {}
    return {
        "scan_id": meta.get("scan_id"),
        "scan_timestamp_utc": meta.get("scan_timestamp_utc"),
        "generated_at_utc": snap.get("generated_at_utc") or utc_now_iso(),
        "count": len(long_c) + len(short_c),
        "long": long_c,
        "short": short_c,
        "symbols": {
            "long": [r.get("symbol") for r in long_c],
            "short": [r.get("symbol") for r in short_c],
        },
        "state_counts": meta.get("state_counts") or {},
        # 计划 §13.4: never ship occupancy without the day roll-up beside it.
        "occupancy": meta.get("occupancy") or {},
        "daily_unique": meta.get("daily_unique") or {},
        "control": meta.get("control") or {},
        "dmr": meta.get("dmr") or {},
        "alerts": meta.get("alerts") or ([meta.get("alert")] if meta.get("alert") else []),
        "parameter_version": meta.get("parameter_version"),
    }


def load_dmr_confirmed_messages(
    scan_id: Optional[str] = None, board_key: str = MAIN_KEY
) -> list[dict]:
    msgs: list[dict] = []
    inbox = board_inbox_dir(board_key)
    if inbox.is_dir():
        files = sorted(inbox.glob("*.candidates.json"), key=lambda p: p.stat().st_mtime)
        # Default endpoint means the current executable DMR zone, not a history
        # aggregate. A requested scan_id still searches recent files explicitly.
        selected = files[-1:] if scan_id is None else files[-96:]
        for p in selected:
            try:
                body = load_json_file(p)
            except Exception:
                continue
            sid = body.get("scan_id")
            if scan_id and sid != scan_id:
                continue
            for m in body.get("candidates") or []:
                if m.get("state") == "CONFIRMED":
                    msgs.append(m)
    # example 兜底只给主板面：次级板面空就是空，不许拿 v1.4.0 的样例充数。
    if not msgs and not scan_id and board_key == MAIN_KEY:
        try:
            ex = load_example("dmr-candidate.example.json")
            if ex.get("state") == "CONFIRMED":
                msgs = [ex]
        except Exception:
            pass
    # newest first by score
    msgs.sort(key=lambda m: float(m.get("total_score") or 0), reverse=True)
    return msgs


def _http_get_json(url: str, timeout: float = 12.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "hermes-api-gateway/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def decode_path_symbol(raw: str) -> str:
    """Path segment → exchange symbol.

    Browsers send ``encodeURIComponent('龙虾USDT')`` once. ``urlparse`` keeps
    the percent-encoding; ``.upper()`` then ``urlencode`` again turns it into
    ``%25E9…`` and Binance returns 400. Each failed interval then synthesized
    an independent random walk (30m ~80, 2h/6h ~33) instead of the live print.
    """
    s = (raw or "").strip()
    for _ in range(3):
        nxt = unquote(s)
        if nxt == s:
            break
        s = nxt
    return s.strip()


def _normalize_fapi_klines(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in raw or []:
        if isinstance(row, (list, tuple)) and len(row) >= 6:
            out.append(
                {
                    "time": int(row[0]) // 1000,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        elif isinstance(row, dict) and "time" in row:
            t = row["time"]
            if t > 10_000_000_000:
                t = int(t) // 1000
            out.append({**row, "time": int(t)})
    return out


def fetch_live_klines(symbol: str, interval: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Prefer market-ingest (has watchdog + cache); fall back to native fapi."""
    q = urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    try:
        body = _http_get_json(f"{MARKET_INGEST_URL}/v1/klines?{q}", timeout=14.0)
        bars = body.get("bars") or []
        if bars and isinstance(bars[0], (list, tuple)):
            bars = _normalize_fapi_klines(bars)
        if bars:
            return bars, body.get("source") or "market-ingest"
    except Exception as e:
        sys.stderr.write(f"ingest klines failed: {e}\n")
    q2 = urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    raw = _http_get_json(f"{FAPI_REST}/fapi/v1/klines?{q2}", timeout=14.0)
    return _normalize_fapi_klines(raw), "binance_fapi"


def _last_price_hint(symbol: str) -> Optional[float]:
    """Best live print so a synthetic fallback stays on the real price scale."""
    try:
        body = _http_get_json(f"{MARKET_INGEST_URL}/v1/prices", timeout=4.0)
        rows = body.get("prices") if isinstance(body, dict) else body
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            for key in ("last", "last_price", "mark", "mark_price", "close"):
                v = row.get(key)
                if v is not None:
                    px = float(v)
                    if px > 0:
                        return px
    except Exception:
        return None
    return None


def synthetic_klines(
    symbol: str,
    interval: str,
    limit: int,
    *,
    last_price: Optional[float] = None,
) -> list[dict[str, Any]]:
    """One 15m random walk, bucketed to ``interval``.

    Previous fallback seeded ``sum(ord)+step`` independently per interval, so
    30m/2h/6h were three unrelated price series. Same seed + aggregation keeps
    the path continuous across the chart bar even when Binance is unreachable.
    """
    step = {"15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400}.get(
        interval, 900
    )
    base = 900
    ratio = max(1, int(step // base))
    n_fine = max(limit * ratio, limit)
    end = int(datetime.now(timezone.utc).timestamp()) // base * base
    seed = sum(ord(c) for c in symbol)
    price = float(last_price) if last_price and last_price > 0 else 20 + (seed % 80)
    state = seed & 0xFFFFFFFF

    def rnd() -> float:
        nonlocal state
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        return state / 0xFFFFFFFF

    fine: list[dict[str, Any]] = []
    for i in range(n_fine - 1, -1, -1):
        t = end - i * base
        drift = (rnd() - 0.48) * price * 0.02
        o = price
        c = max(0.01 * (last_price or 1), o + drift) if last_price else max(0.01, o + drift)
        h = max(o, c) * (1 + rnd() * 0.008)
        l = min(o, c) * (1 - rnd() * 0.008)
        v = 1000 + rnd() * 50000
        fine.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v})
        price = c

    if ratio == 1:
        return fine[-limit:]
    out: list[dict[str, Any]] = []
    for i in range(0, len(fine) - ratio + 1, ratio):
        chunk = fine[i : i + ratio]
        out.append(
            {
                "time": chunk[0]["time"],
                "open": chunk[0]["open"],
                "high": max(b["high"] for b in chunk),
                "low": min(b["low"] for b in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(b["volume"] for b in chunk),
            }
        )
    return out[-limit:]


def load_universe_payload() -> dict[str, Any]:
    try:
        body = _http_get_json(f"{MARKET_INGEST_URL}/v1/universe", timeout=8.0)
        if isinstance(body, dict) and body.get("symbols"):
            body["source"] = "market-ingest"
            return body
    except Exception as e:
        sys.stderr.write(f"ingest universe failed: {e}\n")
    path = ROOT / "data" / "market-ingest" / "universe.json"
    if path.is_file():
        body = load_json_file(path)
        body["source"] = "file"
        return body
    return {"source": "empty", "count": 0, "symbols": [], "stats": {}}


class SSEHub:
    """一块板面一个推送通道。

    选币榜与选币榜Y 各自监视自己的 ``latest.json``：Y 投影失败时，选币榜的
    ``screener.updated`` 照常推，反之亦然 —— 两个通道从不互相拖累。
    """

    def __init__(self, board_key: str = MAIN_KEY) -> None:
        self.board_key = board_key
        self._lock = threading.Lock()
        self._seq = 0
        self._last_fp = ""
        self._last_confirmed_key = ""
        self._cond = threading.Condition(self._lock)
        self._event: Optional[dict[str, Any]] = None

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        with self._cond:
            self._seq += 1
            self._event = {
                "id": self._seq,
                "type": event_type,
                "data": data,
                "at": utc_now_iso(),
            }
            self._cond.notify_all()

    def wait_event(self, last_id: int, timeout: float = 25.0) -> Optional[dict[str, Any]]:
        with self._cond:
            if self._event and self._event["id"] > last_id:
                return dict(self._event)
            self._cond.wait(timeout=timeout)
            if self._event and self._event["id"] > last_id:
                return dict(self._event)
            return None

    def poll_file(self) -> None:
        fp = file_fingerprint(board_latest_path(self.board_key))
        if fp == self._last_fp:
            return
        self._last_fp = fp
        try:
            snap, source = load_screener_latest(self.board_key)
            if not snap:
                return
            conf = extract_confirmed(snap)
            key = json.dumps(conf.get("symbols"), sort_keys=True)
            changed = key != self._last_confirmed_key
            self._last_confirmed_key = key
            meta = snap.get("meta") or {}
            payload = {
                "scan_id": meta.get("scan_id"),
                "board": self.board_key,
                "parameter_version": meta.get("parameter_version"),
                "source": source,
                "fingerprint": fp,
                "confirmed_count": conf["count"],
                "confirmed_symbols": conf["symbols"],
                "state_counts": conf["state_counts"],
                "confirmed_changed": changed,
            }
            self.publish("screener.updated", payload)
            if changed:
                self.publish("confirmed.changed", payload)
            # 新指纹立刻编 gzip，公网第一击不要再 parse 2MB。
            try:
                latest_wire(self.board_key)
            except Exception as we:
                sys.stderr.write(f"warm latest after scan ({self.board_key}): {we}\n")
        except Exception as e:
            sys.stderr.write(f"sse poll error ({self.board_key}): {e}\n")


_HUBS: dict[str, SSEHub] = {}
_HUBS_LOCK = threading.Lock()


def hub_for(board_key: str) -> SSEHub:
    with _HUBS_LOCK:
        hub = _HUBS.get(board_key)
        if hub is None:
            hub = SSEHub(board_key)
            _HUBS[board_key] = hub
        return hub


#: 主板面通道。保留这个名字是因为既有代码/脚本按 HUB 引用它。
HUB = hub_for(MAIN_KEY)


def _watcher() -> None:
    while True:
        keys = [v.key for v in board_variants()] or [MAIN_KEY]
        if MAIN_KEY not in keys:
            keys.insert(0, MAIN_KEY)
        for key in keys:
            try:
                hub_for(key).poll_file()
            except Exception as e:
                sys.stderr.write(f"watcher ({key}): {e}\n")
        time.sleep(2.0)


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesGateway/0.2"
    # HTTP/1.0 + Caddy keepalive 会把 2MB latest 卡在连接复用上；公网表现为
    # 浏览器 30s AbortError「请求超时 /api/v1/screener-x/latest」。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, Authorization, Last-Event-ID"
        )

    def _send_json_bytes(
        self, code: int, raw: bytes, gz: Optional[bytes] = None
    ) -> None:
        """按 Accept-Encoding 发 compact JSON 或预压缩 gzip。"""
        accept = (self.headers.get("Accept-Encoding") or "").lower()
        payload = raw
        encoding = None
        if "gzip" in accept:
            if gz is None and len(raw) >= _GZIP_MIN_BYTES:
                gz = gzip_wire(raw)
            if gz is not None:
                payload = gz
                encoding = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self._cors()
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code: int, body: object) -> None:
        self._send_json_bytes(code, dumps_wire(body))

    def _serve_spa(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        candidate = (WEB_DIST / rel).resolve()
        dist_root = WEB_DIST.resolve()
        if not (candidate == dist_root or dist_root in candidate.parents):
            return self._json(403, {"error": "forbidden"})
        if candidate.is_file():
            data = candidate.read_bytes()
            ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            if candidate.name == "index.html":
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
            else:
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)
            return
        index = dist_root / "index.html"
        if index.is_file():
            data = index.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(data)
            return
        return self._json(404, {"error": "web_dist_missing", "path": path})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_PUT(self) -> None:  # noqa: N802
        """OnlyCoin controls supply only; no execution or account routes."""
        if urlparse(self.path).path.rstrip('/') != '/api/v1/onlycoin/sources/screener-y/control':
            return self._json(404, {'error': 'not_found'})
        token = os.environ.get('ONLYCOIN_ADMIN_TOKEN', '')
        if not token:
            return self._json(503, {'error': 'onlycoin_control_not_configured'})
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
            return self._json(401, {'error': 'unauthorized'})
        origin = self.headers.get('Origin')
        if origin and (urlparse(origin).scheme not in ('http', 'https') or urlparse(origin).netloc != self.headers.get('Host')):
            return self._json(403, {'error': 'cross_origin_control_forbidden'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 8192:
                return self._json(413, {'error': 'invalid_control_size'})
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                return self._json(415, {'error': 'json_required'})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or type(payload.get('enabled')) is not bool:
                return self._json(400, {'error': 'strict_boolean_enabled_required'})
            if (set(payload) != {'enabled', 'expected_revision', 'request_id', 'reason'}
                    or type(payload.get('expected_revision')) is not int
                    or payload['expected_revision'] < 0
                    or any(not isinstance(payload.get(k), str) or not payload[k].strip()
                           for k in ('request_id', 'reason'))):
                return self._json(400, {'error': 'invalid_control_fields'})
            from coin_selection.onlycoin_source import set_source_control
            body = set_source_control(payload, root=ROOT, actor='authenticated-admin')
            return self._json(200, body)
        except (ValueError, TypeError) as exc:
            code = 409 if any(s in str(exc).lower() for s in ('revision', 'conflict', 'idempotency')) else 400
            return self._json(code, {'error': 'onlycoin_control_rejected', 'detail': str(exc)})
        except Exception:
            return self._json(503, {'error': 'onlycoin_control_unavailable'})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path == '/api/v1/onlycoin/live':
            from onlycoin_api import live_payload
            code, body = live_payload(parse_qs(parsed.query, keep_blank_values=True))
            return self._json(code, body)
        if path in ('/api/v1/screener-y/dmr-daily', '/api/v1/review/onlycoin'):
            try:
                from onlycoin_api import daily_payload
                code, body = daily_payload(qs, historical=path.endswith('/review/onlycoin'))
                return self._json(code, body)
            except Exception:
                return self._json(503, {'error': 'onlycoin_reader_unavailable'})
        if path == '/api/v1/onlycoin/sources/screener-y/status':
            try:
                from coin_selection.onlycoin_source import source_status
                return self._json(200, source_status(root=ROOT))
            except Exception:
                return self._json(503, {'error': 'onlycoin_source_unavailable'})

        if path in ("/health", "/api/v1/health"):
            loop_status = {}
            lp = SELECTION_DIR / "loop_status.json"
            if lp.is_file():
                try:
                    loop_status = load_json_file(lp)
                except Exception:
                    loop_status = {"status": "unreadable"}
            boards_health = [board_health(v) for v in board_variants()]
            degraded_boards = [
                b["key"] for b in boards_health if b.get("status") != "ok"
            ]
            # 循环自己报的降级也算：Y 拒绝出数时 loop_status.status=degraded。
            if str(loop_status.get("status")) not in ("ok", "", "None"):
                for k in loop_status.get("degraded_boards") or []:
                    if k not in degraded_boards:
                        degraded_boards.append(k)
            return self._json(
                200,
                {
                    "status": "degraded" if degraded_boards else "ok",
                    "service": "api-gateway",
                    "root": str(ROOT),
                    "time_utc": utc_now_iso(),
                    "selection_latest_path": str(SELECTION_LATEST),
                    "selection_latest_exists": SELECTION_LATEST.is_file(),
                    "use_live_selection": USE_LIVE_SELECTION,
                    "loop_status": loop_status,
                    "boards": boards_health,
                    "degraded_boards": degraded_boards,
                    "endpoints": [
                        "/api/v1/boards",
                        "/api/v1/screener/latest",
                        "/api/v1/screener/meta",
                        "/api/v1/screener/confirmed",
                        "/api/v1/screener/events",
                        # X v1.3.0 独立通道，复盘同源；策略复刻 main，调参只经 X overrides。
                        "/api/v1/screener-x/latest",
                        "/api/v1/screener-x/confirmed",
                        "/api/v1/screener-x/events",
                        "/api/v1/screener-y/latest",
                        "/api/v1/screener-y/confirmed",
                        "/api/v1/screener-y/events",
                        "/api/v1/markets/universe",
                        "/api/v1/market/{symbol}/klines",
                        "/api/v1/market/prices",
                        "/api/v1/dmr/candidates",
                        "/api/v1/review/coverage",
                        "/api/v1/review/summary",
                        "/api/v1/review/trades",
                        "/api/v1/review/symbols/{symbol}",
                    ],
                    "web_dist": str(WEB_DIST),
                    "web_dist_exists": WEB_DIST.is_dir(),
                    "serve_web": SERVE_WEB,
                },
            )

        if path == "/api/v1/selection/loop-status":
            lp = SELECTION_DIR / "loop_status.json"
            if not lp.is_file():
                return self._json(200, {"status": "unknown", "hint": "start scripts/run-coin-selection-loop.sh"})
            return self._json(200, load_json_file(lp))

        # —— 板面变体路由（/api/v1/screener → 选币榜，/api/v1/screener-y → 选币榜Y）——
        # 只对 /api/ 前缀做解析：SPA 的每个静态资源请求都走这里，不该为它们去 stat 注册表。
        if path.startswith("/api/"):
            board_key, sub = resolve_board_path(path)
            if board_key is not None and self._screener(board_key, sub, qs):
                return None

        if path == "/api/v1/boards":
            # 前端与运维靠它确认「有哪几块板面、各自什么参数版本、数据到没到」。
            out = []
            for v in board_variants():
                latest = board_latest_path(v.key)
                info = v.to_public_dict()
                info.update(
                    {
                        "latest_path": str(latest),
                        "latest_exists": latest.is_file(),
                        "ledger_path": str(v.ledger_path(ROOT)),
                        "ledger_exists": v.ledger_path(ROOT).is_file(),
                    }
                )
                out.append(info)
            return self._json(200, {"count": len(out), "boards": out})

        if path in (
            "/api/v1/review/coverage",
            "/api/v1/review/summary",
            "/api/v1/review/trades",
        ):
            return self._review(qs, kind=path.rsplit("/", 1)[-1])

        m = re.fullmatch(r"/api/v1/review/symbols/([^/]+)", path)
        if m:
            return self._review(qs, kind="symbol", symbol=decode_path_symbol(m.group(1)))

        if path in ("/api/v1/markets/universe", "/api/v1/universe"):
            uni = load_universe_payload()
            q = (qs.get("q", [""])[0] or "").upper()
            kind = (qs.get("kind", [""])[0] or "").lower()
            symbols = list(uni.get("symbols") or [])
            if q:
                symbols = [
                    s
                    for s in symbols
                    if q in str(s.get("symbol", "")).upper()
                    or q in str(s.get("base_asset", "")).upper()
                ]
            if kind:
                symbols = [s for s in symbols if str(s.get("market_kind", "")).lower() == kind]
            return self._json(
                200,
                {
                    "source": uni.get("source"),
                    "version": uni.get("version"),
                    "count": len(symbols),
                    "total": uni.get("count") or len(uni.get("symbols") or []),
                    "stats": uni.get("stats") or {},
                    "symbols": symbols,
                },
            )

        if path in ("/api/v1/market/prices", "/api/v1/markets/prices"):
            try:
                body = _http_get_json(f"{MARKET_INGEST_URL}/v1/prices", timeout=8.0)
                return self._json(200, {"source": "market-ingest", **(body if isinstance(body, dict) else {"prices": body})})
            except Exception as e:
                return self._json(502, {"error": "ingest_prices_failed", "detail": str(e)})

        m = re.fullmatch(r"/api/v1/market/([^/]+)/klines", path)
        if m:
            symbol = decode_path_symbol(m.group(1)).upper()
            interval = qs.get("interval", ["15m"])[0]
            if interval not in ALLOWED_KLINE_INTERVALS:
                return self._json(400, {"error": "bad_interval", "allowed": sorted(ALLOWED_KLINE_INTERVALS)})
            limit = max(1, min(int(qs.get("limit", ["200"])[0]), 1500))
            try:
                bars, source = fetch_live_klines(symbol, interval, limit)
                return self._json(
                    200,
                    {
                        "symbol": symbol,
                        "interval": interval,
                        "source": source,
                        "bars": bars,
                    },
                )
            except Exception as e:
                sys.stderr.write(f"live klines failed, mock fallback: {e}\n")
                hint = _last_price_hint(symbol)
                bars = synthetic_klines(symbol, interval, limit, last_price=hint)
                return self._json(
                    200,
                    {
                        "symbol": symbol,
                        "interval": interval,
                        "source": "synthetic",
                        "bars": bars,
                        "error": str(e),
                    },
                )

        if path == "/api/v1/dmr/candidates":
            scan_id = qs.get("scan_id", [None])[0]
            state_filter = (qs.get("state", ["CONFIRMED"])[0] or "CONFIRMED").upper()
            messages = load_dmr_confirmed_messages(scan_id)
            if state_filter != "ALL":
                messages = [m for m in messages if m.get("state") == state_filter]
            return self._json(
                200,
                {
                    "scan_id": scan_id or (messages[0]["scan_id"] if messages else None),
                    "count": len(messages),
                    "messages": messages,
                },
            )

        if path.startswith("/contracts/"):
            rel = path[len("/contracts/") :]
            contracts_root = (ROOT / "contracts").resolve()
            fpath = (contracts_root / rel).resolve()
            if fpath.is_file() and (
                fpath == contracts_root or contracts_root in fpath.parents
            ):
                data = fpath.read_bytes()
                ctype = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
                return

        if SERVE_WEB and WEB_DIST.is_dir() and not path.startswith("/api/"):
            return self._serve_spa(path)

        return self._json(
            404,
            {
                "error": "not_found",
                "path": path,
                "hint": "Try /api/v1/boards | /api/v1/screener/latest | /api/v1/screener-x/latest | /api/v1/screener-y/latest",
            },
        )

    def _screener(self, board_key: str, sub: str, qs) -> bool:
        """一块板面的全部只读接口。返回 True = 已经应答，调用方直接结束。

        选币榜（main）与选币榜Y（y）走的是**同一段代码**，只有 data_dir /
        DMR inbox / 参数版本不同 —— 这正是「Y 是 100% 复刻」在服务端的落点：
        没有第二套 handler，就没有第二套行为可以偷偷跑偏。
        """
        if sub in ("/latest", "/"):
            force_example = qs.get("source", [""])[0] == "example"
            if force_example and board_key == MAIN_KEY:
                snap, source = load_example("screener-latest.example.json"), "example"
                if not snap:
                    self._json(503, self._board_missing(board_key))
                    return True
                self._json(200, annotate_latest(snap, source, board_key))
                return True
            wire = latest_wire(board_key)
            if not wire:
                self._json(503, self._board_missing(board_key))
                return True
            self._send_json_bytes(200, wire[0], wire[1])
            return True

        if sub == "/meta":
            body = latest_meta(board_key)
            if not body:
                self._json(503, self._board_missing(board_key))
                return True
            self._json(200, body)
            return True

        if sub == "/confirmed":
            snap, source = load_screener_latest(board_key)
            if not snap:
                self._json(503, self._board_missing(board_key))
                return True
            conf = extract_confirmed(snap)
            conf["api_source"] = source
            conf["board_key"] = board_key
            conf["dmr_messages"] = load_dmr_confirmed_messages(
                conf.get("scan_id"), board_key=board_key
            )
            v = board_by_key(board_key)
            # 选币榜Y 的候选不可执行；调用方要能一眼看出来，而不是靠猜参数版本。
            conf["consumable_by_dmr"] = bool(v.dmr_executable) if v is not None else True
            self._json(200, conf)
            return True

        if sub == "/ready-confirm":
            # §10.2 "不足" side output. QUALIFIED rows that already satisfy
            # pass_confirmed but still owe dwell/streak. Display + audit only —
            # DMR must never consume this (P6: inbox is CONFIRMED-only).
            snap, source = load_screener_latest(board_key)
            if not snap:
                self._json(503, self._board_missing(board_key))
                return True
            meta = snap.get("meta") or {}
            rows = [
                r
                for r in (snap.get("long_pool") or []) + (snap.get("short_pool") or [])
                if r.get("state") == "QUALIFIED" and r.get("ready_confirm")
            ]
            rows.sort(key=lambda r: -float(r.get("score_up" if r.get("direction") == "up" else "score_down") or 0))
            self._json(
                200,
                {
                    "scan_id": meta.get("scan_id"),
                    "scan_timestamp_utc": meta.get("scan_timestamp_utc"),
                    "board_key": board_key,
                    "api_source": source,
                    "consumable_by_dmr": False,
                    "count": len(rows),
                    "rows": rows,
                    "note": "READY_CONFIRM is an audit label on QUALIFIED, not a second offer zone",
                },
            )
            return True

        if sub == "/events":
            self._sse(board_key)
            return True

        m = re.fullmatch(r"/scans/([^/]+)", sub)
        if m:
            scan_id = m.group(1)
            snap_path = board_data_dir(board_key) / "snapshots" / f"{scan_id}.json"
            if snap_path.is_file():
                self._json(200, load_json_file(snap_path))
                return True
            snap, _ = load_screener_latest(board_key)
            if not snap or snap.get("meta", {}).get("scan_id") != scan_id:
                self._json(404, {"error": "scan_not_found", "board": board_key, "scan_id": scan_id})
                return True
            self._json(200, snap)
            return True

        m = re.fullmatch(r"/symbols/([^/]+)", sub)
        if m:
            symbol = decode_path_symbol(m.group(1)).upper()
            direction = (qs.get("direction", ["up"])[0] or "up").lower()
            snap, _ = load_screener_latest(board_key)
            if not snap:
                self._json(503, self._board_missing(board_key))
                return True
            pool = snap["long_pool"] if direction == "up" else snap["short_pool"]
            current = next((r for r in pool if r["symbol"].upper() == symbol), None)
            seq = int(snap["meta"]["scan_sequence"])
            score = 50.0
            if current:
                score = float(current["score_up"] if direction == "up" else current["score_down"])
            base = datetime.fromisoformat(
                snap["meta"]["scan_timestamp_utc"].replace("Z", "+00:00")
            )
            path_nodes = []
            for i in range(max(0, seq - 8), seq + 1):
                sc = score - (seq - i) * 1.5
                state = (
                    "CONFIRMED"
                    if sc >= 78
                    else "QUALIFIED"
                    if sc >= 62
                    else "WATCH"
                    if sc >= 48
                    else "NONE"
                )
                ts = base - timedelta(minutes=(seq - i) * 15)
                path_nodes.append(
                    {
                        "scan_sequence": i,
                        "scan_timestamp_utc": ts.astimezone(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "state": state,
                        "score_up": sc if direction == "up" else max(0.0, 100 - sc),
                        "score_down": max(0.0, 100 - sc) if direction == "up" else sc,
                        "ret_since_anchor": (sc - 50) / 500,
                    }
                )
            self._json(200, {"current": current, "path": path_nodes, "meta": snap["meta"]})
            return True

        return False

    def _board_missing(self, board_key: str) -> dict[str, Any]:
        v = board_by_key(board_key)
        return {
            "error": "board_snapshot_missing",
            "board": board_key,
            "parameter_version": getattr(v, "parameter_version", None),
            "latest_path": str(board_latest_path(board_key)),
            "hint": (
                "该板面还没有产出。跑一轮扫描即可生成（选币榜Y 由主扫描顺带投影）："
                "PYTHONPATH=services/coin-selection/src python3 -m coin_selection --force"
            ),
        }

    def _review(self, qs, *, kind, symbol=None):
        """Review endpoints degrade to a JSON error, never to a dropped socket.

        The module import is already guarded (a broken review module must not
        stop the gateway serving the screener); the call has to be guarded for
        the same reason.
        """
        if review_payload is None:
            return self._json(503, {"error": "review_module_unavailable"})
        try:
            code, body = review_payload(qs, kind=kind, symbol=symbol)
        except Exception as e:  # noqa: BLE001 — never take the gateway down
            traceback.print_exc()
            return self._json(500, {"error": "review_query_failed", "detail": str(e)})
        return self._json(code, body)

    def _sse(self, board_key: str = MAIN_KEY) -> None:
        hub = hub_for(board_key)
        last_id = 0
        raw_last = self.headers.get("Last-Event-ID") or parse_qs(
            urlparse(self.path).query
        ).get("last_id", ["0"])[0]
        try:
            last_id = int(raw_last or 0)
        except ValueError:
            last_id = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()

        # hello
        hello = {
            "type": "hello",
            "board": board_key,
            "time_utc": utc_now_iso(),
            "latest_fp": file_fingerprint(board_latest_path(board_key)),
        }
        self.wfile.write(b"event: hello\n")
        self.wfile.write(f"data: {json.dumps(hello, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()
        # push current snapshot once
        hub.poll_file()

        try:
            while True:
                ev = hub.wait_event(last_id, timeout=20.0)
                if ev is None:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                last_id = int(ev["id"])
                self.wfile.write(f"id: {last_id}\n".encode())
                self.wfile.write(f"event: {ev['type']}\n".encode())
                self.wfile.write(
                    f"data: {json.dumps(ev['data'], ensure_ascii=False)}\n\n".encode()
                )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes api-gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "18080")))
    args = parser.parse_args()

    t = threading.Thread(target=_watcher, name="latest-watcher", daemon=True)
    t.start()
    warm_latest_wires()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    # Python 3.12 default request_queue_size=5. Public Caddy + SSE (/events)
    # + 15s price poll occupy those slots; extra connects then RST and Caddy
    # surfaces HTTP 502 to the browser. Keep the bind on 127.0.0.1.
    httpd.request_queue_size = 128
    httpd.daemon_threads = True
    try:
        httpd.socket.listen(httpd.request_queue_size)
    except OSError:
        pass
    print(f"api-gateway on http://{args.host}:{args.port}  (HERMES_ROOT={ROOT})", flush=True)
    print("  GET /api/v1/boards", flush=True)
    for _v in board_variants():
        print(
            f"  GET {_v.api_prefix}/latest|/confirmed|/events  "
            f"({_v.label} · {_v.parameter_version})",
            flush=True,
        )
    print("  GET /api/v1/markets/universe", flush=True)
    print("  GET /api/v1/market/{symbol}/klines", flush=True)
    # X v1.3.0 与 main v1.4.0 复刻、独立复盘闭环；后续演进入口仍在注册表。
    print("  GET /api/v1/review/coverage|summary|trades  (?board=main|x|y)", flush=True)
    print("  GET /api/v1/review/symbols/{symbol}", flush=True)
    if SERVE_WEB:
        print(
            f"  SPA {WEB_DIST}  →  /screener  /screener-x  /screener-y  /review  /markets  /market/:symbol",
            flush=True,
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutdown", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
