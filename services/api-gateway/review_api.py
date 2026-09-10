"""Query helpers for GET /api/v1/review/* (imported by api-gateway).

Read-only consumer of the occupancy ledger. The gateway is a synchronous
``http.server``, so every call here has to stay cheap: the ledger is opened in
read-only mode (no OPEN-trade rehydration) and identical queries are served
from a small cache that is invalidated by the ledger watermark.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "services" / "coin-selection" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from coin_selection.board_variants import (  # noqa: E402
    MAIN_KEY,
    CycleConfig,
    get_variant,
    load_variants,
)
from coin_selection.coverage import (  # noqa: E402
    MAX_RETENTION_DAYS,
    clamp_cycles,
    physical_coverage,
)
from coin_selection.review_ledger import ReviewLedger, default_ledger_path  # noqa: E402
from coin_selection.review_replay import ZONES  # noqa: E402

SELECTION_DIR = ROOT / "data" / "coin-selection"
LEDGER_PATH = default_ledger_path(SELECTION_DIR)
LATEST_PATH = SELECTION_DIR / "latest.json"


# ---------------------------------------------------------------------------
# 板面变体 → 复盘账本
#
# 「复盘选币」的规则版本切换（v1.4.0 / v2.0.0）落在这里：一个 ``board`` 参数换一个
# sqlite 文件。**从不共用一张表** —— 两套选入标准产出的交易混在一个库里，任何
# 「胜率」都失去意义（§14.4），而且 v2.0.0 重放重建时会波及 v1.4.0 的历史。
#
#   ?board=main（默认）→ data/coin-selection/latest.json   + review/ledger.sqlite
#   ?board=y           → data/coin-selection-y/latest.json + review/ledger.sqlite
# ---------------------------------------------------------------------------
def resolve_board(qs: dict[str, list[str]]) -> str:
    """从 query 取板面 key；未知值一律回落主板面，绝不 500。"""
    raw = (qs.get("board", [""])[0] or qs.get("ruleset", [""])[0] or "").strip().lower()
    if not raw:
        return MAIN_KEY
    try:
        keys = {v.key for v in load_variants()}
    except Exception:
        keys = {MAIN_KEY}
    return raw if raw in keys else MAIN_KEY


def board_paths(board_key: str) -> tuple[Path, Path, Optional[str]]:
    """(账本路径, 该板面 latest.json, 参数版本)。"""
    if board_key != MAIN_KEY:
        v = get_variant(board_key)
        if v is not None:
            data = v.data_path(ROOT)
            return default_ledger_path(data), data / "latest.json", v.parameter_version
    main = get_variant(MAIN_KEY)
    return LEDGER_PATH, LATEST_PATH, getattr(main, "parameter_version", None)


def board_cycle(board_key: str) -> Optional[CycleConfig]:
    """该板面的时间区配置；没有周期管理（选币榜）返回 None。"""
    v = get_variant(board_key)
    if v is None or not v.cycle.enabled:
        return None
    return v.cycle


# ---------------------------------------------------------------------------
# 周期对齐的时间窗口
#
# 「选币榜Y」每 24 小时（00:00 UTC）清空全部分区重建。复盘窗口如果还按
# 「水位往前推 N×24h」算，就会从半夜切开两个周期：既漏掉当前周期头几个小时，
# 又把上一个周期的尾巴和它的重置强平算进来。实测同一时刻两种口径的均值
# 连正负号都不一样（滚动近 1 天 −0.31% vs 本周期 +0.22%）。
#
# 所以周期板面的窗口一律落在周期栅格上，且**只有服务端算**：
# 栅格定义在 CycleConfig 里，前端再实现一遍必然漂开。
#
# 边界约定（利用「重置后没有任何已平仓交易能跨周期」这一事实）：
#   周期 C 的交易满足  enter ∈ [C_start, C_end)  且  exit ∈ (C_start, C_end]
# 于是按归属口径取不同的闭合方向，相邻周期既不重叠也不漏：
#   exit      → (start, end]    00:00 的重置强平算进它自己那个周期
#   enter     → [start, end)    00:00 的入选算进新开的那个周期
#   contained → [start, end]    两端都要在窗口内，本来就不会跨周期
# ---------------------------------------------------------------------------
def cycle_window(
    cfg: CycleConfig,
    *,
    end_ref: Optional[str],
    cycles: int,
    whole: bool,
    attribution: str,
) -> Optional[dict[str, Any]]:
    """把「最近 N 个周期」翻译成一个具体窗口。``end_ref`` 通常是账本水位。"""
    ref = _parse_iso(end_ref)
    if ref is None:
        return None
    n = max(1, int(cycles))
    period = timedelta(seconds=cfg.period_seconds())
    cur_start = cfg.cycle_start(ref)

    if whole:
        # 只要跑完的周期：上界是当前周期的起点，当前这个进行中的周期整个不算。
        end_dt = cur_start
        start_dt = end_dt - n * period
    else:
        # 含当前（可能只跑了一半的）周期：它 + 前面 n-1 个完整周期。
        end_dt = min(ref, cfg.cycle_end(ref))
        start_dt = cur_start - (n - 1) * period

    lo, hi = start_dt, end_dt
    if attribution == "exit":
        lo = lo + timedelta(seconds=1)
    elif attribution == "enter":
        hi = hi - timedelta(seconds=1)
    # contained: 两端都闭

    # 水位正好压在周期起点上（000 号节点刚入账）：当前周期一秒都还没跑，
    # 它是**空的**，不是「跑了一半」。此时窗口天然为空区间（from > to，SQL 返回 0 行，
    # 正确），但标签必须仍然指向当前周期，别把上一个周期的号写上去。
    empty_current = (not whole) and end_dt <= cur_start
    last_key = cfg.cycle_key(cur_start) if not whole else cfg.cycle_key(
        end_dt - timedelta(seconds=1)
    )

    return {
        "from": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": hi.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cycles": n,
        "whole_cycles": whole,
        "attribution": attribution,
        # start_dt 在两种模式下都恰好落在某个周期起点上，所以直接取它的周期号。
        "first_cycle_key": cfg.cycle_key(start_dt),
        "last_cycle_key": last_key,
        "window_start_utc": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_end_utc": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "includes_partial_current": (not whole)
        and cur_start < end_dt < cfg.cycle_end(ref),
        "empty_current_cycle": empty_current,
    }


def cycle_coverage(cfg: Optional[CycleConfig], cov: dict[str, Any]) -> dict[str, Any]:
    """给复盘页用的周期栅格快照：当前周期、进度、账本里攒了几个完整周期。"""
    if cfg is None:
        return {"enabled": False}
    ref = _parse_iso(cov.get("watermark_ts") or cov.get("last_ts"))
    first = _parse_iso(cov.get("first_ts"))
    out: dict[str, Any] = {
        "enabled": True,
        "period_hours": cfg.period_hours,
        "anchor_utc": cfg.anchor_utc,
        "nodes_per_cycle": cfg.nodes_per_cycle(),
    }
    if ref is None:
        return out
    cur_start, cur_end = cfg.cycle_start(ref), cfg.cycle_end(ref)
    elapsed = (ref - cur_start).total_seconds() / 3600.0
    out.update(
        {
            "current_key": cfg.cycle_key(ref),
            "current_start_utc": cur_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "current_end_utc": cur_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "current_elapsed_hours": round(elapsed, 2),
            "current_complete": ref >= cur_end,
            "current_progress": round(min(1.0, elapsed / max(cfg.period_hours, 1)), 4),
        }
    )
    if first is not None:
        first_start = cfg.cycle_start(first)
        out["first_cycle_key"] = cfg.cycle_key(first)
        # 账本起点如果落在某个周期中间，那个周期是残缺的，不算完整周期。
        out["first_cycle_partial"] = first > first_start
        span = (cur_start - first_start).total_seconds() / cfg.period_seconds()
        out["complete_cycles"] = max(0, int(round(span)) - (1 if out["first_cycle_partial"] else 0))
    return out


PRESETS = {
    "executable": ["DMR", "CONFIRMED"],
    "funnel": ["DMR", "CONFIRMED", "QUALIFIED", "WATCH"],
    "control": ["ELIMINATED", "DATA_INSUFFICIENT", "LOW_CONFIDENCE"],
}
CONTROL_ZONES = tuple(PRESETS["control"])

SORT_KEYS = (
    "enter_time_utc",
    "exit_time_utc",
    "dwell_minutes",
    "dwell_nodes",
    "pnl_pct",
    "pnl_sign",
    "score",
    "direction_confidence",
    "symbol",
    "zone",
    "enter_price",
    "exit_price",
)

# Bound by rows, not entry count: one /review/trades body can hold 5000 rows,
# so an entry-count-only cap would let the gateway retain hundreds of MB.
_CACHE_MAX_ENTRIES = 32
_CACHE_MAX_ROWS = 12_000
_cache: "OrderedDict[str, tuple[Any, int, dict[str, Any]]]" = OrderedDict()
_cache_rows = 0
_cache_lock = threading.Lock()


def clear_review_cache() -> None:
    global _cache_rows
    with _cache_lock:
        _cache.clear()
        _cache_rows = 0


def _file_identity(path: Path) -> tuple:
    """X v1.3.0 独立演进换段时，不把 v1.4.0 克隆旧库冒充新复盘数据。

    水位/行数相同不代表同一本库；只读stat补上原子替换和WAL提交身份，不改参数。
    """
    try:
        st = path.stat()
        return (str(path.resolve()), st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path.resolve()), None)


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse an instant as UTC-aware.

    A naive value (``2026-08-17T05:15``) is read as UTC — mixing naive and aware
    datetimes would raise on subtraction and take the whole endpoint down.
    """
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _canon_iso(ts: Optional[str]) -> Optional[str]:
    """Canonical `YYYY-MM-DDTHH:MM:SSZ`.

    Ledger timestamps are stored as strings and compared lexically in SQL, so
    `…+00:00` or a bare date must be rewritten or the range silently matches the
    wrong rows (`"2026-08-17T05:15:00+00:00" > "2026-08-17T05:15:00Z"` is false
    for every row, not just the ones outside the window).
    """
    d = _parse_iso(ts)
    return None if d is None else d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _truthy(v: Optional[str]) -> bool:
    return (v or "0").strip().lower() in ("1", "true", "yes", "on")


