"""v2.0.0 流通市值主导层的不变量与业务断言。

覆盖本轮裁决的每一条：
* 裁决二·1  三周期 A–F 等级决定准入 / 退出 / 分区 / 优先级 / 排序
* 裁决二·2  动能降为辅助，不得覆盖 / 替代 / 逆转市值主导判定
* 裁决三·3  禁止静默降级：未授权即拒绝出数
* 裁决三·4  S_MC 与 mcap_grade 不得混淆
* 裁决三·6  旧版 (-Score,-SS,-M) 排序不得再主导
"""

from __future__ import annotations

import itertools
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import mcap_dominance as md  # noqa: E402
from coin_selection.mcap_effective import ZONE_RANK, ZONES_EN  # noqa: E402



# —— stdlib 替身：本仓库的测试用 `python3 <file>` 直接跑，不依赖 pytest ——
class _Approx:
    def __init__(self, v, abs=1e-9):
        self.v, self.abs = v, abs
    def __eq__(self, other):
        return abs(float(other) - float(self.v)) <= self.abs
    def __repr__(self):
        return f"approx({self.v})"


def approx(v, abs=1e-9):
    return _Approx(v, abs)


@contextmanager
def raises(exc, match=None):
    try:
        yield
    except exc as e:
        if match is not None and match not in str(e):
            raise AssertionError(f"异常信息不含 {match!r}: {e}") from None
        return
    raise AssertionError(f"未抛出 {exc.__name__}")


BUSINESS = ("DMR", "CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED")


def _row(**kw):
    """构造一个已经过 mcap_effective 的行。默认值中性、不触发任何谓词。"""
    base = {
        "symbol": "TESTUSDT",
        "base_state_up": "CONFIRMED",
        "state_up": "CONFIRMED",
        "effective_zone_up": "CONFIRMED",
        "mcap_ceiling_zone_up": "CONFIRMED",
        "mcap_combo_status_up": "COMPLETE",
        "mcap_z10_up": 12,
        "mcap_priority_up": 3,
        "mcap_resonance_k_up": 70,
        "score_up": 70.0,
        "ss_up": 55.0,
        "momentum_score_up": 58.0,
        "consistency_up": 2 / 3,
        # 反方向默认弱，避免误触 P3
        "momentum_score_down": 40.0,
        "consistency_down": 0.0,
        "ss_down": 10.0,
        "score_down": 30.0,
        "mcap_z10_down": -12,
    }
    base.update(kw)
    return base


CFG = md.DominanceConfig()


# --------------------------------------------------------------------------
# INV-1 单调性：本层只能保持或降级，永不升级
# --------------------------------------------------------------------------
def test_inv1_dominance_never_promotes_exhaustive():
    """穷举 分区 × 谓词触发 × 互印证触发，最终分区必不优于输入。"""
    checked = 0
    for eff, mom, cons_opp, mom_opp, ss, z10 in itertools.product(
        BUSINESS,
        (30.0, 54.0, 60.0, 75.0),
        (0.0, 1.0),
        (40.0, 70.0, 90.0),
        (40.0, 55.0, 80.0),
        (-25, -8, 0, 8, 16, 25),
    ):
        r = _row(
            base_state_up=eff,
            state_up=eff,
            effective_zone_up=eff,
            momentum_score_up=mom,
            momentum_score_down=mom_opp,
            consistency_down=cons_opp,
            ss_up=ss,
            mcap_z10_up=z10,
        )
        res = md.resolve_dominance(r, "up", cfg=CFG)
        assert ZONE_RANK[res["final_zone"]] >= ZONE_RANK[eff], (
            f"升级了: {eff} -> {res['final_zone']} "
            f"(mom={mom} mom_opp={mom_opp} cons_opp={cons_opp} ss={ss} z10={z10})"
        )
        checked += 1
    assert checked == 4 * 2 * 3 * 3 * 6 * len(BUSINESS)


