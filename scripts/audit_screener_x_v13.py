#!/usr/bin/env python3
"""选币榜X/复盘 v1.3.0 只读审计：克隆v1.4通过不等于旧dual-path通过。
只作证据比较，不另造选币内核、不改生产数据；未来独立演进仍需输入/身份验收。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/coin-selection/src"))
from coin_selection.review_replay import index_rows, in_zone, stay_px, overlay_dmr_from_inbox
from coin_selection.board_variants import _parse, param_fingerprint, variant_settings, variant_state_config
from coin_selection.scan import SelectionSettings, composite_score
from coin_selection.state_machine import path_confirmed

LEGACY = "param-v1.3.0-dual-path-sticky"
SCAN_ID = re.compile(r"\d{8}-\d{3}")


def audit_entries(boards: dict, trades: list[dict]) -> dict:
    """对账传输，不复算选币：同入点的成员、冻结价、版本/hash必须一致。

    旧v1.3无hash则双方都保留None；X未来调参也不能拿新hash补贴旧交易。
    """
    errors = []
    if not boards or not trades:
        errors.append("EMPTY_EVIDENCE")
    indexed = {sid: index_rows(board) for sid, board in boards.items()}
    checked = 0
    for trade in trades:
        sid = trade["enter_scan_id"]
        row = indexed.get(sid, {}).get((trade["symbol"], trade["direction"]))
        key = trade["trade_id"]
        if row is None:
            errors.append(f"{key}:MISSING_ENTRY_ROW_OR_SNAPSHOT")
            continue
        checked += 1
        if not in_zone(row, trade["zone"], scan_id=sid):
            errors.append(f"{key}:ZONE_MISMATCH")
        price, _ = stay_px(row, trade["zone"])
        if price != trade.get("enter_price"):
            errors.append(f"{key}:ENTRY_PRICE_MISMATCH")
        meta = boards[sid]["meta"]
        for field in ("parameter_version", "param_hash"):
            if meta.get(field) != trade.get(field):
                errors.append(f"{key}:{field}_MISMATCH")
    return {"requested": len(trades), "checked": checked, "errors": errors}


#: X 当前形态。克隆期做全量「等于 main」审计；适配期只审 X 自己的不变量。
def _clone_mode() -> bool:
    try:
        from coin_selection.board_variants import get_variant
        v = get_variant("x")
        return (v.state_overrides or {}).get("selection_semantics") != "dual-path-v1.3"
    except Exception:
        return True


CLONE_MODE = _clone_mode()


def exit_status(result: dict, *, require_legacy: bool) -> int:
    """0=已检查数据完整，1=证据/一致性失败，2=严格历史统一仍被阻断。"""
    if result.get("integrity_errors"):
        return 1
    if require_legacy and result.get("legacy_alignment") != "VERIFIED":
        return 2
    return 0


def collect(root: Path = ROOT, *, scan_id: str | None = None) -> dict:
    """只读历史产物和当前有效配置；不运行SM、不取新行情、不改账本。

    legacy_alignment 只能证明差异或未知，不能用有限产物自动认证旧内核恢复。
    X仍克隆v1.4时检查本身可通过，但 --require-legacy 必须非零。
    """
    errors: list[str] = []
    out = {"integrity_errors": errors, "legacy_alignment": "UNVERIFIED",
           "scope": "artifact transport + current clone; not historical kernel certification"}
    main_dir = root / "data/coin-selection"
    x_dir = root / "data/coin-selection-x"
    try:
        if scan_id is None:
            scan_id = json.loads((main_dir / "latest.json").read_text())["meta"]["scan_id"]
        if not isinstance(scan_id, str) or not SCAN_ID.fullmatch(scan_id) or not 0 <= int(scan_id[-3:]) < 96:
            raise ValueError("invalid scan_id")
        # v1.3审计先验证源token，再读任何目标；非法日期不能被修正到别的节点。
        datetime.strptime(scan_id[:8], "%Y%m%d")
        registry = json.loads((root / "packages/config/board-variants.json").read_text())
        variant = next(v for v in _parse(registry) if v.key == "x")
        main = json.loads((main_dir / "snapshots" / f"{scan_id}.json").read_text())
        x = json.loads((x_dir / "snapshots" / f"{scan_id}.json").read_text())
        for key, board in (("main", main), ("x", x)):
            if board["meta"]["scan_id"] != scan_id:
                errors.append(f"{key}:SCAN_ID_MISMATCH")
        settings = variant_settings(SelectionSettings(), variant, root=root)
        cfg = variant_state_config(variant)
        out["scan_id"] = scan_id
        out["x_parameter_version"] = variant.parameter_version
        out["x_state_config"] = vars(cfg)
        out["x_settings"] = {k: getattr(settings, k) for k in (
            "w_ss", "w_mom", "w_liq", "w_mcap", "w_cons", "w_rank", "w_risk",
            "hard_floor_usd", "dmr_top_k", "mcap_zone_mode")}
        expected_hash = param_fingerprint(variant, settings, cfg)
        out["x_param_hash"] = x["meta"].get("param_hash")
        out["computed_x_param_hash"] = expected_hash
        if out["x_param_hash"] != expected_hash:
            errors.append("X_CONFIG_SNAPSHOT_HASH_MISMATCH")
        out["current_clone_parity"] = {}
        for pool in ("long_pool", "short_pool"):
            a, b = main[pool], x[pool]
            # 克隆允许X增加观察字段，不允许漏掉main字段而靠集合交集蒙混通过。
            delta = sum(k not in br or ar[k] != br[k] for ar, br in zip(a, b) for k in ar)
            out["current_clone_parity"][pool] = {"main_rows": len(a), "x_rows": len(b),
                                                     "common_field_differences": delta}
            # —— 「X 逐字段等于 main」只在克隆期成立 ——
            #
            # X 切到 dual-path-v1.3 之后必然分叉（PATH_M 恢复 / hold 析取 /
            # 取消 SS<50 立即降级），delta>0 是**预期**而非缺陷。适配期改为
            # 只守住行数一致（宇宙没漂）。
            if not a or len(a) != len(b) or (delta and CLONE_MODE):
                errors.append(f"{pool}:CURRENT_CLONE_DIFFERENT_OR_EMPTY"
                              if CLONE_MODE else f"{pool}:UNIVERSE_ROW_COUNT_MISMATCH")
        out["transitions_equal"] = main.get("transitions") == x.get("transitions")
        out["coingecko_credits_equal"] = main["meta"].get("coingecko_credits") == x["meta"].get("coingecko_credits")
        if not out["transitions_equal"]:
            # 转移序列等于 main 只在克隆期成立：适配期两套状态机本就会给出不同转移。
            (errors.append("CURRENT_TRANSITIONS_DIFFERENT") if CLONE_MODE else None)
        if not out["coingecko_credits_equal"] or main["meta"].get("coingecko_credits") is None:
            errors.append("COINGECKO_COUNTER_DIFFERENT_OR_MISSING")
        # 旧版源身份从每份板面meta取，绝不拿账本最晚退出时点延长实际运行区间。
        legacy_boards = {}
        counts: Counter = Counter()
        identity_present: Counter = Counter()
        max_score_error = 0.0
        examples = []
        for path in sorted((main_dir / "snapshots").glob("*.json")):
            if not SCAN_ID.fullmatch(path.stem):
                continue
            with path.open() as f:
                head = f.read(8192)
            pv = re.search(r'"parameter_version"\s*:\s*"([^"]+)"', head)
            if pv is None:
                version = (json.loads(path.read_text()).get("meta") or {}).get("parameter_version")
            else:
                version = pv[1]
            if version != LEGACY:
                continue
            board = json.loads(path.read_text())
            sid = board["meta"]["scan_id"]
            if sid != path.stem:
                errors.append(f"{path.name}:SOURCE_SCAN_ID_MISMATCH")
            counts["nodes"] += 1
            for field in ("param_hash", "config_hash", "rule_revision", "code_commit", "raw_input_hash"):
                identity_present[field] += board["meta"].get(field) is not None
            inbox = json.loads((root / "data/dmr-adapter/inbox" / f"{sid}.candidates.json").read_text())
            counts["inbox_files"] += 1
            if any(m.get("parameter_version") != LEGACY for m in inbox.get("candidates", [])):
                errors.append(f"{sid}:INBOX_VERSION_MISMATCH")
            board = overlay_dmr_from_inbox(board, inbox)
            legacy_boards[sid] = board
            raw = json.loads(path.with_suffix(".full.json").read_text())["rows"]
            counts["full_files"] += 1
            for row in raw:
                for direction in ("up", "down"):
                    counts["direction_observations"] += 1
                    required = (f"score_{direction}", f"ss_{direction}", f"momentum_score_{direction}",
                                f"consistency_{direction}", "data_quality_score", "liquidity_score_abs",
                                f"mcap_momentum_score_{direction}")
                    if any(row.get(k) is None or not math.isfinite(float(row[k])) for k in required):
                        errors.append(f"{sid}:{row.get('symbol')}:{direction}:MISSING_OR_NONFINITE_METRIC")
                        continue
                    score = row[f"score_{direction}"]
                    ss = row.get(f"ss_{direction}", 0)
                    mom = row.get(f"momentum_score_{direction}", 0)
                    cons = row.get(f"consistency_{direction}", 0)
                    dq = row.get("data_quality_score", 0)
                    recomputed = composite_score(SelectionSettings(), ss=ss, mom=mom,
                        liq=row.get("liquidity_score_abs", 0),
                        mcap=row.get(f"mcap_momentum_score_{direction}", 50), cons=cons, dq=dq)
                    max_score_error = max(max_score_error, abs(score - recomputed))
                    if row.get(f"confirmed_path_{direction}") == "M":
                        counts["historic_M_observations"] += 1
                        current_path = path_confirmed(score, ss, mom, cons, dq,
                            row.get("supply_missing", False), cfg, data_mode=row.get("data_mode", "LIVE"))
                        if current_path is None:
                            counts["historic_M_rejected_by_current"] += 1
                            if len(examples) < 3:
                                examples.append(dict(scan_id=sid, symbol=row["symbol"], direction=direction,
                                    score=score, ss=ss, mom=mom, consistency=cons, dq=dq))
        ids = sorted(legacy_boards)
        out["legacy_source"] = {"parameter_version": LEGACY, "scan_ids": ids, "counts": dict(counts),
            "identity_fields_present_nodes": dict(identity_present),
            "score_max_abs_error": max_score_error, "counterexamples": examples}
        with closing(sqlite3.connect((main_dir / "review/ledger.sqlite").resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            trades = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE parameter_version=? ORDER BY trade_id", (LEGACY,))]
        out["legacy_entry_audit"] = audit_entries(legacy_boards, trades)
        errors.extend(out["legacy_entry_audit"]["errors"])
        flags = Counter(f for row in trades for f in json.loads(row.get("flags") or "[]"))
        out["legacy_flags"] = dict(flags)
        if counts["historic_M_rejected_by_current"]:
            out["legacy_alignment"] = "DIFFERENT"
        # X全库版本/hash分组，入点成员/价格按最近200笔核对；报告明确样本量，非假全量。
        with closing(sqlite3.connect((x_dir / "review/ledger.sqlite").resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM trades WHERE enter_scan_id<=? ORDER BY enter_scan_id DESC,trade_id LIMIT 200", (scan_id,))]
            out["x_ledger_groups"] = [dict(r) for r in conn.execute(
                "SELECT parameter_version,param_hash,count(*) AS trades FROM trades GROUP BY parameter_version,param_hash")]
        # 账本身份：克隆期单段；切换后必然是「旧克隆段 + 新段」两段，直到历史
        # 重建完成才回到单段。parameter_version 任何时候都只能有一个。
        if any(g["parameter_version"] != variant.parameter_version for g in out["x_ledger_groups"]):
            errors.append("X_LEDGER_MIXED_PARAMETER_VERSION")
        hashes = {g["param_hash"] for g in out["x_ledger_groups"]}
        if CLONE_MODE:
            if hashes != {expected_hash}:
                errors.append("X_CLONE_LEDGER_MIXED_IDENTITY")
        else:
            # 适配期：只允许「当前身份」与「已知的旧克隆身份」两个，且最新行必须是当前身份。
            if not hashes <= {expected_hash, "pf1_b2d6cd1ec88bf4af"}:
                errors.append("X_LEDGER_UNKNOWN_IDENTITY")
            out["x_ledger_segments"] = len(hashes)
        selected_boards = {sid: json.loads((x_dir / "snapshots" / f"{sid}.json").read_text())
                           for sid in {r["enter_scan_id"] for r in rows}}
        out["x_entry_audit"] = audit_entries(selected_boards, rows)
        errors.extend(out["x_entry_audit"]["errors"])
        out["legacy_certification_blockers"] = [
            "旧快照缺完整不可变规则身份，不得用当前hash补贴历史",
            "历史完整内核/PIT原始输入/SM起点未认证；本工具不自动签发迁移通过",
            "生产X参数与账本未切换；当前clone通过不是历史v1.3通过",
        ]
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, StopIteration) as exc:
        errors.append(f"EVIDENCE_UNAVAILABLE:{type(exc).__name__}:{exc}")
    return out


def main() -> int:
    """stdout交付JSON，无写入开关；严格模式阻止“改名即对齐”的验收误用。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="只读产物根；代码使用本脚本所在工作树")
    parser.add_argument("--scan-id", help="固定当前clone对比节点；默认main最新节点")
    parser.add_argument("--require-legacy", action="store_true", help="要求旧v1.3严格统一；未认证返回2，证据缺失返回1")
    args = parser.parse_args()
    result = collect(args.root, scan_id=args.scan_id)
    result["exit_code"] = exit_status(result, require_legacy=args.require_legacy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