def _days(a: Optional[str], b: Optional[str]) -> Optional[float]:
    da, db = _parse_iso(a), _parse_iso(b)
    if da is None or db is None:
        return None
    return max(0.0, (db - da).total_seconds() / 86400.0)


def sanitize_param_filters(params: dict[str, Any], cov: dict[str, Any]) -> list[str]:
    """Drop ``pv`` / ``ph`` values that do not exist on *this* ledger.

    Cross-board leftovers (``/review?board=y&pv=param-v1.4.0-…``) AND into an
    empty set: Y only ever wrote ``param-v2.0.0-screener-y``, so the page
    renders 0 笔 with every chip at 0. Historical versions that *are* on the
    ledger (main v1.2 / v1.3) are kept.
    """
    dropped: list[str] = []
    segs = {
        str(s.get("version"))
        for s in (cov.get("parameter_segments") or [])
        if s.get("version")
    }
    pvs = list(params.get("parameter_versions") or [])
    if pvs:
        keep = [v for v in pvs if v in segs]
        if keep != pvs:
            dropped.extend(f"pv:{v}" for v in pvs if v not in segs)
            params["parameter_versions"] = keep or None
    hashes_on: set[str] = set()
    for s in cov.get("param_hash_segments") or []:
        h = s.get("param_hash")
        hashes_on.add("null" if h is None else str(h))
    phs = list(params.get("param_hashes") or [])
    if phs:
        keep_h: list[str] = []
        for h in phs:
            key = "null" if str(h).lower() == "null" else str(h)
            if key in hashes_on:
                keep_h.append(h)
            else:
                dropped.append(f"ph:{h}")
        if keep_h != phs:
            params["param_hashes"] = keep_h or None
    if dropped:
        params["dropped_params"] = list(params.get("dropped_params") or []) + dropped
    return dropped