def test_inv1_cap_and_demote_are_monotone():
    for z in ZONES_EN:
        for cap in ZONES_EN:
            assert ZONE_RANK[md.cap_at(z, cap)] >= ZONE_RANK[z]
        assert ZONE_RANK[md.demote_one(z)] >= ZONE_RANK[z]
    # 文档A §3.4：降级链在观察止步，淘汰不再下沉
    assert md.demote_one("WATCH") == "WATCH"
    assert md.demote_one("ELIMINATED") == "ELIMINATED"


# --------------------------------------------------------------------------
# INV-2 RankKey <= Score/100（文档A §5.6 唯一的可执行约束）
# --------------------------------------------------------------------------
def test_inv2_rank_key_never_exceeds_score():
    for eff, score, prio, k in itertools.product(
        BUSINESS, (0.0, 45.0, 70.0, 100.0), (1, 2, 3, 4, 5), (10, 40, 70, 100)
    ):
        r = _row(
            base_state_up=eff,
            state_up=eff,
            effective_zone_up=eff,
            score_up=score,
            mcap_priority_up=prio,
            mcap_resonance_k_up=k,
        )
        res = md.resolve_dominance(r, "up", cfg=CFG)
        assert res["rank_key"] <= score / 100.0 + 1e-12
        # W_final 的上界是 W_combo（<= RANK_NORM），不是 F_MAX ——
        # F_MAX 钳的是**因子乘积**，见 mcap_dominance.F_MIN/F_MAX 的注释。
        assert res["w_final"] <= md.RANK_NORM + 1e-12


def test_w_final_never_exceeds_w_combo():
    """动态因子只能下调：穷举下 W_final 恒 <= W_combo（裁决二·2 的机器表达）。"""
    for eff, mom, mom_opp, cons_opp, ss, z10, status in itertools.product(
        BUSINESS, (30.0, 50.0, 65.0), (40.0, 75.0), (0.0, 1.0),
        (40.0, 60.0), (-20, -10, 0, 10, 20), ("COMPLETE", "INCOMPLETE"),
    ):
        r = _row(
            base_state_up=eff, state_up=eff, effective_zone_up=eff,
            momentum_score_up=mom, momentum_score_down=mom_opp,
            consistency_down=cons_opp, ss_up=ss, mcap_z10_up=z10,
            mcap_combo_status_up=status,
        )
        res = md.resolve_dominance(r, "up", cfg=CFG)
        assert res["w_final"] <= res["w_combo"] + 1e-12, (
            f"W_final {res['w_final']} > W_combo {res['w_combo']} "
            f"({eff} mom={mom} status={status})"
        )


def test_w_combo_theoretical_max_equals_rank_norm():
    """文档A §5.6 的归一化分母 1.210 = 1.00 × 1.10 × 1.10。"""
    top = CFG.base_weight("DMR") * CFG.prio_weight(5) * CFG.k_weight(100)
    assert top == approx(md.RANK_NORM, abs=1e-9)


def test_eliminated_w_base_is_hard_zero():
    r = _row(base_state_up="ELIMINATED", state_up="ELIMINATED", effective_zone_up="ELIMINATED")
    res = md.resolve_dominance(r, "up", cfg=CFG)
    assert res["w_base"] == 0.0
    assert res["w_final"] == 0.0
    assert res["rank_key"] == 0.0


# --------------------------------------------------------------------------
# 维度谓词 P1 / P2 / P3（文档A §5.2），优先级 P3 > P2 > P1
# --------------------------------------------------------------------------
def test_p3_veto_caps_at_watch():
    """市值方向强，但反方向动能 >=70 且一致性 =1 → 封到观察。"""
    r = _row(
        effective_zone_up="DMR",
        base_state_up="DMR",
        mcap_z10_up=20,           # Z = 2.0 >= 1.5
        momentum_score_down=75.0,
        consistency_down=1.0,
    )
    res = md.resolve_dominance(r, "up", cfg=CFG)
    assert res["predicate"] == "P3_VETO"
    assert res["final_zone"] == "WATCH"
    assert md.RC_P3 in res["dominance_reason_codes"]


def test_p2_soften_demotes_one_level():
    """市值方向强但本方向动能 <55 → 降一级。"""
    r = _row(
        effective_zone_up="CONFIRMED",
        base_state_up="CONFIRMED",
        mcap_z10_up=20,
        momentum_score_up=50.0,
    )
    res = md.resolve_dominance(r, "up", cfg=CFG)
    assert res["predicate"] == "P2_SOFTEN"
    assert res["final_zone"] == "QUALIFIED"


