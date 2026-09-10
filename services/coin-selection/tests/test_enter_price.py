"""state_enter_price: stamp on 符合/确认, backfill from snapshots."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.state_machine import (  # noqa: E402
    StateConfig,
    StateMachineStore,
    SymState,
    apply_state_machine,
    backfill_enter_prices_from_snapshots,
    set_clock,
    transition_one,
)


def _cfg() -> StateConfig:
    return StateConfig(
        min_streak_watch=1,
        min_streak_qualified=1,
        min_streak_confirmed=1,
        min_dwell_watch=0,
        min_dwell_qualified=0,
        min_dwell_confirmed=0,
        enter_confirmed=70,
        ss_confirmed=55,
        mom_confirmed=60,
        cons_confirmed=0.5,
        dq_confirm_floor=50,
    )


def test_enter_qualified_stamps_price():
    set_clock(1_700_000_000.0)
    st = SymState(symbol="BMTUSDT", direction="up", state="WATCH", state_enter_ts=1.0)
    st.consecutive_pass = 1
    st, reason = transition_one(
        st,
        score=70,
        ss=60,
        momentum=70,
        consistency=0.8,
        dq=90,
        supply_missing=False,
        hard_fail=False,
        cfg=_cfg(),
        scan_id="t1",
        last_price=0.01587,
    )
    assert reason == "ENTER_QUALIFIED"
    assert st.state == "QUALIFIED"
    assert abs(st.state_enter_price - 0.01587) < 1e-12


def test_watch_and_elim_clear_price():
    set_clock(1_700_000_100.0)
    st = SymState(
        symbol="BMTUSDT",
        direction="up",
        state="QUALIFIED",
        state_enter_ts=1.0,
        state_enter_price=0.02,
        consecutive_fail=2,
    )
    st, reason = transition_one(
        st,
        score=10,
        ss=10,
        momentum=10,
        consistency=0.1,
        dq=90,
        supply_missing=False,
        hard_fail=False,
        cfg=_cfg(),
        scan_id="t2",
        last_price=0.019,
    )
    assert reason == "DEMOTE_QUALIFIED"
    assert st.state == "WATCH"
    assert abs(st.state_enter_price - 0.019) < 1e-12


def test_enter_watch_stamps_first_print():
    set_clock(1_700_000_050.0)
    st = SymState(symbol="EDENUSDT", direction="up", state="NONE")
    st.consecutive_pass = 1
    st, reason = transition_one(
        st,
        score=50,
        ss=10,
        momentum=10,
        consistency=0.1,
        dq=90,
        supply_missing=False,
        hard_fail=False,
        cfg=_cfg(),
        scan_id="t0",
        last_price=0.04327,
    )
    assert reason == "ENTER_WATCH"
    assert st.state == "WATCH"
    assert abs(st.state_enter_price - 0.04327) < 1e-12


def test_apply_writes_side_fields():
    set_clock(1_700_000_200.0)
    store = StateMachineStore(Path("/tmp/sm-enter-price-test.json"))
    store.states.clear()
    store.states["NILUSDT|up"] = SymState(
        symbol="NILUSDT",
        direction="up",
        state="WATCH",
        state_enter_ts=1.0,
        consecutive_pass=1,
    )
    rows = [
        {
            "symbol": "NILUSDT",
            "last_price": 0.04944,
            "score_up": 80,
            "ss_up": 70,
            "momentum_score_up": 80,
            "consistency_up": 0.9,
            "score_down": 20,
            "ss_down": 10,
            "momentum_score_down": 10,
            "consistency_down": 0.1,
            "data_quality_score": 90,
            "supply_missing": False,
            "liquidity_hard_pass": True,
        }
    ]
    apply_state_machine(rows, store, cfg=_cfg(), scan_id="t3", now_ts=1_700_000_200.0)
    assert rows[0]["state_up"] == "QUALIFIED"
    assert abs(rows[0]["state_up_enter_price"] - 0.04944) < 1e-12
    assert rows[0]["state_down_enter_price"] is None


def test_backfill_uses_first_print_after_enter(tmp_path: Path | None = None):
    root = Path("/tmp/sm-enter-price-snaps")
    if tmp_path is not None:
        root = tmp_path
    snaps = root / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / "a.json").write_text(
        json.dumps(
            {
                "meta": {"scan_timestamp_utc": "2026-08-15T10:00:05Z"},
                "long_pool": [
                    {
                        "symbol": "TUTUSDT",
                        "direction": "up",
                        "state": "QUALIFIED",
                        "last_price": 0.03000,
                    }
                ],
                "short_pool": [],
            }
        ),
        encoding="utf-8",
    )
    (snaps / "b.json").write_text(
        json.dumps(
            {
                "meta": {"scan_timestamp_utc": "2026-08-15T14:15:05Z"},
                "long_pool": [
                    {
                        "symbol": "TUTUSDT",
                        "direction": "up",
                        "state": "QUALIFIED",
                        "last_price": 0.03626,
                    }
                ],
                "short_pool": [],
            }
        ),
        encoding="utf-8",
    )
    store = StateMachineStore(root / "state_machine.json")
    store.states.clear()
    # entered at 14:15 UTC — should take 0.03626, not the 10:00 print
    from datetime import datetime, timezone

    enter_ts = datetime(2026, 8, 15, 14, 15, 5, tzinfo=timezone.utc).timestamp()
    store.states["TUTUSDT|up"] = SymState(
        symbol="TUTUSDT",
        direction="up",
        state="QUALIFIED",
        state_enter_ts=enter_ts,
        state_enter_price=None,
    )
    n = backfill_enter_prices_from_snapshots(store, snaps)
    assert n == 1
    assert abs(store.states["TUTUSDT|up"].state_enter_price - 0.03626) < 1e-12


if __name__ == "__main__":
    from pathlib import Path as P

    test_enter_qualified_stamps_price()
    test_watch_and_elim_clear_price()
    test_enter_watch_stamps_first_print()
    test_apply_writes_side_fields()
    test_backfill_uses_first_print_after_enter(P("/tmp/sm-enter-price-snaps"))
    print("ok")
