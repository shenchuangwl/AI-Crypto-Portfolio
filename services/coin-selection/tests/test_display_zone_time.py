"""「入选时间 / 停留时间 / 停留价格」必须跟随**用户看到的分区**。

实测缺陷（20260903 USELESSUSDT）：
  node 044 base=CONFIRMED final=CONFIRMED 显示确认区   enter=01:15Z dwell=585
  node 045 base=CONFIRMED final=QUALIFIED 显示符合区   enter=01:15Z dwell=600  ← 离开确认区
  node 048 base=CONFIRMED final=CONFIRMED 显示确认区   enter=01:15Z dwell=645  ← 又回来了
用户看到它离开确认区又回来，进入时间却从未重置、停留一路累加、停留价格停在最初那次。

根因：三列跟随 base_state（层 A 状态机），而板面显示的是 final_zone（层 C 主导层）
与 dmr_selected。主导层生效后两者可以不同 —— 实测当刻 1036 行中 241 行不同。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.state_machine import (  # noqa: E402
    StateMachineStore,
    display_zone_of,
    stamp_display_zone,
)

T0 = 1_771_999_200.0          # 已对齐到 15 分钟节点栅格（1771999200 % 900 == 0）
NODE = 900.0


def _store():
    return StateMachineStore(Path(tempfile.mkdtemp(prefix="dispzone-")) / "sm.json")


def _row(**kw):
    r = {"symbol": "USELESSUSDT", "direction": "up", "state": "CONFIRMED",
         "final_zone": "CONFIRMED", "dmr_selected": False, "last_price": 1.0}
    r.update(kw)
    return r


def _stamp(store, row, t):
    stamp_display_zone(store, ([row], []), now_ts=t)
    return row


# --------------------------------------------------------------- display_zone_of
def test_display_zone_prefers_dmr_then_final_then_state():
    assert display_zone_of({"dmr_selected": True, "final_zone": "CONFIRMED"}) == "DMR"
    assert display_zone_of({"final_zone": "QUALIFIED", "state": "CONFIRMED"}) == "QUALIFIED"
    assert display_zone_of({"state": "WATCH"}) == "WATCH", "主导层关闭时回退 state"
    assert display_zone_of({}) is None


# --------------------------------------------------------------- 核心回归
def test_reentering_a_zone_resets_enter_time_and_price():
    """离开确认区再回来 ⇒ 进入时间必须刷新为当前时刻，停留归零，价格重取。"""
    st = _store()
    r = _stamp(st, _row(last_price=1.00), T0)                 # 进确认区
    first_enter = r["zone_enter_time_utc"]
    assert r["zone_duration_minutes"] == 0.0
    assert r["zone_enter_price"] == 1.00

    r = _stamp(st, _row(last_price=1.10), T0 + NODE)          # 仍在确认区 → 累加
    assert r["zone_enter_time_utc"] == first_enter
    assert r["zone_duration_minutes"] == 15.0
    assert r["zone_enter_price"] == 1.00, "同一次停留内价格不得改动"

    r = _stamp(st, _row(final_zone="QUALIFIED", state="QUALIFIED",
                        last_price=1.20), T0 + 2 * NODE)      # 掉到符合区
    assert r["zone_duration_minutes"] == 0.0, "换区必须归零"
    assert r["zone_enter_price"] == 1.20, "换区必须重取价格"
    q_enter = r["zone_enter_time_utc"]
    assert q_enter != first_enter

    r = _stamp(st, _row(last_price=1.30), T0 + 3 * NODE)      # 回到确认区
    assert r["zone_duration_minutes"] == 0.0, "重新进入确认区必须归零，不得沿用旧戳"
    assert r["zone_enter_time_utc"] not in (first_enter, q_enter), "进入时间必须是当前时刻"
    assert r["zone_enter_price"] == 1.30, "重新进入必须重取价格"


def test_dmr_in_and_out_is_tracked_as_its_own_zone():
    """DMR 的每一次进出都要重新打戳 —— 它是展示区之一，且无粘性。"""
    st = _store()
    _stamp(st, _row(dmr_selected=True, last_price=2.00), T0)              # 进 DMR
    r = _stamp(st, _row(dmr_selected=False, last_price=2.50), T0 + NODE)  # 掉出 DMR（仍确认）
    assert r["zone_duration_minutes"] == 0.0
    assert r["zone_enter_price"] == 2.50
    out_enter = r["zone_enter_time_utc"]

    r = _stamp(st, _row(dmr_selected=True, last_price=3.00), T0 + 2 * NODE)  # 重回 DMR
    assert r["zone_duration_minutes"] == 0.0, "重回 DMR 必须归零"
    assert r["zone_enter_time_utc"] != out_enter
    assert r["zone_enter_price"] == 3.00


def test_base_state_churn_without_display_change_does_not_reset():
    """base_state 变了但显示区没变 ⇒ 不得打戳（否则停留时长会被无故清零）。"""
    st = _store()
    _stamp(st, _row(state="CONFIRMED", final_zone="WATCH", last_price=1.0), T0)
    r = _stamp(st, _row(state="QUALIFIED", final_zone="WATCH", last_price=9.9), T0 + NODE)
    assert r["zone_duration_minutes"] == 15.0, "显示区未变，停留应继续累加"
    assert r["zone_enter_price"] == 1.0, "显示区未变，价格不得改动"


def test_both_directions_tracked_independently():
    st = _store()
    up = {"symbol": "AAAUSDT", "direction": "up", "final_zone": "CONFIRMED", "last_price": 1.0}
    dn = {"symbol": "AAAUSDT", "direction": "down", "final_zone": "WATCH", "last_price": 1.0}
    stamp_display_zone(st, ([up], [dn]), now_ts=T0)
    dn2 = dict(dn, final_zone="ELIMINATED", last_price=5.0)
    up2 = dict(up, last_price=5.0)
    stamp_display_zone(st, ([up2], [dn2]), now_ts=T0 + NODE)
    assert up2["zone_duration_minutes"] == 15.0, "up 侧未换区，应继续累加"
    assert dn2["zone_duration_minutes"] == 0.0, "down 侧换区，应归零"


def test_stamp_survives_a_restart():
    """戳必须持久化：重启后同一展示区不得被当成新进入。"""
    st = _store()
    _stamp(st, _row(last_price=1.0), T0)
    st.save()
    reopened = StateMachineStore(st.path)
    r = _stamp(reopened, _row(last_price=2.0), T0 + NODE)
    assert r["zone_duration_minutes"] == 15.0, "重启后停留被清零了"
    assert r["zone_enter_price"] == 1.0


def test_first_sight_inherits_the_state_machine_stamp_when_zones_agree():
    """新字段上线的第一个节点，不得把所有行的停留集体清零。

    回归：20260904-044 上线首个节点把 1044 行的 dwell 全部打成 0。
    展示区与状态机分区一致时旧戳本来就是对的，应当继承。
    """
    st = _store()
    sym = st.get("USELESSUSDT", "up")
    sym.state = "CONFIRMED"
    sym.state_enter_ts = T0 - 10 * NODE          # 已经停留了 150 分钟
    sym.state_enter_price = 0.5
    r = _stamp(st, _row(final_zone="CONFIRMED", state="CONFIRMED"), T0)
    assert r["zone_duration_minutes"] == 150.0, "一致时应继承旧戳，而不是清零"
    assert r["zone_enter_price"] == 0.5


def test_first_sight_stamps_now_when_zones_already_disagree():
    """展示区与状态机分区本来就不一致时，旧戳不可信，用当前节点。"""
    st = _store()
    sym = st.get("USELESSUSDT", "up")
    sym.state = "CONFIRMED"
    sym.state_enter_ts = T0 - 10 * NODE
    sym.state_enter_price = 0.5
    r = _stamp(st, _row(final_zone="WATCH", state="WATCH", last_price=9.0), T0)
    assert r["zone_duration_minutes"] == 0.0
    assert r["zone_enter_price"] == 9.0


def test_never_overwrites_the_ledger_entry_price_fields():
    """铁律：绝不碰 state_enter_* —— 复盘账本拿 state_enter_price 当入场价来源。

    回归：初版实现直接覆写了这三个键，实测污染了 42 笔交易的入场价
    （Y 16 笔 + 主榜 26 笔，enter_price_source='state_enter_price'）。
    """
    st = _store()
    row = _row(last_price=9.99)
    row["state_enter_time_utc"] = "2026-01-01T00:00:00Z"
    row["state_duration_minutes"] = 123.0
    row["state_enter_price"] = 0.5
    stamp_display_zone(st, ([row], []), now_ts=T0)
    assert row["state_enter_time_utc"] == "2026-01-01T00:00:00Z", "覆写了账本读的时刻"
    assert row["state_duration_minutes"] == 123.0, "覆写了账本读的停留"
    assert row["state_enter_price"] == 0.5, "覆写了账本读的入场价 —— 会改写历史盈亏"
    assert row["zone_enter_price"] == 9.99, "新字段没写上"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