def test_p1_confirm_takes_table_value_without_bonus():
    """P1 命中只做确认，**不改分区、不加分**（文档A §5.2 action=查表值）。"""
    r = _row(momentum_score_up=65.0, consistency_up=1.0, mcap_z10_up=12)
    res = md.resolve_dominance(r, "up", cfg=CFG)
    assert res["predicate"] == "P1_CONFIRM"
    assert res["final_zone"] == "CONFIRMED"  # 未被改动


def test_predicate_priority_p3_beats_p2():
    """同时满足 P2 与 P3 时必须取 P3（文档A §5.2 末：P3 > P2 > P1）。"""
    r = _row(
        effective_zone_up="DMR",
        base_state_up="DMR",
        mcap_z10_up=20,
        momentum_score_up=50.0,    # 触发 P2
        momentum_score_down=75.0,  # 触发 P3
        consistency_down=1.0,
    )
    res = md.resolve_dominance(r, "up", cfg=CFG)
    assert res["predicate"] == "P3_VETO"


# --------------------------------------------------------------------------
# SS × 流通市值互印证（文档A §6）
# --------------------------------------------------------------------------
def test_crosscheck_divergent_caps_at_qualified():
    """楼梯强(SS>=55) 但市值方向相反(Z<=-0.8) → 封到符合。"""
    r = _row(effective_zone_up="DMR", base_state_up="DMR", ss_up=60.0, mcap_z10_up=-10)
    res = md.resolve_dominance(r, "up", cfg=CFG)
    assert res["crosscheck"] == "DIVERGENT"
    assert res["final_zone"] == "QUALIFIED"


def test_crosscheck_aligned_gives_no_bonus():
    """一致**不加分**（文档A §6 action: NONE）。"""
    r = _row(effective_zone_up="QUALIFIED", base_state_up="QUALIFIED", ss_up=60.0, mcap_z10_up=10)
    res = md.resolve_dominance(r, "up", cfg=CFG)
    assert res["crosscheck"] == "ALIGNED"
    assert res["final_zone"] == "QUALIFIED"  # 没被抬到确定


# --------------------------------------------------------------------------
# 裁决二·2：动能不得覆盖 / 替代 / 逆转市值主导判定
# --------------------------------------------------------------------------
def test_momentum_cannot_rescue_a_bad_ceiling():
    """天花板判淘汰时，动能与 Score 拉满也救不回来。"""
    r = _row(
        base_state_up="CONFIRMED",
        effective_zone_up="ELIMINATED",   # max(CONFIRMED, ELIMINATED) 已压到淘汰
        mcap_ceiling_zone_up="ELIMINATED",
        momentum_score_up=100.0,
        consistency_up=1.0,
        ss_up=100.0,
        score_up=100.0,
        mcap_z10_up=-30,
    )
    res = md.resolve_dominance(r, "up", cfg=CFG)
    assert res["final_zone"] == "ELIMINATED"
    assert res["rank_key"] == 0.0


def test_momentum_only_lowers_rank_weight_never_raises():
    """所有动态因子 <= 1：动能只能下调排序权重。"""
    strong = md.resolve_dominance(_row(momentum_score_up=65.0, consistency_up=1.0), "up", cfg=CFG)
    weak = md.resolve_dominance(_row(momentum_score_up=50.0, mcap_z10_up=20), "up", cfg=CFG)
    assert weak["w_final"] <= strong["w_final"]
    for name in ("f_p2_soften", "f_p3_veto", "f_divergent", "f_incomplete"):
        assert 0.0 < getattr(CFG, name) <= 1.0


