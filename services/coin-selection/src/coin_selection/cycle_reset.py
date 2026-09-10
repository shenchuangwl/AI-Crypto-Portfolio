"""时间区（24 小时周期）重置 —— 「选币榜Y」的周期管理。

要解决的问题
------------
「选币榜」(param-v1.4.0) 没有周期概念：一个币进了确认区，只要它一直满足 hold 条件，
就会跨天连续持有下去。停留时长可以累积到几十小时，「今天的榜」实际上混着前几天的存量。

「选币榜Y」(param-v2.0.0) 改成 **24 小时循环**：周期节点一到，
**清空全部分区的历史币种数据**，再由同一节点的实时扫描从零重建分区。
于是「本周期的榜」只包含本周期内真正走完晋级链的币。

周期起点沿用系统既有的每日锚点 **00:00 UTC** —— 就是 ``scan_id`` 的 000 号节点、
``meta.anchor_date`` 用的那个刻度。不另造一套时间标准（见 CycleConfig.cycle_start，
有单测钉住它与 ``scan.resolve_anchor_00utc`` 逐秒相同）。

清空什么、不清空什么
--------------------
清空（当刻板面的分区归属）：

* ``state_machine.json`` —— 分区归属 + 停留 + 连击 + 入区时间/价格。
  这一条就是「清空所有分区内的历史币种数据」：DMR / 确认 / 符合 / 观察 /
  淘汰 / 数据不足 / 低置信度，全部回到 NONE。
* ``daily_unique.json`` —— 本周期日去重入选台账。

**绝不**清空（历史事实）：

* ``snapshots/`` —— 复盘账本的唯一原始事实源，删了就再也回放不出来（retention.json）。
* ``review/ledger.sqlite`` —— 已落库的交易是历史，不是「前日数据」。
* ``dmr-adapter-y/inbox/`` —— 复盘还原 DMR 区成员要靠它。

重置前会把分区名单存一份到 ``<data_dir>/cycle/<周期>-close.json``（只读审计）。

重建节奏（重要，不是 bug）
--------------------------
重置后状态机从 NONE 起步，仍受 v1.4.0 的时间门槛约束（一个节点只升一级）：

===========  ==========  ================================================
节点（24h）  时刻(UTC)   状态
===========  ==========  ================================================
000          00:00       全部 NONE；G1 未过的币当节点直接落淘汰区
001          00:15       观察区（连击 2 达成）
003          00:45       符合区（观察停留 ≥30min + 连击 2）
005          01:15       确认区 / DMR 区（符合停留 ≥15min + 连击 2）
===========  ==========  ================================================

也就是说每个周期的头 75 分钟确认区是空的 —— 这是「日内从零重建」的必然代价，
不是数据没到。板面会在 ``meta.cycle`` 里报出本周期第几节点，前端据此显示「分区重建中」。

想缩短这段窗口，走 ``board-variants.json`` 的 ``cycle.warmup``（默认关闭），
它**只允许**调 min_dwell_* / min_streak_*；N1–N4、F2、300 万美元硬门槛、
SS / 一致性 / 数据质量阈值不在白名单里，放宽不了。这也不是 SM_FAST。

幂等
----
重置的幂等键是周期标识（``20260825T0000Z``）。同一周期内无论扫描跑多少次
（``--force`` 重启、补扫、回放重跑），只会重置一次 —— 否则一次循环重启就会把
刚重建起来的分区又清空一遍。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .board_variants import BoardVariant, CycleConfig

log = logging.getLogger("coin_selection.cycle_reset")

#: 被清空的分区。NONE 不是分区，是「不在任何分区里」。
TRACKED_ZONES = ("CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED", "DATA_INSUFFICIENT", "LOW_CONFIDENCE")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_cycle_state(data_dir: Path) -> dict[str, Any]:
    """读周期水位。传 ``data_dir`` 而不是变体 —— 回放与冒烟会把板面写到别的目录。"""
    path = Path(data_dir) / "cycle_state.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 — 水位文件坏了最多多重置一次，不能炸扫描
        log.warning("cycle state unreadable (%s); treating as never reset", e)
        return {}


def _zone_census(states: dict[str, Any]) -> dict[str, int]:
    out = {z: 0 for z in TRACKED_ZONES}
    out["NONE"] = 0
    for st in states.values():
        z = str((st or {}).get("state") or "NONE")
        out[z] = out.get(z, 0) + 1
    return out


def _clear_state_machine(
    variant: BoardVariant,
    cfg: CycleConfig,
    *,
    data_dir: Path,
    cycle_key: str,
    scan_id: str,
    now: datetime,
    parameter_version: str,
) -> dict[str, Any]:
    """把状态机清空到「谁都不在任何分区里」，并留一份收官名单。"""
    path = data_dir / "state_machine.json"
    doc: dict[str, Any] = {}
    if path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            log.warning("state machine unreadable at cycle reset (%s); clearing anyway", e)
            doc = {}
    states = doc.get("states") or {}
    census = _zone_census(states)

    if cfg.archive_close and states:
        # 收官名单：这一刻谁在哪个区。只读审计用 —— 复盘的事实源是 snapshots，不是它。
        members = [
            {
                "symbol": st.get("symbol"),
                "direction": st.get("direction"),
                "zone": st.get("state"),
                "state_enter_ts": st.get("state_enter_ts"),
                "state_enter_price": st.get("state_enter_price"),
                "confirmed_count": st.get("confirmed_count"),
            }
            for st in states.values()
            if str((st or {}).get("state") or "NONE") != "NONE"
        ]
        members.sort(key=lambda m: (str(m.get("zone")), str(m.get("symbol")), str(m.get("direction"))))
        _atomic_write(
            data_dir / "cycle" / f"{cycle_key}-close.json",
            {
                "board": variant.key,
                "parameter_version": parameter_version,
                "cycle_key": cycle_key,
                "closed_at_scan_id": scan_id,
                "closed_at_utc": _iso(now),
                "census": census,
                "members": members,
                "note": (
                    "周期收官名单：重置前一刻各分区的成员。只读审计用，"
                    "复盘的事实源仍是 snapshots/ 与 review/ledger.sqlite。"
                ),
            },
        )

    _atomic_write(
        path,
        {
            "ts": now.timestamp(),
            "parameter_version": parameter_version,
            "states": {},
            "cycle_key": cycle_key,
            "cycle_reset_at_utc": _iso(now),
            "note": "cleared by 24h cycle reset; zones rebuild from the live scan of this node",
        },
    )
    return census


def maybe_reset(
    variant: BoardVariant,
    *,
    data_dir: Path,
    now: datetime,
    scan_id: str,
    parameter_version: str,
    force: bool = False,
) -> dict[str, Any]:
    """周期到点就清空分区，并返回要写进 ``meta.cycle`` 的那份状态。

    无论有没有重置都返回同一形状的 dict —— 前端靠它显示周期进度与倒计时，
    形状变来变去会让「本周期第几节点」时有时无。
    ``cycle.enabled=False``（选币榜）只返回 ``{"enabled": False}``。
    """
    cfg = variant.cycle
    if not cfg.enabled:
        return {"enabled": False}

    start = cfg.cycle_start(now)
    end = cfg.cycle_end(now)
    key = cfg.cycle_key(now)
    node = cfg.node_in_cycle(now)
    state = load_cycle_state(data_dir)
    already = state.get("cycle_key") == key
    cold_start = not state

    meta: dict[str, Any] = {
        "enabled": True,
        "period_hours": cfg.period_hours,
        "anchor_utc": cfg.anchor_utc,
        "cycle_key": key,
        "cycle_start_utc": _iso(start),
        "cycle_end_utc": _iso(end),
        "node_in_cycle": node,
        "nodes_per_cycle": cfg.nodes_per_cycle(),
        "reset_at_this_node": False,
        "last_reset_cycle_key": state.get("cycle_key"),
        "last_reset_scan_id": state.get("reset_scan_id"),
        "last_reset_at_utc": state.get("reset_at_utc"),
        "last_reset_node_in_cycle": state.get("reset_node_in_cycle"),
        # 本周期的重建不是从 0 号节点开始的：要么首次启用周期管理，要么循环停机
        # 跨过了周期节点。板面得说清楚，否则「本周期的榜」四个字是假的。
        "partial_cycle": bool(state.get("partial_cycle")) if already else None,
        "warmup": {
            "enabled": cfg.warmup_enabled,
            "nodes": cfg.warmup_nodes,
            "active": cfg.in_warmup(now),
            "state_config": cfg.warmup_overrides() if cfg.warmup_enabled else {},
        },
    }

    if already and not force:
        return meta

    census: dict[str, int] = {}
    cleared: dict[str, Any] = {}
    if "state_machine" in cfg.clear:
        census = _clear_state_machine(
            variant,
            cfg,
            data_dir=data_dir,
            cycle_key=key,
            scan_id=scan_id,
            now=now,
            parameter_version=parameter_version,
        )
        cleared["state_machine"] = sum(v for k, v in census.items() if k != "NONE")
    if "daily_unique" in cfg.clear:
        du = data_dir / "daily_unique.json"
        if du.is_file():
            du.unlink()
        cleared["daily_unique"] = True
    # 216 滞回与周期记忆一起清掉，否则跨天会带着昨日 combo_demote_streak。
    hyst = data_dir / "combo_hysteresis.json"
    if hyst.is_file():
        hyst.unlink()
        cleared["combo_hysteresis"] = True

    partial = node > 0
    meta["reset_at_this_node"] = True
    meta["closed_zones"] = census
    meta["cleared"] = cleared
    meta["last_reset_cycle_key"] = key
    meta["last_reset_scan_id"] = scan_id
    meta["last_reset_at_utc"] = _iso(now)
    meta["last_reset_node_in_cycle"] = node
    meta["partial_cycle"] = partial
    meta["partial_reason"] = (
        None
        if not partial
        else ("cold_start" if cold_start else "missed_cycle_node")
    )

    _atomic_write(
        data_dir / "cycle_state.json",
        {
            "board": variant.key,
            "parameter_version": parameter_version,
            "cycle_key": key,
            "cycle_start_utc": _iso(start),
            "cycle_end_utc": _iso(end),
            "reset_scan_id": scan_id,
            "reset_at_utc": _iso(now),
            "reset_node_in_cycle": node,
            "partial_cycle": partial,
            "partial_reason": (
                None if not partial else ("cold_start" if cold_start else "missed_cycle_node")
            ),
            "closed_zones": census,
            "cleared": cleared,
            "forced": bool(force),
            "note": (
                "24h 周期水位。cycle_key 是重置的幂等键：同一周期内扫描跑多少次都只清一次。"
            ),
        },
    )
    log.info(
        "board %s cycle reset %s at scan=%s node=%s/%s%s cleared=%s",
        variant.key,
        key,
        scan_id,
        node,
        cfg.nodes_per_cycle(),
        f" PARTIAL({meta['partial_reason']})" if partial else "",
        {k: v for k, v in census.items() if v},
    )
    return meta
