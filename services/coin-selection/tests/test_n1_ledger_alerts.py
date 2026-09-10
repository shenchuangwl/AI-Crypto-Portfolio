"""New-standard hardening: §6.3 N1, 日去重口径, §10.2 alerts, DMR 参数白名单.

These cover the gaps that the dual-path landing left open: a CONFIRMED row must
carry a *hard True* G1 verdict, the board must publish occupancy and the day
roll-up side by side, alerts must annotate without relaxing anything, and the
adapter must refuse an unknown parameter_version (v1.2 §39.3 ⑦).
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(ROOT / "services" / "dmr-adapter" / "src"))

from coin_selection.daily_ledger import DailyUniqueLedger, update_daily_unique  # noqa: E402
from coin_selection.scan import (  # noqa: E402
    board_from_rows,
    not_confirmed_reasons_for,
)
from coin_selection.state_machine import (  # noqa: E402
    StateMachineStore,
    apply_state_machine,
    default_state_config,
    set_clock,
)

T0 = 1_800_000_000.0


def _confirmable_row(symbol: str, hard: object) -> dict:
    """A row whose numbers clear PATH_M confirm on every axis except G1."""
    return {
        "symbol": symbol,
        "base_asset": symbol.replace("USDT", ""),
        "last_price": 1.0,
        "score_up": 80,
        "ss_up": 55,
        "momentum_score_up": 75,
        "consistency_up": 1.0,
        "score_down": 10,
        "ss_down": 0,
        "momentum_score_down": 10,
        "consistency_down": 0.0,
        "mcap_momentum_score_up": 60,
        "mcap_momentum_score_down": 40,
        "liquidity_score_abs": 80,
        "liquidity_grade": "A",
        "data_quality_score": 90,
        "supply_missing": False,
        "circulating_supply": 1e9,
        "liquidity_hard_pass": hard,
        "data_mode": "LIVE",
        "ret_1h": 0.01,
        "ret_4h": 0.02,
        "ret_24h": 0.03,
        "ret_15m": None,
        "ret_since_anchor": 0.01,
    }


def _drive(rows: list[dict], ticks: int, store: StateMachineStore) -> None:
    for i in range(ticks):
        apply_state_machine(
            rows, store, cfg=default_state_config(), scan_id=f"t{i}", now_ts=T0 + i * 900
        )


def test_n1_requires_hard_true_not_merely_not_false():
    """liquidity_hard_pass=None (e.g. --skip-gate1) must never mint a CONFIRMED."""
    with tempfile.TemporaryDirectory() as d:
        rows_true = [_confirmable_row("AAAUSDT", True)]
        rows_none = [_confirmable_row("BBBUSDT", None)]
        _drive(rows_true, 9, StateMachineStore(Path(d) / "a.json"))
        _drive(rows_none, 9, StateMachineStore(Path(d) / "b.json"))
    assert rows_true[0]["state_up"] == "CONFIRMED", rows_true[0]["state_up"]
    assert rows_none[0]["state_up"] == "QUALIFIED", rows_none[0]["state_up"]
    assert rows_none[0]["ready_confirm_up"] is False
    set_clock(None)


def test_not_confirmed_reason_reports_liquidity():
    codes = not_confirmed_reasons_for(
        state="QUALIFIED",
        score=80,
        ss=46,
        mom=75,
        cons=1.0,
        dq=90,
        supply_missing=False,
        dwell=99,
        streak=9,
        hard_pass=None,
    )
    assert "liquidity" in codes


def test_board_marks_liquidity_reason():
    row = _confirmable_row("CCCUSDT", None)
    row.update({"state_up": "QUALIFIED", "state_down": "WATCH",
                "state_up_dwell_min": 90.0, "state_down_dwell_min": 90.0,
                "state_up_streak": 4, "state_down_streak": 0})
    pool = board_from_rows([row], "up", now=datetime(2026, 8, 21, tzinfo=timezone.utc))
    assert "liquidity" in pool[0]["not_confirmed_reasons"]


def test_daily_dmr_unique_is_post_top_k_symbol_union():
    """DMR 今日去重只记实际已被 Top-K 选中的 symbol，且跨扫描保留并集。"""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        first = update_daily_unique(
            base,
            "2026-09-04",
            [],
            dmr_messages=[{"symbol": "AAAUSDT"}, {"symbol": "AAAUSDT"}, {"symbol": "BBBUSDT"}],
        )
        assert first["dmr"] == 2
        assert first["dmr_symbols"] == ["AAAUSDT", "BBBUSDT"]
        second = update_daily_unique(
            base,
            "2026-09-04",
            [],
            dmr_messages=[{"symbol": "BBBUSDT"}, {"symbol": "CCCUSDT"}],
        )
        assert second["dmr"] == 3
        assert second["dmr_symbols"] == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]


def test_daily_unique_is_union_not_occupancy():
    """Occupancy is per-scan; the ledger is the day's union and only grows."""
    with tempfile.TemporaryDirectory() as d:
        data = Path(d)
        c1 = update_daily_unique(data, "2026-08-21", [
            {"symbol": "XUSDT", "state_up": "CONFIRMED", "state_down": "WATCH"},
        ])
        assert c1["confirmed"] == 1 and c1["watch"] == 1
        # X leaves, Y arrives: occupancy stays 1 but the day roll-up becomes 2
        c2 = update_daily_unique(data, "2026-08-21", [
            {"symbol": "XUSDT", "state_up": "QUALIFIED", "state_down": "WATCH"},
            {"symbol": "YUSDT", "state_up": "CONFIRMED", "state_down": "ELIMINATED"},
        ])
        assert c2["confirmed"] == 2, c2
        assert sorted(c2["confirmed_symbols"]) == ["XUSDT", "YUSDT"]
        # anchor roll resets the union
        c3 = update_daily_unique(data, "2026-08-22", [
            {"symbol": "ZUSDT", "state_up": "CONFIRMED", "state_down": "NONE"},
        ])
        assert c3["confirmed"] == 1 and c3["anchor_date"] == "2026-08-22"


