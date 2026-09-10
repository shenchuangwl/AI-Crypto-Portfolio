"""Board row fields: enter time, no fake 15m/anchor zeros."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.scan import board_from_rows


def _row(**kw):
    base = {
        "symbol": "NILUSDT",
        "base_asset": "NIL",
        "liquidity_hard_pass": True,
        "liquidity_grade": "A",
        "state_up": "QUALIFIED",
        "state_down": "WATCH",
        "state_up_dwell_min": 0.0,
        "state_down_dwell_min": 15.0,
        "score_up": 67.8,
        "score_down": 39.1,
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
        "ret_1h": -0.0006,
        "ret_4h": 0.01,
        "ret_24h": 0.136,
        "ret_15m": None,
        "ret_since_anchor": 0.042,
        "last_price": 0.048,
        "state_up_enter_price": 0.0512,
        "state_down_enter_price": 0.047,
        "supply_missing": False,
    }
    base.update(kw)
    return base


def test_enter_time_is_now_minus_dwell():
    now = datetime(2026, 8, 15, 6, 0, 5, tzinfo=timezone.utc)
    pool = board_from_rows(
        [_row(symbol="HEIUSDT", state_up_dwell_min=240.0)],
        "up",
        now=now,
    )
    # Off-cadence now (06:00:05) still publishes the node (02:00:00), not :05.
    assert pool[0]["state_enter_time_utc"] == "2026-08-15T02:00:00Z"
    assert pool[0]["state_duration_minutes"] == 240.0


def test_just_entered_qualified_has_zero_dwell_and_enter_eq_scan():
    now = datetime(2026, 8, 15, 6, 0, 5, tzinfo=timezone.utc)
    pool = board_from_rows([_row()], "up", now=now)
    assert pool[0]["state_duration_minutes"] == 0.0
    assert pool[0]["state_enter_time_utc"] == "2026-08-15T06:00:00Z"


def test_enter_time_is_always_on_the_quarter_hour():
    """选入时间只允许 :00/:15/:30/:45，涨跌两侧同一条路径。"""
    now = datetime(2026, 9, 1, 5, 17, 44, tzinfo=timezone.utc)
    for direction, dwell, want in (
        ("up", 0.0, "2026-09-01T05:15:00Z"),
        ("up", 15.0, "2026-09-01T05:00:00Z"),
        ("up", 30.0, "2026-09-01T04:45:00Z"),
        ("down", 45.0, "2026-09-01T04:30:00Z"),
        ("down", 135.0, "2026-09-01T03:00:00Z"),
    ):
        r = _row()
        r["state_up_dwell_min"] = dwell
        r["state_down_dwell_min"] = dwell
        pool = board_from_rows([r], direction, now=now)
        got = pool[0]["state_enter_time_utc"]
        assert got == want, (direction, dwell, got, want)
        t = datetime.fromisoformat(got.replace("Z", "+00:00"))
        assert t.second == 0 and t.minute % 15 == 0


def test_unclassified_grade_kept_and_reason_g1u():
    pool = board_from_rows(
        [_row(symbol="AKEUSDT", liquidity_grade="UNCLASSIFIED")],
        "up",
        now=datetime(2026, 8, 15, 6, 0, 5, tzinfo=timezone.utc),
    )
    assert pool[0]["liquidity_grade"] == "UNCLASSIFIED"
    assert "G1U" in pool[0]["reason_codes"]


def test_no_fake_zero_for_missing_15m_and_anchor_passthrough():
    pool = board_from_rows(
        [_row(ret_15m=None, ret_since_anchor=0.042)],
        "up",
        now=datetime(2026, 8, 15, 6, 0, 5, tzinfo=timezone.utc),
    )
    assert pool[0]["ret_15m"] is None
    assert abs(pool[0]["ret_since_anchor"] - 0.042) < 1e-9


def test_enter_price_on_listed_states():
    now = datetime(2026, 8, 15, 6, 0, 5, tzinfo=timezone.utc)
    up = board_from_rows([_row()], "up", now=now)
    assert up[0]["state"] == "QUALIFIED"
    assert abs(up[0]["state_enter_price"] - 0.0512) < 1e-9
    dn = board_from_rows([_row()], "down", now=now)
    assert dn[0]["state"] == "WATCH"
    assert abs(dn[0]["state_enter_price"] - 0.047) < 1e-9


SEVEN_STATES = (
    "WATCH",
    "QUALIFIED",
    "CONFIRMED",
    "ELIMINATED",
    "DATA_INSUFFICIENT",
    "LOW_CONFIDENCE",
    "NONE",
)


def test_t7_conservation_is_seven_state():
    """守恒式必须是**七态**求和（缺陷 N2，文档A §0.5-D12 / 文档B §2.3）。

    旧版写的是 ``W+Q+C+E+NONE == 宇宙×2``，漏了 ``DATA_INSUFFICIENT`` 与
    ``LOW_CONFIDENCE``。现网这两个值常为 0，所以漏了也看不出来 —— 直到某天
    出现供应量批量缺失事件，校验开始误报。这里用一个**故意含这两态**的样本，
    让「漏项」这件事在测试里必然暴露。
    """
    from coin_selection.scan import board_from_rows

    states = [
        ("A", "WATCH", "ELIMINATED"),
        ("B", "QUALIFIED", "DATA_INSUFFICIENT"),
        ("C", "CONFIRMED", "LOW_CONFIDENCE"),
        ("D", "NONE", "NONE"),
    ]
    rows = []
    for name, up, down in states:
        r = _row()
        r = dict(r)
        r["symbol"] = f"{name}USDT"
        r["base_asset"] = name
        r["state_up"] = up
        r["state_down"] = down
        rows.append(r)

    counts = {k: 0 for k in SEVEN_STATES}
    for r in rows:
        for st in (r["state_up"], r["state_down"]):
            counts[st] = counts.get(st, 0) + 1

    universe = len(rows)
    seven = sum(counts[k] for k in SEVEN_STATES)
    assert seven == universe * 2, (seven, universe)

    # 旧的五态求和在这份样本上**故意对不上** —— 那正是 N2 想说的事
    five = sum(counts[k] for k in ("WATCH", "QUALIFIED", "CONFIRMED", "ELIMINATED", "NONE"))
    assert five != universe * 2, "样本没有覆盖 DATA_INSUFFICIENT / LOW_CONFIDENCE"

    # 板面行数 == 七态 − NONE（NONE 侧不登载）
    pool = board_from_rows(rows, "up") + board_from_rows(rows, "down")
    assert len(pool) == seven - counts["NONE"], (len(pool), seven, counts["NONE"])


def test_t7_none_side_is_not_listed():
    from coin_selection.scan import board_from_rows

    r = dict(_row())
    r["state_up"] = "NONE"
    assert board_from_rows([r], "up") == []


if __name__ == "__main__":
    test_enter_time_is_now_minus_dwell()
    test_just_entered_qualified_has_zero_dwell_and_enter_eq_scan()
    test_enter_time_is_always_on_the_quarter_hour()
    test_unclassified_grade_kept_and_reason_g1u()
    test_no_fake_zero_for_missing_15m_and_anchor_passthrough()
    test_enter_price_on_listed_states()
    test_t7_conservation_is_seven_state()
    test_t7_none_side_is_not_listed()
    print("ok")
