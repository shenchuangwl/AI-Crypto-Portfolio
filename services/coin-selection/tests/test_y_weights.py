"""选币榜Y To-Be 权重（阶段 2）：w_mcap=0.25 > w_mom=0.20，组合门仍关。

主板 ``scan.SelectionSettings`` 默认值不得被改（0.30/0.25/0.10）。
机器入口只有 ``boards[key=y].overrides.settings``。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.board_variants import BOARD_Y_KEY, get_variant, variant_settings
from coin_selection.scan import SelectionSettings, composite_score


TOBE = dict(w_ss=0.20, w_mom=0.20, w_liq=0.15, w_mcap=0.25, w_cons=0.10, w_rank=0.05, w_risk=0.05)
MAIN = dict(w_ss=0.30, w_mom=0.25, w_liq=0.15, w_mcap=0.10, w_cons=0.10, w_rank=0.05, w_risk=0.05)


def test_main_defaults_untouched():
    s = SelectionSettings()
    assert abs(s.w_ss - 0.30) < 1e-12
    assert abs(s.w_mom - 0.25) < 1e-12
    assert abs(s.w_mcap - 0.10) < 1e-12
    assert abs(s.w_ss + s.w_mom + s.w_liq + s.w_mcap + s.w_cons + s.w_rank + s.w_risk - 1.0) < 1e-12
    assert not getattr(s, "enable_mcap_zone", False)


def test_y_live_overrides_are_tobe_weights():
    y = get_variant(BOARD_Y_KEY)
    ov = y.settings_overrides
    for k, v in TOBE.items():
        assert abs(float(ov[k]) - v) < 1e-12, (k, ov.get(k), v)
    assert abs(ov["w_mcap"] - ov["w_mom"]) > 0
    assert ov["w_mcap"] > ov["w_mom"]
    assert "enable_mcap_zone" not in ov  # 阶段 2 不钉开关
    s = variant_settings(SelectionSettings(), y)
    for k, v in TOBE.items():
        assert abs(getattr(s, k) - v) < 1e-12
    assert abs(s.w_ss + s.w_mom + s.w_liq + s.w_mcap + s.w_cons + s.w_rank + s.w_risk - 1.0) < 1e-12
    assert s.w_mcap > s.w_mom
    assert s.parameter_version == "param-v2.0.0-screener-y"
    assert s.data_dir.endswith("data/coin-selection-y")
    assert s.enable_mcap_zone is False


def test_same_components_two_scores_are_hand_computable():
    main = SelectionSettings()
    y = variant_settings(SelectionSettings(), get_variant(BOARD_Y_KEY))
    kwargs = dict(ss=70.0, mom=70.0, liq=80.0, mcap=50.0, cons=1.0, dq=100.0)
    a = composite_score(main, **kwargs)
    b = composite_score(y, **kwargs)
    # Δraw = -0.10·SS -0.05·M +0.15·S_MC = -7 -3.5 +7.5 = -3.0
    assert abs((b - a) - (-3.0)) < 1e-9
    kwargs2 = dict(ss=50.0, mom=55.0, liq=80.0, mcap=80.0, cons=1.0, dq=100.0)
    a2 = composite_score(main, **kwargs2)
    b2 = composite_score(y, **kwargs2)
    # Δraw = -0.10*50 -0.05*55 +0.15*80 = -5 -2.75 +12 = +4.25
    assert abs((b2 - a2) - 4.25) < 1e-9


def test_y_yaml_status_tuned():
    root = Path(__file__).resolve().parents[3]
    text = (root / "packages" / "config" / "param-v2.0.0-screener-y.yaml").read_text(encoding="utf-8")
    assert "TUNED_V2_MCAP_COMBO" in text
    assert "CLONE_PENDING_TUNING" not in text.split("status:")[1].splitlines()[0]
    assert "ss: 0.20" in text
    assert "mcap: 0.25" in text
    assert "momentum: 0.20" in text


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
