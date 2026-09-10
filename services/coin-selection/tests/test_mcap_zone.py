"""216 组合天花板层 —— 文档B §9.1 的测试 T8–T12 / T14。

    PYTHONPATH=services/coin-selection/src python3 services/coin-selection/tests/test_mcap_zone.py

覆盖：
  T8  SelectionSettings 的 6 个新字段缺省全部为关闭态
  T9  代码内 216 查表与 scripts/mcap_combo_zones.py 输出**逐格相等**；9 条恒等式再跑一遍
  T10 **只降不升**：216 组合 × 5 状态 × 2 方向 穷举 2,160 例
  T11 **开关关 = 恒等映射**：mode="off" 时输出恒等于输入（红线第 14 条）
  T12 8 个新 reason_codes 的触发条件；grade=None → MCAP_ZONE_ABSTAIN + 降一级、观察止步
  T14 shadow 模式：state 列与 off 模式 100% 相同，但新字段已写
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from fractions import Fraction as F
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection import mcap_zone as mz  # noqa: E402
from coin_selection.board_variants import mcap_zone_mode_of  # noqa: E402
from coin_selection.scan import SelectionSettings  # noqa: E402
from coin_selection.state_machine import StateConfig  # noqa: E402


def _load_generator():
    """把 scripts/mcap_combo_zones.py 当模块加载 —— 它是 216 表的第二份独立实现。"""
    path = ROOT / "scripts" / "mcap_combo_zones.py"
    spec = importlib.util.spec_from_file_location("_mcap_combo_zones", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


GEN = _load_generator()

ALL_STATES = ("DMR", "CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED")
SM_STATES = ("CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED", "NONE")


def _row(g30=None, g2h=None, g6h=None, **kw):
    r = {
        "symbol": kw.pop("symbol", "TESTUSDT"),
        "mcap_grade_30m": g30,
        "mcap_grade_2h": g2h,
        "mcap_grade_6h": g6h,
        "state_up": kw.pop("state_up", "CONFIRMED"),
        "state_down": kw.pop("state_down", "ELIMINATED"),
        "momentum_score_up": 70.0,
        "momentum_score_down": 30.0,
        "consistency_up": 1.0,
        "consistency_down": 0.0,
        "ss_up": 60.0,
        "ss_down": 10.0,
    }
    r.update(kw)
    return r


# ---------------------------------------------------------------------------
# T8 —— 缺省全部为关闭态
# ---------------------------------------------------------------------------
def test_t8_defaults_are_off():
    s = SelectionSettings()
    assert s.enable_mcap_zone is False
    assert s.mcap_zone_mode == "off"
    assert mcap_zone_mode_of(s) == "off"
    # 4 个切点的缺省值 = 文档A §4.4
    assert abs(s.mcap_zone_cut_dmr - 2.1) < 1e-12
    assert abs(s.mcap_zone_cut_confirmed - 1.5) < 1e-12
    assert abs(s.mcap_zone_cut_qualified - 0.8) < 1e-12
    assert abs(s.mcap_zone_cut_watch - (-0.5)) < 1e-12
    assert s.mcap_zone_abstain_demote is True


def test_t8_mode_alias_and_bad_value_degrade_to_off():
    from dataclasses import replace

    s = SelectionSettings()
    assert mcap_zone_mode_of(replace(s, enable_mcap_zone=True)) == "on"
    assert mcap_zone_mode_of(replace(s, mcap_zone_mode="shadow")) == "shadow"
    assert mcap_zone_mode_of(replace(s, mcap_zone_mode="on")) == "on"
    # 坏开关必须退化为「现网行为」，不是「未定义行为」
    assert mcap_zone_mode_of(replace(s, mcap_zone_mode="ON_PLEASE")) == "off"
    assert mcap_zone_mode_of(replace(s, mcap_zone_mode="")) == "off"


# ---------------------------------------------------------------------------
# T9 —— 与生成器逐格相等 + 9 条恒等式
# ---------------------------------------------------------------------------
def test_t9_table_matches_generator_cell_by_cell():
    gen_rows = {r["组合"]: r for r in GEN.build()}
    code_rows = {r["combo"]: r for r in mz.build_table()}
    assert len(gen_rows) == 216 and len(code_rows) == 216
    for combo, g in gen_rows.items():
        c = code_rows[combo]
        assert c["no"] == g["编号"], combo
        assert c["z_up"] == g["Z_up"], combo
        assert c["z_down"] == g["Z_down"], combo
        assert c["priority"] == g["优先级"], combo
        assert c["trend"] == g["趋势分类"], combo
        assert c["macro"] == g["宏观模式"], combo
        assert c["ceiling_up"] == g["上涨侧天花板"], (combo, c["ceiling_up"], g["上涨侧天花板"])
        assert c["ceiling_down"] == g["下跌侧天花板"], combo


def test_t9_nine_assertions_pass_in_code():
    ok = mz.run_assertions()
    assert len(ok) == 9, ok
    assert ok[0].startswith("A1") and ok[-1].startswith("A9")


def test_t9_census_matches_report():
    """文档A §4.4：每侧 DMR 13 / 确认 22 / 符合 32 / 观察 72 / 淘汰 77，严格镜像。"""
    up = mz.census("up")
    down = mz.census("down")
    assert up == {"DMR": 13, "CONFIRMED": 22, "QUALIFIED": 32, "WATCH": 72, "ELIMINATED": 77}, up
    assert up == down
    # 文档A §4.3 的优先级与趋势分布
    tbl = mz.build_table()
    pri = {p: sum(1 for r in tbl if r["priority"] == p) for p in (1, 2, 3, 4, 5)}
    assert pri == {1: 28, 2: 54, 3: 74, 4: 46, 5: 14}, pri
    trend = {t: sum(1 for r in tbl if r["trend"] == t) for t in ("偏多", "中性/冲突", "偏空")}
    assert trend == {"偏多": 94, "中性/冲突": 28, "偏空": 94}, trend


def test_t9_z_agrees_with_mcap_combo_z10():
    """与上一轮 mcap_combo 的整数 Z10 交叉核对：两个模块的 Z 必须是同一个量。"""
    from coin_selection import mcap_combo as mc

    for g30, g2h, g6h in product(mz.GRADES, repeat=3):
        assert mz.z10(g30, g2h, g6h) == mc.z10(g30, g2h, g6h), (g30, g2h, g6h)


# ---------------------------------------------------------------------------
# T10 —— 只降不升，穷举 216 × 5 × 2 = 2,160 例
# ---------------------------------------------------------------------------
def test_t10_monotone_demote_only_exhaustive():
    settings = SelectionSettings()
    cfg = StateConfig()
    n = 0
    for g30, g2h, g6h in product(mz.GRADES, repeat=3):
        for state in ALL_STATES:
            for direction in ("up", "down"):
                row = _row(g30, g2h, g6h, state_up=state, state_down=state)
                res = mz.resolve_row_side(
                    row, direction, state, settings=settings, cfg=cfg
                )
                out = res["final_state"]
                assert mz.ZONE_RANK[out] >= mz.ZONE_RANK[state], (
                    g30 + g2h + g6h,
                    state,
                    direction,
                    out,
                )
                n += 1
    assert n == 216 * 5 * 2 == 2160, n


def test_t10_ceiling_never_promotes_watch_to_dmr():
    """WATCH + 天花板 DMR（AAA/up）仍是 WATCH —— 天花板只解除阻挡，不新建状态。"""
    settings = SelectionSettings()
    res = mz.resolve_row_side(
        _row("A", "A", "A", state_up="WATCH"), "up", "WATCH", settings=settings, cfg=StateConfig()
    )
    assert res["combo_zone"] == "DMR"
    assert res["final_state"] == "WATCH"
    assert not any(c.startswith(mz.RC_ZONE_CAP) for c in res["reason_codes"])


def test_t10_eliminated_is_untouched():
    settings = SelectionSettings()
    for direction in ("up", "down"):
        res = mz.resolve_row_side(
            _row("A", "A", "A", state_up="ELIMINATED", state_down="ELIMINATED"),
            direction,
            "ELIMINATED",
            settings=settings,
            cfg=StateConfig(),
        )
        assert res["final_state"] == "ELIMINATED"
        assert res["zone_ceiling"] is None  # 不在 CAPPABLE_STATES 内，天花板不作用


def test_t10_demote_one_level_floors_at_watch():
    assert mz.demote_one_level("DMR") == "CONFIRMED"
    assert mz.demote_one_level("CONFIRMED") == "QUALIFIED"
    assert mz.demote_one_level("QUALIFIED") == "WATCH"
    assert mz.demote_one_level("WATCH") == "WATCH"  # 在观察止步
    assert mz.demote_one_level("ELIMINATED") == "ELIMINATED"


# ---------------------------------------------------------------------------
# T11 —— 开关关 = 恒等映射（红线第 14 条）
# ---------------------------------------------------------------------------
def test_t11_off_is_identity():
    rows = [
        _row("A", "A", "A", symbol="AAAUSDT", state_up="CONFIRMED"),
        _row("F", "F", "F", symbol="FFFUSDT", state_up="CONFIRMED"),
        _row(None, "B", "C", symbol="NULLUSDT", state_up="QUALIFIED"),
    ]
    before = copy.deepcopy(rows)
    meta = mz.apply_mcap_ceiling(rows, mode="off", settings=SelectionSettings(), cfg=StateConfig())
    assert rows == before, "mode=off 写了字段 —— 违反红线第 14 条"
    assert meta["mode"] == "off"
    assert meta["capped"] == 0
    # 未知模式同样退化为恒等映射
    rows2 = copy.deepcopy(before)
    mz.apply_mcap_ceiling(rows2, mode="ON_PLEASE", settings=SelectionSettings(), cfg=StateConfig())
    assert rows2 == before


def test_t11_board_from_rows_writes_no_keys_when_off():
    """快照层的恒等性：off 时 board_from_rows 一个天花板键都不写。"""
    from coin_selection.scan import board_from_rows

    rows = [
        {
            "symbol": "AAAUSDT",
            "base_asset": "AAA",
            "state_up": "CONFIRMED",
            "state_down": "ELIMINATED",
            "score_up": 70.0,
            "score_down": 20.0,
            "ss_up": 60.0,
            "ss_down": 5.0,
            "momentum_score_up": 70.0,
            "momentum_score_down": 30.0,
            "consistency_up": 1.0,
            "consistency_down": 0.0,
            "mcap_momentum_score_up": 60.0,
            "mcap_momentum_score_down": 40.0,
            "mcap_grade_30m": "A",
            "mcap_grade_2h": "A",
            "mcap_grade_6h": "A",
            "liquidity_hard_pass": True,
            "data_quality_score": 90.0,
            "last_price": 1.0,
        }
    ]
    pool = board_from_rows(rows, "up")
    assert "zone_ceiling" not in pool[0]
    assert "combo_code" not in pool[0]
    assert "z_score" not in pool[0]


# ---------------------------------------------------------------------------
# T12 —— 8 个新 reason_codes
# ---------------------------------------------------------------------------
def test_t12_abstain_demotes_one_level_and_floors_at_watch():
    s = SelectionSettings()
    cfg = StateConfig()
    for state, want in (("CONFIRMED", "QUALIFIED"), ("QUALIFIED", "WATCH"), ("WATCH", "WATCH")):
        res = mz.resolve_row_side(_row(None, "A", "A"), "up", state, settings=s, cfg=cfg)
        assert mz.RC_ZONE_ABSTAIN in res["reason_codes"], state
        assert res["combo_code"] is None
        assert res["final_state"] == want, (state, res["final_state"])


def test_t12_abstain_can_be_switched_off():
    from dataclasses import replace

    s = replace(SelectionSettings(), mcap_zone_abstain_demote=False)
    res = mz.resolve_row_side(_row("A", None, "A"), "up", "CONFIRMED", settings=s, cfg=StateConfig())
    assert mz.RC_ZONE_ABSTAIN in res["reason_codes"]
    assert res["final_state"] == "CONFIRMED"


def test_t12_zone_on_and_combo_codes():
    res = mz.resolve_row_side(
        _row("A", "B", "C"), "up", "CONFIRMED", settings=SelectionSettings(), cfg=StateConfig()
    )
    assert mz.RC_ZONE_ON in res["reason_codes"]
    assert "COMBO_ABC" in res["reason_codes"]
    assert res["combo_code"] == "ABC"


def test_t12_cap_code_names_the_target_zone():
    # FFF 的 Z_up = -3 → 上涨侧天花板 ELIMINATED；CONFIRMED 被压到淘汰
    res = mz.resolve_row_side(
        _row("F", "F", "F"), "up", "CONFIRMED", settings=SelectionSettings(), cfg=StateConfig()
    )
    assert res["combo_zone"] == "ELIMINATED"
    assert res["final_state"] == "ELIMINATED"
    assert f"{mz.RC_ZONE_CAP}ELIMINATED" in res["reason_codes"]


def test_t12_p2_soften_when_structure_good_but_momentum_missing():
    """P2：Z ≥ 1.5 但 M < mom_qualified(55) → 天花板降一级 + MCAP_STRUCT_NO_MOM。"""
    cfg = StateConfig()
    row = _row("A", "A", "A", momentum_score_up=40.0, consistency_up=1.0)
    res = mz.resolve_row_side(row, "up", "CONFIRMED", settings=SelectionSettings(), cfg=cfg)
    assert mz.RC_STRUCT_NO_MOM in res["reason_codes"]
    assert res["combo_zone"] == "DMR"
    assert res["zone_ceiling"] == "CONFIRMED"  # DMR 降一级
    assert res["final_state"] == "CONFIRMED"


def test_t12_p3_veto_when_opposite_momentum_is_strong():
    """P3：Z ≥ 1.5 且反向动量 ≥70 且反向一致性 =1 → 压到观察 + MCAP_MOM_CONTRADICT。"""
    row = _row(
        "A", "A", "A",
        momentum_score_up=80.0,
        consistency_up=1.0,
        momentum_score_down=75.0,
        consistency_down=1.0,
    )
    res = mz.resolve_row_side(row, "up", "CONFIRMED", settings=SelectionSettings(), cfg=StateConfig())
    assert mz.RC_MOM_CONTRADICT in res["reason_codes"]
    assert res["zone_ceiling"] == "WATCH"
    assert res["final_state"] == "WATCH"
    # P3 优先于 P2：动量不足码不得同时出现
    assert mz.RC_STRUCT_NO_MOM not in res["reason_codes"]


def test_t12_ss_mcap_aligned_adds_no_bonus():
    """互印证成立只写码，不做任何加成（只降不升的直接推论）。"""
    row = _row("A", "A", "A", ss_up=60.0, momentum_score_up=80.0, consistency_up=1.0)
    res = mz.resolve_row_side(row, "up", "CONFIRMED", settings=SelectionSettings(), cfg=StateConfig())
    assert mz.RC_SS_ALIGNED in res["reason_codes"]
    assert res["zone_ceiling"] == "DMR"          # 名义天花板不变
    assert res["final_state"] == "CONFIRMED"     # 状态机分区不动 —— 没有被「升」


def test_t12_ss_mcap_divergent_caps_at_qualified():
    """SS ≥ dmr_ss(55) 但 Z_方向 ≤ −0.8 → 压到符合区 + SS_MCAP_DIVERGENT。"""
    row = _row("F", "F", "F", ss_up=60.0, momentum_score_up=70.0, consistency_up=1.0)
    res = mz.resolve_row_side(row, "up", "CONFIRMED", settings=SelectionSettings(), cfg=StateConfig())
    assert mz.RC_SS_DIVERGENT in res["reason_codes"]
    # FFF 的名义天花板已是淘汰，背离压制取 worse ⇒ 仍是淘汰（只降不升）
    assert mz.ZONE_RANK[res["zone_ceiling"]] >= mz.ZONE_RANK["QUALIFIED"]
    assert res["final_state"] == "ELIMINATED"

    # 名义天花板较松的一格：CCF（Z_up = 0.6·(1/3) + 0.9·(1/3) + 1.5·(−1) = −1.0）
    # 上涨侧天花板 = WATCH；背离压制取 worse(WATCH, QUALIFIED) = WATCH
    row2 = _row("A", "A", "F", ss_up=60.0)
    res2 = mz.resolve_row_side(row2, "up", "CONFIRMED", settings=SelectionSettings(), cfg=StateConfig())
    assert res2["final_state"] in ("QUALIFIED", "WATCH", "ELIMINATED")
    assert mz.ZONE_RANK[res2["final_state"]] >= mz.ZONE_RANK["CONFIRMED"]


def test_t12_all_eight_reason_codes_exist():
    assert len(mz.NEW_REASON_CODES) == 8
    assert set(mz.NEW_REASON_CODES) == {
        "MCAP_ZONE_ON",
        "MCAP_ZONE_CAP_",
        "MCAP_ZONE_ABSTAIN",
        "MCAP_STRUCT_NO_MOM",
        "MCAP_MOM_CONTRADICT",
        "SS_MCAP_ALIGNED",
        "SS_MCAP_DIVERGENT",
        "COMBO_",
    }


# ---------------------------------------------------------------------------
# T14 —— shadow 模式：算并落库，但不改分区
# ---------------------------------------------------------------------------
def _sample_rows():
    out = []
    for i, (g30, g2h, g6h) in enumerate(
        [("A", "A", "A"), ("F", "F", "F"), ("A", "B", "C"), (None, "A", "A"), ("D", "E", "F")]
    ):
        out.append(
            _row(
                g30,
                g2h,
                g6h,
                symbol=f"S{i}USDT",
                state_up=("CONFIRMED", "QUALIFIED", "WATCH", "CONFIRMED", "ELIMINATED")[i],
                state_down=("QUALIFIED", "CONFIRMED", "ELIMINATED", "WATCH", "CONFIRMED")[i],
            )
        )
    return out


def test_t14_shadow_leaves_state_untouched():
    base = _sample_rows()
    off_rows = copy.deepcopy(base)
    mz.apply_mcap_ceiling(off_rows, mode="off", settings=SelectionSettings(), cfg=StateConfig())
    sh_rows = copy.deepcopy(base)
    meta = mz.apply_mcap_ceiling(sh_rows, mode="shadow", settings=SelectionSettings(), cfg=StateConfig())
    assert meta["mode"] == "shadow"
    for a, b in zip(off_rows, sh_rows):
        assert a["state_up"] == b["state_up"], (a["symbol"], a["state_up"], b["state_up"])
        assert a["state_down"] == b["state_down"], a["symbol"]
    # 但新字段全都写了
    for r in sh_rows:
        assert "zone_ceiling_up" in r and "zone_ceiling_down" in r
        assert "z_score_up" in r and "product_zone_y_up" in r
        assert "mcap_zone_codes_up" in r


def test_t14_on_rewrites_state_to_capped_zone():
    rows = _sample_rows()
    meta = mz.apply_mcap_ceiling(rows, mode="on", settings=SelectionSettings(), cfg=StateConfig())
    assert meta["mode"] == "on"
    for r in rows:
        for d in ("up", "down"):
            assert r[f"state_{d}"] == r[f"product_zone_y_{d}"], (r["symbol"], d)
    assert meta["capped"] >= 1


def test_t14_shadow_and_on_agree_on_the_ceiling():
    """shadow 与 on 唯一的差别就是「写不写回 state」。"""
    sh = _sample_rows()
    on = _sample_rows()
    mz.apply_mcap_ceiling(sh, mode="shadow", settings=SelectionSettings(), cfg=StateConfig())
    mz.apply_mcap_ceiling(on, mode="on", settings=SelectionSettings(), cfg=StateConfig())
    for a, b in zip(sh, on):
        for d in ("up", "down"):
            assert a[f"zone_ceiling_{d}"] == b[f"zone_ceiling_{d}"]
            assert a[f"product_zone_y_{d}"] == b[f"product_zone_y_{d}"]
            assert a[f"z_score_{d}"] == b[f"z_score_{d}"]


# ---------------------------------------------------------------------------
# 切点：唯一可调参数，且必须精确
# ---------------------------------------------------------------------------
def test_cuts_are_read_from_settings_exactly():
    from dataclasses import replace

    s = replace(SelectionSettings(), mcap_zone_cut_dmr=2.1)
    cuts = mz.cuts_from_settings(s)
    # Fraction("2.1") 精确 = 21/10；Fraction(2.1) 会带二进制误差
    assert cuts["DMR"] == F(21, 10), cuts["DMR"]
    assert cuts["CONFIRMED"] == F(3, 2)
    assert cuts["QUALIFIED"] == F(4, 5)
    assert cuts["WATCH"] == F(-1, 2)


def test_cut_boundary_is_closed_below():
    """恰好落在切点上归**更松**的那一档（`Z >= 切点`）。"""
    assert mz.ceiling_for_z(F(21, 10)) == "DMR"
    assert mz.ceiling_for_z(F(21, 10) - F(1, 100)) == "CONFIRMED"
    assert mz.ceiling_for_z(F(-1, 2)) == "WATCH"
    assert mz.ceiling_for_z(F(-1, 2) - F(1, 100)) == "ELIMINATED"


def test_looser_cuts_can_only_loosen_never_invert():
    """改切点仍不破坏单调性 —— A6 在任意合法切点下都成立。"""
    from dataclasses import replace

    s = replace(
        SelectionSettings(),
        mcap_zone_cut_dmr=2.8,
        mcap_zone_cut_confirmed=2.0,
        mcap_zone_cut_qualified=1.0,
        mcap_zone_cut_watch=-1.0,
    )
    ok = mz.run_assertions(mz.cuts_from_settings(s))
    assert len(ok) == 9
    cen = mz.census("up", mz.cuts_from_settings(s))
    assert sum(cen.values()) == 216


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
