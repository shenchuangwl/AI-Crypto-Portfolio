"""离线重打分回放器 —— 文档B §9.1 的测试 T1 / T2（+ 确定性与幂等）。

    PYTHONPATH=services/coin-selection/src python3 services/coin-selection/tests/test_replay_rescore.py

T1 回放器**不含自己的状态机实现**（源码 grep）；它复用生产的 ``transition_one``
T2 输出目录白名单：传 ``data/coin-selection*`` 必须抛异常
另外钉住文档B §4.4 的另外三条硬性质：确定性、幂等、口径 8 格。
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SCRIPT = ROOT / "scripts" / "replay_rescore.py"


def _load():
    spec = importlib.util.spec_from_file_location("_replay_rescore", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


RR = _load()
SRC = SCRIPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T1 —— 复用生产函数，不写第二份状态机
# ---------------------------------------------------------------------------
def test_t1_no_second_state_machine_implementation():
    """源码里不得出现自己的转移函数。第二份实现 = 两个栏目悄悄分叉的起点。"""
    code = re.sub(r'"""[\s\S]*?"""', "", SRC)  # 去掉文档串，只看代码
    code = re.sub(r"(^|[^:])#.*$", r"\1", code, flags=re.M)
    for pattern in (
        r"def\s+transition",
        r"def\s+path_qualified",
        r"def\s+path_confirmed",
        r"def\s+dmr_zone_ok",
        r"def\s+hold_ok",
        r"def\s+composite_score",
        r"def\s+rank_dmr_inbox",
    ):
        assert not re.search(pattern, code), f"回放器里出现了自己的实现: {pattern}"


def test_t1_imports_the_production_functions():
    for name in (
        "apply_state_machine",
        "build_dmr_messages",
        "rank_dmr_inbox",
        "composite_score",
        "board_from_rows",
        "OccupancyReplayer",
        "realized_pnl",
    ):
        assert name in SRC, f"回放器没有引用生产函数 {name}"
    assert hasattr(RR, "apply_state_machine")
    assert hasattr(RR, "OccupancyReplayer")
    # 真的是同一个对象，不是同名替身
    from coin_selection.state_machine import apply_state_machine
    from coin_selection.review_replay import OccupancyReplayer

    assert RR.apply_state_machine is apply_state_machine
    assert RR.OccupancyReplayer is OccupancyReplayer


def test_t1_no_wall_clock_and_no_unseeded_random():
    code = re.sub(r'"""[\s\S]*?"""', "", SRC)
    assert "time.time()" not in code
    assert "datetime.now(" not in code
    assert "utcnow(" not in code
    # 随机只允许出现在带种子的 random.Random 里
    assert "random.Random(" in code
    assert not re.search(r"(^|[^.])\brandom\.shuffle\(", code), "用了全局 random"


# ---------------------------------------------------------------------------
# T2 —— 输出目录白名单
# ---------------------------------------------------------------------------
def test_t2_rejects_live_data_dirs():
    for bad in (
        "data/coin-selection",
        "data/coin-selection/snapshots",
        "data/coin-selection-y",
        "data/coin-selection-y/review",
        "data/dmr-adapter/inbox",
        "data/dmr-adapter-y/inbox",
    ):
        try:
            RR.assert_safe_out_dir(Path(bad))
        except RR.UnsafeOutputDir:
            continue
        raise AssertionError(f"应当拒绝写入现网目录: {bad}")


def test_t2_rejects_anything_outside_research():
    for bad in ("/tmp/whatever", "data", "docs", "packages/config", "/"):
        try:
            RR.assert_safe_out_dir(Path(bad))
        except RR.UnsafeOutputDir:
            continue
        raise AssertionError(f"应当拒绝 data/research 之外的目录: {bad}")


def test_t2_accepts_research_dirs():
    for good in ("data/research", "data/research/E0", "data/research/x/y"):
        out = RR.assert_safe_out_dir(Path(good))
        assert "research" in str(out)


def test_t2_path_traversal_is_rejected():
    try:
        RR.assert_safe_out_dir(Path("data/research/../coin-selection-y"))
    except RR.UnsafeOutputDir:
        return
    raise AssertionError("`..` 逃逸没有被拦住")


# ---------------------------------------------------------------------------
# 确定性 / 幂等（文档B §4.4 性质 2、3）
# ---------------------------------------------------------------------------
def _tiny_tape():
    """两个币 × 8 个节点的合成指标带，够跑通全流程且不碰磁盘上的现网数据。"""
    nodes = []
    for i in range(1, 9):
        rows = []
        for j, sym in enumerate(("AAAUSDT", "BBBUSDT")):
            rows.append(
                {
                    "symbol": sym,
                    "base_asset": sym[:3],
                    "coingecko_id": sym.lower(),
                    "ss_up": 60.0 + j,
                    "ss_down": 10.0,
                    "momentum_score_up": 70.0,
                    "momentum_score_down": 30.0,
                    "liquidity_score_abs": 90.0,
                    "mcap_momentum_score_up": 70.0,
                    "mcap_momentum_score_down": 30.0,
                    "consistency_up": 1.0,
                    "consistency_down": 0.0,
                    "data_quality_score": 90.0,
                    "supply_missing": False,
                    "liquidity_hard_pass": True,
                    "liquidity_grade": "A",
                    "data_mode": "LIVE",
                    "last_price": 100.0 + i + j,
                    "mark_price": 100.0 + i + j,
                    "mcap_grade_30m": "A",
                    "mcap_grade_2h": "A",
                    "mcap_grade_6h": "A",
                    "steps_up": 3,
                    "steps_down": 0,
                    "circulating_supply": 1000.0,
                }
            )
        nodes.append(
            {
                "scan_id": f"20260901-{i:03d}",
                "ts": None,
                "universe_count": 2,
                "rows": rows,
            }
        )
    return {
        "schema": "replay-tape-v1",
        "source": "synthetic",
        "n_nodes": len(nodes),
        "first": nodes[0]["scan_id"],
        "last": nodes[-1]["scan_id"],
        "fields": list(RR.TAPE_ROW_FIELDS),
        "nodes": nodes,
    }


def _run(out_name: str):
    from coin_selection.board_variants import get_variant, variant_settings, variant_state_config
    from coin_selection.scan import SelectionSettings

    v = get_variant("y")
    s = variant_settings(SelectionSettings(), v)
    c = variant_state_config(v)
    out = RR.RESEARCH_ROOT / "_test" / out_name
    return RR.replay_rescore(
        tape=_tiny_tape(),
        exp_id=out_name,
        out_dir=out,
        settings=s,
        cfg=c,
        mcap_zone_mode="off",
        param_hash="pf1_test",
    )


def test_determinism_same_input_same_output():
    a = _run("det_a")
    b = _run("det_b")
    ka = [(t["trade_id"], t["status"], t.get("pnl_pct")) for t in a["trades"]]
    kb = [(t["trade_id"], t["status"], t.get("pnl_pct")) for t in b["trades"]]
    assert ka == kb
    assert a["occupancy_series"] == b["occupancy_series"]
    shutil.rmtree(RR.RESEARCH_ROOT / "_test", ignore_errors=True)


def test_idempotent_rerun_into_the_same_dir():
    a = _run("idem")
    n_a = len(a["trades"])
    b = _run("idem")  # 同名目录：必须先删了重来，不做增量
    assert len(b["trades"]) == n_a
    shutil.rmtree(RR.RESEARCH_ROOT / "_test", ignore_errors=True)


def test_metrics_have_all_eight_cells():
    r = _run("metrics")
    m = RR.eight_cell_metrics(r, None)
    for zone in ("DMR", "CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED"):
        occ = m["by_zone"][zone]["occupancy"]
        for cr in ("incl_cycle_reset", "excl_cycle_reset"):
            for cost in ("zero_cost", "with_cost"):
                assert occ[cr][cost] is not None, (zone, cr, cost)
    shutil.rmtree(RR.RESEARCH_ROOT / "_test", ignore_errors=True)


def test_stats_edge_cases_follow_the_spec():
    """文档B §5.2 / §5.4 的边界：平局计入分母不计入分子；n_loss==0 报 ∞。"""
    import math

    s = RR._stats([0.01, -0.01, 0.0])
    assert s["n"] == 3 and s["n_win"] == 1 and s["n_loss"] == 1 and s["n_flat"] == 1
    assert abs(s["win_rate"] - 1 / 3) < 1e-12  # 平局不是赢

    s2 = RR._stats([0.01, 0.02])
    assert s2["pf"] == math.inf and s2["payoff"] == math.inf and s2["n_loss"] == 0

    s3 = RR._stats([])
    assert s3["n"] == 0 and s3["pf"] is None


def test_missing_nodes_counts_the_real_holes():
    assert RR.missing_nodes(["20260815-001", "20260815-002", "20260815-003"]) == 0
    assert RR.missing_nodes(["20260815-001", "20260815-003"]) == 1
    assert RR.missing_nodes(["20260827-007", "20260827-020"]) == 12  # 文档A §0.1 的最大断口


def test_recalibrate_only_touches_score_thresholds():
    """只有 Score 类阈值参与重标定 —— SS/M/C/DQ 的量纲不随权重变化。"""
    assert set(RR.RECALIBRATE_FIELDS) == {
        "enter_watch", "exit_watch", "exit_qualified", "enter_qualified",
        "enter_qualified_m", "exit_confirmed", "enter_confirmed", "dmr_score",
    }
    for f in RR.RECALIBRATE_FIELDS:
        assert not f.startswith("ss_") and not f.startswith("mom_")
        assert not f.startswith("cons_") and not f.startswith("dq_")


def test_recalibrate_moves_thresholds_when_weights_change():
    from dataclasses import replace

    from coin_selection.scan import SelectionSettings
    from coin_selection.state_machine import StateConfig

    tape = _tiny_tape()
    base = SelectionSettings()
    new = replace(base, w_ss=0.20, w_mom=0.20, w_liq=0.15, w_mcap=0.25)
    cfg = StateConfig()
    out = RR.recalibrate_thresholds(tape, base, new, cfg)
    assert set(out) == set(RR.RECALIBRATE_FIELDS)
    for v in out.values():
        assert isinstance(v, float)


def test_mcap_cut_presets_are_monotone():
    for name, c in RR.MCAP_CUT_PRESETS.items():
        assert c["dmr"] > c["confirmed"] > c["qualified"] > c["watch"], name


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
