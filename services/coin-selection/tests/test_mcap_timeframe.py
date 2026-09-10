"""30m / 2h / 6h 周期流通市值等级 —— 等级定义与板面透传的回归闸。

这个文件是 A/B/C/D/E/F 定义的**可执行副本**。任何一次误读（把 D/E/F 当成
"下跌候选专用"、把 C 与 D 写反、给某个周期换一套阈值、把无法判级当成
第七个等级）都会在这里翻车。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.mcap_timeframe import (  # noqa: E402
    MA_PERIODS,
    REQUIRED_BARS,
    TIMEFRAMES,
    McapTfResult,
    closes_from_klines,
    evaluate_closes,
    grade_from_mas,
    mcap_series,
    row_fields,
    tail_mean,
    ttl_for,
)
from coin_selection.scan import board_from_rows  # noqa: E402

#: 六条严格排列 → 等级。键是 (ma6, ma12, ma26)，直接照抄需求 §五 的定义。
DEFINITION: list[tuple[str, float, float, float, str]] = [
    ("A", 3.0, 2.0, 1.0, "6日 > 12日 > 26日 | 多头排列"),
    ("B", 2.0, 3.0, 1.0, "12日 > 6日 > 26日 | 多头轻度回调"),
    ("C", 1.0, 3.0, 2.0, "12日 > 26日 > 6日 | 多头重度回调"),
    ("D", 3.0, 1.0, 2.0, "12日 < 26日 < 6日 | 空头重度回调"),
    ("E", 2.0, 1.0, 3.0, "12日 < 6日 < 26日 | 空头轻度回调"),
    ("F", 1.0, 2.0, 3.0, "6日 < 12日 < 26日 | 空头排列"),
]

#: 端到端序列 → 等级。每条都是「前 14 根 / 中 6 根 / 后 6 根」三段常数收盘价。
SERIES_CASES: list[tuple[str, float, float, float]] = [
    ("A", 10.0, 20.0, 30.0),
    ("B", 10.0, 30.0, 22.0),
    ("C", 20.0, 40.0, 12.0),
    ("D", 30.0, 8.0, 40.0),
    ("E", 60.0, 10.0, 20.0),
    ("F", 30.0, 20.0, 10.0),
]


def _three_segment(a: float, b: float, c: float) -> list[float]:
    return [a] * 14 + [b] * 6 + [c] * 6


def test_periods_are_6_12_26_and_need_26_bars():
    assert MA_PERIODS == (6, 12, 26)
    assert REQUIRED_BARS == 26
    assert TIMEFRAMES == ("30m", "2h", "6h")


def test_definition_table_is_exact():
    """A–F 必须与需求原文逐条一致，且六种排列互斥。"""
    seen = set()
    for grade, ma6, ma12, ma26, meaning in DEFINITION:
        got = grade_from_mas(ma6, ma12, ma26)
        assert got == grade, f"{meaning}: expected {grade}, got {got}"
        seen.add(got)
    assert seen == {"A", "B", "C", "D", "E", "F"}, seen


def test_definition_covers_every_ordering_of_three_distinct_values():
    """三个互不相等的均线共 6 种排列，必须恰好命中 A–F 各一次。"""
    import itertools

    grades = [grade_from_mas(*p) for p in itertools.permutations((1.0, 2.0, 3.0))]
    assert sorted(grades) == ["A", "B", "C", "D", "E", "F"], grades


def test_ties_are_not_a_seventh_grade():
    """并列时返回 None（前端显示 —），绝不新造等级名称。"""
    assert grade_from_mas(1.0, 1.0, 1.0) is None
    assert grade_from_mas(2.0, 2.0, 1.0) is None
    assert grade_from_mas(1.0, 2.0, 2.0) is None
    assert grade_from_mas(None, 1.0, 2.0) is None


def test_tail_mean_is_last_k_bars_only():
    series = [float(i) for i in range(1, 27)]  # 1..26
    assert tail_mean(series, 6) == sum(range(21, 27)) / 6
    assert tail_mean(series, 12) == sum(range(15, 27)) / 12
    assert tail_mean(series, 26) == sum(range(1, 27)) / 26
    assert tail_mean(series[:5], 6) is None


def test_mcap_series_is_supply_times_close_over_multiplier():
    closes = [2.0, 4.0]
    series, known = mcap_series(closes, circulating_supply=1_000.0, contract_multiplier=1000)
    assert known is True
    assert series == [2.0, 4.0]  # 1000 * (2 / 1000)
    series, known = mcap_series(closes, circulating_supply=None, contract_multiplier=1)
    assert known is False
    assert series == [2.0, 4.0]


def test_series_cases_end_to_end():
    """从收盘价一路算到等级：6/12/26 根均线关系必须与等级定义自洽。"""
    for grade, a, b, c in SERIES_CASES:
        closes = _three_segment(a, b, c)
        r = evaluate_closes(
            "TESTUSDT", "2h", closes, circulating_supply=1_000_000.0, contract_multiplier=1
        )
        assert r.bars == 26
        assert r.grade == grade, f"{a}/{b}/{c} -> {r.grade}, want {grade}"
        assert r.supply_known is True
        assert r.reason == "ok"
        assert abs(r.ma6 - sum(closes[-6:]) * 1_000_000.0 / 6) < 1e-6
        assert abs(r.ma12 - sum(closes[-12:]) * 1_000_000.0 / 12) < 1e-6
        assert abs(r.ma26 - sum(closes) * 1_000_000.0 / 26) < 1e-6


def test_grade_is_invariant_to_supply_and_multiplier():
    """流通供应量 / 合约乘数是正的公共因子 —— 等级不受其影响。"""
    for grade, a, b, c in SERIES_CASES:
        closes = _three_segment(a, b, c)
        variants = [
            evaluate_closes("X", "30m", closes, circulating_supply=1.0, contract_multiplier=1),
            evaluate_closes("X", "30m", closes, circulating_supply=9.9e11, contract_multiplier=1),
            evaluate_closes("X", "30m", closes, circulating_supply=9.9e11, contract_multiplier=1000),
            evaluate_closes("X", "30m", closes, circulating_supply=None, contract_multiplier=1),
        ]
        assert {v.grade for v in variants} == {grade}, [v.grade for v in variants]
    r = evaluate_closes("X", "30m", _three_segment(10, 20, 30), circulating_supply=None)
    assert r.grade == "A"
    assert (r.ma6, r.ma12, r.ma26) == (None, None, None)
    assert r.reason == "ok_supply_unknown"


def test_all_three_timeframes_share_one_rule():
    """30m / 2h / 6h 只是取数周期不同，判级规则完全相同。"""
    for grade, a, b, c in SERIES_CASES:
        closes = _three_segment(a, b, c)
        got = {
            tf: evaluate_closes("X", tf, closes, circulating_supply=1e9).grade
            for tf in TIMEFRAMES
        }
        assert set(got.values()) == {grade}, got


def test_insufficient_bars_never_guesses():
    r = evaluate_closes("X", "6h", [1.0] * 25, circulating_supply=1e9)
    assert r.bars == 25
    assert r.grade is None
    assert r.ma6 is None and r.ma12 is None and r.ma26 is None
    assert r.reason.startswith("insufficient_bars")


def test_closes_drop_the_in_progress_bar():
    kl = [[0, "1", "1", "1", str(i), "0", 0, "0"] for i in range(1, 6)]
    assert closes_from_klines(kl) == [1.0, 2.0, 3.0, 4.0]
    assert closes_from_klines(kl, drop_incomplete_last=False) == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_ttl_is_half_a_bar_and_at_least_one_scan_node():
    assert ttl_for("30m") == 900
    assert ttl_for("2h") == 3600
    assert ttl_for("6h") == 10800


def test_row_fields_shape():
    per_tf = {
        tf: evaluate_closes("X", tf, _three_segment(10, 20, 30), circulating_supply=1e9)
        for tf in TIMEFRAMES
    }
    f = row_fields(per_tf)
    assert f["mcap_grade_30m"] == "A"
    assert f["mcap_grade_2h"] == "A"
    assert f["mcap_grade_6h"] == "A"
    assert set(f["mcap_tf"]) == set(TIMEFRAMES)
    assert f["mcap_tf"]["6h"]["bars"] == 26
    assert f["mcap_tf"]["6h"]["supply_known"] is True
    # 供应量未知：省掉恒为 null 的三条均线，但等级与 bars/reason 仍在
    unknown = row_fields(
        {
            tf: evaluate_closes("X", tf, _three_segment(10, 20, 30), circulating_supply=None)
            for tf in TIMEFRAMES
        }
    )
    assert unknown["mcap_grade_2h"] == "A"
    assert "ma6" not in unknown["mcap_tf"]["2h"]
    assert unknown["mcap_tf"]["2h"]["reason"] == "ok_supply_unknown"
    # 整行都没算过：三列为 None，明细为空，不伪造等级
    empty = row_fields(None)
    assert empty["mcap_grade_30m"] is None
    assert empty["mcap_grade_2h"] is None
    assert empty["mcap_grade_6h"] is None
    assert empty["mcap_tf"] == {}


# --------------------------------------------------------------------------
# 板面透传：七个分区共用同一张状态栏表格，任何一个分区缺列都算回归
# --------------------------------------------------------------------------

ALL_ZONE_STATES = [
    "CONFIRMED",
    "QUALIFIED",
    "WATCH",
    "ELIMINATED",
    "DATA_INSUFFICIENT",
    "LOW_CONFIDENCE",
]


def _board_row(state: str, **kw):
    base = {
        "symbol": "ZZZUSDT",
        "base_asset": "ZZZ",
        "liquidity_hard_pass": True,
        "liquidity_grade": "A",
        "state_up": state,
        "state_down": state,
        "state_up_dwell_min": 15.0,
        "state_down_dwell_min": 15.0,
        "score_up": 60.0,
        "score_down": 40.0,
        "momentum_score_up": 70,
        "momentum_score_down": 40,
        "consistency_up": 0.6,
        "consistency_down": 0.2,
        "ss_up": 60,
        "ss_down": 20,
        "mcap_momentum_score_up": 55,
        "mcap_momentum_score_down": 40,
        "liquidity_score_abs": 80,
        "data_quality_score": 90,
        "ret_1h": 0.0,
        "ret_4h": 0.0,
        "ret_24h": 0.0,
        "ret_15m": None,
        "ret_since_anchor": None,
        "last_price": 1.0,
        "supply_missing": False,
        "mcap_grade_30m": "A",
        "mcap_grade_2h": "C",
        "mcap_grade_6h": "F",
        "mcap_tf": {"30m": {"grade": "A", "bars": 26}},
    }
    base.update(kw)
    return base


def test_every_zone_carries_the_three_columns():
    now = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    for state in ALL_ZONE_STATES:
        for direction in ("up", "down"):
            pool = board_from_rows([_board_row(state)], direction, now=now)
            assert pool, f"{state}/{direction} produced no board row"
            row = pool[0]
            assert row["state"] == state
            # DMR 分区是 CONFIRMED 的子集，同一行同一套列，不另建表
            assert row["mcap_grade_30m"] == "A"
            assert row["mcap_grade_2h"] == "C"
            assert row["mcap_grade_6h"] == "F"
            assert row["mcap_tf"]["30m"]["grade"] == "A"


def test_long_and_short_pools_share_one_grade():
    """禁止把上涨候选与下跌候选拆成两套等级规则。"""
    now = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    r = _board_row("WATCH")
    up = board_from_rows([r], "up", now=now)[0]
    dn = board_from_rows([r], "down", now=now)[0]
    for k in ("mcap_grade_30m", "mcap_grade_2h", "mcap_grade_6h"):
        assert up[k] == dn[k], k


def test_missing_grades_pass_through_as_none():
    now = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    row = _board_row(
        "ELIMINATED", mcap_grade_30m=None, mcap_grade_2h=None, mcap_grade_6h=None, mcap_tf={}
    )
    pool = board_from_rows([row], "up", now=now)
    assert pool[0]["mcap_grade_30m"] is None
    assert pool[0]["mcap_tf"] == {}


def test_result_dataclass_defaults():
    r = McapTfResult(
        symbol="X",
        timeframe="30m",
        bars=0,
        ma6=None,
        ma12=None,
        ma26=None,
        grade=None,
        supply_known=False,
        reason="x",
    )
    assert r.data_source == "binance_klines"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
