"""每 15 分钟节点：所有对外时间戳与停留时长必须落在 15m 栅格上.

The bug this pins down: `scan_id` was always quantized (`seq = ⌊(now-anchor)/900⌋`)
but `scan_timestamp_utc` and the state-machine clock were raw wall clock. Any
off-cadence run — `--force`, a restart, a slow loop wake — stamped e.g. 17:52:14
into node 071 (whose real node is 17:45:00), and every dwell measured against
that stamp inherited the drift forever. That is what surfaced in the 停留时间
column as `38m` / `1h2m` instead of `30m` / `1h`.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.scan import (  # noqa: E402
    board_from_rows,
    make_scan_id,
    node_overrun,
    node_time,
    pending_node_id,
    resolve_anchor_00utc,
    scan_sequence,
)
from coin_selection.state_machine import (  # noqa: E402
    SCAN_INTERVAL_SEC,
    StateMachineStore,
    SymState,
    apply_state_machine,
    default_state_config,
    floor_to_node,
    quantize_enter_times,
    set_clock,
)

GRID_MIN = SCAN_INTERVAL_SEC // 60  # 15


def _row(symbol: str = "GRIDUSDT") -> dict:
    return {
        "symbol": symbol,
        "base_asset": symbol.replace("USDT", ""),
        "last_price": 1.0,
        "score_up": 90,
        "ss_up": 60,
        "momentum_score_up": 80,
        "consistency_up": 1.0,
        "score_down": 5,
        "ss_down": 0,
        "momentum_score_down": 5,
        "consistency_down": 0.0,
        "mcap_momentum_score_up": 60,
        "mcap_momentum_score_down": 40,
        "liquidity_score_abs": 80,
        "liquidity_grade": "A",
        "data_quality_score": 95,
        "supply_missing": False,
        "circulating_supply": 1e9,
        "liquidity_hard_pass": True,
        "data_mode": "LIVE",
        "ret_1h": 0.01,
        "ret_4h": 0.02,
        "ret_24h": 0.03,
        "ret_15m": None,
        "ret_since_anchor": 0.01,
    }


def test_floor_to_node_matches_scan_sequence_node():
    """Absolute flooring must agree with anchor+seq*900 for every offset in a day."""
    day = datetime(2026, 8, 21, tzinfo=timezone.utc)
    for offset_s in (0, 1, 59, 5, 434, 876, 805, 899, 900, 86399):
        t = day + timedelta(seconds=offset_s)
        anchor = resolve_anchor_00utc(t)
        seq = scan_sequence(t, anchor)
        assert floor_to_node(t.timestamp()) == node_time(anchor, seq).timestamp(), offset_s


def test_node_time_is_always_on_the_quarter_hour():
    day = datetime(2026, 8, 21, tzinfo=timezone.utc)
    for seq in range(96):
        n = node_time(day, seq)
        assert n.second == 0 and n.microsecond == 0
        assert n.minute % GRID_MIN == 0, n
    assert make_scan_id(day, 95) == "20260821-095"


def test_off_cadence_run_still_stamps_the_node():
    """A --force run at 17:52:14 belongs to node 071 = 17:45:00, not 17:52:14."""
    wall = datetime(2026, 8, 21, 17, 52, 14, tzinfo=timezone.utc)
    anchor = resolve_anchor_00utc(wall)
    seq = scan_sequence(wall, anchor)
    node = node_time(anchor, seq)
    assert seq == 71
    assert node == datetime(2026, 8, 21, 17, 45, 0, tzinfo=timezone.utc)


def test_enter_stamp_snaps_even_from_an_off_grid_tick():
    """Drive the SM from off-cadence clocks; dwell must still be a 15m multiple."""
    row = _row()
    with tempfile.TemporaryDirectory() as d:
        store = StateMachineStore(Path(d) / "s.json")
        base = floor_to_node(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp())
        # +37s, +412s, +881s … deliberately ragged wall clocks
        for i, jitter in enumerate((37, 412, 881, 5, 623, 44, 790)):
            apply_state_machine(
                [row],
                store,
                cfg=default_state_config(),
                scan_id=f"t{i}",
                now_ts=base + i * SCAN_INTERVAL_SEC + jitter,
            )
            dwell = row["state_up_dwell_min"]
            assert abs(dwell % GRID_MIN) < 1e-6, (i, row["state_up"], dwell)
    set_clock(None)


def test_quantize_migration_pulls_legacy_stamps_back():
    with tempfile.TemporaryDirectory() as d:
        store = StateMachineStore(Path(d) / "s.json")
        node = floor_to_node(datetime(2026, 8, 21, 17, 45, tzinfo=timezone.utc).timestamp())
        st = store.get("LEGACYUSDT", "up")
        st.state = "QUALIFIED"
        st.state_enter_ts = node + 434  # the real 17:52:14 drift seen in production
        moved = quantize_enter_times(store)
        assert moved == 1
        assert st.state_enter_ts == node
        # idempotent
        assert quantize_enter_times(store) == 0


def test_board_enter_time_lands_on_the_grid():
    """state_enter_time_utc is derived as node − dwell; both must be on-grid."""
    row = _row()
    row.update(
        {
            "state_up": "CONFIRMED",
            "state_down": "WATCH",
            "state_up_dwell_min": 45.0,
            "state_down_dwell_min": 15.0,
            "state_up_streak": 3,
            "state_down_streak": 1,
            "confirmed_path_up": "S",
            "qualified_path_up": "S",
        }
    )
    node = datetime(2026, 8, 21, 18, 15, 0, tzinfo=timezone.utc)
    pool = board_from_rows([row], "up", now=node)
    enter = datetime.fromisoformat(pool[0]["state_enter_time_utc"].replace("Z", "+00:00"))
    assert enter == datetime(2026, 8, 21, 17, 30, 0, tzinfo=timezone.utc)
    assert enter.minute % GRID_MIN == 0 and enter.second == 0
    assert pool[0]["state_duration_minutes"] % GRID_MIN == 0


def test_dwell_gates_are_exact_at_the_boundary():
    """Quantization also removes the 29.98min near-miss that used to cost a node."""
    st = SymState(symbol="X", direction="up", state="WATCH")
    base = floor_to_node(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp())
    st.state_enter_ts = base
    from coin_selection.state_machine import dwell_minutes

    assert dwell_minutes(st, base + 2 * SCAN_INTERVAL_SEC) == 30.0
    assert dwell_minutes(st, base + 2 * SCAN_INTERVAL_SEC) >= 30  # min_dwell_watch
    assert dwell_minutes(st, base + SCAN_INTERVAL_SEC) == 15.0


def test_pending_node_id_tracks_the_wall_clock():
    at = lambda h, m, s: datetime(2026, 8, 24, h, m, s, tzinfo=timezone.utc)
    assert pending_node_id(at(11, 15, 0)) == "20260824-045"
    assert pending_node_id(at(11, 29, 59)) == "20260824-045"
    assert pending_node_id(at(11, 30, 0)) == "20260824-046"
    assert pending_node_id(at(11, 45, 5)) == "20260824-047"
    assert pending_node_id(at(0, 0, 0)) == "20260824-000"
    assert pending_node_id(at(23, 59, 59)) == "20260824-095"


def test_overrun_detects_the_node_the_loop_used_to_skip():
    """The exact incident: a restart scan started in node 045 and finished at
    11:30:05, already inside node 046. The old loop slept a full 900s from
    there and node 046 was never written — 20260815-020 / 20260817-005 /
    20260824-046 were all lost this way."""
    finished = datetime(2026, 8, 24, 11, 30, 5, tzinfo=timezone.utc)
    assert node_overrun("20260824-045", finished) == "20260824-046"

    # Normal cadence: scan starts at :00:05 and ends well inside its own node.
    assert node_overrun("20260824-045", datetime(2026, 8, 24, 11, 17, 25, tzinfo=timezone.utc)) is None
    # Boundary is exclusive on the low side: still inside node 045 at 11:29:59.
    assert node_overrun("20260824-045", datetime(2026, 8, 24, 11, 29, 59, tzinfo=timezone.utc)) is None
    # A failed scan reports no scan_id — must not trigger a hot retry loop.
    assert node_overrun(None, finished) is None
    assert node_overrun("", finished) is None


def test_overrun_survives_the_utc_midnight_anchor_rollover():
    """`seq` resets to 0 at 00:00 UTC, so a numeric compare would read node 000
    as *behind* node 095 and skip the first node of every day."""
    just_after_midnight = datetime(2026, 8, 25, 0, 0, 3, tzinfo=timezone.utc)
    assert node_overrun("20260824-095", just_after_midnight) == "20260825-000"
    # And the reverse must not fire: clock still inside the scanned node.
    assert node_overrun("20260825-000", datetime(2026, 8, 25, 0, 10, 0, tzinfo=timezone.utc)) is None


def test_overrun_never_fires_when_the_clock_runs_backwards():
    """An NTP step backwards must fall through to the normal sleep, not spin."""
    assert node_overrun("20260824-047", datetime(2026, 8, 24, 11, 20, 0, tzinfo=timezone.utc)) is None


def _drive_loop(scan_duration_sec: int, stop_after: int) -> list[str]:
    """跑真实的 `main() --loop` 循环，但换掉时钟、扫描函数与 sleep。

    单测只能证明 `node_overrun` 算得对；这里证明**循环真的用了它**。
    """
    import os
    import time as _time
    import coin_selection.scan as scan

    clock = {"t": datetime(2026, 8, 24, 11, 28, 33, tzinfo=timezone.utc)}
    scanned: list[str] = []

    def fake_now() -> datetime:
        return clock["t"]

    def fake_scan(settings=None, **kw):
        sid = scan.pending_node_id(clock["t"])
        scanned.append(sid)
        clock["t"] += timedelta(seconds=scan_duration_sec)   # 扫描耗时
        return {"status": "ok", "scan_id": sid}

    class _Stop(Exception):
        pass

    def fake_sleep(sec):
        clock["t"] += timedelta(seconds=sec)
        if len(scanned) >= stop_after:
            raise _Stop()

    real_now, real_scan, real_sleep = scan.utc_now, scan.run_scan_cycle, _time.sleep
    real_argv = sys.argv
    tmp = tempfile.mkdtemp(prefix="loopfix_")
    real_env = os.environ.get("COIN_SELECTION_DATA_DIR")
    try:
        os.environ["COIN_SELECTION_DATA_DIR"] = tmp
        scan.utc_now = fake_now
        scan.run_scan_cycle = fake_scan
        _time.sleep = fake_sleep
        sys.argv = ["coin_selection", "--loop", "--force"]
        try:
            scan.main()
        except _Stop:
            pass
    finally:
        scan.utc_now, scan.run_scan_cycle = real_now, real_scan
        _time.sleep = real_sleep
        sys.argv = real_argv
        if real_env is None:
            os.environ.pop("COIN_SELECTION_DATA_DIR", None)
        else:
            os.environ["COIN_SELECTION_DATA_DIR"] = real_env
    return scanned


def test_loop_rescans_the_node_it_overran_instead_of_sleeping_past_it():
    """复现并证伪 20260824-046 事故。

    11:28:33 起扫（节点 045），耗时 92s → 11:30:05 结束，此刻已在节点 046。
    旧循环 sleep 900s 直接跳到 047，046 永久丢失。修复后必须立刻补扫 046。
    """
    scanned = _drive_loop(scan_duration_sec=92, stop_after=3)
    assert scanned[0] == "20260824-045", scanned
    assert scanned[1] == "20260824-046", f"越界的节点没有被补扫：{scanned}"
    assert scanned[2] == "20260824-047", scanned
    # 没有任何节点被跳过
    assert scanned == sorted(set(scanned)), scanned


def test_loop_sleeps_normally_when_the_scan_fits_inside_its_node():
    """正常节拍不得因为这次修复而变成连续扫描。"""
    scanned = _drive_loop(scan_duration_sec=150, stop_after=3)   # 2.5 分钟，不越界
    assert scanned == ["20260824-045", "20260824-046", "20260824-047"], scanned


def test_loop_always_takes_the_current_node_when_scans_exceed_the_cadence():
    """单次扫描比一个节点还长时，丢节点是**物理上无法避免**的——那一刻已经过去了。

    这条修复保证的不是「永不丢节点」，而是「绝不 sleep 过一个仍然当前的节点」：
    每一轮都去扫此刻**当前**的节点，而不是滞后的旧节点，也不会像旧循环那样
    在还来得及的时候把节点睡过去。
    """
    scanned = _drive_loop(scan_duration_sec=1000, stop_after=4)   # 16.7 分钟 > 15 分钟
    assert scanned == ["20260824-045", "20260824-047", "20260824-048", "20260824-049"], scanned
    assert scanned == sorted(scanned), "节点必须单调推进"
    assert len(set(scanned)) == len(scanned), "同一节点不得重复扫描"
    # 046 丢了，但那是因为 scan1 跑到 11:45:13 才结束，11:30 的时刻早已过去；
    # 关键是此后每一步都紧贴当前节点，没有再被 sleep 跳过任何一个。
    assert all(b > a for a, b in zip(scanned, scanned[1:]))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