def parse_review_qs(qs: dict[str, list[str]]) -> dict[str, Any]:
    preset = (qs.get("preset", [""])[0] or "").strip().lower()
    raw_zones = (qs.get("zones", [""])[0] or "").strip()
    if raw_zones:
        zones = [z.strip().upper() for z in raw_zones.split(",") if z.strip()]
        zones = [z for z in zones if z in ZONES]
    elif preset in PRESETS:
        zones = list(PRESETS[preset])
    else:
        zones = list(PRESETS["executable"])
    if not zones:
        zones = list(PRESETS["executable"])
    direction = (qs.get("direction", ["both"])[0] or "both").lower()
    if direction not in ("up", "down", "both"):
        direction = "both"
    symbols_raw = (qs.get("symbols", [""])[0] or "").strip()
    symbols = [s.strip() for s in symbols_raw.replace("\n", ",").split(",") if s.strip()] or None
    attribution = (qs.get("attribution", ["exit"])[0] or "exit").lower()
    if attribution not in ("exit", "enter", "contained"):
        attribution = "exit"
    include_open = _truthy(qs.get("include_open", ["0"])[0])
    only_flagged = _truthy(qs.get("only_flagged", ["0"])[0])
    # 剔除带指定旗标的交易。主用途：`exclude_flags=CYCLE_RESET` —— 把「选币榜Y」
    # 24 小时周期重置造成的强制平仓拿掉，只看策略自己的退出决策。
    # 未知旗标名不报错（它只是筛不到东西），但也绝不进 SQL 的列名位置。
    ex_raw = (qs.get("exclude_flags", [""])[0] or "").strip()
    exclude_flags = [f.strip().upper() for f in ex_raw.split(",") if f.strip()] or None
    try:
        min_dwell = max(0, int(qs.get("min_dwell_nodes", ["0"])[0] or 0))
    except ValueError:
        min_dwell = 0
    # 周期对齐窗口：`cycles=N` = 最近 N 个周期（含进行中的当前周期）；
    # 加 `whole_cycles=1` 则只算已经跑完的周期。只对启用了时间区的板面有效，
    # 对「选币榜」是空操作（它没有周期，窗口仍按 from/to 走）。
    # 【ChatGpt_SOL5.6 文档B §11.2 / §18.1 / §21.1】31 天是 retention 上限，必须同时
    # 约束 API、UI、自定义 from/to、compare 与 cycles。改造前这里写的是 366 —— 于是
    # `cycles=366` 会返回一个「看似 1 年」的窗口，而物理数据只有 16.8 天。
    cycles_raw = qs.get("cycles", [""])[0].strip()
    cycles_raw_had_value = bool(cycles_raw)
    _cycles_requested = None
    try:
        if cycles_raw:
            _cycles_requested = int(cycles_raw)
            cycles = clamp_cycles(_cycles_requested)
        else:
            cycles = None
    except ValueError:
        cycles = None
    whole_cycles = _truthy(qs.get("whole_cycles", ["0"])[0])
    try:
        limit = max(1, min(int(qs.get("limit", ["500"])[0] or 500), 5000))
    except ValueError:
        limit = 500
    try:
        offset = max(0, int(qs.get("offset", ["0"])[0] or 0))
    except ValueError:
        offset = 0
    # 参数版本筛选：空 = 全部。跨版本汇总没有意义（选入标准本身变了），
    # 所以要能只看某一套标准自己的成绩。
    pv_raw = (qs.get("pv", [""])[0] or "").strip()
    parameter_versions = [v.strip() for v in pv_raw.split(",") if v.strip() and v.strip() != "all"] or None
    # 调参分段筛选 `?ph=`：同一 parameter_version 内部按 param_hash 分段。
    #
    # 选币榜Y 的 216 主导层上线前后，parameter_version 都是 param-v2.0.0-screener-y，
    # **只有 param_hash 变**（pf1_1b44aba95de8ffe8 → pf1_cc81ad3ac5397f17）。
    # 没有这个过滤器，《复盘选币》会把「天花板关」与「天花板主导」两段混算成
    # 一个胜率 —— 那正是文档B §3.1 禁止的跨段聚合。
    # 特殊值 `ph=null` 单独捞出阶段 0 之前没有指纹的历史行（不可与任何段合并）。
    ph_raw = (qs.get("ph", [""])[0] or "").strip()
    param_hashes = [v.strip() for v in ph_raw.split(",") if v.strip() and v.strip() != "all"] or None
    sort = (qs.get("sort", ["enter_time_utc"])[0] or "enter_time_utc").strip()
    if sort not in SORT_KEYS:
        sort = "enter_time_utc"
    desc = _truthy(qs.get("desc", ["0"])[0])
    # Only accept well-formed instants; a junk `from=` must not silently become
    # a literal SQL string comparison against ISO timestamps.
    start = (qs.get("from", [""])[0] or "").strip() or None
    end = (qs.get("to", [""])[0] or "").strip() or None
    bad_range = []
    if start:
        canon = _canon_iso(start)
        if canon is None:
            bad_range.append("from")
        start = canon
    if end:
        canon = _canon_iso(end)
        if canon is None:
            bad_range.append("to")
        end = canon
    # 自定义窗口同样封顶 31 天：超出时**收紧 from**（保留最近一段），并记进
    # clamped_params 供响应显式告知调用方 —— 不静默返回一个更宽的窗口。
    clamped: list[str] = []
    if cycles_raw_had_value and cycles is not None and cycles != _cycles_requested:
        clamped.append("cycles")
    if start and end:
        span = _days(start, end)
        if span is not None and span > MAX_RETENTION_DAYS:
            dt_end = _parse_iso(end)
            if dt_end is not None:
                start = _canon_iso(
                    (dt_end - timedelta(days=MAX_RETENTION_DAYS)).isoformat()
                )
                clamped.append("from")

    return {
        "clamped_params": clamped,
        "retention_cap_days": MAX_RETENTION_DAYS,
        "zones": zones,
        "direction": direction,
        "symbols": symbols,
        "attribution": attribution,
        "include_open": include_open,
        "only_flagged": only_flagged,
        "exclude_flags": exclude_flags,
        "parameter_versions": parameter_versions,
        "param_hashes": param_hashes,
        "min_dwell_nodes": min_dwell,
        "cycles": cycles,
        "whole_cycles": whole_cycles,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "desc": desc,
        "start_utc": start,
        "end_utc": end,
        "preset": preset or "executable",
        "invalid_params": bad_range,
    }


