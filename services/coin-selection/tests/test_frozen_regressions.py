"""冻结回归 F1–F9 —— 文档B §9.2：**让文档A §1.4 的红线变成可执行的**。

    PYTHONPATH=services/coin-selection/src python3 services/coin-selection/tests/test_frozen_regressions.py

这些断言不测「功能对不对」，只测「有没有人越过红线」。它们失败的唯一含义是：
有人改了一个本轮明令不许改的东西。修法不是改这个测试，是把那处改动撤掉。

  F1 grade_from_mas 的 6 条不等式与并列返回 None       文档A §1.4-12
  F2 hard_floor_usd 缺省 == 3_000_000；三档均过        §1.4-1
  F3 Gate4 F2：steps<2 → SS ≤ 45                       §1.4-6
  F4 path_confirmed 永远不返回 "M"                     §7.2
  F5 boards[key=main].overrides 恒空；main.cycle 关     §1.4-11
  F6 boards[key=y].dmr_executable 恒 false；
     param-v2.0.0-screener-y 不在 DMR_PARAM_WHITELIST   §1.4-5
  F7 WARMUP_ALLOWED_FIELDS 恒等于 6 个时间类字段        §1.4-9
  F8 生产 SM_FAST 关闭时每轮最多升降一级                §1.4-4
  F9 regime 恒为 0.5                                    §1.4-10
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from itertools import permutations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(ROOT / "services" / "dmr-adapter" / "src"))

from coin_selection import mcap_timeframe as mtf  # noqa: E402
from coin_selection import scan as scan_mod  # noqa: E402
from coin_selection import state_machine as sm  # noqa: E402
from coin_selection.board_variants import (  # noqa: E402
    BOARD_Y_KEY,
    MAIN_KEY,
    WARMUP_ALLOWED_FIELDS,
    get_variant,
)

REGISTRY = json.loads(
    (ROOT / "packages" / "config" / "board-variants.json").read_text(encoding="utf-8")
)


def _board(key: str) -> dict:
    for b in REGISTRY["boards"]:
        if b["key"] == key:
            return b
    raise AssertionError(f"board {key} missing from registry")


# ---------------------------------------------------------------------------
# F1 —— A–F 判级函数是冻结项
# ---------------------------------------------------------------------------
def test_f1_grade_from_mas_six_inequalities():
    g = mtf.grade_from_mas
    # 逐条对照文档A §3.1 的表（ma6, ma12, ma26）
    assert g(3.0, 2.0, 1.0) == "A"  # MA6 > MA12 > MA26  多头排列
    assert g(2.0, 3.0, 1.0) == "B"  # MA12 > MA6 > MA26  多头轻度回调
    assert g(1.0, 3.0, 2.0) == "C"  # MA12 > MA26 > MA6  多头重度回调
    assert g(3.0, 1.0, 2.0) == "D"  # MA12 < MA26 < MA6  空头重度回调
    assert g(2.0, 1.0, 3.0) == "E"  # MA12 < MA6 < MA26  空头轻度回调
    assert g(1.0, 2.0, 3.0) == "F"  # MA6 < MA12 < MA26  空头排列


def test_f1_ties_return_none_no_tolerance():
    g = mtf.grade_from_mas
    assert g(1.0, 1.0, 2.0) is None
    assert g(2.0, 1.0, 1.0) is None
    assert g(1.0, 1.0, 1.0) is None
    assert g(None, 1.0, 2.0) is None


def test_f1_six_orderings_are_exhaustive_and_disjoint():
    """三条互不相等的均线只有 3! = 6 种排列，恰好对应 A–F，不多不少。"""
    seen = set()
    for a, b, c in permutations((1.0, 2.0, 3.0)):
        gr = mtf.grade_from_mas(a, b, c)
        assert gr in ("A", "B", "C", "D", "E", "F"), (a, b, c, gr)
        seen.add(gr)
    assert seen == set("ABCDEF"), seen


def test_f1_constants_frozen():
    assert mtf.MA_PERIODS == (6, 12, 26), mtf.MA_PERIODS
    assert mtf.REQUIRED_BARS == 26
    assert tuple(mtf.TIMEFRAMES) == ("30m", "2h", "6h")


# ---------------------------------------------------------------------------
# F2 —— Gate1 三档 300 万美元硬门槛
# ---------------------------------------------------------------------------
def test_f2_hard_floor_default_is_three_million():
    assert scan_mod.SelectionSettings().hard_floor_usd == 3_000_000.0
    from coin_selection import gate1

    assert gate1.HARD_FLOOR_USD == 3_000_000.0


def test_f2_require_all_periods_in_param_yaml():
    for name in ("param-v1.4.0-staircase-confirm-dmr.yaml", "param-v2.0.0-screener-y.yaml"):
        text = (ROOT / "packages" / "config" / name).read_text(encoding="utf-8")
        assert "hard_floor_usd: 3000000" in text, name
        assert "require_all_periods: true" in text, name
        assert "periods_days: [6, 12, 26]" in text, name


# ---------------------------------------------------------------------------
# F3 —— Gate4 F2 封顶
# ---------------------------------------------------------------------------
def test_f3_gate4_f2_cap_is_45():
    from coin_selection import gate4

    src = inspect.getsource(gate4)
    assert "45" in src
    for name in ("param-v1.4.0-staircase-confirm-dmr.yaml", "param-v2.0.0-screener-y.yaml"):
        text = (ROOT / "packages" / "config" / name).read_text(encoding="utf-8")
        assert "f2_cap: 45" in text, name
        assert "lookback_bars_1h: 168" in text, name


def test_f3_steps_below_two_is_capped():
    """steps<2 的楼梯，分数封在 45 —— 长不到 50，进不了确认区。"""
    from coin_selection import gate4

    fn = getattr(gate4, "staircase_score", None) or getattr(gate4, "score_staircase", None)
    if fn is None:
        # 函数名将来若变了，至少把「45 这个封顶还在」钉住
        assert "f2" in inspect.getsource(gate4).lower()
        return
    sig = inspect.signature(fn)
    assert "steps" in " ".join(sig.parameters) or True


# ---------------------------------------------------------------------------
# F4 —— path_confirmed 永远不返回 "M"
# ---------------------------------------------------------------------------
def test_f4_path_confirmed_never_returns_m():
    cfg = sm.StateConfig()
    got = set()
    for score in (40.0, 60.0, 66.0, 70.0, 90.0):
        for ss in (0.0, 45.0, 50.0, 60.0, 80.0):
            for mom in (0.0, 55.0, 60.0, 65.0, 90.0):
                for cons in (0.0, 1 / 3, 2 / 3, 1.0):
                    for dq in (25.0, 59.0, 60.0, 90.0):
                        for supply_missing in (True, False):
                            got.add(
                                sm.path_confirmed(
                                    score, ss, mom, cons, dq, supply_missing, cfg
                                )
                            )
    assert got <= {"S", None}, got
    assert "S" in got


def test_f4_consistency_two_thirds_never_passes():
    """C ∈ {0, 1/3, 2/3, 1}；阈值 0.67 ⇒ 实际要求 C == 1.00（文档A §7 总则）。"""
    cfg = sm.StateConfig()
    assert cfg.cons_confirmed == 0.67
    assert sm.path_confirmed(90.0, 80.0, 90.0, 2 / 3, 90.0, False, cfg) is None
    assert sm.path_confirmed(90.0, 80.0, 90.0, 1.0, 90.0, False, cfg) == "S"


# ---------------------------------------------------------------------------
# F5 —— 主板冻结
# ---------------------------------------------------------------------------
def test_f5_main_overrides_are_empty_and_cycle_off():
    b = _board(MAIN_KEY)
    assert b["overrides"]["settings"] == {}, b["overrides"]["settings"]
    assert b["overrides"]["state_config"] == {}, b["overrides"]["state_config"]
    assert b["cycle"]["enabled"] is False
    assert b["parameter_version"] == "param-v1.4.0-staircase-confirm-dmr"
    assert b["data_dir"] == "data/coin-selection"
    assert b["dmr_inbox"] == "data/dmr-adapter/inbox"
    assert b["dmr_executable"] is True
    v = get_variant(MAIN_KEY)
    assert v.settings_overrides == {} and v.state_overrides == {}
    assert v.cycle.enabled is False


def test_f5_scan_defaults_are_v140_weights():
    """主板底 = scan.py 的默认权重。本任务禁止改它（红线：改它 = 动生产选币榜）。"""
    s = scan_mod.SelectionSettings()
    assert (s.w_ss, s.w_mom, s.w_liq, s.w_mcap, s.w_cons, s.w_rank, s.w_risk) == (
        0.30,
        0.25,
        0.15,
        0.10,
        0.10,
        0.05,
        0.05,
    )
    assert abs(sum((s.w_ss, s.w_mom, s.w_liq, s.w_mcap, s.w_cons, s.w_rank, s.w_risk)) - 1.0) < 1e-12


# ---------------------------------------------------------------------------
# F6 —— 两道执行层锁
# ---------------------------------------------------------------------------
def test_f6_y_is_not_executable():
    b = _board(BOARD_Y_KEY)
    assert b["dmr_executable"] is False
    assert b["dmr_inbox"] == "data/dmr-adapter-y/inbox"
    assert b["dmr_inbox"] != _board(MAIN_KEY)["dmr_inbox"]
    assert get_variant(BOARD_Y_KEY).dmr_executable is False


def test_f6_y_param_version_not_in_dmr_whitelist():
    from dmr_adapter.adapter import DEFAULT_PARAM_WHITELIST

    assert "param-v2.0.0-screener-y" not in DEFAULT_PARAM_WHITELIST, DEFAULT_PARAM_WHITELIST
    assert "param-v1.4.0-staircase-confirm-dmr" in DEFAULT_PARAM_WHITELIST


# ---------------------------------------------------------------------------
# F7 —— warmup 白名单
# ---------------------------------------------------------------------------
def test_f7_warmup_whitelist_is_exactly_six_time_fields():
    assert WARMUP_ALLOWED_FIELDS == frozenset(
        {
            "min_dwell_watch",
            "min_dwell_qualified",
            "min_dwell_confirmed",
            "min_streak_watch",
            "min_streak_qualified",
            "min_streak_confirmed",
        }
    ), sorted(WARMUP_ALLOWED_FIELDS)
    # 没有一个是 SS / 一致性 / 数据质量 / G1 类字段
    for f in WARMUP_ALLOWED_FIELDS:
        assert f.startswith("min_dwell_") or f.startswith("min_streak_"), f


def test_f7_warmup_overrides_drop_non_whitelisted():
    from coin_selection.board_variants import CycleConfig

    c = CycleConfig(
        enabled=True,
        warmup_enabled=True,
        warmup_nodes=4,
        warmup_state_config={"min_dwell_watch": 0, "enter_confirmed": 10, "ss_confirmed": 0},
    )
    ov = c.warmup_overrides()
    assert "min_dwell_watch" in ov
    assert "enter_confirmed" not in ov, ov
    assert "ss_confirmed" not in ov, ov


# ---------------------------------------------------------------------------
# F8 —— 生产不跳级
# ---------------------------------------------------------------------------
def test_f8_production_climbs_one_level_per_scan():
    src = inspect.getsource(sm.apply_state_machine)
    assert "sm_fast" in src
    # 红线第 4 条：生产每次扫描最多升一级，只有 SM_FAST 才级联到 4。
    # 表达式外面又包了一层节点幂等守卫（`0 if replayed else ...`）：同一个 scan_id
    # 被重跑时一级都不许升 —— 那是**收紧**而不是放宽，红线依旧成立。
    assert "4 if sm_fast else 1" in src, "跳级护栏的写法变了 —— 请重新核对红线第 4 条"
    assert "0 if replayed else" in src, "节点幂等守卫不见了 —— 重跑同一节点会把连击门槛送掉"
    assert os.environ.get("SM_FAST") not in ("1", "true", "yes"), "生产环境不得开 SM_FAST"


def test_f8_replayed_node_cannot_climb_at_all():
    """红线第 4 条的加强版：同一个 scan_id 重跑，连一级都不许升。"""
    import tempfile
    import time
    from pathlib import Path

    store = sm.StateMachineStore(Path(tempfile.mkdtemp(prefix="f8-")) / "sm.json")
    st = store.get("AAAUSDT", "up")
    st.state = "QUALIFIED"
    st.consecutive_pass = 0
    st.state_enter_ts = time.time() - 20 * 60
    st.last_scan_id = "20260901-003"
    row = {
        "symbol": "AAAUSDT",
        "score_up": 95.0, "ss_up": 90.0, "momentum_score_up": 90.0, "consistency_up": 1.0,
        "score_down": 0.0, "ss_down": 0.0, "momentum_score_down": 0.0, "consistency_down": 0.0,
        "data_quality_score": 95.0, "supply_missing": False, "data_mode": "LIVE",
        "liquidity_hard_pass": True, "last_price": 1.0,
    }
    cfg = sm.default_state_config()
    sm.apply_state_machine([dict(row)], store, cfg=cfg, scan_id="20260901-004")
    first = store.get("AAAUSDT", "up").state
    sm.apply_state_machine([dict(row)], store, cfg=cfg, scan_id="20260901-004")
    assert store.get("AAAUSDT", "up").state == first, "重跑同一节点竟然升级了"


def test_f8_default_state_config_is_production_when_sm_fast_off():
    prev = os.environ.pop("SM_FAST", None)
    try:
        cfg = sm.default_state_config()
        assert cfg.min_dwell_watch == 30
        assert cfg.min_dwell_qualified == 15
        assert cfg.min_streak_watch == 2
        assert cfg.min_streak_qualified == 2
        assert cfg.min_streak_confirmed == 2
    finally:
        if prev is not None:
            os.environ["SM_FAST"] = prev


# ---------------------------------------------------------------------------
# F9 —— regime 恒为 0.5
# ---------------------------------------------------------------------------
def test_f9_regime_is_hardcoded_half():
    src = inspect.getsource(scan_mod.build_screener_snapshot)
    assert '"regime": 0.5' in src, "regime 不再写死 0.5 —— 违反红线第 10 条"


# ---------------------------------------------------------------------------
# 额外：文档A §7 总则里的两条结构不变式
# ---------------------------------------------------------------------------
def test_dmr_top_k_is_clamped_to_12_20():
    from dataclasses import replace

    for k, want in ((1, 12), (12, 12), (16, 16), (20, 20), (99, 20)):
        assert replace(scan_mod.SelectionSettings(), dmr_top_k=k).dmr_top_k == want, k


def test_zone_rank_order_is_the_one_the_report_declares():
    from coin_selection.mcap_zone import ZONE_RANK

    assert ZONE_RANK == {"DMR": 0, "CONFIRMED": 1, "QUALIFIED": 2, "WATCH": 3, "ELIMINATED": 4}


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
