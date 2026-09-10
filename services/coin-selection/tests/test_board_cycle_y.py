"""「选币榜Y」24 小时时间区（周期）管理护栏。

要守住的：

  1. **刻度**：周期起点必须就是系统既有的每日锚点 00:00 UTC，不另造标准。
  2. **清空**：周期节点一到，全部分区的历史币种数据清零；历史快照与账本绝不动。
  3. **幂等**：同一周期内重复扫描（--force 重启 / 补扫 / 回放重跑）只清一次。
  4. **隔离**：「选币榜」没有周期，一个文件都不许碰。
  5. **重建**：清空后由实时扫描从零重建，节奏受 v1.4.0 时间门槛约束（可预测）。
  6. **warmup 白名单**：只能调时间门槛，硬门槛放宽不了。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.board_variants import (
    BOARD_Y_KEY,
    MAIN_KEY,
    CycleConfig,
    get_variant,
    variant_state_config,
)
from coin_selection.cycle_reset import load_cycle_state, maybe_reset
from coin_selection.scan import resolve_anchor_00utc
from coin_selection.state_machine import (
    StateMachineStore,
    apply_state_machine,
    default_state_config,
)

PV = "param-v2.0.0-screener-y"


def _now(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _seed_state_machine(data_dir: Path, n: int = 6) -> None:
    """造一份「昨天遗留」的状态机：各分区都有成员。"""
    states = {}
    zones = ["CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED", "DATA_INSUFFICIENT", "LOW_CONFIDENCE"]
    for i in range(n):
        for d in ("up", "down"):
            sym = f"T{i:02d}USDT"
            states[f"{sym}|{d}"] = {
                "symbol": sym,
                "direction": d,
                "state": zones[i % len(zones)],
                "state_enter_ts": _now("2026-08-24T09:00:00Z").timestamp(),
                "consecutive_pass": 3,
                "consecutive_fail": 0,
                "last_score": 71.0,
                "last_scan_id": "20260824-090",
                "confirmed_count": 2,
                "state_enter_price": 1.23,
                "last_path": "S",
            }
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "state_machine.json").write_text(
        json.dumps({"ts": 0.0, "parameter_version": PV, "states": states}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "daily_unique.json").write_text(
        json.dumps({"anchor_date": "2026-08-24", "confirmed": ["T00USDT"]}), encoding="utf-8"
    )


# ------------------------------------------------------------------ 1. 刻度
def test_cycle_start_is_the_existing_daily_anchor():
    """24h/00:00 的周期起点必须与 scan.resolve_anchor_00utc 逐秒相同。"""
    cyc = get_variant(BOARD_Y_KEY).cycle
    assert cyc.enabled and cyc.period_hours == 24 and cyc.anchor_utc == "00:00"
    t = _now("2026-08-25T00:00:00Z")
    for _ in range(96 * 3):  # 三整天，每 15 分钟一个点
        assert cyc.cycle_start(t) == resolve_anchor_00utc(t), t
        t += timedelta(minutes=15)
    assert cyc.nodes_per_cycle() == 96
    assert cyc.node_in_cycle(_now("2026-08-25T00:00:00Z")) == 0
    assert cyc.node_in_cycle(_now("2026-08-25T04:45:00Z")) == 19
    assert cyc.node_in_cycle(_now("2026-08-25T23:45:00Z")) == 95
    assert cyc.cycle_key(_now("2026-08-25T23:45:00Z")) == "20260825T0000Z"
    assert cyc.cycle_key(_now("2026-08-26T00:00:00Z")) == "20260826T0000Z"


def test_shorter_periods_stay_on_the_15m_grid():
    for hours, expect_starts in ((12, ["00:00", "12:00"]), (6, ["00:00", "06:00", "12:00", "18:00"])):
        cyc = CycleConfig(enabled=True, period_hours=hours)
        seen = set()
        t = _now("2026-08-25T00:00:00Z")
        for _ in range(96):
            seen.add(cyc.cycle_start(t).strftime("%H:%M"))
            t += timedelta(minutes=15)
        assert sorted(seen) == expect_starts, (hours, sorted(seen))
        assert cyc.nodes_per_cycle() == hours * 4
    # 非 15m 整倍数被拒，回落 24h，而不是产出半个节点的周期
    assert CycleConfig(enabled=True, period_hours=0).period_seconds() == 24 * 3600


# ------------------------------------------------------------ 2/3. 清空+幂等
def test_reset_clears_every_zone_and_is_idempotent():
    y = get_variant(BOARD_Y_KEY)
    tmp = Path(tempfile.mkdtemp(prefix="cycle-"))
    try:
        data = tmp / "coin-selection-y"
        _seed_state_machine(data)
        before = json.loads((data / "state_machine.json").read_text(encoding="utf-8"))["states"]
        assert len(before) == 12

        node0 = _now("2026-08-25T00:00:00Z")
        meta = maybe_reset(
            y, data_dir=data, now=node0, scan_id="20260825-000", parameter_version=PV
        )
        assert meta["enabled"] and meta["reset_at_this_node"] is True
        assert meta["cycle_key"] == "20260825T0000Z"
        assert meta["node_in_cycle"] == 0 and meta["nodes_per_cycle"] == 96

        # 全部分区清零 —— 这就是「清空所有分区内的历史币种数据」
        after = json.loads((data / "state_machine.json").read_text(encoding="utf-8"))["states"]
        assert after == {}, after
        assert not (data / "daily_unique.json").exists()
        assert meta["cleared"]["state_machine"] == 12
        assert sum(v for k, v in meta["closed_zones"].items() if k != "NONE") == 12

        # 收官名单留档（只读审计），不是复盘事实源
        arch = data / "cycle" / "20260825T0000Z-close.json"
        assert arch.is_file()
        doc = json.loads(arch.read_text(encoding="utf-8"))
        assert len(doc["members"]) == 12 and doc["closed_at_scan_id"] == "20260825-000"

        # 同周期内再跑（--force 重启 / 补扫）：不得再清一次
        StateMachineStore(data / "state_machine.json")  # 模拟本节点重建出的状态
        (data / "state_machine.json").write_text(
            json.dumps({"ts": 1.0, "states": {"REBUILTUSDT|up": {"symbol": "REBUILTUSDT",
             "direction": "up", "state": "WATCH", "state_enter_ts": node0.timestamp()}}}),
            encoding="utf-8",
        )
        again = maybe_reset(
            y, data_dir=data, now=node0 + timedelta(minutes=15),
            scan_id="20260825-001", parameter_version=PV,
        )
        assert again["reset_at_this_node"] is False
        kept = json.loads((data / "state_machine.json").read_text(encoding="utf-8"))["states"]
        assert "REBUILTUSDT|up" in kept, "同周期内重复扫描把重建结果又清掉了"
        assert again["node_in_cycle"] == 1
        assert again["last_reset_cycle_key"] == "20260825T0000Z"

        # 下一个周期节点：再清一次
        nxt = maybe_reset(
            y, data_dir=data, now=_now("2026-08-26T00:00:00Z"),
            scan_id="20260826-000", parameter_version=PV,
        )
        assert nxt["reset_at_this_node"] is True and nxt["cycle_key"] == "20260826T0000Z"
        assert load_cycle_state(data)["cycle_key"] == "20260826T0000Z"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reset_never_touches_history():
    """快照 / 账本 / inbox 是复盘的事实源，周期重置绝不能碰。"""
    y = get_variant(BOARD_Y_KEY)
    tmp = Path(tempfile.mkdtemp(prefix="cycle-"))
    try:
        data = tmp / "coin-selection-y"
        _seed_state_machine(data)
        (data / "snapshots").mkdir(parents=True, exist_ok=True)
        (data / "snapshots" / "20260824-095.json").write_text("{}", encoding="utf-8")
        (data / "review").mkdir(parents=True, exist_ok=True)
        (data / "review" / "ledger.sqlite").write_text("x", encoding="utf-8")
        (data / "latest.json").write_text("{}", encoding="utf-8")

        maybe_reset(y, data_dir=data, now=_now("2026-08-25T00:00:00Z"),
                    scan_id="20260825-000", parameter_version=PV)

        assert (data / "snapshots" / "20260824-095.json").is_file()
        assert (data / "review" / "ledger.sqlite").is_file()
        assert (data / "latest.json").is_file()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 4. 隔离
def test_main_board_has_no_cycle_and_is_never_touched():
    m = get_variant(MAIN_KEY)
    assert m.cycle.enabled is False
    tmp = Path(tempfile.mkdtemp(prefix="cycle-main-"))
    try:
        data = tmp / "coin-selection"
        _seed_state_machine(data)
        meta = maybe_reset(
            m, data_dir=data, now=_now("2026-08-25T00:00:00Z"),
            scan_id="20260825-000", parameter_version="param-v1.4.0-staircase-confirm-dmr",
        )
        assert meta == {"enabled": False}
        states = json.loads((data / "state_machine.json").read_text(encoding="utf-8"))["states"]
        assert len(states) == 12, "选币榜的状态机被周期逻辑动了"
        assert (data / "daily_unique.json").is_file()
        assert not (data / "cycle_state.json").exists()
        assert not (data / "cycle").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 5. 重建
def test_rebuild_schedule_after_reset_is_predictable():
    """清空后从 NONE 起步，v1.4.0 时间门槛下的重建节奏必须可预测。

    这段窗口不是 bug，是「日内从零重建」的必然代价 —— 钉住它，
    以后谁把 min_dwell/min_streak 改了，这里会红。
    """
    cfg = default_state_config()
    store = StateMachineStore(Path(tempfile.mkdtemp(prefix="sm-")) / "sm.json")
    anchor = _now("2026-08-25T00:00:00Z")
    row = {
        "symbol": "AAAUSDT", "liquidity_hard_pass": True, "last_price": 1.0,
        "score_up": 90.0, "ss_up": 80.0, "momentum_score_up": 90.0, "consistency_up": 1.0,
        "score_down": 5.0, "ss_down": 0.0, "momentum_score_down": 5.0, "consistency_down": 0.0,
        "data_quality_score": 95.0, "supply_missing": False, "data_mode": "LIVE",
    }
    first = {}
    for seq in range(12):
        now = anchor + timedelta(minutes=15 * seq)
        apply_state_machine([dict(row)], store, cfg,
                            scan_id=f"20260825-{seq:03d}", now_ts=now.timestamp())
        st = store.get("AAAUSDT", "up").state
        first.setdefault(st, seq)
    assert first.get("WATCH") == 1, first
    assert first.get("QUALIFIED") == 3, first
    assert first.get("CONFIRMED") == 5, first   # 01:15 UTC —— 周期头 75 分钟没有确认区


# --------------------------------------------------------- 6. warmup 白名单
def test_warmup_can_only_relax_time_gates():
    y = get_variant(BOARD_Y_KEY)
    tuned = replace(
        y,
        cycle=replace(
            y.cycle,
            warmup_enabled=True,
            warmup_nodes=4,
            warmup_state_config={
                "min_dwell_watch": 0,
                "min_streak_watch": 1,
                # 下面这些是硬门槛，必须被拒
                "enter_confirmed": 10,
                "ss_confirmed": 0,
                "cons_confirmed": 0.0,
                "dq_confirm_floor": 0,
                "dmr_score": 0,
            },
        ),
    )
    allowed = tuned.cycle.warmup_overrides()
    assert set(allowed) == {"min_dwell_watch", "min_streak_watch"}, allowed

    warm = variant_state_config(tuned, warmup=True)
    cold = variant_state_config(tuned, warmup=False)
    assert warm.min_dwell_watch == 0 and warm.min_streak_watch == 1
    # 硬门槛在两种模式下都必须是 v1.4.0 的生产值
    for c in (warm, cold):
        assert c.enter_confirmed == 66 and c.ss_confirmed == 50
        assert c.cons_confirmed == 0.67 and c.dq_confirm_floor == 60 and c.dmr_score == 70
    assert cold.min_dwell_watch == 30 and cold.min_streak_watch == 2

    # 窗口边界
    assert tuned.cycle.in_warmup(_now("2026-08-25T00:00:00Z")) is True   # node 0
    assert tuned.cycle.in_warmup(_now("2026-08-25T00:45:00Z")) is True   # node 3
    assert tuned.cycle.in_warmup(_now("2026-08-25T01:00:00Z")) is False  # node 4
    # 默认关闭
    assert get_variant(BOARD_Y_KEY).cycle.in_warmup(_now("2026-08-25T00:00:00Z")) is False


# ------------------------------------------------- 7. 复盘：CYCLE_RESET 旗标
def _board(scan_id: str, ts: str, *, rows: list[dict], cycle: dict | None = None) -> dict:
    meta = {
        "scan_id": scan_id,
        "scan_timestamp_utc": ts,
        "parameter_version": PV,
        "anchor_date": ts[:10],
    }
    if cycle is not None:
        meta["cycle"] = cycle
    return {
        "meta": meta,
        "long_pool": [r for r in rows if r["direction"] == "up"],
        "short_pool": [r for r in rows if r["direction"] == "down"],
        "transitions": [],
    }


def _row(state: str, px: float, *, dmr: bool = False) -> dict:
    return {
        "symbol": "AAAUSDT", "direction": "up", "state": state,
        "last_price": px, "state_enter_price": px, "dmr_selected": dmr,
        "canonical_asset_id": "aaa", "underlying_asset": "AAA", "contract_multiplier": 1,
        "score_up": 80.0, "score_down": 20.0,
    }


def _replay(boards: list[dict]) -> list[dict]:
    from coin_selection.review_replay import OccupancyReplayer

    rp = OccupancyReplayer()
    out = []
    for b in boards:
        out.extend(rp.step(b))
    return out


def test_cycle_reset_close_is_flagged_and_replaces_vanished():
    """周期重置那一节点的平仓打 CYCLE_RESET，且**取代** VANISHED。

    重置后币会整个从板面消失（它不在任何分区里，而板面只登载分区成员）。
    那不是「板面莫名其妙丢了一行」的数据异常，原因已知 —— 沿用 VANISHED
    会让「仅显示异常」被每日上千笔强平淹掉。
    """
    cyc_mid = {"enabled": True, "cycle_key": "20260824T0000Z", "reset_at_this_node": False}
    cyc_reset = {"enabled": True, "cycle_key": "20260825T0000Z", "reset_at_this_node": True}
    closed = [
        t
        for t in _replay(
            [
                _board("20260824-094", "2026-08-24T23:30:00Z", rows=[_row("CONFIRMED", 1.00)], cycle=cyc_mid),
                _board("20260824-095", "2026-08-24T23:45:00Z", rows=[_row("CONFIRMED", 1.10)], cycle=cyc_mid),
                # 重置节点：币不在任何分区里 → 整行从板面消失
                _board("20260825-000", "2026-08-25T00:00:00Z", rows=[], cycle=cyc_reset),
            ]
        )
        if t.get("status") == "CLOSED"
    ]
    assert len(closed) == 1, closed
    t = closed[0]
    assert "CYCLE_RESET" in t["flags"], t["flags"]
    assert "VANISHED" not in t["flags"], t["flags"]
    assert t["exit_scan_id"] == "20260825-000"
    # 板面上已经没有这一行了，按约定用上一个节点的最后一次打印定价
    assert t["exit_price_source"] == "prev_node"
    assert abs(t["exit_price"] - 1.10) < 1e-12


def test_board_without_cycle_still_reports_vanished():
    """「选币榜」的板面没有 meta.cycle —— 这条分支对它恒为 False，VANISHED 语义不变。"""
    closed = [
        t
        for t in _replay(
            [
                _board("20260824-094", "2026-08-24T23:30:00Z", rows=[_row("CONFIRMED", 1.00)]),
                _board("20260824-095", "2026-08-24T23:45:00Z", rows=[]),
            ]
        )
        if t.get("status") == "CLOSED"
    ]
    assert len(closed) == 1
    assert "VANISHED" in closed[0]["flags"]
    assert "CYCLE_RESET" not in closed[0]["flags"]


def test_exclude_flags_filters_forced_closes_out_of_the_stats():
    """强平能被整段剔除 —— 否则 v2.0.0 的胜率里混着每天上千笔非策略退出。"""
    from coin_selection.review_ledger import ReviewLedger

    tmp = Path(tempfile.mkdtemp(prefix="ledger-cyc-"))
    try:
        led = ReviewLedger(tmp / "l.sqlite")
        cyc_mid = {"enabled": True, "cycle_key": "20260824T0000Z", "reset_at_this_node": False}
        cyc_reset = {"enabled": True, "cycle_key": "20260825T0000Z", "reset_at_this_node": True}
        for b in (
            _board("20260824-094", "2026-08-24T23:30:00Z", rows=[_row("CONFIRMED", 1.00, dmr=True)], cycle=cyc_mid),
            _board("20260824-095", "2026-08-24T23:45:00Z", rows=[_row("CONFIRMED", 1.10, dmr=True)], cycle=cyc_mid),
            _board("20260825-000", "2026-08-25T00:00:00Z", rows=[], cycle=cyc_reset),
        ):
            led.ingest_board(b)
        kw = dict(zones=["CONFIRMED"], direction="both", symbols=None,
                  start_utc=None, end_utc=None, attribution="exit")
        assert led.count_trades(include_open=False, min_dwell_nodes=0, **kw) == 1
        assert led.count_trades(include_open=False, min_dwell_nodes=0,
                                exclude_flags=["CYCLE_RESET"], **kw) == 0
        s_all = led.summarize(**kw, min_dwell_nodes=0)
        s_net = led.summarize(**kw, min_dwell_nodes=0, exclude_flags=["CYCLE_RESET"])
        assert s_all["trades"] == 1 and s_net["trades"] == 0
        assert (s_all.get("flag_counts") or {}).get("CYCLE_RESET") == 1
        assert "CYCLE_RESET" not in (s_net.get("flag_counts") or {})
        led.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------- 8. 复盘窗口必须落在周期栅格上
def _review_api():
    """复盘查询层在 api-gateway 里，测试要显式把它加进 path。"""
    import importlib

    gw = Path(__file__).resolve().parents[3] / "services" / "api-gateway"
    if str(gw) not in sys.path:
        sys.path.insert(0, str(gw))
    return importlib.import_module("review_api")


def test_cycle_window_partitions_trades_without_overlap_or_gap():
    """相邻周期的窗口既不重叠也不漏。

    24h 重置保证「没有任何已平仓交易能跨周期」：
    周期 C 的交易满足 enter ∈ [C_start, C_end) 且 exit ∈ (C_start, C_end]。
    所以窗口按归属口径取不同的闭合方向 —— 取错了，00:00:00 的重置强平
    要么被两个周期各算一遍，要么两个都不算。
    """
    api = _review_api()
    cfg = get_variant(BOARD_Y_KEY).cycle
    ref = "2026-08-25T07:00:00Z"

    expect = {
        # (口径, whole) -> (from, to)
        ("exit", True): ("2026-08-24T00:00:01Z", "2026-08-25T00:00:00Z"),
        ("exit", False): ("2026-08-25T00:00:01Z", "2026-08-25T07:00:00Z"),
        ("enter", True): ("2026-08-24T00:00:00Z", "2026-08-24T23:59:59Z"),
        ("enter", False): ("2026-08-25T00:00:00Z", "2026-08-25T06:59:59Z"),
        ("contained", True): ("2026-08-24T00:00:00Z", "2026-08-25T00:00:00Z"),
        ("contained", False): ("2026-08-25T00:00:00Z", "2026-08-25T07:00:00Z"),
    }
    for (attrib, whole), (want_from, want_to) in expect.items():
        w = api.cycle_window(cfg, end_ref=ref, cycles=1, whole=whole, attribution=attrib)
        assert (w["from"], w["to"]) == (want_from, want_to), (attrib, whole, w)

    # 相邻的两个完整周期不得重叠（exit 口径下，上一个的 to 必须严格早于下一个的 from）
    prev = api.cycle_window(cfg, end_ref=ref, cycles=1, whole=True, attribution="exit")
    cur = api.cycle_window(cfg, end_ref=ref, cycles=1, whole=False, attribution="exit")
    assert prev["to"] < cur["from"], (prev["to"], cur["from"])
    # 也不得留空洞：上一个的 to 就是下一个 from 的前一秒
    assert prev["to"] == "2026-08-25T00:00:00Z" and cur["from"] == "2026-08-25T00:00:01Z"


def test_cycle_window_spans_n_cycles_from_the_grid_not_the_watermark():
    """N 个周期是从周期起点往前数的，绝不是「水位往前推 N×24h」。"""
    api = _review_api()
    cfg = get_variant(BOARD_Y_KEY).cycle
    # 水位停在 07:00 —— 滚动口径会给出 08-18T07:00，周期口径必须给 08-19T00:00
    w = api.cycle_window(
        cfg, end_ref="2026-08-25T07:00:00Z", cycles=7, whole=False, attribution="exit"
    )
    assert w["window_start_utc"] == "2026-08-19T00:00:00Z", w
    assert w["first_cycle_key"] == "20260819T0000Z" and w["last_cycle_key"] == "20260825T0000Z"
    assert w["includes_partial_current"] is True

    whole = api.cycle_window(
        cfg, end_ref="2026-08-25T07:00:00Z", cycles=7, whole=True, attribution="exit"
    )
    assert whole["window_start_utc"] == "2026-08-18T00:00:00Z", whole
    assert whole["window_end_utc"] == "2026-08-25T00:00:00Z"
    assert whole["first_cycle_key"] == "20260818T0000Z"
    assert whole["last_cycle_key"] == "20260824T0000Z"
    assert whole["includes_partial_current"] is False

    # 水位正好压在周期起点上（000 号节点刚入账）：当前周期一秒都没跑，是**空的**，
    # 不是「跑了一半」。窗口天然为空区间，但标签必须仍指向当前周期。
    edge = api.cycle_window(
        cfg, end_ref="2026-08-25T00:00:00Z", cycles=1, whole=False, attribution="exit"
    )
    assert edge["includes_partial_current"] is False, edge
    assert edge["empty_current_cycle"] is True, edge
    assert edge["first_cycle_key"] == "20260825T0000Z", edge
    assert edge["last_cycle_key"] == "20260825T0000Z", edge
    assert edge["from"] > edge["to"], edge   # 空区间：SQL 返回 0 行，正确

    # 正常进行中的周期不该被误标成空
    mid = api.cycle_window(
        cfg, end_ref="2026-08-25T00:15:00Z", cycles=1, whole=False, attribution="exit"
    )
    assert mid["empty_current_cycle"] is False and mid["includes_partial_current"] is True


def test_cycle_coverage_reports_progress_and_complete_cycles():
    api = _review_api()
    cfg = get_variant(BOARD_Y_KEY).cycle
    cov = api.cycle_coverage(
        cfg,
        {"watermark_ts": "2026-08-25T06:00:00Z", "first_ts": "2026-08-15T00:15:00Z"},
    )
    assert cov["enabled"] and cov["period_hours"] == 24 and cov["nodes_per_cycle"] == 96
    assert cov["current_key"] == "20260825T0000Z"
    assert cov["current_start_utc"] == "2026-08-25T00:00:00Z"
    assert cov["current_complete"] is False
    assert abs(cov["current_elapsed_hours"] - 6.0) < 1e-9
    # 账本起点 08-15T00:15 落在周期中间 → 那个周期残缺，不算完整周期
    assert cov["first_cycle_partial"] is True
    assert cov["complete_cycles"] == 9, cov      # 0816..0824
    # 没有时间区的板面
    assert api.cycle_coverage(None, {}) == {"enabled": False}


def test_main_board_has_no_review_cycle():
    """「选币榜」的复盘窗口口径一个字都不许变。"""
    api = _review_api()
    assert api.board_cycle(MAIN_KEY) is None
    assert api.board_cycle(BOARD_Y_KEY) is not None
    # cycles 参数对主板面是空操作：review_payload 只在 board_cycle 非 None 时才对齐
    qs = {"cycles": ["7"], "whole_cycles": ["1"]}
    parsed = api.parse_review_qs(qs)
    assert parsed["cycles"] == 7 and parsed["whole_cycles"] is True
    assert api.parse_review_qs({})["cycles"] is None


def test_t15_replayer_cycle_nodes_match_cycle_reset(  ):
    """回放器的周期语义必须与 ``cycle_reset.maybe_reset`` 逐节点一致（测试 T15）。

    回放器不能调 ``maybe_reset``（那会写 ``cycle_state.json`` 到现网目录），
    所以它自己判断「这个节点是不是周期节点」。两者只要错开一个节点，回放出来的
    ``CYCLE_RESET`` 笔数就会整体偏移，主口径「排除 CR」也就跟着错。
    """
    import importlib.util
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "_rr_cycle", root / "scripts" / "replay_rescore.py"
    )
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)

    from coin_selection.board_variants import BOARD_Y_KEY, get_variant

    cyc = get_variant(BOARD_Y_KEY).cycle
    assert cyc.enabled and cyc.period_hours == 24 and cyc.anchor_utc == "00:00"

    anchor_min = cyc.anchor_minutes()
    start = datetime(2026, 8, 15, tzinfo=timezone.utc)
    hits_replay = []
    hits_cycle = []
    for i in range(96 * 3):  # 三整天，逐 15m 节点
        ts = start + timedelta(minutes=15 * i)
        if rr._is_cycle_node(ts, cyc.period_hours, anchor_min):
            hits_replay.append(ts)
        if cyc.node_in_cycle(ts) == 0:
            hits_cycle.append(ts)
    assert hits_replay == hits_cycle, (hits_replay[:3], hits_cycle[:3])
    assert len(hits_replay) == 3, hits_replay          # 三天 = 三个 00:00 UTC 节点
    assert all(t.hour == 0 and t.minute == 0 for t in hits_replay)


def test_t15_scan_id_to_timestamp_is_the_15m_grid():
    import importlib.util
    from datetime import datetime, timezone
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "_rr_grid", root / "scripts" / "replay_rescore.py"
    )
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)

    assert rr._node_ts("20260815-000") == datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)
    assert rr._node_ts("20260815-001") == datetime(2026, 8, 15, 0, 15, tzinfo=timezone.utc)
    assert rr._node_ts("20260815-095") == datetime(2026, 8, 15, 23, 45, tzinfo=timezone.utc)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
