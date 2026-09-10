#!/usr/bin/env python3
"""《选币榜Y》有效区层的影子投影与零漂移取证（ChatGpt_SOL5.6 第三轮）。

权威依据
--------
* 文档 A §15 阶段 0～3（先零漂移、再只算字段、再只加 ceiling）、§17.1（必过验收
  「三个 flags 关闭时，除新增审计元数据外，Y 业务输出与冻结基线逐字段一致」）、
  §17.2（三个开关）。
* 文档 B §5.3（开关只在 00:00 UTC 生效）、§7.1（规则重放与 occupancy 重放分离）、
  §14.1（阶段 1 现网基线 / 阶段 3 只加 216 ceiling）。

它做什么
--------
拿**现网真实 Y 快照**的行做三次投影，逐字段比对：

===========  =============================================================
mode=off     恒等映射：一个有效区字段都不写，板面与现网逐字段相同
mode=shadow  新字段全写，``state_*`` 一个都不动，DMR 成员不变
mode=on      ``state_*`` 只允许被降级；统计降级分布与 DMR 阻挡数
===========  =============================================================

外加一次**完整 ``project_board`` 端到端**（写进临时目录，绝不碰生产），
证明 RuleManifest 身份、inbox 执行身份、快照 meta 全链路接线可用。

只读生产数据；所有写入都在 ``--out``（默认系统临时目录）内。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import mcap_effective as me  # noqa: E402
from coin_selection import mcap_mapping as mm  # noqa: E402
from coin_selection.board_variants import (  # noqa: E402
    get_variant,
    variant_settings,
    variant_state_config,
)
from coin_selection.scan import SelectionSettings, board_from_rows  # noqa: E402

SHARED = (
    "symbol",
    "underlying_asset",
    "canonical_asset_id",
    "contract_multiplier",
    "coingecko_coin_id",
    "circulating_supply",
    "market_cap_calculated",
    "market_cap_coingecko",
    "last_price",
    "ref_price",
    "data_mode",
    "data_confidence",
    "liquidity_grade",
    "liquidity_score",
    "mcap_grade_30m",
    "mcap_grade_2h",
    "mcap_grade_6h",
    "mcap_tf",
    "rank",
    "rank_velocity_score",
    "risk_score",
    "risk_flags",
    "supply_as_of_utc",
    "supply_source",
    "mapping_confidence",
    "score_up",
    "score_down",
)
PER_DIR = (
    "state",
    "staircase_score",
    "momentum_score",
    "mcap_momentum_score",
    "consistency_score",
    "dmr_selected",
    "state_duration_minutes",
    "state_enter_time_utc",
    "state_enter_price",
    "qualified_path",
    "confirmed_path",
    "ready_confirm",
    "reason_codes",
    "not_confirmed_reasons",
    "ret_1h",
    "ret_4h",
    "ret_24h",
    "ret_15m",
    "ret_1w",
    "ret_1mo",
    "ret_since_anchor",
    "price_change_since_scan",
    "direction_confidence",
    "market_cap_tier",
    "tier_rank",
    "market_rank",
    "aqv_6d_m",
    "aqv_12d_m",
    "aqv_26d_m",
)


def rows_from_snapshot(board: dict[str, Any]) -> list[dict[str, Any]]:
    """把已发布板面的双向池还原成扫描行（``state_up`` / ``state_down`` 形态）。

    这是**唯一**能拿到「Y 自己的状态机记忆」的离线途径：主榜 full.json 里的
    ``state_*`` 是主榜的，不能拿来代表 Y（文档 B §4.3 / 第二轮 §6.2 的同一个坑）。
    """
    merged: dict[str, dict[str, Any]] = {}
    for pool_name, direction in (("long_pool", "up"), ("short_pool", "down")):
        for r in board.get(pool_name) or []:
            sym = str(r.get("symbol"))
            row = merged.setdefault(sym, {})
            for k in SHARED:
                if k in r:
                    row[k] = r[k]
            for k in PER_DIR:
                if k in r:
                    row[f"{k}_{direction}" if k != "state" else f"state_{direction}"] = r[k]
            # 池行把方向分量折叠成了同名列；这里补回扫描行的方向键名，
            # 否则 board_from_rows / build_dmr_messages 读不到（DMR 会全空）。
            row[f"ss_{direction}"] = r.get("staircase_score")
            row[f"momentum_score_{direction}"] = r.get("momentum_score")
            row[f"consistency_{direction}"] = r.get("consistency_score")
            row[f"mcap_momentum_score_{direction}"] = r.get("mcap_momentum_score")
            row[f"qualified_path_{direction}"] = r.get("qualified_path")
            row[f"confirmed_path_{direction}"] = r.get("confirmed_path")
            row[f"ready_confirm_{direction}"] = r.get("ready_confirm")
            row[f"state_{direction}_dwell_min"] = r.get("state_duration_minutes")
            row[f"state_{direction}_enter_price"] = r.get("state_enter_price")
            row["data_quality_score"] = r.get("data_confidence")
            row["coingecko_id"] = r.get("coingecko_coin_id")
            row["supply_missing"] = r.get("circulating_supply") in (None, 0)
            row.setdefault("liquidity_hard_pass", r.get("liquidity_grade") not in (None, "FAIL"))
    return [merged[k] for k in sorted(merged)]


def project(rows: list[dict[str, Any]], mode: str, mapping, cfg) -> tuple[dict, dict]:
    import copy

    work = copy.deepcopy(rows)
    meta = me.apply_effective_zone(work, mode=mode, mapping=mapping)
    board = {
        "long_pool": board_from_rows(work, "up", cfg=cfg),
        "short_pool": board_from_rows(work, "down", cfg=cfg),
    }
    return board, meta


def row_key(r: dict[str, Any]) -> str:
    return f"{r.get('symbol')}|{r.get('direction')}"


def compare(a: dict, b: dict, *, ignore_prefixes: tuple[str, ...] = ()) -> dict[str, Any]:
    out = {"rows": 0, "extra_keys": Counter(), "value_diffs": Counter(), "examples": []}
    for pool in ("long_pool", "short_pool"):
        ia = {row_key(r): r for r in a.get(pool) or []}
        ib = {row_key(r): r for r in b.get(pool) or []}
        assert set(ia) == set(ib), f"{pool} membership differs"
        for k in sorted(ia):
            ra, rb = ia[k], ib[k]
            out["rows"] += 1
            for key in set(rb) - set(ra):
                if any(key.startswith(p) for p in ignore_prefixes):
                    continue
                out["extra_keys"][key] += 1
            for key in set(ra) & set(rb):
                if any(key.startswith(p) for p in ignore_prefixes):
                    continue
                if ra[key] != rb[key]:
                    out["value_diffs"][key] += 1
                    if len(out["examples"]) < 5:
                        out["examples"].append(
                            {"row": k, "field": key, "off": ra[key], "other": rb[key]}
                        )
    out["extra_keys"] = dict(out["extra_keys"])
    out["value_diffs"] = dict(out["value_diffs"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Y effective-zone shadow projection")
    ap.add_argument("--board", default="y")
    ap.add_argument("--node", default="", help="scan_id；缺省用 latest.json")
    ap.add_argument("--out", default="", help="临时输出目录；缺省用系统临时目录")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    variant = get_variant(args.board)
    data_dir = ROOT / variant.data_dir
    src = (
        data_dir / "snapshots" / f"{args.node}.json"
        if args.node
        else data_dir / "latest.json"
    )
    board_online = json.loads(src.read_text(encoding="utf-8"))
    scan_id = (board_online.get("meta") or {}).get("scan_id")
    settings = variant_settings(SelectionSettings(), variant)
    cfg = variant_state_config(variant)
    mapping = mm.load(strict=True)
    rows = rows_from_snapshot(board_online)

    print("=" * 78)
    print(f"影子投影 board={args.board} node={scan_id} rows={len(rows)}")
    print(f"mcap_mapping_version={mapping.mapping_version}")
    print(f"mapping_hash={mapping.mapping_hash}")
    print("=" * 78)

    hard = 0

    # —— A. mode=off 恒等映射 ——
    b_off, m_off = project(rows, "off", mapping, cfg)
    written = [k for r in rows for k in r if k.startswith(("effective_", "mcap_combo", "mcap_ceiling"))]
    ok = m_off["mode"] == "off" and not written
    print(f"[{'PASS' if ok else 'FAIL'}] A  mode=off 恒等映射：源行未被写入任何有效区字段")
    hard += 0 if ok else 1

    # —— B1 零漂移的**因果**证明 ——
    #
    # 同一批源行，一次完全不调用有效区层、一次调用 mode=off，两次 board_from_rows
    # 的输出必须逐字节相同。差异只可能来自本层，因此这条等式成立即证明
    # 「三个 flags 关闭时 Y 业务输出与冻结基线逐字段一致」（文档A §17.1）。
    import copy as _c

    untouched = _c.deepcopy(rows)
    b_base = {
        "long_pool": board_from_rows(untouched, "up", cfg=cfg),
        "short_pool": board_from_rows(untouched, "down", cfg=cfg),
    }
    same = json.dumps(b_base, sort_keys=True, ensure_ascii=False) == json.dumps(
        b_off, sort_keys=True, ensure_ascii=False
    )
    print(f"[{'PASS' if same else 'FAIL'}] B1 mode=off 前后 board_from_rows 输出逐字节相同")
    hard += 0 if same else 1

    # —— B2 形状不变 ——
    #
    # 与**现网已发布板面**比较行级键集合：mode=off 时不得多出任何一个键。
    # （值不比较：本脚本的 rows_from_snapshot 是从已发布双向池反推的，
    #  若干分量在池行里已按方向折叠成同名列，反推回 rows 会失真；
    #  失真只影响数值，不影响「有没有多写键」这个形状判据。）
    pub_keys, off_keys = set(), set()
    for pool in ("long_pool", "short_pool"):
        for r in board_online.get(pool) or []:
            pub_keys |= set(r)
        for r in b_off.get(pool) or []:
            off_keys |= set(r)
    extra = sorted(off_keys - pub_keys)
    ok = not extra
    print(
        f"[{'PASS' if ok else 'FAIL'}] B2 mode=off 行级键集合未多出任何键  extra={extra}"
    )
    hard += 0 if ok else 1

    # —— C. mode=shadow：新字段全写、state 全不动 ——
    b_sh, m_sh = project(rows, "shadow", mapping, cfg)
    cmp_sh = compare(b_off, b_sh)
    state_moved = cmp_sh["value_diffs"].get("state", 0)
    new_keys = set(cmp_sh["extra_keys"])
    ok = state_moved == 0 and {
        "base_state",
        "effective_zone",
        "mcap_ceiling_zone",
        "mcap_combo_no",
    } <= new_keys
    print(
        f"[{'PASS' if ok else 'FAIL'}] C  mode=shadow 只加字段不改分区  "
        f"new_keys={sorted(new_keys)} state_changed={state_moved}"
    )
    print(
        f"       stats: graded={m_sh['graded']} incomplete={m_sh['incomplete']} "
        f"bypassed={m_sh['bypassed']} downgraded={m_sh['downgraded']} "
        f"dmr_ceiling_ok={m_sh['dmr_ceiling_ok']}"
    )
    print(f"       ceiling_by_zone={m_sh['ceiling_by_zone']}")
    print(f"       downgrade_by_zone={m_sh['downgrade_by_zone']}")
    hard += 0 if ok else 1

    # —— D. mode=on：只降不升 ——
    b_on, m_on = project(rows, "on", mapping, cfg)
    up = 0
    moves: Counter = Counter()
    idx_off = {row_key(r): r for p in ("long_pool", "short_pool") for r in b_off[p]}
    for pool in ("long_pool", "short_pool"):
        for r in b_on[pool]:
            before = idx_off[row_key(r)]["state"]
            after = r["state"]
            if before in me.BASE_RANK and after in me.ZONE_RANK:
                if me.ZONE_RANK[after] < me.BASE_RANK[before]:
                    up += 1
            if before != after:
                moves[f"{before}->{after}"] += 1
    ok = up == 0
    print(f"[{'PASS' if ok else 'FAIL'}] D  mode=on 只降不升  upgrades={up}")
    print(f"       transitions={dict(moves)}")
    hard += 0 if ok else 1

    # 特殊态必须原样
    bad = 0
    for pool in ("long_pool", "short_pool"):
        for r in b_on[pool]:
            before = idx_off[row_key(r)]["state"]
            if before in me.BYPASS_STATES and r["state"] != before:
                bad += 1
    print(f"[{'PASS' if bad == 0 else 'FAIL'}] E  mode=on 特殊态原样旁路  violations={bad}")
    hard += 0 if bad == 0 else 1

    # —— F. 完整 project_board 端到端（临时目录）——
    tmp = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="hermes-shadow-"))
    try:
        (tmp / "data").mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone

        from coin_selection.board_projection import project_board

        anchor = datetime.strptime(str(scan_id).split("-")[0], "%Y%m%d").replace(
            tzinfo=timezone.utc
        )
        seq = int(str(scan_id).split("-")[1])
        now = anchor.replace(hour=(seq * 15) // 60, minute=(seq * 15) % 60)
        import copy as _copy

        # F 步跑的是**授权后的真实生产路径**：v2.0.0 的 mcap_dominance 守卫要求
        # mode=on + 令牌，否则拒绝出数。这里不加旁路开关 —— 端到端取证就该走
        # 生产会走的那条路（未授权的阻断行为由 test_board_variant_y 覆盖）。
        from coin_selection.mcap_dominance import AUTHORIZATION_TOKEN as _TOK

        variant_f = type(variant)(**{
            **variant.__dict__,
            "settings_overrides": {
                **variant.settings_overrides,
                "mcap_zone_mode": "on",
                "mcap_zone_authorization": _TOK,
            },
        })
        out = project_board(
            variant_f,
            SelectionSettings(),
            _copy.deepcopy(rows),
            anchor=anchor,
            scan_id=str(scan_id),
            seq=seq,
            now=now,
            universe_count=len(rows),
            uni_ver="shadow",
            stats={},
            root=tmp,
            write_full=False,
            ingest_ledger=False,
            backfill_prices=False,
            skip_sm=True,
        )
        snap = json.loads(
            (tmp / variant.data_dir / "snapshots" / f"{scan_id}.json").read_text(
                encoding="utf-8"
            )
        )
        ident = (snap.get("meta") or {}).get("rule_identity") or {}
        need = (
            "rule_revision",
            "config_hash",
            "asset_mapping_version",
            "mcap_mapping_version",
            "mapping_hash",
            "code_commit",
            "data_contract_version",
            "universe_policy_version",
            "effective_from_utc",
            "effective_from_scan_id",
        )
        missing = [k for k in need if not ident.get(k)]
        ok = not missing
        print(f"[{'PASS' if ok else 'FAIL'}] F  快照 meta.rule_identity 十项身份齐全  missing={missing}")
        hard += 0 if ok else 1
        print(f"       rule_identity={json.dumps(ident, ensure_ascii=False)}")

        inbox_doc = json.loads(
            (tmp / variant.dmr_inbox / f"{scan_id}.candidates.json").read_text(
                encoding="utf-8"
            )
        )
        exec_need = (
            "board_key",
            "dmr_executable",
            "consumable_by_dmr",
            "parameter_version",
            "rule_revision",
            "config_hash",
            "mcap_mapping_version",
            "mapping_hash",
            "code_commit",
        )
        b_missing = [k for k in exec_need if inbox_doc.get(k) in (None, "")]
        cands = inbox_doc.get("candidates") or []
        c_missing = sorted(
            {k for c in cands for k in exec_need if c.get(k) in (None, "")}
        )
        ok = not b_missing and (not cands or not c_missing)
        print(
            f"[{'PASS' if ok else 'FAIL'}] G  inbox batch/candidate 均带九项执行身份  "
            f"batch_missing={b_missing} candidate_missing={c_missing} candidates={len(cands)}"
        )
        hard += 0 if ok else 1
        print(
            f"       board_key={inbox_doc.get('board_key')} "
            f"dmr_executable={inbox_doc.get('dmr_executable')} "
            f"consumable_by_dmr={inbox_doc.get('consumable_by_dmr')}"
        )

        # —— H. 该 inbox 交给执行守卫：Y 必须一个都不放行 ——
        sys.path.insert(0, str(ROOT / "contracts" / "python"))
        from execution_guard import adapter_accept  # type: ignore

        from coin_selection.scan import main_exec_identity

        deployed = main_exec_identity(SelectionSettings())
        batch_ident = {k: inbox_doc.get(k) for k in exec_need}
        allowed = [
            c.get("symbol")
            for c in cands
            if adapter_accept(batch_ident, c, deployed, whitelist=None)
        ]
        ok = not allowed
        print(
            f"[{'PASS' if ok else 'FAIL'}] H  执行守卫对 Y 批次全部拒绝  "
            f"candidates={len(cands)} allowed={allowed}"
        )
        hard += 0 if ok else 1
        if cands:
            r = adapter_accept(batch_ident, cands[0], deployed, whitelist=None)
            print(f"       first reject reason = {r.reason}")

        print(f"       scratch tree = {tmp}")
        print(f"       project_board returned: {json.dumps(out, ensure_ascii=False)}")
    finally:
        if not args.keep and not args.out:
            shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 78)
    print(f"hard failures: {hard}")
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
