"""选币榜X v1.3.0：DMR 由「确认 ∩ 基础消息条件 ∩ 216 方向 ceiling==DMR」派生。

本模块钉住的核心主张（提示词 §七 / 阶段E）：
  * D(t) ⊆ C(t)：DMR 是确认区的派生精选，不是新状态；
  * 216 **只**做 DMR 准入，绝不改写四区归属、排序与入区时刻；
  * 缺等级 / 非法码 / 缺映射 / 天花板层降级 一律 fail-closed（拒进 DMR），
    且不反向影响四区；
  * 默认 strict-v1.4 一个字节都不变（main/Y 零回归）。

真实节点锚点见 fixtures/x_v13_real_node_20260906-039.json —— 合成夹具证明谓词，
真实夹具证明「这套谓词接到生产数据上确实会排除现网正在进 DMR 的币」。
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from coin_selection.board_variants import DMR_SELECTION_MODES, DMR_SELECTION_MODE_DEFAULT
from coin_selection.mcap_mapping import build_rows, load
from coin_selection.scan import SelectionSettings, build_dmr_messages
from coin_selection.state_machine import StateConfig

NOW = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "x_v13_real_node_20260906-039.json"


def x_settings(**kw):
    """X 候选身份：必须三件齐备，缺一 build_dmr_messages 就该拒。"""
    s = SelectionSettings(
        mcap_zone_mode="shadow", parameter_version="param-v1.3.0-screener-x", **kw
    )
    s.dmr_selection_mode = "confirmed-basic-216-v1.3"
    return s


def x_cfg():
    return StateConfig(selection_semantics="dual-path-v1.3")


def row(combo="AAA", ceiling=None, **patch):
    """一行生产形状的合并 row（``_up`` / ``_down`` 双向）。

    ``mcap_ceiling_zone_{up,down}`` 是 apply_effective_zone 在 shadow 下写的证据列；
    build_dmr_messages 用它判断天花板层是否真的跑过，因此夹具必须带上。
    """
    m = load(strict=True).by_combo.get(combo)
    up = ceiling if ceiling is not None else (m["long_ceiling"] if m else None)
    dn = ceiling if ceiling is not None else (m["short_ceiling"] if m else None)
    r = dict(
        symbol="AAAUSDT",
        state_up="CONFIRMED", state_down="CONFIRMED",
        score_up=66, score_down=66,
        ss_up=45, ss_down=45,
        momentum_score_up=62, momentum_score_down=62,
        consistency_up=1, consistency_down=1,
        mcap_momentum_score_up=50, mcap_momentum_score_down=50,
        data_quality_score=60, data_mode="CACHE",
        supply_missing=False, liquidity_hard_pass=True,
        mcap_combo_code=combo,
        mcap_ceiling_zone_up=up, mcap_ceiling_zone_down=dn,
    )
    r.update(patch)
    return r


def msgs(rows, settings=None, cfg=None):
    if isinstance(rows, dict):
        rows = [rows]
    return build_dmr_messages(
        rows, settings=settings or x_settings(), cfg=cfg or x_cfg(),
        anchor=NOW, scan_id="20260906-048", seq=48, now=NOW,
    )


class DmrAllowSet(unittest.TestCase):
    """216 允许集合：谓词本身。"""

    def test_basic_dmr_is_not_the_v14_strict_subset(self):
        # 这一行 Score=66 / SS=45 / M=62 —— 现网 strict-v1.4 要求 70/55/65 全不满足，
        # 但历史 v1.3 的 DMR 基线是「确认 + 基础数据条件」，因此它必须进 DMR。
        self.assertEqual([m["direction"] for m in msgs(row("AAA"))], ["LONG"])

    def test_all_216_combos_both_directions(self):
        """216 个组合 × 2 方向逐个走一遍，与冻结映射逐字段对齐。"""
        counts = {"LONG": 0, "SHORT": 0}
        for combo in build_rows():
            code = "".join(combo[k] for k in ("m30", "h2", "h6"))
            got = {m["direction"] for m in msgs(row(code))}
            want = {
                d for d, k in (("LONG", "long_ceiling"), ("SHORT", "short_ceiling"))
                if combo[k] == "DMR"
            }
            self.assertEqual(got, want, code)
            for d in got:
                counts[d] += 1
        # 每侧恰好 12/216 —— 这是 216 的排除力，不是「216 个都能进」。
        self.assertEqual(counts, {"LONG": 12, "SHORT": 12})

    def test_no_combo_allows_both_directions(self):
        """没有任何组合两侧同时为 DMR ⇒ DMR-only 模式下同币双向不会打架。"""
        both = [
            c for c in build_rows()
            if c["long_ceiling"] == "DMR" and c["short_ceiling"] == "DMR"
        ]
        self.assertEqual(both, [])


class DmrFailClosed(unittest.TestCase):
    """缺数据 / 坏配置一律拒进 DMR，且不反向改四区。"""

    def test_hard_gates_and_invalid_combo(self):
        for patch in (
            {"supply_missing": True},
            {"liquidity_hard_pass": None},      # None 不是 True，不得放行
            {"liquidity_hard_pass": False},
            {"data_quality_score": 59.99},      # 略小于 DQ>=60
            {"data_mode": "MISSING"},
            {"mcap_combo_code": None},          # 缺等级
            {"mcap_combo_code": "ZZZ"},         # 非法码
            {"mcap_combo_code": "AA"},          # 长度不对
        ):
            self.assertEqual(msgs(row("AAA", **patch)), [], patch)

    def test_dq_boundary_is_inclusive_at_60(self):
        self.assertEqual([m["direction"] for m in msgs(row("AAA", data_quality_score=60))], ["LONG"])
        self.assertEqual(msgs(row("AAA", data_quality_score=59.999999)), [])

    def test_not_confirmed_cannot_be_promoted_by_216(self):
        """216 允许 ≠ 可以升级：不确认的边永远进不了 DMR。"""
        for st in ("QUALIFIED", "WATCH", "ELIMINATED", "NONE", None):
            r = row("AAA", state_up=st)
            self.assertEqual([m["direction"] for m in msgs(r)], [], st)

    def test_confirmed_but_216_disallowed_stays_confirmed(self):
        """确认且 216 不允许：保留 CONFIRMED，只是不进 DMR —— 四区零触碰。"""
        r = row("FAA")  # long_ceiling != DMR
        before = copy.deepcopy(r)
        self.assertEqual([m["direction"] for m in msgs(r)], [])
        self.assertEqual(r["state_up"], "CONFIRMED")
        self.assertEqual(r, before, "build_dmr_messages 不得改写任何一行")

    def test_degraded_ceiling_layer_is_refused_not_silently_empty(self):
        """天花板层没跑（缺证据列）必须抛，而不是静默给出空 DMR。"""
        r = row("AAA")
        r.pop("mcap_ceiling_zone_up")
        with self.assertRaisesRegex(ValueError, "216 天花板层未生效"):
            msgs(r)

    def test_relaxed_ceiling_min_is_refused(self):
        """不得把 Y 放宽到 CONFIRMED 的 ceiling 门槛照搬到 X（D3）。"""
        s = x_settings()
        s.mcap_dmr_ceiling_min = "CONFIRMED"
        with self.assertRaisesRegex(ValueError, "mcap_dmr_ceiling_min"):
            msgs(row("AAA"), settings=s)

    def test_identity_triple_is_mandatory(self):
        """X 身份 / dual-path / shadow 三件缺一即拒，防止别的板面误开。"""
        for kw, cfg in (
            ({"parameter_version": "param-v1.4.0-staircase-confirm-dmr"}, None),
            ({"mcap_zone_mode": "on"}, None),
            ({}, StateConfig()),  # staircase-v1.4
        ):
            s = x_settings()
            for k, v in kw.items():
                setattr(s, k, v)
            with self.assertRaises(ValueError):
                msgs(row("AAA"), settings=s, cfg=cfg)


class DmrDefaultUnchanged(unittest.TestCase):
    """main/Y 零回归：默认模式一个字节都不变。"""

    def test_default_still_strict(self):
        # 默认 SelectionSettings ⇒ strict-v1.4 ⇒ 该行的 66/45/62 达不到 70/55/65。
        self.assertEqual(msgs(row("AAA"), settings=SelectionSettings()), [])

    def test_mode_table_is_closed(self):
        self.assertEqual(
            DMR_SELECTION_MODES,
            ("strict-v1.4", "confirmed-basic-216-v1.3", "confirmed-basic-rank216-v1.3"),
        )
        self.assertEqual(DMR_SELECTION_MODE_DEFAULT, "strict-v1.4")
        self.assertEqual(SelectionSettings().dmr_selection_mode, DMR_SELECTION_MODE_DEFAULT)

    def test_unknown_mode_raises(self):
        s = x_settings()
        s.dmr_selection_mode = "confirmed-216"  # 少个后缀的真实笔误形态
        with self.assertRaisesRegex(ValueError, "unknown dmr_selection_mode"):
            msgs(row("AAA"), settings=s)


class DmrRealNode(unittest.TestCase):
    """真实节点锚点：这套谓词接到生产数据上确实会排除现网正在进 DMR 的币。"""

    @classmethod
    def setUpClass(cls):
        cls.fix = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def _row_from(self, e):
        d = e["direction"]
        r = row(e["combo"])
        # 真实节点的四个打分列与状态原样搬进来；另一侧压成 WATCH，只测本方向。
        other = "down" if d == "up" else "up"
        r[f"state_{d}"] = e["state"]
        r[f"state_{other}"] = "WATCH"
        r[f"score_{d}"] = e["score"]
        r[f"ss_{d}"] = e["ss"]
        r[f"momentum_score_{d}"] = e["mom"]
        r[f"consistency_{d}"] = e["cons"]
        r["symbol"] = e["symbol"]
        return r

    def test_real_ceiling_dmr_members_are_admitted(self):
        for e in self.fix["ceiling_is_dmr"]:
            want = "LONG" if e["direction"] == "up" else "SHORT"
            got = [m["direction"] for m in msgs(self._row_from(e))]
            self.assertEqual(got, [want], f"{e['symbol']}/{e['direction']} combo={e['combo']}")

    def test_real_ceiling_non_dmr_members_are_excluded(self):
        for e in self.fix["ceiling_not_dmr"]:
            got = msgs(self._row_from(e))
            self.assertEqual(got, [], f"{e['symbol']}/{e['direction']} combo={e['combo']}")

    def test_fixture_actually_contains_live_dmr_members_that_216_kills(self):
        """非空控制夹具：216 必须至少排除一个**现网真的在 DMR 里**的成员。

        夹具为空或全是本来就不在 DMR 的币，那么「216 收窄了 DMR」就没有被证明。
        """
        killed = [e for e in self.fix["ceiling_not_dmr"] if e["dmr_selected"]]
        self.assertTrue(
            killed,
            "夹具没有钉住任何『现网已进 DMR 但 216 不允许』的样本，收窄性未获证明",
        )

    def test_216_touches_nothing_but_dmr_on_real_rows(self):
        rows = [self._row_from(e) for e in self.fix["ceiling_is_dmr"] + self.fix["ceiling_not_dmr"]]
        before = copy.deepcopy(rows)
        msgs(rows)
        self.assertEqual(rows, before, "216 裁决不得改写四区 / 排序 / 任何行字段")



class Rank216Mode(unittest.TestCase):
    """216 从准入降级为排序权重（confirmed-basic-rank216-v1.3）。

    依据：staging 2139 节点实测，方向天花板做**准入**是反向指标
    （确认区 ceil=DMR 合计 -15.1%，ceil=WATCH 合计 +188.8%）；
    而 216 里的**三周期全同向**是正向的（合计 +161.0%、盈亏比 3.21）。
    因此天花板退出准入，全同向提为排序首键。
    """

    def settings(self):
        s = x_settings()
        s.dmr_selection_mode = "confirmed-basic-rank216-v1.3"
        return s

    def test_ceiling_no_longer_blocks_admission(self):
        """basic216 会挡掉的行，rank216 必须放行 —— 这就是本模式的全部意义。"""
        r = row("FAA")   # long_ceiling != DMR
        self.assertEqual(msgs(r), [], "基线：basic216 应挡住")
        got = sorted(m["direction"] for m in msgs(r, settings=self.settings()))
        # 夹具两侧都是 CONFIRMED。basic216 下没有任何组合允许双向（见
        # test_no_combo_allows_both_directions），所以最多进一侧；rank216 取消
        # 准入后两侧都进，同币择优交给 rank_dmr_inbox —— 这正是历史 v1.3 的
        # same_symbol_tiebreak 行为，不是缺陷。
        self.assertEqual(got, ["LONG", "SHORT"], "rank216 应放行两侧，去重交给排序层")

    def test_missing_mapping_still_fail_closed(self):
        """放开准入不等于放弃 fail-closed：缺映射仍拒，因为排序键要用它。"""
        for patch in ({"mcap_combo_code": None}, {"mcap_combo_code": "ZZZ"}):
            self.assertEqual(msgs(row("AAA", **patch), settings=self.settings()), [], patch)

    def test_hard_gates_still_apply(self):
        for patch in ({"supply_missing": True}, {"liquidity_hard_pass": None},
                      {"data_quality_score": 59.99}, {"data_mode": "MISSING"}):
            self.assertEqual(msgs(row("AAA", **patch), settings=self.settings()), [], patch)

    def test_reason_code_is_distinct(self):
        m = msgs(row("FAA"), settings=self.settings())[0]
        self.assertIn("DMR_RANK_216", m["reason_codes"])
        self.assertNotIn("DMR_BASIC_216", m["reason_codes"])

    def test_aligned_combos_rank_first(self):
        from coin_selection.scan import rank_dmr_inbox

        def mk(sym, combo, score):
            return dict(state="CONFIRMED", symbol=sym, direction="LONG",
                        mcap_combo_code=combo, total_score=score,
                        staircase_score=50, momentum_score=60,
                        reason_codes=["DMR_RANK_216"])
        # 分数最高的是混合组合，但全同向必须排在它前面
        out, _ = rank_dmr_inbox([mk("MIXED_HI", "ABC", 95), mk("ALIGNED_LO", "AAA", 60),
                                 mk("ALIGNED_HI", "FFF", 70)], top_k=16)
        self.assertEqual([x["symbol"] for x in out],
                         ["ALIGNED_HI", "ALIGNED_LO", "MIXED_HI"])

    def test_default_mode_ranking_unchanged(self):
        """没有 DMR_RANK_216 标记的消息（main/Y）排序行为一个字节不变。"""
        from coin_selection.scan import rank_dmr_inbox

        def mk(sym, score):
            return dict(state="CONFIRMED", symbol=sym, direction="LONG",
                        total_score=score, staircase_score=50, momentum_score=60,
                        reason_codes=["DMR_STRICT"])
        out, _ = rank_dmr_inbox([mk("A", 60), mk("B", 95), mk("C", 70)], top_k=16)
        self.assertEqual([x["symbol"] for x in out], ["B", "C", "A"])

    def test_mode_is_registered_and_validated(self):
        self.assertIn("confirmed-basic-rank216-v1.3", DMR_SELECTION_MODES)
        s = x_settings(); s.dmr_selection_mode = "rank216"
        with self.assertRaisesRegex(ValueError, "unknown dmr_selection_mode"):
            msgs(row("AAA"), settings=s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
