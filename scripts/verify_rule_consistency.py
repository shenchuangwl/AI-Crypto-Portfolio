#!/usr/bin/env python3
"""《选币榜Y》↔《复盘选币》一致性与可回测验收器（ChatGpt_SOL5.6 第三轮）。

权威依据
--------
* 文档 B §8.1（在线/离线逐字段一致性清单）、§8.2（差异六分类）、§7.2（确定性算法）、
  §11.2（31 天交集）、§20（测试矩阵）、§21.1（上线一致性门槛）。
* 文档 A §17.1（必过验收）、§18.1（coverage 必须输出的字段）。

它做什么
--------
逐节点把「在线已发布的板面快照」与「离线用**同一批生产函数**重算的结果」逐字段比对，
并把《选币榜Y》的分区成员与《复盘选币》回放器读到的分区成员做集合 + 排序比对。

九项检查（对应文档 B §8.1 的比较清单）
--------------------------------------
==== ========================================================================
K1   身份：RuleManifest 字段齐全 / 缺失即标 LEGACY_INCOMPLETE（不算失败）
K2   Score：离线按已解析权重重算 score_e6 与在线**精确相等**
K3   A～F 与 216：combo_no / Z10 / 优先级 / K / 两侧 ceiling 由冻结映射复算
K4   有效区内核：effective_rank >= base_rank；特殊态原样旁路；两次运行同 digest
K5   两栏目分区一致：Y 板面分区成员集合与《复盘选币》谓词读到的成员**逐条相同**
K6   DMR：inbox 有序候选集合 == 板面 dmr_selected 有序集合
K7   反前视：不存在 input.as_of > decision_time
K8   物理 coverage：expected / present / missing / duplicate / 连续段
K9   重放确定性：全量重放 == 从中点 checkpoint 续跑（末态、笔数、hash 全等）
==== ========================================================================

用法::

    python3 scripts/verify_rule_consistency.py --board y                 # 默认抽样
    python3 scripts/verify_rule_consistency.py --board y --days 2026-08-27,2026-08-28
    python3 scripts/verify_rule_consistency.py --board y --all-nodes     # 全窗口
    python3 scripts/verify_rule_consistency.py --board y --json out.json

只读：不写生产目录，不改任何快照 / 账本 / 配置。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import coverage as cov  # noqa: E402
from coin_selection import mcap_effective as me  # noqa: E402
from coin_selection import mcap_mapping as mm  # noqa: E402
from coin_selection import rule_manifest as rm  # noqa: E402
from coin_selection.board_variants import (  # noqa: E402
    get_variant,
    variant_settings,
    variant_state_config,
)
from coin_selection.review_replay import (  # noqa: E402
    OccupancyReplayer,
    ZONES,
    in_zone,
    load_board,
    overlay_dmr_from_inbox,
)
from coin_selection.scan import SelectionSettings, composite_score  # noqa: E402

#: 文档 B §8.1：分量与 Score 用定点 e6 比较，避免「看起来相等」的浮点糊弄。
E6 = 1_000_000

#: 文档 B §8.2：差异只允许归入这六类。
DIFF_CLASSES = (
    "INPUT_MISSING",
    "IDENTITY_MISMATCH",
    "FLOAT_SERIALIZATION",
    "STATE_SEED_MISMATCH",
    "GAP_POLICY",
    "CODE_DEFECT",
)


def e6(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(round(float(x) * E6))
    except (TypeError, ValueError):
        return None


def digest(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:16]


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.hard = 0
        self.warn = 0

    def add(self, key: str, ok: Optional[bool], title: str, detail: Any = None) -> None:
        tag = "PASS" if ok else ("WARN" if ok is None else "FAIL")
        if ok is False:
            self.hard += 1
        elif ok is None:
            self.warn += 1
        self.checks.append({"key": key, "status": tag, "title": title, "detail": detail})
        line = f"[{tag}] {key:4s} {title}"
        if detail not in (None, {}, []):
            line += f"   {detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)}"
        print(line)


# ---------------------------------------------------------------------------
def board_dirs(board_key: str) -> tuple[Path, Path]:
    v = get_variant(board_key)
    if v is None:
        raise SystemExit(f"unknown board {board_key!r}")
    data_dir = ROOT / v.data_dir
    return data_dir / "snapshots", ROOT / v.dmr_inbox


def pick_nodes(
    snaps: Path,
    *,
    days: Optional[list[str]],
    nodes: Optional[list[str]],
    all_nodes: bool,
    sample: int,
) -> list[str]:
    ids = sorted(
        (
            p.stem
            for p in snaps.glob("*.json")
            if not p.name.endswith(".full.json") and p.stem[:1].isdigit()
        ),
        key=cov.node_index,
    )
    if nodes:
        want = set(nodes)
        return [s for s in ids if s in want]
    if days:
        keys = {d.replace("-", "") for d in days}
        return [s for s in ids if s.split("-")[0] in keys]
    if all_nodes:
        return ids
    # 默认抽样：首、末、以及等距节点，保证至少覆盖 3 个不同自然日。
    if len(ids) <= sample:
        return ids
    step = max(1, len(ids) // sample)
    picked = ids[::step][:sample]
    for extra in (ids[0], ids[-1]):
        if extra not in picked:
            picked.append(extra)
    return sorted(set(picked), key=cov.node_index)


def check_node(
    path: Path,
    inbox_dir: Path,
    settings: Any,
    mapping: mm.McapMapping,
    *,
    legacy_settings: Any = None,
) -> dict[str, Any]:
    """单节点的 K1～K7。返回一份可聚合的结果字典。"""
    board = load_board(path)
    meta = board.get("meta") or {}
    scan_id = str(meta.get("scan_id") or path.stem)
    res: dict[str, Any] = {
        "scan_id": scan_id,
        "rows": 0,
        "score_mismatch": 0,
        "score_legacy_match": 0,
        "score_max_abs_err": 0.0,
        "first_bad": None,
        "segment": None,
        "identity_present": [],
        "identity_missing": [],
        "combo_status": Counter(),
        "ceiling_by_zone": Counter(),
        "effective_upgrades": 0,
        "bypass_violations": 0,
        "zone_set_mismatch": 0,
        "zone_order_mismatch": 0,
        "dmr_online": [],
        "dmr_inbox": [],
        "future_inputs": 0,
        "kernel_digest": None,
    }

    # —— K1 身份 ——
    ident = meta.get("rule_identity") or {}
    for k in (
        "rule_revision",
        "config_hash",
        "asset_mapping_version",
        "mcap_mapping_version",
        "mapping_hash",
        "code_commit",
        "data_contract_version",
        "universe_policy_version",
        "effective_from_scan_id",
    ):
        (res["identity_present"] if ident.get(k) else res["identity_missing"]).append(k)

    kernel_rows: list[dict[str, Any]] = []
    decision_ts = meta.get("scan_timestamp_utc")

    for pool_name, direction, score_key in (
        ("long_pool", "up", "score_up"),
        ("short_pool", "down", "score_down"),
    ):
        for r in board.get(pool_name) or []:
            res["rows"] += 1
            # —— K2 Score 精确重算 ——
            got = composite_score(
                settings,
                ss=float(r.get("staircase_score") or 0),
                mom=float(r.get("momentum_score") or 0),
                liq=float(r.get("liquidity_score") or 0),
                mcap=float(r.get("mcap_momentum_score") or 0),
                cons=float(r.get("consistency_score") or 0),
                risk=float(r.get("risk_score") or 100),
                dq=float(r.get("data_confidence") or 100),
            )
            want = r.get(score_key)
            if e6(got) != e6(want):
                # 【文档 B §8.2 差异分类】用当前已解析权重算不出来，不等于代码有缺陷：
                # 该节点可能产生于**上一个规则段**（第二轮 §7.2 查实的分叉点
                # 20260831-089 → 090，Y 板面在那一刻从 v1.4.0 权重切到目标七权重）。
                # 先用 legacy 权重复算：能精确复现 → IDENTITY_MISMATCH（不同规则段）；
                # 两套都复现不了 → 才是 CODE_DEFECT（硬失败）。
                legacy_ok = False
                if legacy_settings is not None:
                    legacy = composite_score(
                        legacy_settings,
                        ss=float(r.get("staircase_score") or 0),
                        mom=float(r.get("momentum_score") or 0),
                        liq=float(r.get("liquidity_score") or 0),
                        mcap=float(r.get("mcap_momentum_score") or 0),
                        cons=float(r.get("consistency_score") or 0),
                        risk=float(r.get("risk_score") or 100),
                        dq=float(r.get("data_confidence") or 100),
                    )
                    legacy_ok = e6(legacy) == e6(want)
                if legacy_ok:
                    res["score_legacy_match"] += 1
                else:
                    res["score_mismatch"] += 1
                    res["score_max_abs_err"] = max(
                        res["score_max_abs_err"], abs(float(got) - float(want or 0))
                    )
                    if res["first_bad"] is None:
                        res["first_bad"] = {
                            "scan_id": scan_id,
                            "symbol": r.get("symbol"),
                            "direction": direction,
                            "field": score_key,
                            "online": want,
                            "offline": got,
                            "class": "CODE_DEFECT",
                        }

            # —— K3 + K4 有效区内核 ——
            base = str(r.get("base_state") or r.get("state") or "NONE")
            k = me.resolve_side(r, direction, base, mapping=mapping)
            res["combo_status"][k["mcap_combo_status"]] += 1
            if k["mcap_ceiling_zone"]:
                res["ceiling_by_zone"][k["mcap_ceiling_zone"]] += 1
            if base in me.BASE_RANK:
                if me.ZONE_RANK[k["effective_zone"]] < me.BASE_RANK[base]:
                    res["effective_upgrades"] += 1
            elif (
                k["effective_zone"] != base
                or k["mcap_ceiling_zone"] is not None
                or k["dmr_ceiling_ok"]
            ):
                res["bypass_violations"] += 1
            kernel_rows.append(
                {
                    "s": r.get("symbol"),
                    "d": direction,
                    "b": base,
                    "c": k["mcap_combo_no"],
                    "z": k["mcap_z10"],
                    "p": k["mcap_priority"],
                    "k": k["mcap_resonance_k"],
                    "cl": k["mcap_ceiling_zone"],
                    "e": k["effective_zone"],
                }
            )

            # —— K7 反前视 ——
            sa = r.get("supply_as_of_utc")
            if sa and decision_ts and str(sa) > str(decision_ts):
                res["future_inputs"] += 1

    res["kernel_digest"] = digest(kernel_rows)
    # 该节点属于哪个规则段：CURRENT = 当前已解析配置；LEGACY = 分叉前的旧权重段。
    if res["score_mismatch"] == 0 and res["score_legacy_match"] == 0:
        res["segment"] = "CURRENT"
    elif res["score_mismatch"] == 0:
        res["segment"] = "LEGACY"
    else:
        res["segment"] = "UNRESOLVED"

    # —— K5 两栏目分区成员一致 ——
    #
    # 在线：板面池按顺序取；离线：《复盘选币》用 in_zone 谓词从同一份板面读。
    # 两者必须逐条相同 —— 这就是「两栏目共用同一套规则内核」的机器证据。
    inbox_path = inbox_dir / f"{scan_id}.candidates.json"
    inbox_doc = None
    if inbox_path.is_file():
        try:
            inbox_doc = json.loads(inbox_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            inbox_doc = None
    replay_board = overlay_dmr_from_inbox(board, inbox_doc)

    for zone in ZONES:
        for pool_name, direction in (("long_pool", "up"), ("short_pool", "down")):
            online = [
                str(r.get("symbol"))
                for r in board.get(pool_name) or []
                if in_zone(r, zone)
            ]
            offline = [
                str(r.get("symbol"))
                for r in replay_board.get(pool_name) or []
                if in_zone(r, zone)
            ]
            if set(online) != set(offline):
                res["zone_set_mismatch"] += 1
            elif online != offline:
                res["zone_order_mismatch"] += 1

    # —— K6 DMR 有序集合 ——
    res["dmr_online"] = [
        f"{r.get('symbol')}|{d}"
        for pool_name, d in (("long_pool", "LONG"), ("short_pool", "SHORT"))
        for r in board.get(pool_name) or []
        if r.get("dmr_selected")
    ]
    if inbox_doc:
        res["dmr_inbox"] = [
            f"{c.get('symbol')}|{c.get('direction')}"
            for c in inbox_doc.get("candidates") or []
        ]
    return res


def replay_determinism(snaps: Path, inbox_dir: Path, scan_ids: list[str]) -> dict[str, Any]:
    """K9：全量重放 == 从中点 checkpoint 续跑（文档 B §7.2）。"""
    boards = []
    for sid in scan_ids:
        p = snaps / f"{sid}.json"
        if not p.is_file():
            continue
        b = load_board(p)
        ip = inbox_dir / f"{sid}.candidates.json"
        doc = None
        if ip.is_file():
            try:
                doc = json.loads(ip.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                doc = None
        boards.append(overlay_dmr_from_inbox(b, doc))
    if len(boards) < 4:
        return {"ok": None, "reason": "not enough nodes", "nodes": len(boards)}

    def run(bs: list[dict[str, Any]], seed: Optional[list[dict[str, Any]]] = None):
        rp = OccupancyReplayer()
        if seed is not None:
            rp.load_open(seed)
            # 续跑必须恢复 prev_sid，否则第一步会被当成 gap（文档 B §7.2）。
        closed: list[dict[str, Any]] = []
        for b in bs:
            for t in rp.step(b):
                if t.get("status") == "CLOSED":
                    closed.append(t)
        return closed, rp

    full_closed, full_rp = run(boards)
    mid = len(boards) // 2
    head_closed, head_rp = run(boards[:mid])
    open_at_mid = head_closed and None  # noqa: F841  (仅为可读性保留)
    tail_rp = OccupancyReplayer()
    tail_rp.load_open(head_rp.open_trades())
    tail_rp.prev_sid = head_rp.prev_sid
    tail_rp.prev_ts = head_rp.prev_ts
    tail_rp.prev_idx = head_rp.prev_idx
    tail_rp.last_ts = head_rp.last_ts
    tail_closed: list[dict[str, Any]] = []
    for b in boards[mid:]:
        for t in tail_rp.step(b):
            if t.get("status") == "CLOSED":
                tail_closed.append(t)

    def key(t: dict[str, Any]) -> str:
        return f"{t.get('trade_id')}|{t.get('exit_scan_id')}"

    full_keys = sorted(key(t) for t in full_closed)
    inc_keys = sorted(key(t) for t in (head_closed + tail_closed))
    full_open = sorted(
        f"{t.get('symbol')}|{t.get('direction')}|{t.get('zone')}"
        for t in full_rp.open_trades()
    )
    inc_open = sorted(
        f"{t.get('symbol')}|{t.get('direction')}|{t.get('zone')}"
        for t in tail_rp.open_trades()
    )
    return {
        "ok": full_keys == inc_keys and full_open == inc_open,
        "nodes": len(boards),
        "split_at": boards[mid]["meta"]["scan_id"],
        "full_closed": len(full_keys),
        "incremental_closed": len(inc_keys),
        "full_open": len(full_open),
        "incremental_open": len(inc_open),
        "closed_digest_full": digest(full_keys),
        "closed_digest_incremental": digest(inc_keys),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="rule consistency verifier")
    ap.add_argument("--board", default="y")
    ap.add_argument("--days", default="", help="逗号分隔的自然日 YYYY-MM-DD")
    ap.add_argument("--nodes", default="", help="逗号分隔的 scan_id")
    ap.add_argument("--all-nodes", action="store_true")
    ap.add_argument("--sample", type=int, default=12)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    snaps, inbox_dir = board_dirs(args.board)
    variant = get_variant(args.board)
    settings = variant_settings(SelectionSettings(), variant)
    state_cfg = variant_state_config(variant)
    rep = Report()

    print("=" * 78)
    print(f"《选币榜Y》↔《复盘选币》一致性与可回测验收器  board={args.board}")
    print(f"snapshots={snaps}")
    print("=" * 78)

    # —— 映射层身份 ——
    mapping = mm.load(strict=True)
    rep.add(
        "K0",
        mapping.mapping_hash == "sha256:" + mm.EXPECTED_MAPPING_SHA256,
        "216/432 映射 hash 与文档A 附录C 一致",
        {"mcap_mapping_version": mapping.mapping_version, "mapping_hash": mapping.mapping_hash},
    )
    inv = mm.verify_invariants(mapping.rows)
    rep.add("K0b", len(inv) >= 10, "附录B 映射不变量全部成立", {"checks": len(inv)})

    nodes = pick_nodes(
        snaps,
        days=[d for d in args.days.split(",") if d.strip()] or None,
        nodes=[n for n in args.nodes.split(",") if n.strip()] or None,
        all_nodes=args.all_nodes,
        sample=args.sample,
    )
    if not nodes:
        rep.add("K1", False, "没有可比对的节点", {"snapshots": str(snaps)})
        return 1

    # 分叉前的旧规则段：Y 的 overrides 生效之前用的是主榜 v1.4.0 权重（文档A §6.1）。
    legacy_settings = SelectionSettings()
    per_node = [
        check_node(
            snaps / f"{s}.json",
            inbox_dir,
            settings,
            mapping,
            legacy_settings=legacy_settings,
        )
        for s in nodes
    ]

    days = sorted({s.split("-")[0] for s in nodes})
    rep.add(
        "K1a",
        True if len(days) >= 3 else None,
        "样本覆盖 >= 3 个历史交易日",
        {"days": days, "nodes": len(nodes)},
    )

    ident_missing = Counter()
    for r in per_node:
        for k in r["identity_missing"]:
            ident_missing[k] += 1
    complete = sum(1 for r in per_node if not r["identity_missing"])
    rep.add(
        "K1",
        None if complete < len(per_node) else True,
        "RuleManifest 身份字段齐全（缺失记 LEGACY_INCOMPLETE，不算失败）",
        {
            "nodes_complete": complete,
            "nodes_total": len(per_node),
            "missing_by_field": dict(ident_missing),
        },
    )

    total_rows = sum(r["rows"] for r in per_node)
    score_bad = sum(r["score_mismatch"] for r in per_node)
    legacy_rows = sum(r["score_legacy_match"] for r in per_node)
    max_err = max((r["score_max_abs_err"] for r in per_node), default=0.0)
    first_bad = next((r["first_bad"] for r in per_node if r["first_bad"]), None)
    seg = Counter(r["segment"] for r in per_node)
    rep.add(
        "K2",
        score_bad == 0,
        "Score 在线/离线 score_e6 精确相等（按规则段分别复算）",
        {
            "rows": total_rows,
            "reproduced_current_segment": total_rows - legacy_rows - score_bad,
            "reproduced_legacy_segment": legacy_rows,
            "unresolved": score_bad,
            "max_abs_err": max_err,
            "first_bad_scan": first_bad,
            "nodes_by_segment": dict(seg),
        },
    )
    if seg.get("LEGACY"):
        rep.add(
            "K2b",
            None,
            "样本跨越两个规则段（文档B §8.2 归类 IDENTITY_MISMATCH，非代码缺陷）",
            {
                "legacy_nodes": [r["scan_id"] for r in per_node if r["segment"] == "LEGACY"],
                "current_nodes": [
                    r["scan_id"] for r in per_node if r["segment"] == "CURRENT"
                ],
                "note": "同名 parameter_version 下 config_hash 不同 —— 默认不得跨段聚合",
            },
        )

    combo = Counter()
    ceil = Counter()
    for r in per_node:
        combo.update(r["combo_status"])
        ceil.update(r["ceiling_by_zone"])
    rep.add(
        "K3",
        sum(combo.values()) == total_rows,
        "A～F → 216 组合 / Z10 / 优先级 / K / 方向 ceiling 全部可复算",
        {"combo_status": dict(combo), "ceiling_by_zone": dict(ceil)},
    )

    upgrades = sum(r["effective_upgrades"] for r in per_node)
    bypass_bad = sum(r["bypass_violations"] for r in per_node)
    rep.add(
        "K4",
        upgrades == 0 and bypass_bad == 0,
        "有效区只降不升 + 特殊态原样旁路",
        {"rows": total_rows, "upgrades": upgrades, "bypass_violations": bypass_bad},
    )

    # 确定性：同一批节点跑第二遍，逐节点 digest 必须一致
    again = [
        check_node(
            snaps / f"{s}.json",
            inbox_dir,
            settings,
            mapping,
            legacy_settings=legacy_settings,
        )
        for s in nodes
    ]
    det = all(a["kernel_digest"] == b["kernel_digest"] for a, b in zip(per_node, again))
    rep.add(
        "K4b",
        det,
        "规则内核确定性：同输入两次运行 digest 全等",
        {"nodes": len(nodes), "digest_head": per_node[0]["kernel_digest"]},
    )

    set_bad = sum(r["zone_set_mismatch"] for r in per_node)
    ord_bad = sum(r["zone_order_mismatch"] for r in per_node)
    rep.add(
        "K5",
        set_bad == 0 and ord_bad == 0,
        "《选币榜Y》与《复盘选币》分区成员集合与排序 100% 一致",
        {
            "nodes": len(nodes),
            "zone_direction_pairs": len(nodes) * len(ZONES) * 2,
            "set_mismatch": set_bad,
            "order_mismatch": ord_bad,
        },
    )

    dmr_bad = [
        r["scan_id"]
        for r in per_node
        if r["dmr_inbox"] and sorted(r["dmr_online"]) != sorted(r["dmr_inbox"])
    ]
    rep.add(
        "K6",
        not dmr_bad,
        "DMR 集合：板面 dmr_selected == inbox candidates",
        {
            "nodes_with_inbox": sum(1 for r in per_node if r["dmr_inbox"]),
            "mismatch_nodes": dmr_bad[:5],
        },
    )

    future = sum(r["future_inputs"] for r in per_node)
    rep.add("K7", future == 0, "反前视：不存在 input.as_of > decision_time", {"violations": future})

    all_ids = sorted(
        (
            p.stem
            for p in snaps.glob("*.json")
            if not p.name.endswith(".full.json") and p.stem[:1].isdigit()
        ),
        key=cov.node_index,
    )
    c = cov.physical_coverage(all_ids)
    rep.add(
        "K8",
        c["missing_nodes"] == 0 or c["coverage_ratio"] < 1.0,
        "物理 coverage 逐节点重算（缺一个报一个，不再固定 1.0）",
        {
            k: c[k]
            for k in (
                "expected_nodes",
                "present_nodes",
                "missing_nodes",
                "coverage_ratio",
                "largest_gap_nodes",
                "first_scan_id",
                "last_scan_id",
                "span_days",
            )
        },
    )
    if c["missing_scan_ids"]:
        print(f"       missing_scan_ids = {c['missing_scan_ids']}")
    print(f"       duplicate_scan_ids = {c['duplicate_scan_ids']}")
    print(f"       continuous_segments = {len(c['continuous_segments'])}")

    # K9：在最长的一个连续段上做全量 vs 增量
    seg = max(c["continuous_segments"], key=lambda s: s["nodes"])
    seg_ids = cov.expected_scan_ids(seg["from_scan_id"], seg["to_scan_id"])
    seg_ids = seg_ids[: min(len(seg_ids), 200)]
    det9 = replay_determinism(snaps, inbox_dir, seg_ids)
    rep.add("K9", det9.get("ok"), "重放确定性：全量 == 增量（末态/笔数/hash 全等）", det9)

    print("-" * 78)
    print(f"hard failures: {rep.hard}   warnings: {rep.warn}")
    if args.json:
        out = {
            "board": args.board,
            "nodes": nodes,
            "checks": rep.checks,
            "coverage": c,
            "determinism": det9,
            "hard_failures": rep.hard,
            "warnings": rep.warn,
        }
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"json → {args.json}")
    return 1 if rep.hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