# --------------------------------------------------------------------------
# INV-3 排序：三周期维度必须排在动能之前
# --------------------------------------------------------------------------
def test_inv3_ceiling_beats_score_in_ordering():
    """天花板更好的币，即便 Score 更低，也必须排在前面。"""
    good_ceiling = _row(
        symbol="AAA", final_zone_up="CONFIRMED", mcap_ceiling_zone_up="DMR",
        mcap_z10_up=20, mcap_resonance_k_up=100, rank_key_up=0.4, score_up=67.0,
    )
    high_score = _row(
        symbol="ZZZ", final_zone_up="CONFIRMED", mcap_ceiling_zone_up="QUALIFIED",
        mcap_z10_up=6, mcap_resonance_k_up=40, rank_key_up=0.3, score_up=99.0,
    )
    ordered = sorted([high_score, good_ceiling], key=lambda r: md.rank_tuple(r, "up"))
    assert ordered[0]["symbol"] == "AAA", "Score 99 不应压过更好的天花板"


def test_inv3_z_beats_momentum_in_ordering():
    """同分区同天花板时，Z 大的先于动能大的。"""
    hi_z = _row(symbol="AAA", final_zone_up="CONFIRMED", mcap_ceiling_zone_up="CONFIRMED",
                mcap_z10_up=18, momentum_score_up=61.0)
    hi_mom = _row(symbol="BBB", final_zone_up="CONFIRMED", mcap_ceiling_zone_up="CONFIRMED",
                  mcap_z10_up=10, momentum_score_up=99.0)
    ordered = sorted([hi_mom, hi_z], key=lambda r: md.rank_tuple(r, "up"))
    assert ordered[0]["symbol"] == "AAA"


def test_inv3_first_four_keys_are_all_mcap():
    """排序元组前四位必须全部来自流通市值维度（分区/天花板/Z/K）。"""
    r = _row(final_zone_up="CONFIRMED", mcap_ceiling_zone_up="DMR",
             mcap_z10_up=18, mcap_resonance_k_up=100)
    t = md.rank_tuple(r, "up")
    assert t[0] == ZONE_RANK["CONFIRMED"]
    assert t[1] == ZONE_RANK["DMR"]
    assert t[2] == approx(-1.8)
    assert t[3] == approx(-100.0)
    assert t[-1] == "TESTUSDT"  # symbol 只做兜底


# --------------------------------------------------------------------------
# 裁决三·3：授权守卫必须 fail-closed
# --------------------------------------------------------------------------
class _S:
    def __init__(self, **kw):
        self.parameter_version = "param-v2.0.0-screener-y"
        self.mcap_zone_mode = "on"
        self.mcap_zone_authorization = md.AUTHORIZATION_TOKEN
        self.__dict__.update(kw)


def test_guard_rejects_mode_off():
    with raises(md.McapDominanceError, match="mcap_zone_mode"):
        md.assert_production_ready(
            _S(mcap_zone_mode="off"), mode="off", mapping=object(),
            parameter_version="param-v2.0.0-screener-y",
        )


def test_guard_rejects_shadow_for_production():
    with raises(md.McapDominanceError):
        md.assert_production_ready(
            _S(mcap_zone_mode="shadow"), mode="shadow", mapping=object(),
            parameter_version="param-v2.0.0-screener-y",
        )


def test_guard_rejects_missing_authorization():
    with raises(md.McapDominanceError, match="未获显式授权"):
        md.assert_production_ready(
            _S(mcap_zone_authorization=None), mode="on", mapping=object(),
            parameter_version="param-v2.0.0-screener-y",
        )


def test_guard_rejects_wrong_authorization_token():
    with raises(md.McapDominanceError, match="未获显式授权"):
        md.assert_production_ready(
            _S(mcap_zone_authorization="please-just-work"), mode="on", mapping=object(),
            parameter_version="param-v2.0.0-screener-y",
        )


def test_guard_rejects_missing_mapping():
    with raises(md.McapDominanceError, match="映射未加载"):
        md.assert_production_ready(
            _S(), mode="on", mapping=None,
            parameter_version="param-v2.0.0-screener-y",
        )


def test_guard_accepts_full_authorization():
    cfg = md.assert_production_ready(
        _S(), mode="on", mapping=object(),
        parameter_version="param-v2.0.0-screener-y",
    )
    assert isinstance(cfg, md.DominanceConfig)


def test_main_board_is_not_governed():
    """主榜 v1.4.0 完全不受主导层约束。"""
    assert not md.is_governed("param-v1.4.0-staircase-confirm-dmr")
    assert md.is_governed("param-v2.0.0-screener-y")