def open_ledger(board_key: str = MAIN_KEY) -> Optional[ReviewLedger]:
    path, _, _ = board_paths(board_key)
    if not path.is_file():
        return None
    return ReviewLedger(path, readonly=True)


_LATEST_HEAD_BYTES = 4096
_latest_cache: dict[str, tuple[tuple, dict[str, Any]]] = {}


def _latest_scan(board_key: str = MAIN_KEY) -> dict[str, Any]:
    """Live scan head, used only to report ledger freshness.

    `latest.json` is ~2.5 MB and the gateway is single-threaded, so parsing it
    on every poll would make the freshness banner cost more than the query it
    annotates. `meta` is the first key of the serialised board, so read the head
    and regex the three fields out; fall back to a full parse only if the shape
    ever changes. Memoised on file identity, including atomic replacement.
    """
    _, latest_path, _ = board_paths(board_key)
    # X独立演进换段可能保留mtime；复盘 freshness 不能沿用v1.4克隆旧文件头。
    identity = _file_identity(latest_path)
    if identity[-1] is None:
        return {}
    hit = _latest_cache.get(board_key)
    if hit and hit[0] == identity and hit[1]:
        return hit[1]
    out: dict[str, Any] = {}
    try:
        with latest_path.open("rb") as f:
            head = f.read(_LATEST_HEAD_BYTES).decode("utf-8", "ignore")
        for key, field in (
            ("scan_id", "scan_id"),
            ("ts", "scan_timestamp_utc"),
            ("parameter_version", "parameter_version"),
        ):
            m = re.search(rf'"{field}"\s*:\s*"([^"]*)"', head)
            if m:
                out[key] = m.group(1)
        if not out.get("scan_id"):
            with latest_path.open("r", encoding="utf-8") as f:
                meta = (json.load(f) or {}).get("meta") or {}
            out = {
                "scan_id": meta.get("scan_id"),
                "ts": meta.get("scan_timestamp_utc"),
                "parameter_version": meta.get("parameter_version"),
            }
    except Exception:
        return {}
    _latest_cache[board_key] = (identity, out)
    return out


