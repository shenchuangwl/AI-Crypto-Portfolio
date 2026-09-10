"""1Week / 1Month 涨跌幅 —— 计算口径与板面透传的回归闸。

这个文件是"两列口径严格参照 1h/4h/24h"这句话的**可执行副本**。任何一次误读
（把滚动窗口写成本周/本自然月的开→收、丢掉正在形成的最后一根导致窗口整体后移、
把算不出折叠成 0、把两列插错位置、给上涨池和下跌池算两套数）都会在这里翻车。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.gate2 import roc  # noqa: E402
from coin_selection.long_returns import (  # noqa: E402
    KLINE_LIMIT,
    LOOKBACK_BARS,
    PERIODS,
    RET_FIELD,
    LongReturnResult,
    closes_from_klines,
    evaluate_closes,
    pct_change,
    row_fields,
)
from coin_selection.scan import board_from_rows  # noqa: E402

ALL_ZONE_STATES = [
    "CONFIRMED",
    "QUALIFIED",
    "WATCH",
    "ELIMINATED",
    "DATA_INSUFFICIENT",
    "LOW_CONFIDENCE",
]


# --------------------------------------------------------------------------
# 口径
# --------------------------------------------------------------------------


def test_periods_and_lookbacks_are_the_column_order():
    """列顺序 1Week → 1Month，回看 7 / 30 根日 K。"""
    assert PERIODS == ("1w", "1mo")
    assert LOOKBACK_BARS == {"1w": 7, "1mo": 30}
    assert RET_FIELD == {"1w": "ret_1w", "1mo": "ret_1mo"}
    # 请求根数必须够回看最长的那个周期，且留出正在形成的那根
    assert KLINE_LIMIT > max(LOOKBACK_BARS.values())


def test_pct_change_matches_gate2_roc():
    """与 1h/4h/24h 用的是同一个式子，只是这里如实返回 None 而不是 0.0。"""
    closes = [float(i) for i in range(1, 41)]  # 1..40
    for bars in (1, 4, 7, 24, 30):
        assert pct_change(closes, bars) == roc(closes, bars)


def test_pct_change_is_a_trailing_window_not_a_calendar_bucket():
    """近 7 天 = 最新价 / 7 根之前的收盘价 − 1。不是"本周开→收"。"""
    closes = [100.0] * 30 + [110.0]  # 最后一根（正在形成）涨到 110
    assert pct_change(closes, 7) == 110.0 / 100.0 - 1.0
    # 分母确实取的是第 7 根之前那一根
    closes = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    assert pct_change(closes, 7) == 9.0 / 2.0 - 1.0


def test_insufficient_bars_returns_none_never_zero():
    """根数不足绝不能折叠成 0 —— 0 会被读成"这周没涨没跌"。"""
    assert pct_change([1.0] * 7, 7) is None      # 需要 8 根
    assert pct_change([1.0] * 8, 7) == 0.0       # 刚好够
    assert pct_change([1.0] * 30, 30) is None    # 需要 31 根
    assert pct_change([], 7) is None
    assert pct_change([0.0] * 40, 7) is None     # 基准为 0


def test_evaluate_closes_reports_each_period_independently():
    # 15 根：够算 1Week，不够算 1Month
    closes = [10.0] * 7 + [10.0] * 7 + [12.0]
    r = evaluate_closes("XUSDT", closes)
    assert r.ret_1w == 12.0 / 10.0 - 1.0
    assert r.ret_1mo is None
    assert r.reason.startswith("insufficient_bars")

    long_enough = [float(100 + i) for i in range(35)]
    ok = evaluate_closes("XUSDT", long_enough)
    assert ok.ret_1w is not None and ok.ret_1mo is not None
    assert ok.reason == "ok"
    assert ok.bars == 35

    empty = evaluate_closes("XUSDT", [])
    assert empty.ret_1w is None and empty.ret_1mo is None


def test_closes_keeps_the_in_progress_last_bar():
    """丢掉最后一根会让"近 7 天"变成"截至昨收的 7 天"，与 24h 列的口径对不上。"""
    klines = [[0, "1", "2", "0.5", str(i), "9"] for i in range(1, 6)]
    assert closes_from_klines(klines) == [1.0, 2.0, 3.0, 4.0, 5.0]
    # 脏数据（缺字段 / 非数 / 非正）跳过，不顶位
    assert closes_from_klines([[0, "1"], [0, "1", "2", "3", "x"], [0, "1", "2", "3", "0"]]) == []
    assert closes_from_klines([]) == []
    assert closes_from_klines(None) == []


# --------------------------------------------------------------------------
# 板面字段
# --------------------------------------------------------------------------


def test_row_fields_always_emits_both_keys():
    """两个键永远存在（None 表示算不出），列结构才能在七个分区里一致。"""
    got = row_fields(evaluate_closes("XUSDT", [float(100 + i) for i in range(35)]))
    assert set(got) == {"ret_1w", "ret_1mo"}
    assert got["ret_1w"] is not None

    missing = row_fields(None)
    assert missing == {"ret_1w": None, "ret_1mo": None}


def _board_row(state: str, **kw):
    base = {
        "symbol": "AAAUSDT",
        "base_asset": "AAA",
        "state_up": state,
        "state_down": state,
        "score_up": 70,
        "score_down": 30,
        "liquidity_grade": "A",
        "momentum_score_up": 70,
        "momentum_score_down": 30,
        "consistency_up": 1,
        "consistency_down": 0,
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
        "ret_1w": 0.1234,
        "ret_1mo": -0.0567,
        "ret_since_anchor": None,
        "last_price": 1.0,
        "supply_missing": False,
    }
    base.update(kw)
    return base


def test_every_zone_carries_the_two_columns():
    now = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    for state in ALL_ZONE_STATES:
        for direction in ("up", "down"):
            pool = board_from_rows([_board_row(state)], direction, now=now)
            assert pool, f"{state}/{direction} produced no board row"
            row = pool[0]
            assert row["state"] == state
            # DMR 分区是 CONFIRMED 的子集，同一行同一套列，不另建表
            assert row["ret_1w"] == 0.1234
            assert row["ret_1mo"] == -0.0567


def test_long_and_short_pools_share_one_return():
    """禁止把上涨候选与下跌候选拆成两套涨跌幅。"""
    now = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    r = _board_row("WATCH")
    up = board_from_rows([r], "up", now=now)[0]
    dn = board_from_rows([r], "down", now=now)[0]
    for k in ("ret_1w", "ret_1mo"):
        assert up[k] == dn[k], k


def test_missing_returns_pass_through_as_none():
    now = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    row = _board_row("ELIMINATED", ret_1w=None, ret_1mo=None)
    pool = board_from_rows([row], "up", now=now)
    assert pool[0]["ret_1w"] is None
    assert pool[0]["ret_1mo"] is None
    # 既有的 1h/4h/24h 仍然折叠成 0（未改动它们的口径）
    assert pool[0]["ret_24h"] == 0.0


def test_columns_sit_between_24h_and_since_anchor():
    """键序即列序：… → ret_24h → ret_1w → ret_1mo → ret_since_anchor → …"""
    now = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    keys = list(board_from_rows([_board_row("WATCH")], "up", now=now)[0])
    i = keys.index("ret_24h")
    assert keys[i + 1 : i + 4] == ["ret_1w", "ret_1mo", "ret_since_anchor"]


def test_result_dataclass_defaults():
    r = LongReturnResult(symbol="X", bars=0, ret_1w=None, ret_1mo=None, reason="x")
    assert r.data_source == "binance_klines"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
