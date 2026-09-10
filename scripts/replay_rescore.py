#!/usr/bin/env python3
"""离线重打分回放器 —— 文档B §4 的技术基石。

    # 1. 建压缩指标带（一次性；只读主板 full.json，不写任何现网目录）
    python3 scripts/replay_rescore.py --build-tape --out data/research/tape.pkl

    # 2. E0 基线 + 与现网账本对账
    python3 scripts/replay_rescore.py --exp E0 --tape data/research/tape.pkl \\
            --settings '{}' --state '{}' --no-mcap-zone \\
            --reconcile data/coin-selection-y/review/ledger.sqlite --metrics

    # 3. E1 仅改权重
    python3 scripts/replay_rescore.py --exp E1 --tape data/research/tape.pkl \\
            --settings '{"w_ss":0.20,"w_mom":0.20,"w_liq":0.15,"w_mcap":0.25,
                         "w_cons":0.10,"w_rank":0.05,"w_risk":0.05}' --metrics

    # 4. E2a 权重 + 阈值分位对齐（阈值由脚本从 E0/E1 分布现算，禁止手填）
    python3 scripts/replay_rescore.py --exp E2a --tape data/research/tape.pkl \\
            --settings '<同上>' --recalibrate-thresholds quantile --metrics

    # 5. E3 天花板四档扫描
    python3 scripts/replay_rescore.py --exp E3_C1 --mcap-zone --mcap-cuts C1 --metrics

    # 6. 交易制 + 成本 + 资金曲线
    python3 scripts/replay_rescore.py --exp E0 --metrics-mode bracket \\
            --target 0.02 --stop 0.02 --horizon-nodes 96 --cost-bps 20 \\
            --concurrency 16 --equity-curve

    # 7. 样本内 / 外切分
    python3 scripts/replay_rescore.py --exp E0 --split 0.6 --metrics

为什么可行（文档B §4.1，已实测）
--------------------------------
G1–G4 是**取数 + 冻结公式**；权重、阈值、天花板、Top-K 全部作用在它们**之上**。
用快照里已存的分项指标按 ``composite_score`` 重算 Score，与快照已发布值最大绝对
误差 = 0.0。**改权重不需要重新访问 Binance 或 CoinGecko。**

五条硬性质（文档B §4.4，测试 T1/T2 钉住）
-----------------------------------------
1. **复用生产函数**：逐节点调用**真实的** ``apply_state_machine`` / ``transition_one``
   / ``build_dmr_messages`` / ``rank_dmr_inbox`` / ``OccupancyReplayer.step`` /
   ``realized_pnl``。本文件里没有第二份状态机实现（T1 用源码 grep 钉住）。
2. **确定性**：无 ``time.time()``（用 ``state_machine.set_clock(node_ts)``）、无随机
   （置换检验用固定种子的 ``random.Random``）、字典遍历一律 ``sorted``、
   输出 JSON ``sort_keys=True``。同输入跑两次 ``output_digest`` 相等。
3. **幂等**：输出目录先整体重建；不做增量。
4. **不污染现网**：输出目录白名单 —— 只允许 ``data/research/**``，显式拒绝
   ``data/coin-selection*``（T2）。账本文件名 ``ledger-<exp_id>.sqlite``。
5. **与现网对齐**：``--reconcile`` 用现网权重跑 E0 后与现网账本逐区对账。

事实源（文档B §4.2）：主板 ``data/coin-selection/snapshots/*.full.json``，
**不是** Y 的板面快照 —— 后者不登载 NONE 侧（缺陷 N3），以它为源的回放会漏掉
全窗口均值约 28 侧/节点。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import random
import shutil
import subprocess
import sys
from bisect import bisect_left
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import mcap_zone as mz  # noqa: E402
from coin_selection import mcap_dominance as _md  # noqa: E402
from coin_selection import mcap_effective as _me  # noqa: E402
from coin_selection import mcap_mapping as _mm  # noqa: E402
from coin_selection import state_machine as sm_mod  # noqa: E402
from coin_selection.board_variants import (  # noqa: E402
    BOARD_Y_KEY,
    get_variant,
    param_fingerprint,
    variant_settings,
    variant_state_config,
)
from coin_selection.review_pnl import FLAT_EPS, realized_pnl  # noqa: E402
from coin_selection.review_replay import OccupancyReplayer  # noqa: E402
from coin_selection.scan import (  # noqa: E402
    SelectionSettings,
    board_from_rows,
    build_dmr_messages,
    composite_score,
    rank_dmr_inbox,
)
from coin_selection.state_machine import (  # noqa: E402
    StateConfig,
    StateMachineStore,
    apply_state_machine,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
RESEARCH_ROOT = ROOT / "data" / "research"
NODES_PER_DAY = 96
SCAN_INTERVAL_MIN = 15

#: 指标带保留的行级字段。只留 Score / 状态机 / 天花板 / 复盘用得到的，
#: 1.37 MB/份的 full.json 压到约 1/10。
TAPE_ROW_FIELDS = (
    "symbol",
    "base_asset",
    "coingecko_id",
    "ss_up",
    "ss_down",
    "momentum_score_up",
    "momentum_score_down",
    "liquidity_score_abs",
    "mcap_momentum_score_up",
    "mcap_momentum_score_down",
    "consistency_up",
    "consistency_down",
    "data_quality_score",
    "supply_missing",
    "liquidity_hard_pass",
    "liquidity_grade",
    "data_mode",
    "last_price",
    "mark_price",
    "mcap_grade_30m",
    "mcap_grade_2h",
    "mcap_grade_6h",
    "steps_up",
    "steps_down",
    "circulating_supply",
)

#: 天花板切点四档（文档A §0.4 / 文档B §7.2 的 C1–C4）。C1 = 优先级对齐档（默认）。
MCAP_CUT_PRESETS: dict[str, dict[str, float]] = {
    "C1": {"dmr": 2.1, "confirmed": 1.5, "qualified": 0.8, "watch": -0.5},
    "C2": {"dmr": 2.5, "confirmed": 2.0, "qualified": 1.2, "watch": 0.0},
    "C3": {"dmr": 1.5, "confirmed": 0.8, "qualified": 0.0, "watch": -1.5},
    "C4": {"dmr": 1.8, "confirmed": 1.2, "qualified": 0.4, "watch": -1.0},
}

#: 分位对齐重标定的目标字段（只有 Score 类阈值参与，文档A §2.3 / §9.1）。
#: SS / M / C / DQ 类阈值一律不动 —— 权重变更只改变 Score 的量纲。
RECALIBRATE_FIELDS = (
    "enter_watch",
    "exit_watch",
    "exit_qualified",
    "enter_qualified",
    "enter_qualified_m",
    "exit_confirmed",
    "enter_confirmed",
    "dmr_score",
)


# ---------------------------------------------------------------------------
# 输出目录白名单（性质 4 / 测试 T2）
# ---------------------------------------------------------------------------
class UnsafeOutputDir(ValueError):
    """有人想让研究脚本写进现网目录。"""


def assert_safe_out_dir(path: Path) -> Path:
    """只允许 ``data/research/**``。显式拒绝任何 ``data/coin-selection*``。

    研究脚本写坏现网目录是不可逆的：快照与账本是《复盘选币》的唯一事实源，
    删了就再也回放不出来。所以这里用白名单，不用黑名单。
    """
    p = Path(path).expanduser()
    p = p if p.is_absolute() else (ROOT / p)
    try:
        resolved = p.resolve()
    except OSError:  # pragma: no cover - 路径不存在时 resolve 仍可用 strict=False
        resolved = p
    research = RESEARCH_ROOT.resolve() if RESEARCH_ROOT.exists() else RESEARCH_ROOT
    parts = resolved.parts
    # X v1.3.0 复刻 main v1.4.0 的实时/复盘目录不可被研究回放误写；演进只经 X overrides。
    for bad in ("coin-selection", "coin-selection-x", "coin-selection-y",
                "dmr-adapter", "dmr-adapter-x", "dmr-adapter-y"):
        if bad in parts:
            raise UnsafeOutputDir(
                f"refusing to write inside a live data dir: {resolved} "
                f"(only {research}/** is allowed)"
            )
    if not (str(resolved) == str(research) or str(resolved).startswith(str(research) + os.sep)):
        raise UnsafeOutputDir(
            f"output must live under {research}/**, got {resolved}"
        )
    return resolved


# ---------------------------------------------------------------------------
# 指标带
# ---------------------------------------------------------------------------
def _node_ts(scan_id: str) -> Optional[datetime]:
    """``20260815-001`` → 2026-08-15T00:15Z。节点号 × 15 分钟，锚点 00:00 UTC。"""
    try:
        day, node = scan_id.split("-")
        base = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]), tzinfo=timezone.utc)
        return base + timedelta(minutes=SCAN_INTERVAL_MIN * int(node))
    except (ValueError, IndexError):
        return None


def build_tape(
    source: Path,
    out: Path,
    *,
    from_scan: Optional[str] = None,
    to_scan: Optional[str] = None,
) -> dict[str, Any]:
    """主板 ``*.full.json`` → 压缩指标带（pickle）。只读源目录。"""
    out = assert_safe_out_dir(out.parent) / out.name
    out.parent.mkdir(parents=True, exist_ok=True)
    paths = sorted(Path(source).glob("*.full.json"))
    nodes: list[dict[str, Any]] = []
    for p in paths:
        sid = p.name.removesuffix(".full.json")
        if from_scan and sid < from_scan:
            continue
        if to_scan and sid > to_scan:
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] unreadable {p.name}: {e}", file=sys.stderr)
            continue
        rows = []
        for r in doc.get("rows") or []:
            rows.append({k: r.get(k) for k in TAPE_ROW_FIELDS})
        ts = _node_ts(sid)
        nodes.append(
            {
                "scan_id": sid,
                "ts": ts.isoformat().replace("+00:00", "Z") if ts else None,
                "universe_count": doc.get("universe_count") or len(rows),
                "rows": rows,
            }
        )
    nodes.sort(key=lambda n: n["scan_id"])
    payload = {
        "schema": "replay-tape-v1",
        "source": str(Path(source)),
        "n_nodes": len(nodes),
        "first": nodes[0]["scan_id"] if nodes else None,
        "last": nodes[-1]["scan_id"] if nodes else None,
        "fields": list(TAPE_ROW_FIELDS),
        "nodes": nodes,
    }
    with open(out, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return {
        "tape": str(out),
        "n_nodes": len(nodes),
        "first": payload["first"],
        "last": payload["last"],
        "bytes": out.stat().st_size,
    }


def load_tape(path: Path) -> dict[str, Any]:
    with open(path, "rb") as fh:
        tape = pickle.load(fh)
    if tape.get("schema") != "replay-tape-v1":
        raise ValueError(f"unknown tape schema: {tape.get('schema')}")
    return tape


def missing_nodes(scan_ids: Iterable[str]) -> int:
    """理论节点数 − 实存节点数。``20260827-007 → 020`` 这样的断口会被算出来。"""
    ids = sorted(scan_ids)
    if len(ids) < 2:
        return 0

    def idx(sid: str) -> int:
        day, node = sid.split("-")
        d = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]), tzinfo=timezone.utc)
        return int(d.timestamp() // 86400) * NODES_PER_DAY + int(node)

    return (idx(ids[-1]) - idx(ids[0]) + 1) - len(ids)


# ---------------------------------------------------------------------------
# 分位对齐重标定（文档A §2.3）
# ---------------------------------------------------------------------------
def score_distribution(tape: dict[str, Any], settings: SelectionSettings) -> list[float]:
    """全窗口 (symbol, direction, node) 三元组的 Score 经验分布。"""
    out: list[float] = []
    for node in tape["nodes"]:
        for r in node["rows"]:
            liq = float(r.get("liquidity_score_abs") or 0)
            dq = float(r.get("data_quality_score") or 80)
            out.append(
                composite_score(
                    settings,
                    ss=float(r.get("ss_up") or 0),
                    mom=float(r.get("momentum_score_up") or 50),
                    liq=liq,
                    mcap=float(r.get("mcap_momentum_score_up") or 50),
                    cons=float(r.get("consistency_up") or 0),
                    dq=dq,
                )
            )
            out.append(
                composite_score(
                    settings,
                    ss=float(r.get("ss_down") or 0),
                    mom=float(r.get("momentum_score_down") or 50),
                    liq=liq,
                    mcap=float(r.get("mcap_momentum_score_down") or 50),
                    cons=float(r.get("consistency_down") or 0),
                    dq=dq,
                )
            )
    out.sort()
    return out


def _quantile_of(sorted_xs: list[float], value: float) -> float:
    if not sorted_xs:
        return 0.0
    return bisect_left(sorted_xs, value) / len(sorted_xs)


def _value_at(sorted_xs: list[float], q: float) -> float:
    if not sorted_xs:
        return 0.0
    i = min(len(sorted_xs) - 1, max(0, int(round(q * len(sorted_xs)))))
    return sorted_xs[i]


def recalibrate_thresholds(
    tape: dict[str, Any],
    base_settings: SelectionSettings,
    new_settings: SelectionSettings,
    cfg: StateConfig,
) -> dict[str, float]:
    """对每个现网阈值 τ 求 E0 分位 p = F₀(τ)，再取 E1 分布的同分位点 F₁⁻¹(p)。

    **阈值由脚本从分布现算，禁止手填**（文档B §7.4 第 4 步的原话）。
    只重标定 Score 类阈值 —— SS/M/C/DQ 的量纲不随权重变化。
    """
    d0 = score_distribution(tape, base_settings)
    d1 = score_distribution(tape, new_settings)
    out: dict[str, float] = {}
    for f in RECALIBRATE_FIELDS:
        tau = float(getattr(cfg, f))
        p = _quantile_of(d0, tau)
        out[f] = round(_value_at(d1, p), 2)
    return out


# ---------------------------------------------------------------------------
# 回放核心
# ---------------------------------------------------------------------------
def _rows_for_node(node: dict[str, Any], settings: SelectionSettings) -> list[dict[str, Any]]:
    """指标带的一节点 → 可喂给生产状态机的 rows（Score 用新权重重算）。"""
    rows: list[dict[str, Any]] = []
    for raw in node["rows"]:
        r = dict(raw)
        liq = float(r.get("liquidity_score_abs") or 0)
        dq = float(r.get("data_quality_score") or 80)
        r["score_up"] = composite_score(
            settings,
            ss=float(r.get("ss_up") or 0),
            mom=float(r.get("momentum_score_up") or 50),
            liq=liq,
            mcap=float(r.get("mcap_momentum_score_up") or 50),
            cons=float(r.get("consistency_up") or 0),
            dq=dq,
        )
        r["score_down"] = composite_score(
            settings,
            ss=float(r.get("ss_down") or 0),
            mom=float(r.get("momentum_score_down") or 50),
            liq=liq,
            mcap=float(r.get("mcap_momentum_score_down") or 50),
            cons=float(r.get("consistency_down") or 0),
            dq=dq,
        )
        r["state_up"] = "NONE"
        r["state_down"] = "NONE"
        rows.append(r)
    rows.sort(key=lambda r: str(r.get("symbol") or ""))
    return rows


def _is_cycle_node(ts: datetime, cycle_hours: int, anchor_min: int) -> bool:
    minutes = ts.hour * 60 + ts.minute
    period_min = cycle_hours * 60
    return ((minutes - anchor_min) % period_min) == 0


def replay_rescore(
    *,
    tape: dict[str, Any],
    exp_id: str,
    out_dir: Path,
    settings: SelectionSettings,
    cfg: StateConfig,
    mcap_zone_mode: str = "off",
    cycle_hours: int = 24,
    cycle_anchor_utc: str = "00:00",
    from_scan: Optional[str] = None,
    to_scan: Optional[str] = None,
    param_hash: str = "",
    progress: bool = False,
) -> dict[str, Any]:
    """快照 → 新权重重算 Score → ``transition_one`` 逐节点重放（含 00:00 周期清空）
    → 天花板层（可关）→ DMR Top-K → ``OccupancyReplayer`` 重建账本 → 绩效指标。
    """
    out_dir = assert_safe_out_dir(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)  # 幂等：删了重来，不做增量
    out_dir.mkdir(parents=True, exist_ok=True)

    hh, mm = (cycle_anchor_utc.split(":") + ["0"])[:2]
    anchor_min = (int(hh) % 24) * 60 + (int(mm) % 60)

    store = StateMachineStore(out_dir / "state_machine.json")
    rp = OccupancyReplayer()
    trades: list[dict[str, Any]] = []
    occupancy: list[dict[str, Any]] = []
    alerts_hist: dict[str, int] = {}
    n_reset = 0
    scan_ids: list[str] = []

    nodes = [
        n
        for n in tape["nodes"]
        if (not from_scan or n["scan_id"] >= from_scan)
        and (not to_scan or n["scan_id"] <= to_scan)
    ]

    for i, node in enumerate(nodes):
        sid = node["scan_id"]
        scan_ids.append(sid)
        ts = _node_ts(sid)
        if ts is None:
            continue
        rows = _rows_for_node(node, settings)

        # —— 24h 周期重置：必须排在状态机之前（与 cycle_reset.maybe_reset 同序）——
        reset_here = False
        if cycle_hours and _is_cycle_node(ts, cycle_hours, anchor_min):
            if store.states:
                store.states.clear()
            reset_here = True
            n_reset += 1

        # 生产状态机，一个字都不重写
        sm_mod.set_clock(ts.timestamp())
        apply_state_machine(rows, store, cfg, scan_id=sid, now_ts=ts.timestamp())

        # —— 天花板 / 流通市值主导层（可关；off 时是恒等映射）——
        #
        # 必须与**生产 board_projection 走同一套裁决**，否则回测测的不是现网规则：
        #   mcap_ruleset="sol5.6"（缺省，生产口径）→ mcap_effective + mcap_dominance
        #   mcap_ruleset="opus5"（消融基线）      → mcap_zone 切点式
        # 改造前这里无条件走 opus5 切点式，于是「复盘选币对 v2.0.0 的回测」
        # 量的其实是消融基线，不是生产规则。
        if mcap_zone_mode != "off":
            if str(getattr(settings, "mcap_ruleset", "sol5.6") or "sol5.6").lower() == "sol5.6":
                mapping = _mm.load(strict=True)
                _me.apply_effective_zone(
                    rows, mode=mcap_zone_mode, mapping=mapping,
                    dmr_ceiling_min=str(
                        getattr(settings, 'mcap_dmr_ceiling_min', 'DMR') or 'DMR'
                    ),
                )
                _md.apply_dominance(
                    rows, cfg=_md.config_from_settings(settings), mode=mcap_zone_mode
                )
                if mcap_zone_mode == "on":
                    for r in rows:
                        for d in ("up", "down"):
                            if r.get(f"mcap_ceiling_zone_{d}") is not None:
                                r[f"combo_block_dmr_{d}"] = not bool(
                                    r.get(f"dmr_ceiling_ok_{d}")
                                )
            else:
                mz.apply_mcap_ceiling(rows, mode=mcap_zone_mode, settings=settings, cfg=cfg)

        # 生产 DMR 逻辑
        dmr_all = build_dmr_messages(
            rows, settings=settings, anchor=ts, scan_id=sid, seq=i, now=ts, cfg=cfg
        )
        dmr_msgs, dmr_rank = rank_dmr_inbox(dmr_all, top_k=settings.dmr_top_k)
        dmr_keys = {
            (m["symbol"], "up" if m.get("direction") == "LONG" else "down") for m in dmr_msgs
        }

        long_pool = board_from_rows(rows, "up", now=ts, cfg=cfg)
        short_pool = board_from_rows(rows, "down", now=ts, cfg=cfg)
        for pool in (long_pool, short_pool):
            for row in pool:
                row["dmr_selected"] = (row["symbol"], row["direction"]) in dmr_keys

        counts: dict[str, int] = {}
        for r in rows:
            for st in (r.get("state_up"), r.get("state_down")):
                counts[str(st or "NONE")] = counts.get(str(st or "NONE"), 0) + 1
        occupancy.append(
            {
                "scan_id": sid,
                "ts": ts.isoformat().replace("+00:00", "Z"),
                "universe": node["universe_count"],
                **{k: counts.get(k, 0) for k in
                   ("WATCH", "QUALIFIED", "CONFIRMED", "ELIMINATED",
                    "DATA_INSUFFICIENT", "LOW_CONFIDENCE", "NONE")},
                "confirmed_unique": len({
                    r["symbol"] for r in rows
                    if r.get("state_up") == "CONFIRMED" or r.get("state_down") == "CONFIRMED"
                }),
                "dmr_unique": dmr_rank.get("unique_before_k", len(dmr_keys)),
                "dmr_inbox": len(dmr_msgs),
                "underfilled": bool(dmr_rank.get("underfilled")),
            }
        )
        if dmr_rank.get("underfilled"):
            alerts_hist["DMR_UNDERFILLED"] = alerts_hist.get("DMR_UNDERFILLED", 0) + 1
        cu = occupancy[-1]["confirmed_unique"]
        if cu > 40:
            alerts_hist["CONFIRM_OVERFLOW"] = alerts_hist.get("CONFIRM_OVERFLOW", 0) + 1
        if cu < 10:
            alerts_hist["CONFIRM_UNDERFILLED"] = alerts_hist.get("CONFIRM_UNDERFILLED", 0) + 1

        board = {
            "meta": {
                "scan_id": sid,
                "scan_timestamp_utc": ts.isoformat().replace("+00:00", "Z"),
                "parameter_version": settings.parameter_version,
                "param_hash": param_hash or None,
                "cycle": {"enabled": bool(cycle_hours), "reset_at_this_node": reset_here},
            },
            "long_pool": long_pool,
            "short_pool": short_pool,
        }
        trades.extend(rp.step(board))
        if progress and (i % 200 == 0):
            print(f"  … {sid} ({i + 1}/{len(nodes)})", file=sys.stderr)

    trades.extend(rp.open_trades())
    sm_mod.set_clock(None)

    # 一个 (trade_id) 只保留最后一次（OPEN 更新会被 CLOSED 覆盖）
    by_id: dict[str, dict[str, Any]] = {}
    for t in trades:
        by_id[t["trade_id"]] = t
    final = [by_id[k] for k in sorted(by_id)]

    return {
        "exp_id": exp_id,
        "out_dir": str(out_dir),
        "param_hash": param_hash,
        "n_nodes": len(nodes),
        "n_missing_nodes": missing_nodes(scan_ids),
        "first_scan": scan_ids[0] if scan_ids else None,
        "last_scan": scan_ids[-1] if scan_ids else None,
        "n_cycle_resets": n_reset,
        "occupancy_series": occupancy,
        "trades": final,
        "alerts_histogram": dict(sorted(alerts_hist.items())),
    }


# ---------------------------------------------------------------------------
# 指标（文档B §5：先定义，后报数）
# ---------------------------------------------------------------------------
def _zone_of(t: dict[str, Any]) -> str:
    return str(t.get("zone") or "")


def occupancy_metrics(
    trades: list[dict[str, Any]],
    *,
    exclude_cycle_reset: bool,
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    """占用制指标。CLOSED 且有 pnl 的才计入；OPEN / 缺价一律剔除并单独报笔数。"""
    n_open = n_missing = n_cr = n_trunc = n_gap = 0
    pnl: list[float] = []
    for t in trades:
        if t.get("status") != "CLOSED":
            n_open += 1
            continue
        flags = set(t.get("flags") or [])
        if "CYCLE_RESET" in flags:
            n_cr += 1
            if exclude_cycle_reset:
                continue
        if "TRUNCATED_ENTER" in flags:
            n_trunc += 1
        if "GAP_BEFORE_EXIT" in flags:
            n_gap += 1
        p = t.get("pnl_pct")
        if p is None:
            n_missing += 1
            continue
        pnl.append(float(p) - cost_bps / 10000.0)
    return {**_stats(pnl), "n_open": n_open, "n_missing_price": n_missing,
            "n_cycle_reset": n_cr, "n_truncated_enter": n_trunc, "n_gap_before_exit": n_gap}


def _stats(pnl: list[float]) -> dict[str, Any]:
    """胜率 / 盈亏比 / PF / E[R]，边界情形逐条按文档B §5.2 / §5.4 处理。"""
    n = len(pnl)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_win": None, "avg_loss": None,
                "payoff": None, "pf": None, "e_r": None, "n_win": 0, "n_loss": 0, "n_flat": 0}
    wins = [p for p in pnl if p > FLAT_EPS]
    losses = [p for p in pnl if p < -FLAT_EPS]
    flats = n - len(wins) - len(losses)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win = (gross_win / len(wins)) if wins else None
    avg_loss = (gross_loss / len(losses)) if losses else None
    # n_loss == 0 时必须显式标 ∞ 并给出 n，不得填 0 或留空（文档B §5.4）
    payoff = math.inf if (avg_loss in (None, 0.0) and avg_win) else (
        (avg_win / avg_loss) if (avg_win is not None and avg_loss) else None
    )
    pf = math.inf if (gross_loss == 0 and gross_win > 0) else (
        (gross_win / gross_loss) if gross_loss else None
    )
    return {
        "n": n,
        "n_win": len(wins),
        "n_loss": len(losses),
        "n_flat": flats,          # 平局计入分母、不计入分子
        "win_rate": len(wins) / n,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": payoff,
        "pf": pf,
        "e_r": sum(pnl) / n,
    }


def bracket_metrics(
    result: dict[str, Any],
    tape: dict[str, Any],
    *,
    zone: str = "DMR",
    target: float = 0.02,
    stop: float = 0.02,
    horizon_nodes: int = 96,
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    """交易制（括号出场）：进区 = 开仓，按 15m 收盘粒度触发止盈 / 止损 / 超时。"""
    px: dict[str, dict[str, float]] = {}
    order: list[str] = []
    for node in tape["nodes"]:
        sid = node["scan_id"]
        order.append(sid)
        m: dict[str, float] = {}
        for r in node["rows"]:
            v = r.get("last_price") or r.get("mark_price")
            if v:
                m[str(r["symbol"])] = float(v)
        px[sid] = m
    pos = {sid: i for i, sid in enumerate(order)}

    pnl: list[float] = []
    exits = {"TARGET": 0, "STOP": 0, "TIMEOUT": 0, "NO_DATA": 0}
    seen: set[tuple[str, str, str]] = set()
    for t in sorted(result["trades"], key=lambda x: (x.get("enter_time_utc") or "", x["trade_id"])):
        if _zone_of(t) != zone:
            continue
        key = (t["symbol"], t["direction"], t["enter_scan_id"])
        if key in seen:
            continue
        seen.add(key)
        i0 = pos.get(t["enter_scan_id"])
        if i0 is None:
            exits["NO_DATA"] += 1
            continue
        entry = px[order[i0]].get(t["symbol"])
        if not entry:
            exits["NO_DATA"] += 1
            continue
        up = t["direction"] == "up"
        out: Optional[float] = None
        kind = "TIMEOUT"
        for j in range(i0 + 1, min(i0 + 1 + horizon_nodes, len(order))):
            p = px[order[j]].get(t["symbol"])
            if p is None:
                continue
            ret = (p / entry - 1.0) if up else (1.0 - p / entry)
            if ret >= target:
                out, kind = target, "TARGET"
                break
            if ret <= -stop:
                out, kind = -stop, "STOP"
                break
            out = ret
        if out is None:
            exits["NO_DATA"] += 1
            continue
        exits[kind] += 1
        pnl.append(out - cost_bps / 10000.0)
    return {**_stats(pnl), "exits": exits, "target": target, "stop": stop,
            "horizon_nodes": horizon_nodes, "cost_bps": cost_bps}


def equity_curve(
    result: dict[str, Any],
    tape: dict[str, Any],
    *,
    zone: str = "DMR",
    target: float = 0.02,
    stop: float = 0.02,
    horizon_nodes: int = 96,
    cost_bps: float = 20.0,
    concurrency: int = 16,
) -> dict[str, Any]:
    """资金曲线（文档B §5.6）：等权 / 并发上限 K / 逐笔复利 / 最大回撤。

    「单笔均值 × 笔数」不是盈利率 —— 它忽略并发上限与资金占用，所以这里显式
    构造组合规则，并**必须同时报「因并发上限跳过的笔数」**（幸存者构造警告）。
    """
    order = [n["scan_id"] for n in tape["nodes"]]
    pos = {sid: i for i, sid in enumerate(order)}
    px: dict[str, dict[str, float]] = {}
    for node in tape["nodes"]:
        px[node["scan_id"]] = {
            str(r["symbol"]): float(r["last_price"] or r["mark_price"] or 0) or 0.0
            for r in node["rows"]
            if (r.get("last_price") or r.get("mark_price"))
        }

    opens: list[tuple[int, int, float, str, str]] = []  # (enter_i, exit_i, pnl, symbol, dir)
    for t in sorted(result["trades"], key=lambda x: (x.get("enter_time_utc") or "", x["trade_id"])):
        if _zone_of(t) != zone:
            continue
        i0 = pos.get(t["enter_scan_id"])
        if i0 is None:
            continue
        entry = px[order[i0]].get(t["symbol"])
        if not entry:
            continue
        up = t["direction"] == "up"
        ret = 0.0
        exit_i = min(i0 + horizon_nodes, len(order) - 1)
        for j in range(i0 + 1, min(i0 + 1 + horizon_nodes, len(order))):
            p = px[order[j]].get(t["symbol"])
            if p is None:
                continue
            ret = (p / entry - 1.0) if up else (1.0 - p / entry)
            if ret >= target:
                ret, exit_i = target, j
                break
            if ret <= -stop:
                ret, exit_i = -stop, j
                break
            exit_i = j
        opens.append((i0, exit_i, ret - cost_bps / 10000.0, t["symbol"], t["direction"]))

    opens.sort(key=lambda x: (x[0], x[3], x[4]))
    equity = 1.0
    live: dict[tuple[str, str], int] = {}
    close_at: dict[int, list[float]] = {}
    curve: list[dict[str, Any]] = []
    taken = skipped = 0
    peak = 1.0
    max_dd = 0.0
    by_enter: dict[int, list[tuple[int, float, str, str]]] = {}
    for i0, i1, r, s, d in opens:
        by_enter.setdefault(i0, []).append((i1, r, s, d))
    for i, sid in enumerate(order):
        for r in close_at.pop(i, []):
            equity *= 1.0 + r / max(1, concurrency)
        for key in [k for k, v in live.items() if v <= i]:
            live.pop(key, None)
        for i1, r, s, d in by_enter.get(i, []):
            if (s, d) in live:
                continue
            if len(live) >= concurrency:
                skipped += 1
                continue
            live[(s, d)] = i1
            close_at.setdefault(i1, []).append(r)
            taken += 1
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
        curve.append({"scan_id": sid, "equity": round(equity, 8)})
    for rs in close_at.values():
        for r in rs:
            equity *= 1.0 + r / max(1, concurrency)
    return {
        "final_equity": equity,
        "window_return": equity - 1.0,
        "max_drawdown": max_dd,
        "opened": taken,
        "skipped_by_concurrency": skipped,
        "concurrency": concurrency,
        "cost_bps": cost_bps,
        "curve": curve,
    }


def eight_cell_metrics(
    result: dict[str, Any],
    tape: Optional[dict[str, Any]],
    *,
    zones: tuple[str, ...] = ("DMR", "CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED"),
    cost_bps: float = 20.0,
    target: float = 0.02,
    stop: float = 0.02,
    horizon_nodes: int = 96,
) -> dict[str, Any]:
    """**强制 8 格**：占用制/交易制 × 含CR/排除CR × 零成本/含成本（文档B §5.1 G3）。

    只报一格是文档A 红线第 15/16/17 条禁止的口径搬运。
    """
    out: dict[str, Any] = {"schema": "metrics-8cell-v1", "by_zone": {}}
    for zone in zones:
        tz = [t for t in result["trades"] if _zone_of(t) == zone]
        cell: dict[str, Any] = {
            "occupancy": {
                "incl_cycle_reset": {
                    "zero_cost": occupancy_metrics(tz, exclude_cycle_reset=False),
                    "with_cost": occupancy_metrics(tz, exclude_cycle_reset=False, cost_bps=cost_bps),
                },
                "excl_cycle_reset": {
                    "zero_cost": occupancy_metrics(tz, exclude_cycle_reset=True),
                    "with_cost": occupancy_metrics(tz, exclude_cycle_reset=True, cost_bps=cost_bps),
                },
            }
        }
        if tape is not None:
            b0 = bracket_metrics(result, tape, zone=zone, target=target, stop=stop,
                                 horizon_nodes=horizon_nodes, cost_bps=0.0)
            b1 = bracket_metrics(result, tape, zone=zone, target=target, stop=stop,
                                 horizon_nodes=horizon_nodes, cost_bps=cost_bps)
            # 交易制不区分 CR：括号出场与周期强平无关，两栏同值并显式标注。
            cell["bracket"] = {
                "incl_cycle_reset": {"zero_cost": b0, "with_cost": b1},
                "excl_cycle_reset": {"zero_cost": b0, "with_cost": b1},
                "note": "括号出场与 CYCLE_RESET 无关（不看何时出区），两栏同值",
            }
        out["by_zone"][zone] = cell
    out["primary_zone_note"] = (
        "DMR / 确认区 → 交易制为主口径；符合 / 观察 / 淘汰区 → 占用制为主口径（文档B §5.1）"
    )
    return out


# ---------------------------------------------------------------------------
# 对账（文档B §4.4）
# ---------------------------------------------------------------------------
def reconcile(result: dict[str, Any], ledger_path: Path) -> dict[str, Any]:
    import sqlite3

    if not Path(ledger_path).is_file():
        return {"status": "missing", "ledger": str(ledger_path)}
    conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    live: dict[str, dict[str, Any]] = {}
    try:
        for r in conn.execute(
            "SELECT zone, COUNT(*) n, "
            "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) w "
            "FROM trades WHERE status='CLOSED' AND pnl_pct IS NOT NULL "
            "AND flags NOT LIKE '%CYCLE_RESET%' GROUP BY zone"
        ):
            live[r["zone"]] = {"n": r["n"], "win_rate": (r["w"] / r["n"]) if r["n"] else None}
    finally:
        conn.close()
    rows = []
    worst = 0.0
    for zone in sorted(set(live) | {_zone_of(t) for t in result["trades"]}):
        tz = [t for t in result["trades"] if _zone_of(t) == zone]
        mine = occupancy_metrics(tz, exclude_cycle_reset=True)
        lw = (live.get(zone) or {}).get("win_rate")
        drift = None
        if lw is not None and mine["win_rate"] is not None:
            drift = abs(lw - mine["win_rate"])
            worst = max(worst, drift)
        rows.append(
            {
                "zone": zone,
                "live_n": (live.get(zone) or {}).get("n"),
                "replay_n": mine["n"],
                "live_win_rate": lw,
                "replay_win_rate": mine["win_rate"],
                "drift": drift,
            }
        )
    return {"status": "ok", "ledger": str(ledger_path), "by_zone": rows, "max_drift": worst}


# ---------------------------------------------------------------------------
# 产物
# ---------------------------------------------------------------------------
def _digest(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _git_sha() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def write_artifacts(
    result: dict[str, Any],
    *,
    settings: SelectionSettings,
    cfg: StateConfig,
    metrics: Optional[dict[str, Any]],
    equity: Optional[dict[str, Any]],
    recon: Optional[dict[str, Any]],
    tape_meta: dict[str, Any],
    argv: list[str],
) -> dict[str, Any]:
    out = Path(result["out_dir"])
    from dataclasses import asdict

    def dump(name: str, obj: Any) -> None:
        (out / name).write_text(
            json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )

    s = {k: v for k, v in asdict(settings).items() if not isinstance(v, Path)}
    s["hermes_root"] = str(settings.hermes_root)
    dump("settings.json", {"param_hash": result["param_hash"], **s})
    dump("state_config.json", asdict(cfg))

    with open(out / "occupancy.csv", "w", encoding="utf-8") as fh:
        cols = list(result["occupancy_series"][0].keys()) if result["occupancy_series"] else []
        fh.write(",".join(cols) + "\n")
        for row in result["occupancy_series"]:
            fh.write(",".join(str(row[c]) for c in cols) + "\n")

    _write_trades_sqlite(out / f"ledger-{result['exp_id']}.sqlite", result["trades"])

    if metrics is not None:
        dump("metrics.json", metrics)
    if equity is not None:
        with open(out / "equity.csv", "w", encoding="utf-8") as fh:
            fh.write("scan_id,equity\n")
            for p in equity["curve"]:
                fh.write(f"{p['scan_id']},{p['equity']}\n")
        dump("equity.json", {k: v for k, v in equity.items() if k != "curve"})
    if recon is not None:
        dump("reconcile.json", recon)

    input_digest = _digest({"tape": tape_meta, "settings": s, "cfg": asdict(cfg)})
    output_digest = _digest(
        {"trades": [{k: t.get(k) for k in ("trade_id", "zone", "status", "pnl_pct")}
                    for t in result["trades"]],
         "occupancy": result["occupancy_series"]}
    )
    manifest = {
        "exp_id": result["exp_id"],
        "param_hash": result["param_hash"],
        "input_digest": input_digest,
        "output_digest": output_digest,
        "n_nodes": result["n_nodes"],
        "n_missing_nodes": result["n_missing_nodes"],
        "first_scan": result["first_scan"],
        "last_scan": result["last_scan"],
        "n_cycle_resets": result["n_cycle_resets"],
        "n_trades": len(result["trades"]),
        "alerts_histogram": result["alerts_histogram"],
        "git_sha": _git_sha(),
        "argv": argv,
        "tape": tape_meta,
        "replayer_precision_note": "回放器精度上界 0.09%（文档B §4.4 对账）",
    }
    dump("manifest.json", manifest)
    return manifest


def _write_trades_sqlite(path: Path, trades: list[dict[str, Any]]) -> None:
    import sqlite3

    from coin_selection.review_ledger import SCHEMA, TRADE_COLS, _row_tuple

    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            f"INSERT OR REPLACE INTO trades ({','.join(TRADE_COLS)}) "
            f"VALUES ({','.join('?' * len(TRADE_COLS))})",
            [_row_tuple(t) for t in trades],
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 置换零模型（文档B §6.5-P4）
# ---------------------------------------------------------------------------
def null_model_permute_combo(
    tape: dict[str, Any], *, iters: int = 200, horizons: tuple[int, ...] = (4, 16, 48, 96), seed: int = 20260901
) -> dict[str, Any]:
    """节点内随机打乱组合标签 N 次，报族错误率 —— 多重比较修正的直接实现。

    做法（与文档A §4.6(b)(c) 一致）：每个节点内先减去该节点全体 G1 通过样本的
    平均前瞻收益（剔除市场 β），再按自然日聚类做 t 检验；然后把标签打乱重跑，
    取每轮的 max|t| 作为零分布。
    """
    rng = random.Random(seed)
    nodes = tape["nodes"]
    order = [n["scan_id"] for n in nodes]
    pos = {sid: i for i, sid in enumerate(order)}
    px = [
        {str(r["symbol"]): float(r.get("last_price") or r.get("mark_price") or 0)
         for r in n["rows"] if (r.get("last_price") or r.get("mark_price"))}
        for n in nodes
    ]

    obs: list[tuple[int, str, str, float]] = []  # (node_i, day, grade_6h, demeaned fwd 4h)
    h = 16
    for i, n in enumerate(nodes):
        if i + h >= len(nodes):
            break
        day = n["scan_id"].split("-")[0]
        rets: list[tuple[str, float]] = []
        for r in n["rows"]:
            if not r.get("liquidity_hard_pass"):
                continue
            g = r.get("mcap_grade_6h")
            if not isinstance(g, str):
                continue
            s = str(r["symbol"])
            p0, p1 = px[i].get(s), px[i + h].get(s)
            if not p0 or not p1:
                continue
            rets.append((g, p1 / p0 - 1.0))
        if len(rets) < 20:
            continue
        mu = sum(v for _, v in rets) / len(rets)
        for g, v in rets:
            obs.append((i, day, g, v - mu))

    def max_abs_t(labels: list[str]) -> float:
        by: dict[tuple[str, str], list[float]] = {}
        for (i, day, _g, v), g in zip(obs, labels):
            by.setdefault((g, day), []).append(v)
        best = 0.0
        for g in "ABCDEF":
            daily = [sum(v) / len(v) for (gg, _d), v in sorted(by.items()) if gg == g and v]
            if len(daily) < 3:
                continue
            m = sum(daily) / len(daily)
            var = sum((x - m) ** 2 for x in daily) / (len(daily) - 1)
            se = math.sqrt(var / len(daily)) if var > 0 else 0.0
            if se:
                best = max(best, abs(m / se))
        return best

    labels = [g for _i, _d, g, _v in obs]
    observed = max_abs_t(labels)
    nulls: list[float] = []
    by_node: dict[int, list[int]] = {}
    for idx, (i, _d, _g, _v) in enumerate(obs):
        by_node.setdefault(i, []).append(idx)
    for _ in range(iters):
        perm = list(labels)
        for _i, idxs in sorted(by_node.items()):
            vals = [labels[j] for j in idxs]
            rng.shuffle(vals)
            for j, v in zip(idxs, vals):
                perm[j] = v
        nulls.append(max_abs_t(perm))
    nulls.sort()

    def q(p: float) -> float:
        return nulls[min(len(nulls) - 1, int(p * len(nulls)))] if nulls else 0.0

    fwer = sum(1 for x in nulls if x >= observed) / len(nulls) if nulls else 1.0
    return {
        "n_obs": len(obs),
        "iters": iters,
        "observed_max_abs_t": observed,
        "null_quantiles": {"p50": q(0.5), "p90": q(0.9), "p95": q(0.95), "p99": q(0.99)},
        "family_wise_p": fwer,
        "verdict": "无可检出边际" if fwer > 0.05 else "边际显著（仍需样本外复验）",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _apply_json_overrides(obj: Any, blob: Optional[str], label: str) -> Any:
    if not blob:
        return obj
    data = json.loads(blob)
    known = set(getattr(obj, "__dataclass_fields__", {}))
    clean = {k: v for k, v in data.items() if k in known}
    for k in sorted(set(data) - known):
        print(f"[WARN] {label} override ignored (unknown field): {k}", file=sys.stderr)
    return replace(obj, **clean) if clean else obj


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="Offline rescoring replayer (文档B §4)")
    ap.add_argument("--build-tape", action="store_true")
    ap.add_argument("--source", default="data/coin-selection/snapshots")
    ap.add_argument("--out", default="data/research/tape.pkl")
    ap.add_argument("--tape", default="data/research/tape.pkl")
    ap.add_argument("--exp", default="E0")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--settings", default=None, help="JSON → SelectionSettings 增量")
    ap.add_argument("--state", default=None, help="JSON → StateConfig 增量")
    ap.add_argument("--board", default=BOARD_Y_KEY)
    ap.add_argument("--recalibrate-thresholds", choices=("quantile",), default=None)
    ap.add_argument(
        "--recalibrate-baseline",
        choices=("frozen-defaults", "variant"),
        default="frozen-defaults",
        help=(
            "分位对齐的 F0 取哪套权重。frozen-defaults（缺省）= scan.py 的 v1.4.0 "
            "冻结默认值，也就是现有阈值 45/40/58/62/52/66/56/70 被标定出来时的权重；"
            "variant = 本变体 overrides 后的权重（当 overrides 已带新权重时，"
            "它会让 F0 == F1，重标定退化为恒等 —— 那不是重标定）"
        ),
    )
    zone = ap.add_mutually_exclusive_group()
    zone.add_argument("--mcap-zone", action="store_true")
    zone.add_argument("--no-mcap-zone", action="store_true")
    ap.add_argument("--mcap-zone-shadow", action="store_true")
    ap.add_argument("--mcap-cuts", choices=tuple(MCAP_CUT_PRESETS), default=None)
    ap.add_argument("--from-scan", default=None)
    ap.add_argument("--to-scan", default=None)
    ap.add_argument("--split", type=float, default=None, help="样本内比例，如 0.6")
    ap.add_argument("--metrics", action="store_true")
    ap.add_argument("--metrics-mode", choices=("occupancy", "bracket", "both"), default="both")
    ap.add_argument("--target", type=float, default=0.02)
    ap.add_argument("--stop", type=float, default=0.02)
    ap.add_argument("--horizon-nodes", type=int, default=96)
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--equity-curve", action="store_true")
    ap.add_argument("--reconcile", default=None)
    ap.add_argument("--null-model", choices=("permute-combo",), default=None)
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--cycle-hours", type=int, default=24)
    ap.add_argument("--cycle-anchor-utc", default="00:00")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.build_tape:
        info = build_tape(
            ROOT / args.source if not Path(args.source).is_absolute() else Path(args.source),
            Path(args.out),
            from_scan=args.from_scan,
            to_scan=args.to_scan,
        )
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    tape_path = Path(args.tape)
    tape_path = tape_path if tape_path.is_absolute() else ROOT / tape_path
    if not tape_path.is_file():
        print(f"[FAIL] tape missing: {tape_path}\n       run: --build-tape", file=sys.stderr)
        return 2
    tape = load_tape(tape_path)

    if args.null_model == "permute-combo":
        out = null_model_permute_combo(tape, iters=args.iters)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    variant = get_variant(args.board)
    base_settings = variant_settings(SelectionSettings(), variant)
    settings = _apply_json_overrides(base_settings, args.settings, "settings")
    cfg = _apply_json_overrides(variant_state_config(variant), args.state, "state_config")

    if args.recalibrate_thresholds == "quantile":
        # F0 必须是**现有阈值被标定出来时**的那套权重。本仓库 Y 的 overrides 已经
        # 带了新权重，若拿它当 F0 就会得到 F0 == F1 -> tau' == tau，重标定变成恒等
        # 映射，而那恰恰是文档A §2.4 点名的「只改权重不改阈值」的坑。
        if args.recalibrate_baseline == "frozen-defaults":
            recal_base = replace(
                SelectionSettings(),
                parameter_version=settings.parameter_version,
                data_dir=settings.data_dir,
                dmr_inbox=settings.dmr_inbox,
            )
        else:
            recal_base = base_settings
        recal = recalibrate_thresholds(tape, recal_base, settings, cfg)
        cfg = replace(cfg, **recal)
        if not args.quiet:
            print("[INFO] 分位对齐重标定（脚本现算，非手填）:",
                  json.dumps(recal, ensure_ascii=False))

    mode = "off"
    if args.mcap_zone:
        mode = "on"
    elif args.mcap_zone_shadow:
        mode = "shadow"
    settings = replace(settings, mcap_zone_mode=mode)
    if args.mcap_cuts:
        c = MCAP_CUT_PRESETS[args.mcap_cuts]
        settings = replace(
            settings,
            mcap_zone_cut_dmr=c["dmr"],
            mcap_zone_cut_confirmed=c["confirmed"],
            mcap_zone_cut_qualified=c["qualified"],
            mcap_zone_cut_watch=c["watch"],
        )

    p_hash = param_fingerprint(variant, settings, cfg)
    out_dir = Path(args.out_dir) if args.out_dir else (RESEARCH_ROOT / args.exp)

    from_scan, to_scan = args.from_scan, args.to_scan
    split_note = None
    if args.split:
        ids = [n["scan_id"] for n in tape["nodes"]]
        cut = ids[min(len(ids) - 1, int(len(ids) * args.split))]
        split_note = {"split": args.split, "cut_scan_id": cut}

    if not args.quiet:
        print(f"[INFO] exp={args.exp} param_hash={p_hash} mcap_zone_mode={mode} "
              f"nodes={tape['n_nodes']} ({tape['first']} → {tape['last']})")

    result = replay_rescore(
        tape=tape,
        exp_id=args.exp,
        out_dir=out_dir,
        settings=settings,
        cfg=cfg,
        mcap_zone_mode=mode,
        cycle_hours=args.cycle_hours,
        cycle_anchor_utc=args.cycle_anchor_utc,
        from_scan=from_scan,
        to_scan=to_scan,
        param_hash=p_hash,
        progress=not args.quiet,
    )

    metrics = None
    if args.metrics:
        metrics = eight_cell_metrics(
            result,
            tape if args.metrics_mode in ("bracket", "both") else None,
            cost_bps=args.cost_bps,
            target=args.target,
            stop=args.stop,
            horizon_nodes=args.horizon_nodes,
        )
        if split_note:
            cut = split_note["cut_scan_id"]
            ins = {**result, "trades": [t for t in result["trades"]
                                        if (t.get("enter_scan_id") or "") <= cut]}
            oos = {**result, "trades": [t for t in result["trades"]
                                        if (t.get("enter_scan_id") or "") > cut]}
            metrics["split"] = {
                **split_note,
                "in_sample": eight_cell_metrics(ins, None, cost_bps=args.cost_bps),
                "out_of_sample": eight_cell_metrics(oos, None, cost_bps=args.cost_bps),
                "warning": (
                    "单段样本外结论不可采信（文档B §6.3 / R15）：段间差 > 实验间差，"
                    "必须配 walk-forward 与 ≥20 个日聚类"
                ),
            }

    equity = None
    if args.equity_curve:
        equity = equity_curve(
            result, tape, target=args.target, stop=args.stop,
            horizon_nodes=args.horizon_nodes, cost_bps=args.cost_bps,
            concurrency=args.concurrency,
        )

    recon = reconcile(result, ROOT / args.reconcile if args.reconcile and not Path(args.reconcile).is_absolute()
                      else Path(args.reconcile)) if args.reconcile else None

    manifest = write_artifacts(
        result,
        settings=settings,
        cfg=cfg,
        metrics=metrics,
        equity=equity,
        recon=recon,
        tape_meta={"path": str(tape_path), "n_nodes": tape["n_nodes"],
                   "first": tape["first"], "last": tape["last"]},
        argv=argv,
    )

    summary = {
        "exp_id": result["exp_id"],
        "param_hash": result["param_hash"],
        "out_dir": result["out_dir"],
        "n_nodes": result["n_nodes"],
        "n_missing_nodes": result["n_missing_nodes"],
        "n_trades": len(result["trades"]),
        "input_digest": manifest["input_digest"],
        "output_digest": manifest["output_digest"],
    }
    if metrics:
        dmr = metrics["by_zone"]["DMR"]["occupancy"]["excl_cycle_reset"]["zero_cost"]
        summary["dmr_occupancy_excl_cr_zero_cost"] = {
            "n": dmr["n"], "win_rate": dmr["win_rate"],
            "payoff": dmr["payoff"], "pf": dmr["pf"],
        }
    if recon:
        summary["reconcile_max_drift"] = recon.get("max_drift")
    if equity:
        summary["equity"] = {k: equity[k] for k in
                             ("final_equity", "window_return", "max_drawdown",
                              "opened", "skipped_by_concurrency")}
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
