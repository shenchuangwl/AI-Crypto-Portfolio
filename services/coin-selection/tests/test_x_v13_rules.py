"""选币榜X：历史 v1.3 四区规则的现代工程适配（``selection_semantics="dual-path-v1.3"``）。

基线来源（三处互相印证，冲突时以冻结配置为准）：
  * packages/config/param-v1.3.0-dual-path-sticky.yaml:52-62（当年冻结的人读快照）
  * doc/Hermes_Grok4.6_新四区币种选入标准综合解读报告.md:224,458
  * doc/Claude_Opus5_新四区币种选入标准综合解读报告.md:209,574,591

被恢复的三条（相对现网 v1.4 克隆）：
  1. 确认 PATH_M：Score>=70 ∧ SS>=45 ∧ M>=70 ∧ C==1（v1.4 是 ``path_m: disabled``）
  2. 确认 hold 析取：Score>=56 ∧ (SS>=45 ∨ (C==1 ∧ M>=62))（v1.4 是 Score>=56 ∧ SS>=50）
  3. 取消 v1.4 的「CONFIRMED 且 SS<50 立即降级」全局硬楼梯分支

**默认 staircase-v1.4 必须逐字段不变** —— main/Y 走的就是默认分支。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from coin_selection.state_machine import (
    CONS_ONE_EPS, StateConfig, SymState, cons_is_one, hold_ok, path_confirmed,
    path_qualified, transition_one,
)

V13 = dict(selection_semantics="dual-path-v1.3")


def cfg13(**kw):
    return StateConfig(**V13, **kw)


def cfg14(**kw):
    return StateConfig(**kw)


def confirm(score, ss, mom, cons, cfg, dq=60, supply_missing=False, data_mode="LIVE"):
    return path_confirmed(score, ss, mom, cons, dq, supply_missing, cfg, data_mode)


class ConfirmPathM(unittest.TestCase):
    """确认 PATH_M：70 / 45 / 70 / C=1，且 S 优先。"""

    def test_exact_thresholds_admit(self):
        self.assertEqual(confirm(70, 45, 70, 1.0, cfg13()), "M")

    def test_each_threshold_is_inclusive_and_one_notch_below_rejects(self):
        # 等于 / 略大于 → 进；略小于 → 不进。四个维度各测一遍。
        self.assertEqual(confirm(70.0001, 45, 70, 1.0, cfg13()), "M")
        for patch in (
            dict(score=69.9999), dict(ss=44.9999),
            dict(mom=69.9999), dict(cons=1 - 2 * CONS_ONE_EPS),
        ):
            kw = dict(score=70, ss=45, mom=70, cons=1.0)
            kw.update(patch)
            self.assertIsNone(
                confirm(kw["score"], kw["ss"], kw["mom"], kw["cons"], cfg13()), patch
            )

    def test_s_wins_when_both_paths_pass(self):
        # 80/60/80/C=1 两条通道都过 —— 历史规则要求返回 S。
        self.assertEqual(confirm(80, 60, 80, 1.0, cfg13()), "S")

    def test_v14_default_has_no_path_m(self):
        self.assertIsNone(confirm(70, 45, 70, 1.0, cfg14()))
        # 而 S 通道在两种语义下完全一致。
        for c in (cfg13(), cfg14()):
            self.assertEqual(confirm(66, 50, 60, 0.67, c), "S")

    def test_cons_tolerance_is_1e9_not_two_thirds(self):
        """C==1 用 1e-9 容差；C>=0.67 不得被"优化"成 2/3。"""
        self.assertEqual(CONS_ONE_EPS, 1e-9)
        self.assertTrue(cons_is_one(1 - 1e-10))
        self.assertFalse(cons_is_one(0.999999))
        # 0.67 是 S 通道的字面阈值：2/3≈0.6667 必须被拒。
        self.assertEqual(confirm(66, 50, 60, 0.67, cfg13()), "S")
        self.assertIsNone(confirm(66, 50, 60, 2 / 3, cfg13()))

    def test_n_conditions_still_gate_path_m(self):
        """N2 缺供应 / N3 DQ / N4 MISSING 对 M 通道同样是必要条件。"""
        self.assertIsNone(confirm(70, 45, 70, 1.0, cfg13(), supply_missing=True))
        self.assertIsNone(confirm(70, 45, 70, 1.0, cfg13(), dq=59.99))
        self.assertIsNone(confirm(70, 45, 70, 1.0, cfg13(), data_mode="MISSING"))
        self.assertEqual(confirm(70, 45, 70, 1.0, cfg13(), dq=60), "M")

    def test_forbidden_entry_ss_lt_50_and_c_lt_1_is_implied(self):
        """冻结配置 forbid: "SS<50 AND C<1" —— 两条通道的析取已蕴含它。

        S 通道要求 SS>=50，M 通道要求 C==1，因此没有任何入口能产生
        SS<50 ∧ C<1 的**新晋**确认。这条不变量不需要额外分支，但必须有测试
        看着：一旦有人放宽 M 的 C 门槛，这里立刻红。
        """
        for score in (66, 70, 80, 99):
            for ss in (0, 30, 45, 49.9999):
                for cons in (0, 0.67, 0.9999):
                    self.assertIsNone(
                        confirm(score, ss, 99, cons, cfg13()),
                        f"SS={ss} C={cons} 不得进入确认",
                    )


class ConfirmHold(unittest.TestCase):
    """确认 hold 析取：Score>=56 ∧ (SS>=45 ∨ (C==1 ∧ M>=62))。"""

    def test_disjunction_branches(self):
        c = cfg13()
        self.assertTrue(hold_ok(56, 45, 0, 0, c))            # 左支：SS>=45
        self.assertTrue(hold_ok(56, 10, 62, 1.0, c))         # 右支：C=1 ∧ M>=62
        self.assertFalse(hold_ok(55.9999, 45, 0, 0, c))      # Score 是合取项
        self.assertFalse(hold_ok(56, 44.9999, 61.9999, 1.0, c))
        self.assertFalse(hold_ok(56, 44.9999, 62, 0.9999, c))  # C 不是 1
        self.assertTrue(hold_ok(56, 44.9999, 62, 1.0, c))

    def test_documented_live_case_ss45_c067_holds(self):
        """文档记载的现盘案例：SS=45 且 C=0.67 属 hold 允许的「进门后衰减」。"""
        self.assertTrue(hold_ok(60, 45, 50, 0.67, cfg13()))

    def test_v14_default_hold_unchanged(self):
        c = cfg14()
        self.assertTrue(hold_ok(56, 50, 0, 0, c))
        self.assertFalse(hold_ok(56, 45, 99, 1.0, c))  # v1.4 只看 SS>=50


class StaircaseDemotion(unittest.TestCase):
    """v1.4 的「SS<50 立即降级」不得出现在 v1.3 语义里。"""

    def _confirmed_state(self):
        st = SymState(symbol="AAAUSDT", direction="up")
        st.state = "CONFIRMED"
        st.state_enter_ts = 0.0
        st.consecutive_pass = 2
        return st

    def _step(self, cfg, **kw):
        base = dict(
            score=60, ss=46, momentum=50, consistency=0.67, dq=70,
            supply_missing=False, hard_fail=False, cfg=cfg,
            scan_id="20260906-048", last_price=1.0, hard_pass=True,
        )
        base.update(kw)
        return transition_one(self._confirmed_state(), **base)

    def test_v13_keeps_confirmed_when_ss_below_50_but_hold_ok(self):
        st, _ = self._step(cfg13())
        self.assertEqual(st.state, "CONFIRMED")

    def test_v14_demotes_immediately_on_same_input(self):
        st, reason = self._step(cfg14())
        self.assertEqual(st.state, "QUALIFIED")
        self.assertIsNotNone(reason)

    def test_v13_still_demotes_when_hold_fails(self):
        """取消的是「仅凭 SS<50 立即降级」，不是取消退出机制本身。

        Score<56 ⇒ hold_ok 为假；历史规则是**连 2 次**失败才退，因此单次
        不动，第二次才降到 QUALIFIED。
        """
        st = self._confirmed_state()
        kw = dict(
            ss=10, momentum=0, consistency=0, dq=70, supply_missing=False,
            hard_fail=False, cfg=cfg13(), scan_id="a", last_price=1.0, hard_pass=True,
        )
        st, _ = transition_one(st, score=50, **kw)
        self.assertEqual(st.state, "CONFIRMED", "单次 hold 失败不得立即退出")
        st, _ = transition_one(st, score=50, **{**kw, "scan_id": "b"})
        self.assertEqual(st.state, "QUALIFIED", "连续两次 hold 失败必须退回符合区")

    def test_hard_liquidity_failure_still_eliminates_in_v13(self):
        st, _ = self._step(cfg13(), hard_fail=True)
        self.assertEqual(st.state, "ELIMINATED")


class UnchangedTiers(unittest.TestCase):
    """观察 / 符合两档在 v1.3 与 v1.4 下完全一致（本轮不改它们）。"""

    def test_qualified_paths_identical_across_semantics(self):
        cases = [
            (58, 50, 55, 0, "S"), (57.9999, 50, 55, 0, None),
            (62, 45, 65, 1.0, "M"), (62, 45, 65, 0.9999, None),
            (80, 60, 80, 1.0, "S"),
        ]
        for score, ss, mom, cons, want in cases:
            for c in (cfg13(), cfg14()):
                self.assertEqual(path_qualified(score, ss, mom, cons, False, c), want,
                                 (score, ss, mom, cons, c.selection_semantics))
        # 缺供应对两条通道都是一票否决。
        for c in (cfg13(), cfg14()):
            self.assertIsNone(path_qualified(80, 60, 80, 1.0, True, c))

    def test_watch_thresholds_are_frozen_in_both(self):
        for c in (cfg13(), cfg14()):
            self.assertEqual((c.enter_watch, c.exit_watch), (45, 40))
            self.assertEqual((c.min_streak_watch, c.min_dwell_watch), (2, 30))

    def test_shared_invariants_unchanged(self):
        for c in (cfg13(), cfg14()):
            self.assertEqual(c.cons_confirmed, 0.67)
            self.assertEqual(c.dq_confirm_floor, 60)
            self.assertEqual(c.min_streak_confirmed, 2)
            self.assertEqual(c.dmr_top_k, 16)
            # 冻结 YAML 的 `confirmed.dwell_min: 15` 指的是**升确认前在符合区的停留**，
            # 对应 min_dwell_qualified；min_dwell_confirmed=60 是代码里注明的
            # legacy 死字段（"unused for upgrade"），不参与任何升级判定。
            # 两者别搞混：把 15 写到后者上等于什么门槛都没改。
            self.assertEqual(c.min_dwell_qualified, 15)
            self.assertEqual(c.min_dwell_confirmed, 60)


class SemanticsField(unittest.TestCase):
    def test_default_is_v14(self):
        self.assertEqual(StateConfig().selection_semantics, "staircase-v1.4")

    def test_unknown_value_rejected_at_construction(self):
        for bad in ("dual-path", "v1.3", "", None, "DUAL-PATH-V1.3"):
            with self.assertRaises(ValueError):
                StateConfig(selection_semantics=bad)



class ConfirmPathMIsConfigurable(unittest.TestCase):
    """确认区 PATH_M 三阈值必须可配，且默认 = 历史 v1.3 原值。

    配置化的动机：**符合区**的 PATH_M 一直是正经字段，而**确认区**的同一组阈值
    原先写死。这个不对称让「确认区 M 门槛」既不能经 overrides 调、也不进指纹的
    state_config 段 —— 回测「M>=75 完整版」时只能改代码。
    """

    def test_defaults_are_the_frozen_v13_values(self):
        c = cfg13()
        self.assertEqual((c.enter_confirmed_m, c.ss_confirmed_m, c.mom_confirmed_m),
                         (70, 45, 70))

    def test_default_behaviour_is_byte_identical(self):
        """默认值下的判定必须与配置化之前完全一致（70/45/70 贴边与略低）。"""
        c = cfg13()
        self.assertEqual(confirm(70, 45, 70, 1.0, c), "M")
        for patch in (dict(score=69.9999), dict(ss=44.9999), dict(mom=69.9999)):
            kw = dict(score=70, ss=45, mom=70); kw.update(patch)
            self.assertIsNone(confirm(kw["score"], kw["ss"], kw["mom"], 1.0, c), patch)

    def test_each_threshold_is_independently_tunable(self):
        # 用 SS=45（低于 S 的 50）确保 S 不触发，这样看到的就是 M 的行为。
        self.assertIsNone(confirm(72, 45, 72, 1.0, cfg13(mom_confirmed_m=75)))
        self.assertEqual(confirm(72, 45, 78, 1.0, cfg13(mom_confirmed_m=75)), "M")
        self.assertIsNone(confirm(72, 45, 78, 1.0, cfg13(enter_confirmed_m=75)))
        self.assertIsNone(confirm(78, 46, 78, 1.0, cfg13(ss_confirmed_m=48)))
        self.assertEqual(confirm(78, 48, 78, 1.0, cfg13(ss_confirmed_m=48)), "M")

    def test_raising_ss_m_to_or_above_ss_s_makes_path_m_unreachable(self):
        """把 ss_confirmed_m 提到 >= ss_confirmed(50) 会让 PATH_M **永远不可达**。

        不是缺陷，是 S 优先的逻辑结果：M 的另外两项（score>=70、C=1）都严于
        S（66、0.67），所以 SS 一旦也不低于 S 的门槛，任何满足 M 的输入必然
        先满足 S，返回 "S"。**PATH_M 存在的意义就是收下 SS∈[45,50) 那一段**。
        调参时把 ss_confirmed_m 提到 50 等于静默关闭 M 通道 —— 钉在这里免得
        有人以为自己只是"收紧了一点"。
        """
        for ss in (50, 55, 60, 99):
            self.assertEqual(confirm(99, ss, 99, 1.0, cfg13(ss_confirmed_m=50)), "S")

    def test_v14_ignores_these_fields_entirely(self):
        """main/Y 走 staircase-v1.4，PATH_M 关闭，这三个字段对它们必须无效。

        取 SS=46 —— 落在 M 的 [45,50) 专属区间：低于 S 的 50 所以 S 不触发，
        不低于 M 的 45 所以 v1.3 会走 M。v1.4 必须返回 None，
        且把 M 的门槛调到最低也改变不了这一点。
        """
        self.assertEqual(confirm(99, 46, 99, 1.0, cfg13()), "M")
        self.assertIsNone(confirm(99, 46, 99, 1.0, cfg14()))
        self.assertIsNone(confirm(99, 46, 99, 1.0, cfg14(mom_confirmed_m=1,
                                                          ss_confirmed_m=1,
                                                          enter_confirmed_m=1)))

    def test_s_still_wins_when_both_paths_pass(self):
        self.assertEqual(confirm(80, 60, 80, 1.0, cfg13(mom_confirmed_m=75)), "S")


if __name__ == "__main__":
    unittest.main(verbosity=2)