def test_daily_ledger_survives_corrupt_file():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "daily_unique.json"
        p.write_text("{not json", encoding="utf-8")
        led = DailyUniqueLedger(p, "2026-08-21")
        led.observe([{"symbol": "QUSDT", "state_up": "CONFIRMED"}])
        led.save()
        assert json.loads(p.read_text())["states"]["CONFIRMED"] == ["QUSDT"]


def test_transitions_record_real_from_state():
    """禁止跳级 is only auditable if the board reports the true predecessor."""
    row = _confirmable_row("DDDUSDT", True)
    with tempfile.TemporaryDirectory() as d:
        store = StateMachineStore(Path(d) / "s.json")
        seen = []
        for i in range(6):
            tr = apply_state_machine(
                [row], store, cfg=default_state_config(), scan_id=f"t{i}", now_ts=T0 + i * 900
            )
            seen += [t for t in tr if t["direction"] == "up"]
    hops = [(t["from_state"], t["to_state"]) for t in seen]
    assert ("NONE", "WATCH") in hops, hops
    assert ("WATCH", "QUALIFIED") in hops, hops
    assert ("QUALIFIED", "CONFIRMED") in hops, hops
    # no level was ever skipped
    assert all(h not in hops for h in (("WATCH", "CONFIRMED"), ("NONE", "QUALIFIED"))), hops
    set_clock(None)


def test_dmr_parameter_version_whitelist():
    from dmr_adapter.adapter import local_reject, parameter_whitelist

    base = {
        "state": "CONFIRMED",
        "direction": "LONG",
        "expires_at_utc": "2099-01-01T00:00:00Z",
        "data_mode": "LIVE",
        "data_confidence": 95,
        "risk_flags": [],
        "circulating_supply": 1e9,
    }
    assert "param-v1.4.0-staircase-confirm-dmr" in parameter_whitelist()
    ok = dict(base, parameter_version="param-v1.4.0-staircase-confirm-dmr")
    assert local_reject(ok) is None
    drift = dict(base, parameter_version="param-v9.9.9-experimental")
    assert local_reject(drift) == "parameter_version"