# --------------------------------------------------------------------------
# 配置校验：越界必须显式失败，不得夹紧后继续跑
# --------------------------------------------------------------------------
def test_config_validation_rejects_bad_values():
    bad = [
        {"p1_mom_gte": 140.0},
        {"p2_z_gte": 9.0},
        {"xc_divergent_z_lte": -9.0},
        {"f_p3_veto": 1.5},      # 动态因子 >1 = 会抬升排序权重
        {"f_divergent": 0.0},
        {"w_k": {100: 1.1, 70: 1.0}},                       # 缺档
        {"w_base": {"DMR": 1.0, "CONFIRMED": 0.85}},        # 缺分区
        {"w_base": dict.fromkeys(ZONES_EN, 1.0)},           # ELIMINATED 必须为 0
    ]
    for kw in bad:
        with raises(md.McapDominanceError):
            md.DominanceConfig(**kw).validate()


# --------------------------------------------------------------------------
# 裁决三·4：S_MC 与 mcap_grade 不得混淆
# --------------------------------------------------------------------------
def test_s_mc_is_not_consumed_by_dominance_layer():
    """S_MC(mcap_momentum_score_*) 拉满也不影响主导层的任何一个输出。"""
    plain = md.resolve_dominance(_row(), "up", cfg=CFG)
    loaded = md.resolve_dominance(
        _row(mcap_momentum_score_up=100.0, mcap_momentum_score_down=100.0), "up", cfg=CFG
    )
    assert plain["final_zone"] == loaded["final_zone"]
    assert plain["rank_key"] == loaded["rank_key"]
    assert plain["w_final"] == loaded["w_final"]


def test_incomplete_grades_discount_rank_but_ceiling_already_watch():
    """三周期任一缺失 → mcap_effective 已封到观察；本层再打排序折扣。"""
    r = _row(
        base_state_up="CONFIRMED",
        effective_zone_up="WATCH",
        mcap_ceiling_zone_up="WATCH",
        mcap_combo_status_up="INCOMPLETE",
        mcap_z10_up=None,
        mcap_priority_up=None,
        mcap_resonance_k_up=None,
    )
    res = md.resolve_dominance(r, "up", cfg=CFG)
    assert res["final_zone"] == "WATCH"
    assert res["w_final"] < CFG.base_weight("WATCH") * CFG.prio_weight(1) * CFG.k_weight(10)


# --------------------------------------------------------------------------
# 旁路态：不进五区、不参与排序权重
# --------------------------------------------------------------------------
def test_bypass_states_untouched():
    for state in ("NONE", "DATA_INSUFFICIENT", "LOW_CONFIDENCE"):
        r = _row(base_state_up=state, state_up=state, effective_zone_up=state)
        res = md.resolve_dominance(r, "up", cfg=CFG)
        assert res["final_zone"] == state
        assert res["rank_key"] == 0.0
        assert res["predicate"] is None


# --------------------------------------------------------------------------
# 上涨池 / 下跌池对称性
# --------------------------------------------------------------------------
def test_up_and_down_use_identical_logic():
    """镜像输入必须得到镜像输出（同一套谓词、同一套权重）。"""
    up = _row(mcap_z10_up=20, momentum_score_up=50.0)
    dn = _row(
        base_state_down="CONFIRMED", state_down="CONFIRMED",
        effective_zone_down="CONFIRMED", mcap_ceiling_zone_down="CONFIRMED",
        mcap_combo_status_down="COMPLETE", mcap_z10_down=20,
        mcap_priority_down=3, mcap_resonance_k_down=70,
        score_down=70.0, ss_down=55.0, momentum_score_down=50.0,
        consistency_down=2 / 3, momentum_score_up=40.0, consistency_up=0.0,
    )
    ru = md.resolve_dominance(up, "up", cfg=CFG)
    rd = md.resolve_dominance(dn, "down", cfg=CFG)
    assert ru["predicate"] == rd["predicate"] == "P2_SOFTEN"
    assert ru["final_zone"] == rd["final_zone"]
    assert ru["rank_key"] == approx(rd["rank_key"])


