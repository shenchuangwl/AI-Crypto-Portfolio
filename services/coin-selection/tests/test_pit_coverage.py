"""PIT 已收盘过滤 + 物理 coverage 单测。

权威：ChatGpt_SOL5.6 文档 A §7.2【上线阻断】、§8、§18.1；
文档 B §4.1–§4.3、§11.1–§11.2、§12、§20（K线 / gap / duplicate / coverage 行）、§21.1。

其中 :func:`test_physical_coverage_reproduces_document_b_facts` 用**现网真实快照目录**
复算文档 B §4.2 的四个读数（1602 / 1617 / 99.072% / 15 个缺失节点）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import coverage as cov  # noqa: E402
from coin_selection import pit  # noqa: E402

HOUR_MS = 3_600_000


def bar(open_ms: int, close: str = "1.0", interval_ms: int = HOUR_MS) -> list:
    """Binance kline 行：closeTime 是该根最后一毫秒。"""
    return [open_ms, "1", "2", "0.5", close, "10", open_ms + interval_ms - 1, "0", 0, "0", "0", "0"]


# ---------------------------------------------------------------------------
# PIT
# ---------------------------------------------------------------------------
def test_only_closed_bars_uses_close_time_not_position():
    bars = [bar(i * HOUR_MS) for i in range(1, 5)]  # 01:00 02:00 03:00 04:00
    # 决策时刻 = 04:00 整 → 01/02/03 三根已收盘，04 那根还没收
    dt = 4 * HOUR_MS
    assert len(pit.only_closed_bars(bars, dt)) == 3
    # 决策时刻 = 05:00 整 → 四根全收盘（旧的「盲丢最后一根」会白丢一根）
    assert len(pit.only_closed_bars(bars, 5 * HOUR_MS)) == 4


def test_boundary_bar_closing_exactly_at_decision_time_counts_as_closed():
    b = bar(HOUR_MS)  # closeTime = 01:59:59.999
    assert len(pit.only_closed_bars([b], 2 * HOUR_MS)) == 1
    assert len(pit.only_closed_bars([b], 2 * HOUR_MS - 1)) == 1
    assert len(pit.only_closed_bars([b], 2 * HOUR_MS - 2)) == 0


def test_strict_mode_raises_on_future_bars():
    bars = [bar(i * HOUR_MS) for i in range(1, 4)]
    try:
        pit.only_closed_bars(bars, 2 * HOUR_MS, strict=True)
        raise AssertionError("strict mode must raise on a future bar")
    except pit.FutureInputError:
        pass


def test_no_decision_time_keeps_backward_compatible_behaviour():
    bars = [bar(i * HOUR_MS) for i in range(1, 4)]
    assert len(pit.only_closed_bars(bars, None)) == 3
    assert len(pit.closes_from_closed_klines(bars)) == 3
    # 没有 decision_time、又要旧启发式时才丢最后一根
    assert len(pit.closes_from_closed_klines(bars, None, drop_incomplete_last_when_unknown=True)) == 2


def test_rows_without_close_time_are_kept_conservatively():
    rows = [[0, "1", "1", "1", "2", "0"]]  # 只有 6 列，没有 closeTime
    assert len(pit.only_closed_bars(rows, 10 * HOUR_MS)) == 1


def test_gate2_and_mcap_timeframe_now_share_the_same_filter():
    """文档 A §7.2 的核心：两条路径必须给出同一批已收盘 K 线。"""
    from coin_selection.gate2 import closes_from_klines as g2_closes
    from coin_selection.mcap_timeframe import closes_from_klines as tf_closes

    bars = [bar(i * HOUR_MS, close=str(i)) for i in range(1, 6)]
    dt = 5 * HOUR_MS
    assert g2_closes(bars, dt) == tf_closes(bars, decision_time=dt)
    assert g2_closes(bars, dt) == [1.0, 2.0, 3.0, 4.0]


def test_epoch_conversions():
    assert pit.to_epoch_ms(1_700_000_000_000) == 1_700_000_000_000
    assert pit.to_epoch_ms(1_700_000_000) == 1_700_000_000_000
    assert pit.to_epoch_ms("2026-09-01T00:00:00Z") == pit.to_epoch_ms(
        datetime(2026, 9, 1, tzinfo=timezone.utc)
    )
    assert pit.to_epoch_ms(None) is None
    assert pit.to_epoch_ms("not-a-time") is None


def test_assert_no_future_input():
    d = "2026-09-01T00:00:00Z"
    pit.assert_no_future_input("2026-08-31T23:45:00Z", d)
    try:
        pit.assert_no_future_input("2026-09-01T00:15:00Z", d, what="supply")
        raise AssertionError("future input must raise")
    except pit.FutureInputError:
        pass


def test_bars_as_of_lineage():
    bars = [bar(i * HOUR_MS) for i in range(1, 4)]
    info = pit.bars_as_of(bars)
    assert info["bars"] == 3
    assert info["first_close_time_utc"].endswith("Z")


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------
def test_scan_id_grid_and_absolute_index():
    assert cov.parse_scan_id("20260815-001") == (datetime(2026, 8, 15).date(), 1)
    assert cov.node_index("20260816-000") - cov.node_index("20260815-095") == 1
    assert cov.scan_id_from_index(cov.node_index("20260827-019")) == "20260827-019"
    assert cov.node_time_utc("20260815-001").isoformat().startswith("2026-08-15T00:15")
    for bad in ("2026815-001", "20260815-096", "", "x"):
        try:
            cov.parse_scan_id(bad)
            raise AssertionError(f"bad scan_id accepted: {bad}")
        except cov.ScanIdError:
            pass


def test_missing_nodes_are_listed_not_rounded_away():
    ids = ["20260815-000", "20260815-001", "20260815-003", "20260815-004"]
    c = cov.physical_coverage(ids)
    assert c["expected_nodes"] == 5
    assert c["present_nodes"] == 4
    assert c["missing_scan_ids"] == ["20260815-002"]
    assert c["coverage_ratio"] == 0.8
    assert len(c["continuous_segments"]) == 2
    assert c["largest_gap_nodes"] == 1


def test_duplicates_are_reported_separately():
    c = cov.physical_coverage(["20260815-000", "20260815-000", "20260815-001"])
    assert c["duplicate_scan_ids"] == ["20260815-000"]
    assert c["present_nodes"] == 2


def test_coverage_crosses_midnight_without_going_backwards():
    c = cov.physical_coverage(["20260815-095", "20260816-000"])
    assert c["expected_nodes"] == 2 and c["missing_nodes"] == 0


def test_physical_coverage_reproduces_document_b_facts():
    """文档 B §4.2 的现网读数：1602 / 1617 / 99.072% / 15 个缺失节点。"""
    snaps = ROOT / "data" / "coin-selection-y" / "snapshots"
    if not snaps.is_dir():
        print("  SKIP  Y snapshots not present")
        return
    ids = [
        p.stem
        for p in snaps.glob("*.json")
        if not p.name.endswith(".full.json") and p.stem[:1].isdigit()
    ]
    ids = [s for s in ids if cov.node_index(s) <= cov.node_index("20260831-081")]
    if not ids:
        print("  SKIP  no snapshot in the audited window")
        return
    c = cov.physical_coverage(ids)
    assert c["first_scan_id"] == "20260815-001", c["first_scan_id"]
    assert c["last_scan_id"] == "20260831-081", c["last_scan_id"]
    assert c["expected_nodes"] == 1617, c["expected_nodes"]
    assert c["present_nodes"] == 1602, c["present_nodes"]
    assert c["missing_nodes"] == 15, c["missing_nodes"]
    assert round(c["coverage_ratio"] * 100, 3) == 99.072, c["coverage_ratio"]
    assert c["span_days"] == 16.833333, c["span_days"]
    assert c["largest_gap_nodes"] == 12
    assert c["missing_scan_ids"][:3] == [
        "20260815-020",
        "20260817-005",
        "20260824-046",
    ]
    assert c["missing_scan_ids"][3:] == [f"20260827-{i:03d}" for i in range(8, 20)]


def test_field_coverage_reports_its_own_start():
    recs = [("20260815-000", None), ("20260816-000", "A"), ("20260817-000", "B")]
    f = cov.field_coverage(recs, field_name="mcap_grade_30m")
    assert f["first_available_scan_id"] == "20260816-000"
    assert f["nodes_with_field"] == 2 and f["ratio"] == round(2 / 3, 6)


# ---------------------------------------------------------------------------
# 31 天上限与交集（文档 B §11.2）
# ---------------------------------------------------------------------------
def test_cycles_and_days_are_capped_at_31():
    assert cov.clamp_cycles(366) == 31
    assert cov.clamp_cycles(7) == 7
    assert cov.clamp_cycles(0) == 1
    assert cov.clamp_days(400) == 31.0
    assert cov.clamp_days(None) is None


def test_effective_window_is_the_intersection():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    r = cov.effective_window(
        requested=(now - timedelta(days=60), now),
        now=now,
        physical=(now - timedelta(days=17), now - timedelta(hours=1)),
        field=(now - timedelta(days=9), now),
    )
    assert r["status"] == "OK"
    # 31 天 retention 与 17 天物理、9 天字段取交集 → 9 天那一段说了算
    assert r["effective_from"] == (now - timedelta(days=9)).isoformat().replace(
        "+00:00", "Z"
    )
    assert r["effective_days"] is not None and r["effective_days"] < 9.01


def test_no_overlap_returns_no_coverage_not_a_fake_zero():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    r = cov.effective_window(
        requested=(now - timedelta(days=60), now - timedelta(days=40)),
        now=now,
    )
    assert r["status"] == "NO_COVERAGE"
    assert r["effective_from"] is None and r["effective_days"] is None


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"ok {t.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"all {len(tests)}" if not fails else f"{fails} failed")
    raise SystemExit(1 if fails else 0)
