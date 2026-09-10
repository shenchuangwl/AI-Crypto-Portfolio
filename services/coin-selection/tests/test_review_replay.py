"""Board-row zone occupancy replay → ReviewTrade (no production snapshots)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.review_replay import replay_boards  # noqa: E402


def _row(
    symbol: str,
    direction: str,
    state: str,
    *,
    last: float | None,
    enter: float | None = None,
    dmr: bool = False,
    score: float = 70.0,
) -> dict:
    return {
        "symbol": symbol,
        "direction": direction,
        "state": state,
        "canonical_asset_id": symbol.replace("USDT", "").lower(),
        "underlying_asset": symbol.replace("USDT", ""),
        "contract_multiplier": 1,
        "last_price": last,
        "ref_price": last,
        "state_enter_price": enter,
        "dmr_selected": dmr,
        "score_up": score if direction == "up" else 40.0,
        "score_down": score if direction == "down" else 40.0,
        "direction_confidence": 0.8,
        "liquidity_grade": "A",
        "mcap_grade_30m": None,
        "mcap_grade_2h": None,
        "mcap_grade_6h": None,
        "ret_1h": 0.01,
        "ret_4h": 0.02,
        "ret_24h": 0.03,
        "ret_1w": None,
        "ret_1mo": None,
        "ret_since_anchor": 0.04,
        "risk_flags": [],
        "reason_codes": ["G1_PASS"],
        "qualified_path": "S" if state in ("QUALIFIED", "CONFIRMED") else None,
        "confirmed_path": "S" if state == "CONFIRMED" else None,
    }


def _board(scan_id: str, ts: str, rows: list[dict], pv: str = "param-v1.4.0-staircase-confirm-dmr") -> dict:
    long_pool = [r for r in rows if r["direction"] == "up"]
    short_pool = [r for r in rows if r["direction"] == "down"]
    return {
        "meta": {
            "scan_id": scan_id,
            "scan_timestamp_utc": ts,
            "parameter_version": pv,
        },
        "long_pool": long_pool,
        "short_pool": short_pool,
    }


def _by_id(trades: list[dict]) -> dict[str, dict]:
    return {t["trade_id"]: t for t in trades}


def test_open_then_close_confirmed_up():
    boards = [
        _board(
            "20260822-001",
            "2026-08-22T00:00:00Z",
            [_row("HYPEUSDT", "up", "WATCH", last=10.0, enter=10.0)],
        ),
        _board(
            "20260822-002",
            "2026-08-22T00:15:00Z",
            [_row("HYPEUSDT", "up", "CONFIRMED", last=10.5, enter=10.5)],
        ),
        _board(
            "20260822-003",
            "2026-08-22T00:30:00Z",
            [_row("HYPEUSDT", "up", "CONFIRMED", last=11.0, enter=10.5)],
        ),
        _board(
            "20260822-004",
            "2026-08-22T00:45:00Z",
            [_row("HYPEUSDT", "up", "QUALIFIED", last=11.55, enter=11.55)],
        ),
    ]
    trades = replay_boards(boards, zones=("CONFIRMED",))
    closed = [t for t in trades if t["status"] == "CLOSED" and t["zone"] == "CONFIRMED"]
    assert len(closed) == 1
    t = closed[0]
    assert t["trade_id"] == "HYPEUSDT|up|CONFIRMED|20260822-002"
    assert t["enter_price"] == 10.5
    assert t["enter_price_source"] == "state_enter_price"
    assert t["exit_price"] == 11.55
    assert abs(t["pnl_pct"] - (11.55 / 10.5 - 1)) < 1e-12
    assert t["pnl_sign"] == 1
    assert t["dwell_nodes"] == 2
    assert t["direction"] == "up"


def test_reentry_counts_twice():
    boards = [
        _board("s1", "2026-08-22T00:00:00Z", [_row("AAAUSDT", "down", "WATCH", last=1.0, enter=1.0)]),
        _board("s2", "2026-08-22T00:15:00Z", [_row("AAAUSDT", "down", "CONFIRMED", last=1.0, enter=1.0)]),
        _board("s3", "2026-08-22T00:30:00Z", [_row("AAAUSDT", "down", "WATCH", last=0.9, enter=0.9)]),
        _board("s4", "2026-08-22T00:45:00Z", [_row("AAAUSDT", "down", "CONFIRMED", last=0.95, enter=0.95)]),
        _board("s5", "2026-08-22T01:00:00Z", [_row("AAAUSDT", "down", "WATCH", last=1.0, enter=1.0)]),
    ]
    trades = replay_boards(boards, zones=("CONFIRMED",))
    closed = [t for t in trades if t["status"] == "CLOSED"]
    assert len(closed) == 2
    assert {t["trade_id"] for t in closed} == {
        "AAAUSDT|down|CONFIRMED|s2",
        "AAAUSDT|down|CONFIRMED|s4",
    }
    # first: down 1.0 → 0.9 = +10%
    a = _by_id(closed)["AAAUSDT|down|CONFIRMED|s2"]
    assert abs(a["pnl_pct"] - 0.10) < 1e-12
    assert a["pnl_sign"] == 1
    # second: down 0.95 → 1.0 = loss
    b = _by_id(closed)["AAAUSDT|down|CONFIRMED|s4"]
    assert b["pnl_sign"] == -1


def test_dmr_uses_last_price_not_confirm_enter():
    """DMR occupancy starts later than CONFIRMED; stay price is last at DMR entry."""
    boards = [
        _board(
            "s1",
            "2026-08-22T00:00:00Z",
            [_row("BBBUSDT", "up", "CONFIRMED", last=100.0, enter=100.0, dmr=False)],
        ),
        _board(
            "s2",
            "2026-08-22T00:15:00Z",
            [_row("BBBUSDT", "up", "CONFIRMED", last=108.0, enter=100.0, dmr=True)],
        ),
        _board(
            "s3",
            "2026-08-22T00:30:00Z",
            [_row("BBBUSDT", "up", "CONFIRMED", last=110.0, enter=100.0, dmr=False)],
        ),
    ]
    trades = replay_boards(boards, zones=("DMR", "CONFIRMED"))
    dmr = [t for t in trades if t["zone"] == "DMR" and t["status"] == "CLOSED"]
    conf = [t for t in trades if t["zone"] == "CONFIRMED" and t["status"] == "OPEN"]
    assert len(dmr) == 1
    assert dmr[0]["enter_price"] == 108.0
    assert dmr[0]["enter_price_source"] == "last_price"
    assert dmr[0]["exit_price"] == 110.0
    assert abs(dmr[0]["pnl_pct"] - (110.0 / 108.0 - 1)) < 1e-12
    assert len(conf) == 1
    assert conf[0]["enter_price"] == 100.0
    # DMR + CONFIRMED trades add; they are distinct books
    assert len(trades) == 2


def test_open_not_in_closed_pnl():
    boards = [
        _board("s1", "2026-08-22T00:00:00Z", [_row("CCCUSDT", "up", "CONFIRMED", last=2.0, enter=2.0)]),
        _board("s2", "2026-08-22T00:15:00Z", [_row("CCCUSDT", "up", "CONFIRMED", last=2.2, enter=2.0)]),
    ]
    trades = replay_boards(boards, zones=("CONFIRMED",))
    assert len(trades) == 1
    assert trades[0]["status"] == "OPEN"
    assert trades[0]["pnl_pct"] is None
    assert trades[0]["pnl_sign"] is None
    assert trades[0]["exit_price"] is None


def test_vanished_row_closes_with_prev_price():
    boards = [
        _board("s1", "2026-08-22T00:00:00Z", [_row("DDDUSDT", "up", "CONFIRMED", last=5.0, enter=5.0)]),
        _board("s2", "2026-08-22T00:15:00Z", []),  # row gone
    ]
    trades = replay_boards(boards, zones=("CONFIRMED",))
    assert len(trades) == 1
    t = trades[0]
    assert t["status"] == "CLOSED"
    assert "VANISHED" in t["flags"]
    assert t["exit_price"] == 5.0
    assert t["exit_price_source"] == "prev_node"
    assert t["pnl_sign"] == 0
    # Only the *price* falls back to the previous node; the exit is stamped at
    # the node where the row went missing, so no trade closes at its own entry.
    assert t["exit_scan_id"] == "s2"
    assert t["exit_time_utc"] == "2026-08-22T00:15:00Z"
    assert t["exit_time_utc"] > t["enter_time_utc"]
    assert t["dwell_minutes"] == 15.0


def test_listed_zone_prefers_zone_enter_when_display_zone_matches():
    """选币榜Y：停留价跟展示区 zone_enter_price，不得误用状态机入场价。"""
    from coin_selection.review_replay import stay_px

    row = _row("RAYSOLUSDT", "up", "CONFIRMED", last=1.3433, enter=1.361)
    row["final_zone"] = "CONFIRMED"
    row["zone_enter_price"] = 1.3525
    px, src = stay_px(row, "CONFIRMED")
    assert px == 1.3525
    assert src == "zone_enter_price"
    # 同一行的 DMR 占用仍是进入节点印价，不是确认区/展示区戳。
    dmr_px, dmr_src = stay_px(row, "DMR")
    assert dmr_px == 1.3433
    assert dmr_src == "last_price"


def test_dmr_selected_does_not_steal_confirmed_stay_from_zone_enter():
    """dmr_selected 时 zone_enter 是 DMR 戳；确认区占用必须回退 state_enter。"""
    from coin_selection.review_replay import stay_px

    row = _row("VELVETUSDT", "down", "CONFIRMED", last=0.0558, enter=0.0589, dmr=True)
    row["final_zone"] = "CONFIRMED"
    row["zone_enter_price"] = 0.0564  # DMR 进入印价
    px, src = stay_px(row, "CONFIRMED")
    assert px == 0.0589
    assert src == "state_enter_price"
    dmr_px, dmr_src = stay_px(row, "DMR")
    assert dmr_px == 0.0558
    assert dmr_src == "last_price"


def test_v14_without_zone_enter_still_uses_state_enter():
    """v1.4.0 / 选币榜X 不下发 zone_enter_*，确认区停留价仍是 state_enter_price。"""
    from coin_selection.review_replay import stay_px

    row = _row("BTWUSDT", "up", "CONFIRMED", last=0.45622, enter=0.46181)
    px, src = stay_px(row, "CONFIRMED")
    assert px == 0.46181
    assert src == "state_enter_price"


def test_y_reenter_display_zone_opens_with_zone_enter_price():
    """离开展示区再回来：确认区占用必须用新的 zone_enter，而不是首次状态机入场价。"""
    boards = [
        _board(
            "20260910-018",
            "2026-09-10T04:30:00Z",
            [_row("RAYSOLUSDT", "up", "CONFIRMED", last=1.361, enter=1.361)
             | {"final_zone": "CONFIRMED", "zone_enter_price": 1.361}],
        ),
        _board(
            "20260910-030",
            "2026-09-10T07:30:00Z",
            [_row("RAYSOLUSDT", "up", "CONFIRMED", last=1.350, enter=1.361)
             | {"final_zone": "QUALIFIED", "zone_enter_price": 1.350}],
        ),
        _board(
            "20260910-036",
            "2026-09-10T09:00:00Z",
            [_row("RAYSOLUSDT", "up", "CONFIRMED", last=1.3525, enter=1.361)
             | {"final_zone": "CONFIRMED", "zone_enter_price": 1.3525}],
        ),
        _board(
            "20260910-042",
            "2026-09-10T10:30:00Z",
            [_row("RAYSOLUSDT", "up", "QUALIFIED", last=1.3433, enter=1.3433)
             | {"final_zone": "QUALIFIED", "zone_enter_price": 1.3433}],
        ),
    ]
    trades = replay_boards(boards, zones=("CONFIRMED",))
    closed = [t for t in trades if t["status"] == "CLOSED"]
    assert len(closed) == 2
    first = _by_id(closed)["RAYSOLUSDT|up|CONFIRMED|20260910-018"]
    assert first["enter_price"] == 1.361
    assert first["enter_price_source"] == "zone_enter_price"
    second = _by_id(closed)["RAYSOLUSDT|up|CONFIRMED|20260910-036"]
    assert second["enter_price"] == 1.3525
    assert second["enter_price_source"] == "zone_enter_price"
    assert second["exit_price"] == 1.3433
    assert abs(second["pnl_pct"] - (1.3433 / 1.3525 - 1)) < 1e-12
    assert second["pnl_sign"] == -1


def test_y_dmr_overlay_splits_confirmed_occupancy_to_match_display_stay():
    """Y：进 DMR 会重打展示停留价；确认区占用必须在进出 DMR 时拆成两笔。"""
    boards = [
        _board(
            "s1",
            "2026-09-10T04:30:00Z",
            [_row("RAYSOLUSDT", "up", "CONFIRMED", last=1.361, enter=1.361)
             | {"final_zone": "CONFIRMED", "zone_enter_price": 1.361, "dmr_selected": True}],
        ),
        _board(
            "s2",
            "2026-09-10T07:00:00Z",
            [_row("RAYSOLUSDT", "up", "CONFIRMED", last=1.3589, enter=1.361)
             | {"final_zone": "CONFIRMED", "zone_enter_price": 1.3589, "dmr_selected": False}],
        ),
        _board(
            "s3",
            "2026-09-10T10:30:00Z",
            [_row("RAYSOLUSDT", "up", "QUALIFIED", last=1.3433, enter=1.3433)
             | {"final_zone": "QUALIFIED", "zone_enter_price": 1.3433}],
        ),
    ]
    trades = replay_boards(boards, zones=("CONFIRMED", "DMR"))
    conf_closed = [t for t in trades if t["zone"] == "CONFIRMED" and t["status"] == "CLOSED"]
    dmr_closed = [t for t in trades if t["zone"] == "DMR" and t["status"] == "CLOSED"]
    assert len(conf_closed) == 1
    assert conf_closed[0]["enter_scan_id"] == "s2"
    assert conf_closed[0]["enter_price"] == 1.3589
    assert conf_closed[0]["enter_price_source"] == "zone_enter_price"
    assert conf_closed[0]["exit_price"] == 1.3433
    assert len(dmr_closed) == 1
    assert dmr_closed[0]["enter_price"] == 1.361
    assert dmr_closed[0]["enter_price_source"] == "last_price"


def test_v14_dmr_does_not_split_confirmed_occupancy():
    """v1.4.0 无 zone_enter：DMR 不拆确认区占用，停留价仍是状态机入场价。"""
    boards = [
        _board("s1", "2026-09-10T04:30:00Z", [_row("BTWUSDT", "up", "CONFIRMED", last=0.46, enter=0.46, dmr=True)]),
        _board("s2", "2026-09-10T07:00:00Z", [_row("BTWUSDT", "up", "CONFIRMED", last=0.45, enter=0.46, dmr=False)]),
        _board("s3", "2026-09-10T10:30:00Z", [_row("BTWUSDT", "up", "QUALIFIED", last=0.44, enter=0.44)]),
    ]
    trades = replay_boards(boards, zones=("CONFIRMED", "DMR"))
    conf_closed = [t for t in trades if t["zone"] == "CONFIRMED" and t["status"] == "CLOSED"]
    assert len(conf_closed) == 1
    assert conf_closed[0]["enter_scan_id"] == "s1"
    assert conf_closed[0]["enter_price"] == 0.46
    assert conf_closed[0]["enter_price_source"] == "state_enter_price"
    assert conf_closed[0]["exit_price"] == 0.44


def test_inbox_marker_does_not_leak_onto_confirmed_stay():
    """The DMR inbox overlay writes onto the shared board row; the marker must
    stay on the DMR trade and not claim the CONFIRMED stay came from inbox."""
    from coin_selection.review_replay import overlay_dmr_from_inbox

    board = _board("s1", "2026-08-22T00:00:00Z", [_row("FFFUSDT", "up", "CONFIRMED", last=9.0, enter=9.0)])
    for pool in ("long_pool", "short_pool"):
        for r in board[pool]:
            r.pop("dmr_selected", None)
    inbox = {"rank": {"inbox_k": 16}, "candidates": [{"symbol": "FFFUSDT", "direction": "LONG"}]}
    patched = overlay_dmr_from_inbox(board, inbox)
    # The caller's board is untouched.
    assert "dmr_selected" not in board["long_pool"][0]
    assert patched["long_pool"][0]["dmr_selected"] is True

    exit_board = _board("s2", "2026-08-22T00:15:00Z", [_row("FFFUSDT", "up", "WATCH", last=9.9, enter=9.9)])
    trades = replay_boards([patched, exit_board], zones=("DMR", "CONFIRMED"))
    by_zone = {t["zone"]: t for t in trades}
    assert "DMR_MEMBER_FROM_INBOX" in by_zone["DMR"]["flags"]
    assert "DMR_MEMBER_FROM_INBOX" not in by_zone["CONFIRMED"]["flags"]


def test_missing_entry_price_counts_trade_but_null_pnl():
    boards = [
        _board(
            "s1",
            "2026-08-22T00:00:00Z",
            [_row("EEEUSDT", "up", "ELIMINATED", last=None, enter=None)],
        ),
        _board(
            "s2",
            "2026-08-22T00:15:00Z",
            [_row("EEEUSDT", "up", "WATCH", last=3.0, enter=3.0)],
        ),
    ]
    trades = replay_boards(boards, zones=("ELIMINATED",))
    closed = [t for t in trades if t["status"] == "CLOSED"]
    assert len(closed) == 1
    assert "MISSING_ENTRY_PRICE" in closed[0]["flags"]
    assert closed[0]["pnl_pct"] is None
    assert closed[0]["pnl_sign"] is None


def test_missing_node_is_flagged_by_sequence_not_by_minutes():
    """一个缺失节点必须打 GAP_BEFORE_EXIT。

    旧实现用 32 分钟墙钟阈值：缺 1 个节点 = 相邻两板相隔 30 分钟 < 32，
    永远看不见。生产窗口里三个真实缺口（20260815-020 / 20260817-005 /
    20260824-046）就是这样被漏掉的。
    """
    boards = [
        _board("20260824-045", "2026-08-24T11:15:00Z", [_row("AAAUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)]),
        # 20260824-046 不存在 —— 直接跳到 047，间隔 30 分钟
        _board("20260824-047", "2026-08-24T11:45:00Z", [_row("AAAUSDT", "up", "WATCH", last=11.0, enter=11.0)]),
    ]
    trades = replay_boards(boards, zones=("CONFIRMED",))
    closed = [t for t in trades if t["status"] == "CLOSED"]
    assert len(closed) == 1, closed
    assert "GAP_BEFORE_EXIT" in closed[0]["flags"], closed[0]["flags"]
    # 缺失节点绝不能被判成退出：这一笔仍然按真实时间戳算 30 分钟
    assert closed[0]["dwell_minutes"] == 30.0, closed[0]
    assert closed[0]["exit_scan_id"] == "20260824-047"


def test_consecutive_nodes_are_not_flagged():
    boards = [
        _board("20260824-045", "2026-08-24T11:15:00Z", [_row("BBBUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)]),
        _board("20260824-046", "2026-08-24T11:30:00Z", [_row("BBBUSDT", "up", "WATCH", last=11.0, enter=11.0)]),
    ]
    closed = [t for t in replay_boards(boards, zones=("CONFIRMED",)) if t["status"] == "CLOSED"]
    assert closed and "GAP_BEFORE_EXIT" not in closed[0]["flags"], closed


def test_restart_rewriting_the_same_node_is_not_a_gap():
    """重启补扫会重写当前节点。序号没断 = 没有缺口，绝不能误报。"""
    from coin_selection.review_replay import missing_nodes_between

    assert missing_nodes_between("20260824-045", "20260824-045") == 0
    assert missing_nodes_between("20260824-095", "20260825-000") == 0  # 跨 UTC 午夜
    assert missing_nodes_between("20260824-094", "20260825-000") == 1


def test_product_zone_drives_replay_not_sm_state():
    """ENABLE=1 快照：SM 仍 CONFIRMED、product_zone=QUALIFIED → 确认区账本不得开仓。"""
    from coin_selection.review_replay import in_zone

    row = _row("ZZZUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)
    row["product_zone"] = "QUALIFIED"
    assert in_zone(row, "QUALIFIED") is True
    assert in_zone(row, "CONFIRMED") is False
    boards = [
        _board("s1", "2026-08-22T00:00:00Z", [row]),
        _board(
            "s2",
            "2026-08-22T00:15:00Z",
            [_row("ZZZUSDT", "up", "WATCH", last=10.5, enter=10.5)],
        ),
    ]
    boards[1]["long_pool"][0]["product_zone"] = "WATCH"
    trades = replay_boards(boards, zones=("CONFIRMED", "QUALIFIED"))
    conf = [t for t in trades if t["zone"] == "CONFIRMED"]
    qual = [t for t in trades if t["zone"] == "QUALIFIED"]
    assert conf == []
    assert len(qual) == 1
    assert qual[0]["status"] == "CLOSED"


def test_legacy_snapshot_without_product_zone_falls_back_to_state():
    from coin_selection.review_replay import in_zone

    row = _row("YYYUSDT", "up", "CONFIRMED", last=1.0, enter=1.0)
    assert "product_zone" not in row or row.get("product_zone") is None
    assert in_zone(row, "CONFIRMED") is True


def test_dmr_still_reads_flag_when_product_zone_present():
    from coin_selection.review_replay import in_zone

    row = _row("XXXUSDT", "up", "CONFIRMED", last=1.0, enter=1.0, dmr=True)
    row["product_zone"] = "CONFIRMED"
    assert in_zone(row, "DMR") is True
    row["dmr_selected"] = False
    assert in_zone(row, "DMR") is False


def _board_ph(scan_id: str, ts: str, rows: list[dict], ph: str | None) -> dict:
    b = _board(scan_id, ts, rows, pv="param-v2.0.0-screener-y")
    b["meta"]["param_hash"] = ph
    return b


def test_t6_param_hash_changed_is_sticky_and_follows_entry():
    """跨 ``param_hash`` 的停留被打标，且黏性到 CLOSED 行（文档B §3.1 / 测试 T6）。"""
    from coin_selection.review_replay import OccupancyReplayer

    rp = OccupancyReplayer()
    row = _row("AAAUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)
    rp.step(_board_ph("20260901-001", "2026-09-01T00:15:00Z", [row], "pf1_aaaaaaaaaaaaaaaa"))
    # 第二个节点换了指纹（overrides 被调了一次）
    rp.step(_board_ph("20260901-002", "2026-09-01T00:30:00Z",
                      [_row("AAAUSDT", "up", "CONFIRMED", last=11.0, enter=10.0)],
                      "pf1_bbbbbbbbbbbbbbbb"))
    # 第三个节点掉出确认区 → 平仓
    out = rp.step(_board_ph("20260901-003", "2026-09-01T00:45:00Z",
                            [_row("AAAUSDT", "up", "WATCH", last=12.0, enter=10.0)],
                            "pf1_bbbbbbbbbbbbbbbb"))
    closed = [t for t in out if t["status"] == "CLOSED" and t["zone"] == "CONFIRMED"]
    assert closed, out
    t = closed[0]
    assert "PARAM_HASH_CHANGED" in t["flags"], t["flags"]
    # 这一笔保留**入场时**的指纹（它是在那套标准下被选中的）
    assert t["param_hash"] == "pf1_aaaaaaaaaaaaaaaa", t["param_hash"]
    assert "_enter_ph" not in t


def test_t6_no_flag_when_hash_is_stable():
    from coin_selection.review_replay import OccupancyReplayer

    rp = OccupancyReplayer()
    rp.step(_board_ph("20260901-001", "2026-09-01T00:15:00Z",
                      [_row("BBBUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)], "pf1_same"))
    out = rp.step(_board_ph("20260901-002", "2026-09-01T00:30:00Z",
                            [_row("BBBUSDT", "up", "WATCH", last=11.0, enter=10.0)], "pf1_same"))
    t = [x for x in out if x["status"] == "CLOSED" and x["zone"] == "CONFIRMED"][0]
    assert "PARAM_HASH_CHANGED" not in t["flags"], t["flags"]


def test_t6_missing_hash_is_not_a_change():
    """旧快照没有 param_hash（阶段0之前）—— 「没有指纹」不是「指纹变了」。"""
    from coin_selection.review_replay import OccupancyReplayer

    rp = OccupancyReplayer()
    rp.step(_board_ph("20260901-001", "2026-09-01T00:15:00Z",
                      [_row("CCCUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)], None))
    out = rp.step(_board_ph("20260901-002", "2026-09-01T00:30:00Z",
                            [_row("CCCUSDT", "up", "WATCH", last=11.0, enter=10.0)], None))
    t = [x for x in out if x["status"] == "CLOSED" and x["zone"] == "CONFIRMED"][0]
    assert "PARAM_HASH_CHANGED" not in t["flags"], t["flags"]
    assert t["param_hash"] is None


def test_combo_columns_land_on_the_trade_row():
    """账本新增 5 列里的 4 个组合列，由板面行原样带过来（文档B §10.2）。"""
    from coin_selection.review_replay import OccupancyReplayer

    row = _row("DDDUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)
    row.update(combo_code="ABC", combo_zone="CONFIRMED", zone_ceiling="QUALIFIED", z_score=1.2)
    rp = OccupancyReplayer()
    out = rp.step(_board_ph("20260901-001", "2026-09-01T00:15:00Z", [row], "pf1_x"))
    t = [x for x in out if x["zone"] == "CONFIRMED"][0]
    assert t["combo_code"] == "ABC"
    assert t["combo_zone"] == "CONFIRMED"
    assert t["zone_ceiling"] == "QUALIFIED"
    assert abs(t["z_score"] - 1.2) < 1e-9


def test_t16_bracket_pnl_matches_realized_pnl_direction_semantics():
    """括号出场的方向语义必须与 ``realized_pnl`` 一致（测试 T16）。

    up: exit/stay − 1 ； down: 1 − exit/stay。方向搞反会让下跌侧的胜率整体镜像。
    """
    from coin_selection.review_pnl import realized_pnl

    pct_up, sign_up = realized_pnl("up", 100.0, 102.0)
    pct_dn, sign_dn = realized_pnl("down", 100.0, 102.0)
    assert abs(pct_up - 0.02) < 1e-12 and sign_up == 1
    assert abs(pct_dn + 0.02) < 1e-12 and sign_dn == -1
    # 括号里 up 侧 +2% 命中止盈，同一价格在 down 侧是 −2% 命中止损：符号严格相反
    assert abs(pct_up + pct_dn) < 1e-12


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
