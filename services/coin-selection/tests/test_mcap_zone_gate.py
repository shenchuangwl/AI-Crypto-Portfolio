"""阶段 3：216 天花板合取旁路。禁止改 SM 边；ENABLE=0 无字段。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.mcap_combo import apply_to_board, zone_enabled_for_variant
from coin_selection.scan import SelectionSettings, board_from_rows, build_dmr_messages
from coin_selection.state_machine import default_state_config
from datetime import datetime, timezone


def _row(**kw):
    base = {
        "symbol": "AAAUSDT",
        "base_asset": "AAA",
        "liquidity_hard_pass": True,
        "liquidity_grade": "A",
        "state_up": "CONFIRMED",
        "state_down": "ELIMINATED",
        "state_up_dwell_min": 30.0,
        "state_down_dwell_min": 0.0,
        "score_up": 80.0,
        "score_down": 20.0,
        "momentum_score_up": 70.0,
        "momentum_score_down": 30.0,
        "consistency_up": 1.0,
        "consistency_down": 0.0,
        "ss_up": 60.0,
        "ss_down": 10.0,
        "mcap_momentum_score_up": 70.0,
        "mcap_momentum_score_down": 40.0,
        "liquidity_score_abs": 80.0,
        "data_quality_score": 90.0,
        "data_mode": "LIVE",
        "supply_missing": False,
        "mcap_grade_30m": "A",
        "mcap_grade_2h": "A",
        "mcap_grade_6h": "A",
        "confirmed_path_up": "S",
        "last_price": 1.0,
    }
    base.update(kw)
    return base


def test_enable_default_false_and_env_only_for_y():
    s = SelectionSettings()
    assert s.enable_mcap_zone is False
    os.environ.pop("ENABLE_MCAP_ZONE", None)
    assert zone_enabled_for_variant(s, variant_key="main") is False
    assert zone_enabled_for_variant(s, variant_key="y") is False
    os.environ["ENABLE_MCAP_ZONE"] = "1"
    try:
        assert zone_enabled_for_variant(s, variant_key="main") is False
        assert zone_enabled_for_variant(s, variant_key="y") is True
        s2 = SelectionSettings()
        s2.enable_mcap_zone = True
        assert zone_enabled_for_variant(s2, variant_key="main") is True
    finally:
        os.environ.pop("ENABLE_MCAP_ZONE", None)


def test_enable_off_writes_no_fields():
    rows = [_row()]
    meta = apply_to_board(rows, enabled=False)
    assert meta["enabled"] is False
    assert "mcap_combo" not in rows[0]
    assert "product_zone_up" not in rows[0]
    pool = board_from_rows(rows, "up", now=datetime(2026, 8, 31, tzinfo=timezone.utc))
    assert "product_zone" not in pool[0]


def test_aaa_confirmed_may_enter_dmr():
    rows = [_row()]
    meta = apply_to_board(rows, enabled=True)
    assert meta["enabled"] is True
    assert rows[0]["mcap_combo"] == "AAA"
    assert rows[0]["product_zone_up"] == "CONFIRMED"
    assert rows[0]["mcap_ceiling_up"] == "DMR"
    assert rows[0]["combo_block_dmr_up"] is False
    now = datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)
    msgs = build_dmr_messages(
        rows, settings=SelectionSettings(), anchor=now, scan_id="20260831-016", seq=16, now=now
    )
    assert any(m["symbol"] == "AAAUSDT" and m["direction"] == "LONG" for m in msgs)


def test_aaf_blocks_dmr_and_demotes_product():
    rows = [_row(mcap_grade_30m="A", mcap_grade_2h="A", mcap_grade_6h="F")]
    apply_to_board(rows, enabled=True)
    assert rows[0]["mcap_combo"] == "AAF"
    assert rows[0]["mcap_ceiling_up"] == "淘汰"
    assert rows[0]["product_zone_up"] == "ELIMINATED"
    assert rows[0]["combo_block_dmr_up"] is True
    assert rows[0]["state_up"] == "CONFIRMED"  # SM 边未改
    now = datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)
    msgs = build_dmr_messages(
        rows, settings=SelectionSettings(), anchor=now, scan_id="20260831-016", seq=16, now=now
    )
    assert not any(m["symbol"] == "AAAUSDT" for m in msgs)


def test_watch_plus_dmr_ceiling_never_promotes():
    rows = [_row(state_up="WATCH", mcap_grade_30m="A", mcap_grade_2h="A", mcap_grade_6h="A")]
    apply_to_board(rows, enabled=True)
    assert rows[0]["product_zone_up"] == "WATCH"
    assert rows[0]["mcap_ceiling_up"] == "DMR"


def test_null_grades_are_watch_ceiling():
    rows = [_row(state_up="WATCH", mcap_grade_30m=None, mcap_grade_2h="A", mcap_grade_6h="A")]
    apply_to_board(rows, enabled=True)
    assert rows[0]["mcap_combo"] is None
    assert rows[0]["product_zone_up"] == "WATCH"
    assert rows[0]["combo_reason_up"] == "mcap_combo_incomplete"


def test_confirmed_plus_qual_ceiling_immediate_product_demote():
    # AAC = 确定天花板（上涨）
    rows = [_row(mcap_grade_30m="A", mcap_grade_2h="A", mcap_grade_6h="C")]
    apply_to_board(rows, enabled=True)
    assert rows[0]["mcap_ceiling_up"] == "确定"
    assert rows[0]["product_zone_up"] == "CONFIRMED"
    assert rows[0]["combo_block_dmr_up"] is True  # 天花板≠DMR
    # 换成符合天花板：ADA 上涨侧符合
    rows2 = [_row(mcap_grade_30m="A", mcap_grade_2h="D", mcap_grade_6h="A")]
    apply_to_board(rows2, enabled=True)
    assert rows2[0]["mcap_ceiling_up"] == "符合"
    assert rows2[0]["product_zone_up"] == "QUALIFIED"  # 立即
    assert rows2[0]["state_up"] == "CONFIRMED"


def test_qual_to_watch_hysteresis_two_nodes():
    tmp = Path(tempfile.mkdtemp(prefix="combo-hyst-"))
    rows = [_row(state_up="QUALIFIED", mcap_grade_30m="A", mcap_grade_2h="A", mcap_grade_6h="D")]
    # AAD 上涨侧观察
    apply_to_board(rows, enabled=True, data_dir=tmp, scan_id="s1")
    assert rows[0]["product_zone_up"] == "QUALIFIED"
    assert rows[0]["combo_demote_streak_up"] == 1
    apply_to_board(rows, enabled=True, data_dir=tmp, scan_id="s2")
    assert rows[0]["product_zone_up"] == "WATCH"
    assert rows[0]["combo_demote_streak_up"] == 0


def test_hard_fail_sm_elim_covers_aaa():
    rows = [_row(state_up="ELIMINATED", liquidity_hard_pass=False)]
    apply_to_board(rows, enabled=True)
    assert rows[0]["product_zone_up"] == "ELIMINATED"
    assert rows[0]["mcap_ceiling_up"] == "DMR"


def test_missing_mapping_degrades_to_off():
    rows = [_row()]
    meta = apply_to_board(rows, enabled=True, mapping=[])
    assert meta["enabled"] is False
    assert meta["reason"] == "mapping_unavailable"
    assert "product_zone_up" not in rows[0]


def test_board_from_rows_omits_keys_when_source_has_none():
    rows = [_row()]
    pool = board_from_rows(rows, "up", now=datetime(2026, 8, 31, tzinfo=timezone.utc))
    assert "product_zone" not in pool[0]
    apply_to_board(rows, enabled=True)
    pool2 = board_from_rows(rows, "up", now=datetime(2026, 8, 31, tzinfo=timezone.utc))
    assert pool2[0]["product_zone"] == "CONFIRMED"
    assert pool2[0]["mcap_combo"] == "AAA"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