# --------------------------------------------------------------------------
# apply_dominance 整板行为
# --------------------------------------------------------------------------
def test_apply_dominance_off_writes_nothing():
    rows = [_row()]
    meta = md.apply_dominance(rows, cfg=CFG, mode="off")
    assert meta["rows"] == 0
    assert "final_zone_up" not in rows[0]


def test_apply_dominance_shadow_does_not_touch_state():
    rows = [_row(effective_zone_up="DMR", base_state_up="DMR", mcap_z10_up=20,
                 momentum_score_up=50.0, state_up="DMR")]
    md.apply_dominance(rows, cfg=CFG, mode="shadow")
    assert rows[0]["state_up"] == "DMR", "shadow 不得改 state"
    assert rows[0]["final_zone_up"] == "CONFIRMED", "但必须算出来落库"


def test_apply_dominance_on_rewrites_state():
    rows = [_row(effective_zone_up="DMR", base_state_up="DMR", mcap_z10_up=20,
                 momentum_score_up=50.0, state_up="DMR")]
    md.apply_dominance(rows, cfg=CFG, mode="on")
    assert rows[0]["state_up"] == "CONFIRMED", "on 必须把最终分区写回展示 state"


# --------------------------------------------------------------------------
# 裁决二·5：DMR inbox 的 Top-K **切口**也必须由流通市值裁决
# --------------------------------------------------------------------------
def _msg(sym, *, score, ceil=None, z10=None, k=None, rank_key=None, mom=0.0):
    m = {
        "symbol": sym,
        "state": "CONFIRMED",
        "total_score": score,
        "staircase_score": 50.0,
        "momentum_score": mom,
    }
    if ceil is not None:
        m.update(
            mcap_ceiling_zone=ceil,
            mcap_z10=z10,
            mcap_resonance_k=k,
            rank_key=rank_key,
        )
    return m


def test_dmr_topk_cut_is_decided_by_mcap_not_by_score():
    """高分但天花板差的币，不得把低分高天花板的币挤出 Top-K。

    回归：此前 ``rank_dmr_inbox`` 去重后回落成旧三键 (-Score,-SS,-M,symbol)
    再切 Top-K，于是「谁能进 DMR」实际由动能/分数裁决 —— 天花板 / Z / K /
    RankKey 一概不参与，正是 v2.0.0 禁止的「动能覆盖流通市值判定」。
    """
    from coin_selection.scan import rank_dmr_inbox

    # 12 个高分但天花板只到 CONFIRMED 的币 + 1 个低分但天花板 DMR 的币。
    msgs = [
        _msg(f"HI{i}USDT", score=99.0 - i, ceil="CONFIRMED", z10=5, k=40, rank_key=0.5, mom=95.0)
        for i in range(12)
    ]
    msgs.append(
        _msg("LOWUSDT", score=10.0, ceil="DMR", z10=30, k=100, rank_key=0.09, mom=1.0)
    )
    inbox, _meta = rank_dmr_inbox(msgs, top_k=12)
    syms = [m["symbol"] for m in inbox]
    assert len(inbox) == 12
    assert syms[0] == "LOWUSDT", f"天花板 DMR 的币必须排第一，实际 {syms[:3]}"
    assert "LOWUSDT" in syms, "天花板最好的币被分数挤出了 Top-K —— 动能覆盖了市值裁决"
    # 被挤掉的应该是天花板同级里分数最低的那个，而不是天花板最好的那个
    assert "HI11USDT" not in syms, f"切口没有按 (天花板, Z, K, RankKey) 走: {syms}"


def test_dmr_order_falls_back_to_legacy_when_layer_is_off():
    """主榜 v1.4.0 的消息不带天花板字段 ⇒ 逐字段退回旧三键，行为不变。"""
    from coin_selection.scan import rank_dmr_inbox

    msgs = [_msg("AUSDT", score=10.0), _msg("BUSDT", score=90.0), _msg("CUSDT", score=50.0)]
    inbox, _ = rank_dmr_inbox(msgs, top_k=12)
    assert [m["symbol"] for m in inbox] == ["BUSDT", "CUSDT", "AUSDT"]



if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))