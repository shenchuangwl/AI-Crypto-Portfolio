"""物理 coverage 与 31 天窗口交集 —— 复盘选币「实际可回测多少」的唯一算法。

权威依据
--------
* 文档 B §4.1（时钟与节点）、§4.2（物理 coverage）、§4.3（字段可用覆盖）、
  §11.1（三种范围）、§11.2（查询规范）、§18.1（当前缺口）、§21.1（验收门槛第 3/4 条）。
* 文档 A §18.1【建议规则】：「目标 coverage 必须同时输出 expected_nodes、present_nodes、
  missing_scan_ids、duplicate_scan_ids、continuous_segments、每区 first/last 和字段可用起点。」

改造前的现网问题（文档 B §4.2 / §18.1【现网事实】）
---------------------------------------------------
两本 SQLite 的 coverage 物理表都是空表，API 只按 episode 首末时间聚合，
**遗漏 15 个节点仍然报 ``coverage_ratio = 1.0``**。本模块按 15 分钟栅格逐节点
重算，缺一个报一个。

节点定义（文档 B §4.1【现网事实】）
-----------------------------------
逻辑节点是 UTC 15 分钟栅格；``scan_id = YYYYMMDD-NNN``，``NNN ∈ [0, 95]``；
绝对 node index 含日期，跨 00:00 不倒退。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

#: 15 分钟栅格 → 每天 96 个节点。
NODES_PER_DAY = 96
NODE_SECONDS = 900

#: 文档 B §11.1：retention 上限 31 天。它是**上限**，不是可用数据保证。
MAX_RETENTION_DAYS = 31

SCAN_ID_RE = re.compile(r"^(\d{8})-(\d{1,3})$")


class ScanIdError(ValueError):
    pass


def parse_scan_id(scan_id: str) -> tuple[date, int]:
    m = SCAN_ID_RE.match(str(scan_id).strip())
    if not m:
        raise ScanIdError(f"bad scan_id {scan_id!r}")
    d = datetime.strptime(m.group(1), "%Y%m%d").date()
    seq = int(m.group(2))
    if not 0 <= seq < NODES_PER_DAY:
        raise ScanIdError(f"scan_id seq out of range: {scan_id!r}")
    return d, seq


def make_scan_id(d: date, seq: int) -> str:
    return f"{d.strftime('%Y%m%d')}-{seq:03d}"


def node_index(scan_id: str) -> int:
    """绝对节点序号（含日期，跨 00:00 单调递增）。"""
    d, seq = parse_scan_id(scan_id)
    return d.toordinal() * NODES_PER_DAY + seq


def scan_id_from_index(idx: int) -> str:
    d = date.fromordinal(idx // NODES_PER_DAY)
    return make_scan_id(d, idx % NODES_PER_DAY)


def node_time_utc(scan_id: str) -> datetime:
    """节点的逻辑 UTC 时间。``NNN`` 号节点 = 当日 00:00 + NNN×15 分钟。"""
    d, seq = parse_scan_id(scan_id)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(
        seconds=seq * NODE_SECONDS
    )


def expected_scan_ids(first: str, last: str) -> list[str]:
    """首末闭区间内的 15m 栅格全量节点（文档 B §4.2 的 expected_nodes）。"""
    a, b = node_index(first), node_index(last)
    if b < a:
        a, b = b, a
    return [scan_id_from_index(i) for i in range(a, b + 1)]


def continuous_segments(scan_ids: Iterable[str]) -> list[dict[str, Any]]:
    """把已存在节点切成连续段（文档 B §11.2 的 continuous_segments）。"""
    idx = sorted({node_index(s) for s in scan_ids})
    segs: list[dict[str, Any]] = []
    if not idx:
        return segs
    start = prev = idx[0]
    for i in idx[1:]:
        if i == prev + 1:
            prev = i
            continue
        segs.append(_seg(start, prev))
        start = prev = i
    segs.append(_seg(start, prev))
    return segs


def _seg(a: int, b: int) -> dict[str, Any]:
    return {
        "from_scan_id": scan_id_from_index(a),
        "to_scan_id": scan_id_from_index(b),
        "from_utc": _iso(node_time_utc(scan_id_from_index(a))),
        "to_utc": _iso(node_time_utc(scan_id_from_index(b))),
        "nodes": b - a + 1,
    }


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def physical_coverage(
    scan_ids: Sequence[str],
    *,
    max_missing_listed: int = 200,
) -> dict[str, Any]:
    """逐节点物理覆盖。

    返回（文档 A §18.1 / 文档 B §11.2 要求的全部字段）::

        expected_nodes / present_nodes / missing_nodes / coverage_ratio
        first_scan_id / last_scan_id / first_utc / last_utc / span_days
        missing_scan_ids / duplicate_scan_ids / continuous_segments
        largest_gap_nodes
    """
    raw = [str(s) for s in scan_ids if s]
    present: list[str] = []
    dupes: list[str] = []
    seen: set[str] = set()
    for s in raw:
        try:
            node_index(s)
        except ScanIdError:
            continue
        if s in seen:
            dupes.append(s)
            continue
        seen.add(s)
        present.append(s)

    out: dict[str, Any] = {
        "expected_nodes": 0,
        "present_nodes": len(present),
        "missing_nodes": 0,
        "coverage_ratio": None,
        "first_scan_id": None,
        "last_scan_id": None,
        "first_utc": None,
        "last_utc": None,
        "span_days": 0.0,
        "missing_scan_ids": [],
        "missing_truncated": False,
        "duplicate_scan_ids": sorted(set(dupes)),
        "continuous_segments": [],
        "largest_gap_nodes": 0,
    }
    if not present:
        return out

    ordered = sorted(present, key=node_index)
    first, last = ordered[0], ordered[-1]
    expected = expected_scan_ids(first, last)
    missing = [s for s in expected if s not in seen]
    segs = continuous_segments(ordered)
    gaps = [
        node_index(segs[i + 1]["from_scan_id"]) - node_index(segs[i]["to_scan_id"]) - 1
        for i in range(len(segs) - 1)
    ]
    ft, lt = node_time_utc(first), node_time_utc(last)
    out.update(
        expected_nodes=len(expected),
        present_nodes=len(ordered),
        missing_nodes=len(missing),
        coverage_ratio=round(len(ordered) / len(expected), 6) if expected else None,
        first_scan_id=first,
        last_scan_id=last,
        first_utc=_iso(ft),
        last_utc=_iso(lt),
        span_days=round((lt - ft).total_seconds() / 86400.0, 6),
        missing_scan_ids=missing[:max_missing_listed],
        missing_truncated=len(missing) > max_missing_listed,
        continuous_segments=segs,
        largest_gap_nodes=max(gaps) if gaps else 0,
    )
    return out


def field_coverage(
    records: Iterable[tuple[str, Any]],
    *,
    field_name: str = "field",
) -> dict[str, Any]:
    """字段可用覆盖：``(scan_id, value)`` 序列里 value 非空的首末节点与占比。

    文档 B §4.3：A～F 之类的列有自己的可用起点，必须与物理节点覆盖分开报告。
    """
    seen_nodes: list[str] = []
    ok_nodes: list[str] = []
    for scan_id, value in records:
        try:
            node_index(scan_id)
        except ScanIdError:
            continue
        seen_nodes.append(scan_id)
        if value not in (None, "", [], {}):
            ok_nodes.append(scan_id)
    out = {
        "field": field_name,
        "nodes_seen": len(seen_nodes),
        "nodes_with_field": len(ok_nodes),
        "first_available_scan_id": None,
        "last_available_scan_id": None,
        "ratio": None,
    }
    if ok_nodes:
        ordered = sorted(set(ok_nodes), key=node_index)
        out["first_available_scan_id"] = ordered[0]
        out["last_available_scan_id"] = ordered[-1]
    if seen_nodes:
        out["ratio"] = round(len(ok_nodes) / len(seen_nodes), 6)
    return out


# ---------------------------------------------------------------------------
# 31 天窗口交集（文档 B §11.2）
# ---------------------------------------------------------------------------
def clamp_days(days: Optional[float], *, cap: int = MAX_RETENTION_DAYS) -> Optional[float]:
    if days is None:
        return None
    return max(0.0, min(float(days), float(cap)))


def clamp_cycles(cycles: Optional[int], *, cap: int = MAX_RETENTION_DAYS) -> Optional[int]:
    """文档 B §11.2：「cycles 最大 31 …… 当前 API 允许 cycles=366，必须修正。」"""
    if cycles is None:
        return None
    return max(1, min(int(cycles), int(cap)))


def effective_window(
    *,
    requested: tuple[Optional[datetime], Optional[datetime]],
    now: datetime,
    physical: Optional[tuple[Optional[datetime], Optional[datetime]]] = None,
    revision: Optional[tuple[Optional[datetime], Optional[datetime]]] = None,
    field: Optional[tuple[Optional[datetime], Optional[datetime]]] = None,
    retention_days: int = MAX_RETENTION_DAYS,
) -> dict[str, Any]:
    """文档 B §11.2 的交集算法::

        effective = intersection(requested, retention, physical, revision, field)

    完全无交集时 ``status = "NO_COVERAGE"``（调用方应返回 422），
    **不得返回一个看似有效的零绩效**。
    """
    lo_bounds: list[datetime] = []
    hi_bounds: list[datetime] = []
    parts: dict[str, Any] = {}

    retention = (now - timedelta(days=retention_days), now)
    for name, win in (
        ("requested", requested),
        ("retention", retention),
        ("physical", physical),
        ("revision", revision),
        ("field", field),
    ):
        if win is None:
            parts[name] = None
            continue
        a, b = win
        parts[name] = {"from": _iso(a) if a else None, "to": _iso(b) if b else None}
        if a is not None:
            lo_bounds.append(a)
        if b is not None:
            hi_bounds.append(b)

    lo = max(lo_bounds) if lo_bounds else None
    hi = min(hi_bounds) if hi_bounds else None
    status = "OK"
    if lo is not None and hi is not None and lo > hi:
        status = "NO_COVERAGE"
    days = None
    if status == "OK" and lo is not None and hi is not None:
        days = round((hi - lo).total_seconds() / 86400.0, 6)
    return {
        "status": status,
        "retention_cap_days": retention_days,
        "windows": parts,
        "effective_from": _iso(lo) if (lo and status == "OK") else None,
        "effective_to": _iso(hi) if (hi and status == "OK") else None,
        "effective_days": days,
    }


__all__ = [
    "MAX_RETENTION_DAYS",
    "NODES_PER_DAY",
    "NODE_SECONDS",
    "ScanIdError",
    "clamp_cycles",
    "clamp_days",
    "continuous_segments",
    "effective_window",
    "expected_scan_ids",
    "field_coverage",
    "make_scan_id",
    "node_index",
    "node_time_utc",
    "parse_scan_id",
    "physical_coverage",
    "scan_id_from_index",
]
