"""同一个节点被跑两次，不得推进状态机（连击门槛不可被重跑绕过）。

生产跑 `--loop --force`，--force 短路了 scan.py 唯一的每节点锁，于是一次重启就会
把同一个 scan_id 再跑一遍。此前 `consecutive_pass` 按**执行次数**累加，重跑一次就
等于白送一个节点的连击 —— 实测 20260901-004 第一次 conf_unique=0、重启后第二次
conf_unique=8，比 75 分钟下限早了一个节点。

复盘账本早有同款水位线守卫；这套用例把状态机侧的守卫钉死。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.state_machine import (  # noqa: E402
    StateMachineStore,
    SymState,
    apply_state_machine,
    default_state_config,
)


def _store():
    tmp = Path(tempfile.mkdtemp(prefix="nodeidem-"))
    return StateMachineStore(tmp / "state_machine.json")


def _row(sym="TESTUSDT"):
    """一行高分币：各项都远超门槛，只剩时间/连击闸门。"""
    return {
        "symbol": sym,
        "score_up": 95.0, "ss_up": 90.0, "momentum_score_up": 90.0, "consistency_up": 1.0,
        "score_down": 0.0, "ss_down": 0.0, "momentum_score_down": 0.0, "consistency_down": 0.0,
        "data_quality_score": 95.0, "supply_missing": False, "data_mode": "LIVE",
        "liquidity_hard_pass": True, "last_price": 1.0,
    }


def _seed_qualified(store, sym="TESTUSDT"):
    """把 up 侧放在「已在 QUALIFIED、停留够、连击 0」——差一个节点就能确认。"""
    st = store.get(sym, "up")
    st.state = "QUALIFIED"
    st.consecutive_pass = 0
    st.state_enter_ts = time.time() - 20 * 60      # > min_dwell_qualified=15
    st.last_scan_id = "20260901-003"
    return st


def test_same_node_twice_does_not_advance_the_state_machine():
    cfg = default_state_config()
    store = _store()
    _seed_qualified(store)

    apply_state_machine([_row()], store, cfg=cfg, scan_id="20260901-004")
    st = store.get("TESTUSDT", "up")
    assert st.state == "QUALIFIED", f"一个节点不该确认，实为 {st.state}"
    assert st.consecutive_pass == 1, st.consecutive_pass

    # 重启后同一个节点又跑了一遍 —— 必须是 no-op
    apply_state_machine([_row()], store, cfg=cfg, scan_id="20260901-004")
    st = store.get("TESTUSDT", "up")
    assert st.state == "QUALIFIED", f"重跑同一节点竟然晋级到 {st.state}（连击被绕过）"
    assert st.consecutive_pass == 1, f"连击被重复累加: {st.consecutive_pass}"


def test_next_distinct_node_still_advances():
    """守卫只挡重跑，不得挡住正常推进。"""
    cfg = default_state_config()
    store = _store()
    _seed_qualified(store)

    apply_state_machine([_row()], store, cfg=cfg, scan_id="20260901-004")
    apply_state_machine([_row()], store, cfg=cfg, scan_id="20260901-004")   # 重跑，no-op
    apply_state_machine([_row()], store, cfg=cfg, scan_id="20260901-005")   # 真正的下一个节点
    st = store.get("TESTUSDT", "up")
    assert st.state == "CONFIRMED", f"下一个节点应当确认，实为 {st.state}"


def test_guard_does_not_kill_the_sm_fast_cascade():
    """SM_FAST 的级联刻意用同一个 scan_id 连调 4 次，守卫不得误伤它。

    这正是守卫必须放在 apply_state_machine 而不是 transition_one 的原因。
    """
    cfg = default_state_config()
    store = _store()
    prev = os.environ.get("SM_FAST")
    os.environ["SM_FAST"] = "1"
    try:
        cfg_fast = default_state_config()   # SM_FAST 下门槛被放宽
        apply_state_machine([_row()], store, cfg=cfg_fast, scan_id="20260901-004")
        st = store.get("TESTUSDT", "up")
        assert st.state != "NONE", "SM_FAST 级联被守卫杀掉了"
        assert st.state in ("WATCH", "QUALIFIED", "CONFIRMED"), st.state
    finally:
        if prev is None:
            os.environ.pop("SM_FAST", None)
        else:
            os.environ["SM_FAST"] = prev


def test_empty_scan_id_is_never_treated_as_a_replay():
    """scan_id 为空时不得误判成重跑（否则状态机整个停摆）。"""
    cfg = default_state_config()
    store = _store()
    st = store.get("TESTUSDT", "up")
    st.last_scan_id = ""
    apply_state_machine([_row()], store, cfg=cfg, scan_id="")
    assert store.get("TESTUSDT", "up").consecutive_pass == 1


# ---------------------------------------------------------------------------
# 上游锁：--force 可以越过「陈旧的进行中锁」，但不得越过「本节点已完成」
# ---------------------------------------------------------------------------
def test_force_may_bypass_a_stale_inflight_lock():
    """--force 存在的理由：进程崩了留下陈旧锁，运维必须能拉起循环。"""
    from coin_selection.scan import FileScanLock

    lk = FileScanLock(Path(tempfile.mkdtemp(prefix="lock-")) / "locks")
    assert lk.acquire("20260903-010") is True
    assert lk.acquire("20260903-010") is False, "进行中锁应当挡住第二个进程"
    # 生产就是靠 --force 越过它的（scan.py: `if not force and not locks.acquire(...)`）


def test_force_must_not_bypass_a_completed_node():
    """已完成的节点绝不重跑 —— 这一条 --force 也不能绕过。"""
    from coin_selection.scan import FileScanLock

    lk = FileScanLock(Path(tempfile.mkdtemp(prefix="lock-")) / "locks")
    assert lk.is_complete("20260903-010") is False
    lk.mark_complete("20260903-010")
    assert lk.is_complete("20260903-010") is True, "完成台账没记住"
    # 台账要能跨进程存活（重启后仍然挡得住）
    lk2 = FileScanLock(lk.lock_dir)
    assert lk2.is_complete("20260903-010") is True, "重启后完成台账丢了"


def test_completed_ledger_is_bounded():
    """台账必须有界，否则 locks/ 会无限增长。"""
    from coin_selection.scan import FileScanLock

    lk = FileScanLock(Path(tempfile.mkdtemp(prefix="lock-")) / "locks")
    for i in range(FileScanLock.COMPLETED_KEEP + 50):
        lk.mark_complete(f"20260903-{i:04d}")
    assert len(lk._completed()) == FileScanLock.COMPLETED_KEEP
    assert lk.is_complete("20260903-0449"), "最近的节点必须还在台账里"
    assert not lk.is_complete("20260903-0000"), "最老的节点应当已被挤出"


# ---------------------------------------------------------------------------
# 一致性目标：《选币榜Y》↔《复盘选币》，同一套 param-v2.0.0
#
# 注意**不是**「主榜 == 选币榜Y」：main 跑 param-v1.4.0，Y 跑 param-v2.0.0，
# 两套参数体系本就独立，板面结果不同才是正确的（board-variants.json 把 main
# 标为冻结项）。要对齐的是同一套 v2.0.0 规则下「在线出的数」与「离线回放出的数」。
# ---------------------------------------------------------------------------
def test_guard_lives_in_the_shared_kernel_so_online_and_replay_agree():
    """守卫必须在共享内核里，在线与离线回放才可能逐字段一致。

    《选币榜Y》在线走 board_projection，《复盘选币》离线回放走 review_replay，
    但分区推进都落在同一个 apply_state_machine 上。守卫只要放在这个共享实现里，
    两条路径就同增同减；放在任一侧的调用点上，K5 立刻会分叉。
    """
    import inspect
    from coin_selection import board_projection
    from coin_selection.state_machine import apply_state_machine as shared

    assert board_projection.apply_state_machine is shared, "在线路径用的不是共享内核"
    assert "0 if replayed else" in inspect.getsource(shared), "共享内核里没有守卫"


def test_replayed_node_is_a_noop_so_online_and_offline_stay_reproducible():
    """同一节点重跑必须是 no-op —— 否则离线回放永远重现不出在线的状态机。

    在线因为重启多跑了一次某个节点，而回放是按 scan_id 逐节点走的、只会跑一次；
    若重跑会推进状态机，两边的分区成员就会永久分叉（K5 的 set/order 不再相等），
    而且**无法回溯修正**。这正是 20260901-004 发生过的事。
    """
    cfg = default_state_config()

    online, offline = _store(), _store()          # 在线（被重跑）/ 离线回放（只跑一次）
    for st in (online, offline):
        _seed_qualified(st)

    apply_state_machine([_row()], online, cfg=cfg, scan_id="20260903-017")
    apply_state_machine([_row()], online, cfg=cfg, scan_id="20260903-017")   # 重启导致的重跑
    apply_state_machine([_row()], offline, cfg=cfg, scan_id="20260903-017")  # 回放只跑一次

    a = online.get("TESTUSDT", "up")
    b = offline.get("TESTUSDT", "up")
    assert (a.state, a.consecutive_pass) == (b.state, b.consecutive_pass), (
        f"在线重跑后与离线回放分叉了: online={a.state}/{a.consecutive_pass} "
        f"offline={b.state}/{b.consecutive_pass} —— K5 会红且无法回溯修正"
    )


def test_completed_ledger_survives_concurrent_writers():
    """完成台账在多进程并发下不丢记录（读-改-写必须持排他锁）。

    背景：``mark_complete`` 是读-改-写。loop 正在跑、运维又手工起一次 ``--force``
    补节点时，无锁写会让后写者用陈旧快照覆盖先写者，**丢掉的正是「这个节点已经
    跑完了」的记录** —— 重跑守卫随之失效，又回到状态机连击 +2 的老问题。

    固定名临时文件同样致命：两个写者互相截断对方写到一半的内容，
    ``os.replace`` 就可能把半个 JSON 提交上去。
    """
    import json as _json
    import shutil as _shutil
    import tempfile as _tempfile
    from multiprocessing import Process as _Process
    from pathlib import Path as _Path

    from coin_selection.scan import FileScanLock as _Lock

    def _worker(d, lo, hi):
        lk = _Lock(_Path(d))
        for i in range(lo, hi):
            lk.mark_complete(f"20260905-{i:03d}")

    tmp = _tempfile.mkdtemp(prefix="completed-race-")
    try:
        procs = [_Process(target=_worker, args=(tmp, i * 40, (i + 1) * 40)) for i in range(6)]
        for pr in procs:
            pr.start()
        for pr in procs:
            pr.join()
        got = set(_json.loads((_Path(tmp) / "completed.json").read_text(encoding="utf-8")))
        want = {f"20260905-{i:03d}" for i in range(240)}
        missing = sorted(want - got)
        assert not missing, f"并发写丢了 {len(missing)} 条完成记录，例: {missing[:5]}"
        leftover = [x.name for x in _Path(tmp).glob("*.tmp")]
        assert not leftover, f"残留临时文件: {leftover}"
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
