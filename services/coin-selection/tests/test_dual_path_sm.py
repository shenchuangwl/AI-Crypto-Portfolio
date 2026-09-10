"""Dual-path QUAL/CONF (param-v1.3.0-dual-path-sticky).

WATCH / ELIMINATED / HARD_FAIL / G1 / F2 / no-skip / no SM_FAST stay frozen.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.scan import (  # noqa: E402
    SelectionSettings,
    board_from_rows,
    build_dmr_messages,
    rank_dmr_inbox,
)
from coin_selection.state_machine import (  # noqa: E402
    StateConfig,
    SymState,
    apply_state_machine,
    cons_is_one,
    default_state_config,
    hold_ok,
    path_confirmed,
    path_qualified,
    set_clock,
    transition_one,
)


def _prod() -> StateConfig:
    return default_state_config()


def _tick(
    st: SymState,
    *,
    score: float,
    ss: float,
    mom: float,
    cons: float,
    dq: float = 90.0,
    supply_missing: bool = False,
    hard_fail: bool = False,
    last_price: float = 1.0,
    scan_id: str = "t",
    freeze_new_confirm: bool = False,
    data_mode: str = "LIVE",
    cfg: StateConfig | None = None,
) -> tuple[SymState, str | None]:
    return transition_one(
        st,
        score=score,
        ss=ss,
        momentum=mom,
        consistency=cons,
        dq=dq,
        supply_missing=supply_missing,
        hard_fail=hard_fail,
        cfg=cfg or _prod(),
        scan_id=scan_id,
        last_price=last_price,
        freeze_new_confirm=freeze_new_confirm,
        data_mode=data_mode,
    )


def test_cons_is_one_float_contract():
    assert cons_is_one(1.0)
    assert cons_is_one(1)
    assert cons_is_one(0.99) is False
    assert cons_is_one(2.0 / 3.0) is False
    assert cons_is_one(0.67) is False


def test_watch_enter_exit_frozen():
    cfg = _prod()
    assert cfg.enter_watch == 45
    assert cfg.exit_watch == 40
    assert cfg.min_streak_watch == 2
    assert cfg.min_dwell_watch == 30
    set_clock(1_800_000_000.0)
    st = SymState(symbol="AAAUSDT", direction="up", state="NONE")
    st, r1 = _tick(st, score=50, ss=20, mom=20, cons=0.0)
    assert r1 is None and st.state == "NONE"
    st, r2 = _tick(st, score=50, ss=20, mom=20, cons=0.0)
    assert r2 == "ENTER_WATCH" and st.state == "WATCH"
    # SS/M/C not required for WATCH
    st, _ = _tick(st, score=41, ss=10, mom=10, cons=0.0)
    assert st.state == "WATCH"
    st, _ = _tick(st, score=39, ss=10, mom=10, cons=0.0)
    assert st.state == "WATCH"  # need 2 fails
    st, r = _tick(st, score=39, ss=10, mom=10, cons=0.0)
    assert r == "EXIT_WATCH" and st.state == "ELIMINATED"


def test_hard_fail_from_any_zone():
    set_clock(1_800_000_100.0)
    st = SymState(symbol="BBBUSDT", direction="down", state="CONFIRMED", state_enter_ts=1.0)
    st, r = _tick(st, score=80, ss=70, mom=80, cons=1.0, hard_fail=True)
    assert r == "HARD_FAIL" and st.state == "ELIMINATED"


def test_path_s_qualified_gates():
    cfg = _prod()
    assert path_qualified(58, 50, 55, 0.33, False, cfg) == "S"
    assert path_qualified(57.9, 50, 55, 1.0, False, cfg) is None
    assert path_qualified(58, 49.9, 55, 1.0, False, cfg) is None
    assert path_qualified(62, 45, 65, 1.0, False, cfg) == "M"
    assert path_qualified(62, 45, 65, 0.67, False, cfg) is None  # no third mouth
    assert path_qualified(80, 80, 80, 1.0, True, cfg) is None


def test_confirmed_requires_hard_staircase():
    cfg = _prod()
    assert path_confirmed(80, 45, 80, 0.67, 90, False, cfg) is None
    assert path_confirmed(80, 45, 90, 1.0, 90, False, cfg) is None
    assert path_confirmed(66, 50, 60, 0.67, 90, False, cfg) == "S"
    assert path_confirmed(70, 45, 70, 1.0, 59, False, cfg) is None  # DQ
    assert path_confirmed(70, 45, 70, 1.0, 90, False, cfg, data_mode="MISSING") is None
    assert path_confirmed(80, 80, 80, 1.0, 90, True, cfg) is None
    # both → prefer S
    assert path_confirmed(80, 55, 80, 1.0, 90, False, cfg) == "S"


def test_confirm_hold_requires_staircase():
    cfg = _prod()
    assert hold_ok(56, 50, 50, 0.67, cfg) is True
    assert hold_ok(90, 45, 90, 1.0, cfg) is False
    assert hold_ok(55.9, 80, 80, 1.0, cfg) is False


def test_no_skip_watch_to_confirmed():
    set_clock(1_800_000_200.0)
    st = SymState(symbol="CCCUSDT", direction="up", state="WATCH", state_enter_ts=1_800_000_200.0 - 3600)
    st.consecutive_pass = 4
    st, r = _tick(st, score=90, ss=80, mom=90, cons=1.0)
    assert r == "ENTER_QUALIFIED" and st.state == "QUALIFIED"


def test_qualified_path_m_from_watch():
    t0 = 1_800_000_300.0
    set_clock(t0)
    st = SymState(symbol="DDDUSDT", direction="up", state="WATCH", state_enter_ts=t0)
    set_clock(t0 + 30 * 60)
    st.consecutive_pass = 0
    st, r = _tick(st, score=62, ss=45, mom=65, cons=1.0)
    assert r is None and st.state == "WATCH" and st.consecutive_pass == 1
    st, r = _tick(st, score=62, ss=45, mom=65, cons=1.0)
    assert r == "ENTER_QUALIFIED" and st.state == "QUALIFIED"


def test_confirm_path_s_dwell_15_streak_2():
    t0 = 1_800_000_400.0
    set_clock(t0)
    st = SymState(symbol="EEEUSDT", direction="down", state="QUALIFIED", state_enter_ts=t0)
    st, r = _tick(st, score=70, ss=55, mom=70, cons=1.0)
    assert r is None and st.state == "QUALIFIED"
    set_clock(t0 + 15 * 60)
    st, r = _tick(st, score=70, ss=55, mom=70, cons=1.0)
    assert r == "ENTER_CONFIRMED" and st.state == "CONFIRMED"


def test_confirm_without_staircase_demotes_immediately():
    t0 = 1_800_000_500.0
    set_clock(t0)
    st = SymState(symbol="FFFUSDT", direction="up", state="CONFIRMED", state_enter_ts=t0)
    st, r = _tick(st, score=90, ss=45, mom=90, cons=1.0)
    assert r == "STAIRCASE_MISSING_DEMOTE" and st.state == "QUALIFIED"


def test_qualified_exit_52():
    t0 = 1_800_000_600.0
    set_clock(t0)
    st = SymState(symbol="GGGUSDT", direction="up", state="QUALIFIED", state_enter_ts=t0)
    st, _ = _tick(st, score=51.9, ss=50, mom=55, cons=0.33)
    st, r = _tick(st, score=51.9, ss=50, mom=55, cons=0.33)
    assert r == "DEMOTE_QUALIFIED" and st.state == "WATCH"


def test_supply_missing_blocks_upgrade_and_demotes_confirm():
    t0 = 1_800_000_700.0
    set_clock(t0)
    st = SymState(symbol="HHHUSDT", direction="up", state="CONFIRMED", state_enter_ts=t0)
    st, r = _tick(st, score=80, ss=70, mom=80, cons=1.0, supply_missing=True)
    assert r == "SUPPLY_MISSING_DEMOTE" and st.state == "QUALIFIED"
    st = SymState(symbol="IIIUSDT", direction="up", state="WATCH", state_enter_ts=t0 - 3600)
    st.consecutive_pass = 5
    st, r = _tick(st, score=80, ss=70, mom=80, cons=1.0, supply_missing=True)
    assert r is None and st.state == "WATCH"


def test_dq_demote_from_confirmed():
    t0 = 1_800_000_750.0
    set_clock(t0)
    st = SymState(symbol="DQUSDT", direction="up", state="CONFIRMED", state_enter_ts=t0)
    st, r = _tick(st, score=80, ss=70, mom=80, cons=1.0, dq=40.0)
    assert r == "DQ_DEMOTE" and st.state == "QUALIFIED"


def test_freeze_new_confirm():
    t0 = 1_800_000_800.0
    set_clock(t0)
    st = SymState(symbol="JJJUSDT", direction="up", state="QUALIFIED", state_enter_ts=t0 - 3600)
    st.consecutive_pass = 1
    st, r = _tick(st, score=80, ss=70, mom=80, cons=1.0, freeze_new_confirm=True)
    assert r is None and st.state == "QUALIFIED"


def test_sm_fast_not_default():
    os.environ.pop("SM_FAST", None)
    cfg = default_state_config()
    assert cfg.min_streak_confirmed == 2
    assert cfg.min_dwell_qualified == 15
    assert cfg.enter_confirmed == 66


def test_apply_annotates_paths(tmp_path: Path | None = None):
    from coin_selection.state_machine import StateMachineStore

    root = Path("/tmp/sm-dual-path-apply") if tmp_path is None else tmp_path
    root.mkdir(parents=True, exist_ok=True)
    set_clock(1_800_000_900.0)
    store = StateMachineStore(root / "state_machine.json")
    store.states.clear()
    store.states["KKKUSDT|up"] = SymState(
        symbol="KKKUSDT",
        direction="up",
        state="QUALIFIED",
        state_enter_ts=1_800_000_900.0 - 3600,
        consecutive_pass=2,
    )
    rows = [
        {
            "symbol": "KKKUSDT",
            "last_price": 1.2,
            "score_up": 72,
            "ss_up": 55,
            "momentum_score_up": 70,
            "consistency_up": 1.0,
            "score_down": 20,
            "ss_down": 10,
            "momentum_score_down": 10,
            "consistency_down": 0.0,
            "data_quality_score": 90,
            "supply_missing": False,
            "liquidity_hard_pass": True,
            "data_mode": "LIVE",
        }
    ]
    apply_state_machine(rows, store, cfg=_prod(), scan_id="t", now_ts=1_800_000_900.0)
    assert rows[0]["state_up"] == "CONFIRMED"
    assert rows[0]["confirmed_path_up"] == "S"
    assert rows[0]["qualified_path_up"] == "S"


def test_board_not_confirmed_reasons_split():
    now = datetime(2026, 8, 21, 12, 0, 5, tzinfo=timezone.utc)
    row = {
        "symbol": "LLLUSDT",
        "base_asset": "LLL",
        "liquidity_hard_pass": True,
        "liquidity_grade": "A",
        "state_up": "QUALIFIED",
        "state_down": "WATCH",
        "state_up_dwell_min": 5.0,
        "state_down_dwell_min": 40.0,
        "state_up_streak": 0,
        "state_down_streak": 1,
        "score_up": 64.0,
        "score_down": 50.0,
        "momentum_score_up": 61.0,
        "momentum_score_down": 40.0,
        "consistency_up": 0.67,
        "consistency_down": 0.33,
        "ss_up": 51.0,
        "ss_down": 45.0,
        "mcap_momentum_score_up": 50,
        "mcap_momentum_score_down": 40,
        "liquidity_score_abs": 80,
        "data_quality_score": 90,
        "ret_1h": 0.01,
        "ret_4h": 0.02,
        "ret_24h": 0.03,
        "ret_15m": None,
        "ret_since_anchor": 0.01,
        "last_price": 1.0,
        "supply_missing": False,
        "qualified_path_up": "S",
        "ready_confirm_up": False,
    }
    pool = board_from_rows([row], "up", now=now)
    assert pool[0]["qualified_path"] == "S"
    assert "dwell" in pool[0]["not_confirmed_reasons"] or "ss" in pool[0]["not_confirmed_reasons"]
    assert "dwell_or_streak" not in pool[0]["not_confirmed_reasons"]


def test_dmr_topk_unique_and_confirmed_only():
    settings = SelectionSettings()
    settings.parameter_version = "param-v1.4.0-staircase-confirm-dmr"
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
    anchor = datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(20):
        rows.append(
            {
                "symbol": f"C{i:02d}USDT",
                "base_asset": f"C{i:02d}",
                "state_up": "CONFIRMED",
                "state_down": "WATCH",
                "score_up": 90 - i,
                "score_down": 10,
                "ss_up": 55,
                "ss_down": 20,
                "momentum_score_up": 75,
                "momentum_score_down": 20,
                "consistency_up": 1.0,
                "consistency_down": 0.0,
                "mcap_momentum_score_up": 50,
                "mcap_momentum_score_down": 50,
                "liquidity_score_abs": 80,
                "data_quality_score": 90,
                "supply_missing": False,
                "liquidity_hard_pass": True,
                "circulating_supply": 1e9,
                "data_mode": "LIVE",
                "state_up_dwell_min": 30,
                "confirmed_path_up": "S" if i % 2 == 0 else "M",
            }
        )
    rows.append(
        {
            "symbol": "QUALONLYUSDT",
            "base_asset": "QUALONLY",
            "state_up": "QUALIFIED",
            "state_down": "WATCH",
            "score_up": 99,
            "score_down": 1,
            "ss_up": 80,
            "ss_down": 10,
            "momentum_score_up": 90,
            "momentum_score_down": 10,
            "consistency_up": 1.0,
            "consistency_down": 0.0,
            "mcap_momentum_score_up": 50,
            "mcap_momentum_score_down": 50,
            "liquidity_score_abs": 80,
            "data_quality_score": 90,
            "supply_missing": False,
            "liquidity_hard_pass": True,
            "circulating_supply": 1e9,
            "data_mode": "LIVE",
            "state_up_dwell_min": 30,
        }
    )
    # both sides confirmed — keep higher score
    rows.append(
        {
            "symbol": "BOTHUSDT",
            "base_asset": "BOTH",
            "state_up": "CONFIRMED",
            "state_down": "CONFIRMED",
            "score_up": 71,
            "score_down": 88,
            "ss_up": 50,
            "ss_down": 56,
            "momentum_score_up": 70,
            "momentum_score_down": 80,
            "consistency_up": 1.0,
            "consistency_down": 1.0,
            "mcap_momentum_score_up": 50,
            "mcap_momentum_score_down": 50,
            "liquidity_score_abs": 80,
            "data_quality_score": 90,
            "supply_missing": False,
            "liquidity_hard_pass": True,
            "circulating_supply": 1e9,
            "data_mode": "LIVE",
            "state_up_dwell_min": 30,
            "state_down_dwell_min": 30,
            "confirmed_path_up": "M",
            "confirmed_path_down": "S",
        }
    )
    msgs = build_dmr_messages(rows, settings=settings, anchor=anchor, scan_id="20260821-048", seq=48, now=now)
    assert all(m["state"] == "CONFIRMED" for m in msgs)
    assert all(m["symbol"] != "QUALONLYUSDT" for m in msgs)
    inbox, meta = rank_dmr_inbox(msgs, top_k=16)
    assert meta["unique_before_k"] == 21  # 20 + BOTH
    assert len(inbox) == 16
    assert len({m["symbol"] for m in inbox}) == 16
    both = [m for m in inbox if m["symbol"] == "BOTHUSDT"]
    assert len(both) == 1 and both[0]["direction"] == "SHORT"
    assert inbox[0]["parameter_version"] == "param-v1.4.0-staircase-confirm-dmr"
    assert "confirm_path" in inbox[0]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