def freshness(cov: dict[str, Any], board_key: str = MAIN_KEY) -> dict[str, Any]:
    """Watermark vs live scan head.

    The ledger is written by the scan loop hook; if the loop is running code
    that predates the hook (or ingest kept failing), the ledger silently stops
    advancing and every number on the page quietly ages. Say so instead.
    """
    latest = _latest_scan(board_key)
    wm_ts = cov.get("watermark_ts")
    lag_min = None
    if latest.get("ts") and wm_ts:
        d = _parse_iso(latest["ts"])
        w = _parse_iso(wm_ts)
        if d and w:
            lag_min = max(0.0, (d - w).total_seconds() / 60.0)
    stale = bool(latest.get("scan_id") and cov.get("watermark_scan_id") != latest.get("scan_id"))
    return {
        "stale": stale,
        "latest_scan_id": latest.get("scan_id"),
        "latest_ts": latest.get("ts"),
        "lag_minutes": lag_min,
        "lag_nodes": int(round(lag_min / 15.0)) if lag_min is not None else None,
        "hint": (
            "账本落后于最新扫描节点：下一轮扫描会自动追补；若持续落后请跑 "
            "python3 scripts/build_review_ledger.py"
            if stale
            else None
        ),
    }


def enrich_coverage(
    cov: dict[str, Any],
    *,
    requested_from: Optional[str],
    requested_to: Optional[str],
    zones: Optional[list[str]] = None,
    board_key: str = MAIN_KEY,
    cycle_cfg: Optional[CycleConfig] = None,
) -> dict[str, Any]:
    last = cov.get("last_ts") or cov.get("watermark_ts")
    first = cov.get("first_ts")
    end = requested_to or last
    start = requested_from
    if start is None and end:
        dt = _parse_iso(end)
        if dt:
            start = (dt - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    requested_days = _days(start, end)
    available_days = _days(first, last)
    zone_available = dict(cov.get("zone_available") or {})
    for z, info in list(zone_available.items()):
        zfrom = (info or {}).get("from")
        zone_available[z] = {
            **(info or {}),
            "from": zfrom,
            "days": _days(zfrom, last),
            # Effective coverage for the *requested* window: a 7d ask against a
            # zone that only started 2.5d ago is 2.5d of evidence, not 7.
            "window_days": _days(max(zfrom, start) if (zfrom and start) else (zfrom or start), end),
        }
    short_zones = []
    for z in zones or []:
        info = zone_available.get(z) or {}
        if info.get("days") is not None and requested_days and info["days"] + 1e-6 < requested_days:
            short_zones.append(z)
    out = dict(cov)
    out.update(
        {
            "requested_from": start,
            "requested_to": end,
            "requested_days": requested_days,
            "available_days": available_days,
            "zone_available": zone_available,
            "short_zones": short_zones,
            "coverage_ratio": (
                min(1.0, available_days / requested_days)
                if (available_days is not None and requested_days)
                else None
            ),
            "ledger_ready": True,
            "freshness": freshness(cov, board_key),
            # 复盘页要能一眼看出「这份账本属于哪块板面 / 哪套规则版本」。
            "board": board_key,
            "board_parameter_version": board_paths(board_key)[2],
            # 时间区栅格。复盘页靠它把窗口落在周期边界上，而不是「水位往前推 N×24h」。
            "cycle": cycle_coverage(cycle_cfg, cov),
            # 【ChatGpt_SOL5.6 文档A §18.1 / 文档B §4.2 §11.2 §21.1】
            #
            # 逐节点物理覆盖。改造前 coverage 只按 episode 首末时间聚合，因此
            # **遗漏 15 个真实缺失节点仍然报 coverage_ratio = 1.0**。下面这一块按
            # 15 分钟栅格重算 expected / present / missing / duplicate / 连续段，
            # 缺一个报一个；上面按天算的 coverage_ratio 作为兼容字段保留不动。
            "physical": snapshot_coverage(board_key),
            "retention_cap_days": MAX_RETENTION_DAYS,
        }
    )
    return out


_PHYSICAL_CACHE: dict[str, tuple[tuple, dict[str, Any]]] = {}


def snapshot_coverage(board_key: str = MAIN_KEY) -> dict[str, Any]:
    """按 15m 栅格逐节点复算板面快照的物理覆盖（文档B §4.2）。

    事实源是 ``<data_dir>/snapshots/<scan_id>.json`` 的文件名 —— 一个节点存在与否
    是**文件系统事实**，不需要读内容，也不受账本 episode 首末时间的影响。
    结果按目录路径及scan_id集合缓存；同数量换段也必须重新计算缺口。
    """
    try:
        # board_paths 返回的是 (ledger.sqlite, latest.json, parameter_version)，
        # 快照目录是 latest.json 的同级 snapshots/。
        snaps = Path(board_paths(board_key)[1]).parent / "snapshots"
        if not snaps.is_dir():
            return {"available": False, "reason": "no_snapshots_dir"}
        ids = [
            p.stem
            for p in snaps.glob("*.json")
            if not p.name.endswith(".full.json") and p.stem[:1].isdigit()
        ]
        key = board_key
        # v1.3对齐审计不补造缺口；未来X换目录/换节点不能借相同文件数复用旧coverage。
        identity = (str(snaps.resolve()), tuple(sorted(ids)))
        cached = _PHYSICAL_CACHE.get(key)
        if cached and cached[0] == identity:
            return cached[1]
        out = physical_coverage(ids)
        out["available"] = True
        out["source"] = str(snaps)
        _PHYSICAL_CACHE[key] = (identity, out)
        return out
    except Exception as e:  # noqa: BLE001 — coverage 计算不许拖垮 API
        return {"available": False, "reason": f"error:{e}"}


def _cache_get(key: str, version: Any) -> Optional[dict[str, Any]]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == version:
            _cache.move_to_end(key)
            return hit[2]
        if hit:
            _drop_locked(key)
    return None


def _drop_locked(key: str) -> None:
    global _cache_rows
    old = _cache.pop(key, None)
    if old:
        _cache_rows -= old[1]


def _cache_put(key: str, version: Any, body: dict[str, Any]) -> None:
    global _cache_rows
    rows = len(body.get("trades") or ())
    with _cache_lock:
        _drop_locked(key)
        _cache[key] = (version, rows, body)
        _cache_rows += rows
        while _cache and (len(_cache) > _CACHE_MAX_ENTRIES or _cache_rows > _CACHE_MAX_ROWS):
            oldest = next(iter(_cache))
            if oldest == key:
                break
            _drop_locked(oldest)


def review_payload(
    qs: dict[str, list[str]], *, kind: str, symbol: Optional[str] = None
) -> tuple[int, dict[str, Any]]:
    board_key = resolve_board(qs)
    ledger_path, _, board_pv = board_paths(board_key)
    led = open_ledger(board_key)
    if led is None:
        # X v1.3.0 复刻 main v1.4.0，但复盘独立；后续 X overrides 演进仍只回放本板。
        hint = (
            "python3 scripts/build_review_ledger.py --reset"
            if board_key == MAIN_KEY
            else f"python3 scripts/replay_screener_y.py --board {board_key}    # 或等下一轮扫描自动增量写入"
        )
        return 503, {
            "error": "ledger_not_ready",
            "board": board_key,
            "board_parameter_version": board_pv,
            "hint": hint,
            "ledger_path": str(ledger_path),
        }
    try:
        params = parse_review_qs(qs)
        if symbol:
            params["symbols"] = [symbol]
        cov_raw = led.coverage()
        cfg = board_cycle(board_key)
        cov = enrich_coverage(
            cov_raw,
            requested_from=params["start_utc"],
            requested_to=params["end_utc"],
            zones=params["zones"],
            board_key=board_key,
            cycle_cfg=cfg,
        )
        if kind == "coverage":
            return 200, cov

        # 入点锁必须属于本账本。外板 pv（Y 上的 v1.4.0）在 SQL AND 之后是空集，
        # 必须在进缓存键 / summarize 之前丢掉，否则旧链接会把整页打成 0 笔。
        sanitize_param_filters(params, cov_raw)

        start = params["start_utc"] or cov.get("requested_from")
        end = params["end_utc"] or cov.get("requested_to")

        # —— 周期对齐 ——
        # 板面有时间区、调用方又给了 `cycles=N` 时，窗口一律由服务端按周期栅格重算，
        # 覆盖掉 from/to。栅格只有这一处实现：前端再算一遍必然与 CycleConfig 漂开，
        # 而漂开的代价是均值连正负号都可能反过来。
        cycle_win = None
        if cfg is not None and params["cycles"]:
            cycle_win = cycle_window(
                cfg,
                end_ref=params["end_utc"] or cov_raw.get("watermark_ts") or cov_raw.get("last_ts"),
                cycles=params["cycles"],
                whole=params["whole_cycles"],
                attribution=params["attribution"],
            )
            if cycle_win:
                start, end = cycle_win["from"], cycle_win["to"]

        # Historical ledger rows are immutable once the watermark passes them,
        # so the watermark is a sound cache version for identical queries — but
        # the *live scan head* must be part of it too, otherwise a body cached
        # while the loop was caught up keeps reporting `stale:false` for exactly
        # as long as the ledger stays stuck, which is when it matters most.
        version = (
            _file_identity(ledger_path),
            _file_identity(Path(str(ledger_path) + "-wal")),
            cov_raw.get("watermark_scan_id"),
            cov_raw.get("trade_rows"),
            (cov.get("freshness") or {}).get("latest_scan_id"),
        )
        # board 必须进缓存键：两块板面的查询参数完全可能一模一样，
        # 少了它就会把选币榜Y 的结果当成选币榜的返回（或反过来）。
        # start/end 已经是周期对齐后的值，所以缓存键天然带上了周期口径。
        ckey = json.dumps(
            [board_key, kind, symbol, params, start, end], sort_keys=True, default=str
        )
        cached = _cache_get(ckey, version)
        if cached is not None:
            return 200, cached

        summary = led.summarize(
            zones=params["zones"],
            direction=params["direction"],
            symbols=params["symbols"],
            start_utc=start,
            end_utc=end,
            attribution=params["attribution"],
            min_dwell_nodes=params["min_dwell_nodes"],
            only_flagged=params["only_flagged"],
            exclude_flags=params["exclude_flags"],
            parameter_versions=params["parameter_versions"],
            param_hashes=params["param_hashes"],
            control_zones=CONTROL_ZONES,
        )
        body: dict[str, Any] = {
            "board": board_key,
            "board_parameter_version": board_pv,
            "coverage": cov,
            "parameter_versions": summary.get("parameter_versions")
            or ([cov.get("parameter_version")] if cov.get("parameter_version") else []),
            "attribution": params["attribution"],
            "filters": {
                "zones": params["zones"],
                "direction": params["direction"],
                "symbols": params["symbols"],
                "from": start,
                "to": end,
                "min_dwell_nodes": params["min_dwell_nodes"],
                "include_open": params["include_open"],
                "only_flagged": params["only_flagged"],
                "exclude_flags": params["exclude_flags"],
                "parameter_versions": params["parameter_versions"],
                "param_hashes": params["param_hashes"],
                "sort": params["sort"],
                "desc": params["desc"],
                "preset": params["preset"],
                "board": board_key,
                "cycles": params["cycles"],
                "whole_cycles": params["whole_cycles"],
                # 服务端最终采用的周期窗口（含首/末周期标识与是否含进行中的当前周期）。
                # 页面标题与导出文件名都用它，避免前端自己再算一个不一样的。
                "cycle_window": cycle_win,
                "invalid_params": params["invalid_params"],
                "dropped_params": params.get("dropped_params") or [],
            },
            "summary": summary,
        }
        if kind in ("trades", "symbol"):
            qkw = dict(
                zones=params["zones"],
                direction=params["direction"],
                symbols=params["symbols"],
                start_utc=start,
                end_utc=end,
                attribution=params["attribution"],
                include_open=params["include_open"],
                min_dwell_nodes=params["min_dwell_nodes"],
                only_flagged=params["only_flagged"],
                exclude_flags=params["exclude_flags"],
                parameter_versions=params["parameter_versions"],
            param_hashes=params["param_hashes"],
            )
            trades = led.query_trades(
                sort=params["sort"], desc=params["desc"], limit=params["limit"], offset=params["offset"], **qkw
            )
            total = led.count_trades(**qkw)
            body["trades"] = trades
            body["count"] = len(trades)
            body["total"] = total
            body["truncated"] = params["offset"] + len(trades) < total
            body["offset"] = params["offset"]
            body["limit"] = params["limit"]
        _cache_put(ckey, version, body)
        return 200, body
    finally:
        led.close()