def test_confirmed_occupancy_band_is_10_to_40():
    """确认占用目标带 10–40：25 不溢出，41 才 CONFIRM_OVERFLOW。两榜共用。"""
    from coin_selection.scan import SelectionSettings, decorate_board
    from coin_selection.state_machine import StateConfig, occupancy_from_rows

    cfg = StateConfig(n_impulse_floor=0)
    assert cfg.occupancy_lo == 10 and cfg.occupancy_hi == 40

    def _rows(n: int) -> list[dict]:
        out = []
        for i in range(n):
            out.append(
                {
                    "symbol": f"C{i:03d}USDT",
                    "state_up": "CONFIRMED",
                    "state_down": "ELIMINATED",
                    "liquidity_hard_pass": True,
                    "data_mode": "LIVE",
                    "momentum_score_up": 50,
                    "consistency_up": 0.0,
                    "momentum_score_down": 50,
                    "consistency_down": 0.0,
                }
            )
        return out

    def _alerts(n: int) -> list[str]:
        rows = _rows(n)
        board = {"long_pool": [], "short_pool": [], "meta": {}}
        _occ, _ctrl, alerts = decorate_board(
            board,
            rows=rows,
            dmr_msgs=[],
            dmr_rank={},
            daily_unique=None,
            settings=SelectionSettings(),
            cfg=cfg,
        )
        assert occupancy_from_rows(rows)["confirmed_unique"] == n
        return alerts

    mid = _alerts(25)
    assert "CONFIRM_OVERFLOW" not in mid, mid
    assert "CONFIRM_UNDERFILLED" not in mid, mid
    hi = _alerts(41)
    assert "CONFIRM_OVERFLOW" in hi, hi
    lo = _alerts(9)
    assert "CONFIRM_UNDERFILLED" in lo, lo


def test_occupancy_band_is_consistent_across_all_three_places():
    """占用目标带在三处必须逐值一致，否则页头说一套、告警做另一套。

    三处：StateConfig（权威）/ 前端 occupancy.ts 镜像 / param yaml 人读登记。
    这两项不进 config_hash 也不进 param_hash（刻意：它们不改变选币结果），
    因此改动**不留身份痕迹** —— 没有这条断言，三处漂开了也没人会发现。
    """
    import re as _re
    from pathlib import Path as _P

    from coin_selection.state_machine import StateConfig as _SC

    root = _P(__file__).resolve().parents[3]
    lo, hi = _SC().occupancy_lo, _SC().occupancy_hi

    ts = (root / "apps/web/src/shared/config/occupancy.ts").read_text(encoding="utf-8")
    m_lo = _re.search(r"CONFIRMED_OCCUPANCY_LO\s*=\s*(\d+)", ts)
    m_hi = _re.search(r"CONFIRMED_OCCUPANCY_HI\s*=\s*(\d+)", ts)
    assert m_lo and m_hi, "前端 occupancy.ts 找不到 LO/HI 常量"
    assert (int(m_lo.group(1)), int(m_hi.group(1))) == (lo, hi), (
        f"前端镜像 {m_lo.group(1)}-{m_hi.group(1)} != StateConfig {lo}-{hi}"
    )

    yml = (root / "packages/config/param-v2.0.0-screener-y.yaml").read_text(encoding="utf-8")
    seg = yml.split("occupancy_band:", 1)
    assert len(seg) == 2, "param yaml 缺 occupancy_band 登记段"
    body = seg[1].split("constants_tech_debt:", 1)[0]
    y_lo = _re.search(r"^\s*lo:\s*(\d+)", body, _re.M)
    y_hi = _re.search(r"^\s*hi:\s*(\d+)", body, _re.M)
    assert y_lo and y_hi, "occupancy_band 段缺 lo/hi"
    assert (int(y_lo.group(1)), int(y_hi.group(1))) == (lo, hi), (
        f"yaml 登记 {y_lo.group(1)}-{y_hi.group(1)} != StateConfig {lo}-{hi}"
    )


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
