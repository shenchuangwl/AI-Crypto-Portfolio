"""候选配置绝不能被生产自动加载（提示词 §十一）。

`effective_manifest()` 的判据只有 `effective_from_scan_id <= scan_id`，因此把一份
格式合法的 manifest 放进 `packages/config/rule-manifests/` 就等于让它到点自动上线。
候选件因此只能待在 `packages/config/candidates/`。本模块把这条约束钉成机器断言。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/coin-selection/src"))
from coin_selection.rule_manifest import effective_manifest, list_published, manifest_dir

CANDIDATES = ROOT / "packages/config/candidates"
CANDIDATE_X = CANDIDATES / "x-v1.3.0-r2.candidate.json"


class CandidatesNeverAutoLoad(unittest.TestCase):
    def test_candidate_dir_is_not_the_manifest_dir(self):
        self.assertNotEqual(CANDIDATES.resolve(), manifest_dir().resolve())

    def test_no_candidate_file_sits_in_the_auto_loaded_dir(self):
        stray = [
            p.name for p in manifest_dir().glob("*.json")
            if "candidate" in p.name.lower()
        ]
        self.assertEqual(stray, [], f"候选件混进了自动加载目录：{stray}")

    def test_only_deliberately_switched_revisions_are_published(self):
        """已发布集合必须**只**包含经切换器批准的 revision。

        原断言写死「r2 不得被发布」—— 那是切换前的事实。r2 已于
        2026-09-07 经 switch_x_to_v13.py --execute 正式发布，再断言它不存在就是
        在拿旧世界卡新世界。要守住的不变量其实是：
        **没有任何候选件是自己溜进去的** —— 每一个已发布 revision 都必须在
        切换审计流水里有对应的 switch 记录。
        """
        revs = {m.rule_revision for m in list_published("x")}
        self.assertIn("x-v1.3.0-r1", revs, "初始发布件不得消失")
        log = ROOT / "doc/verification/screener-x-v13-continue-20260906/switch-log.jsonl"
        switched = set()
        if log.is_file():
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") == "switch" and rec.get("dry_run") is False:
                    # 从流水里读**实际切到哪个目标**，不写死。
                    # 原先硬编码 "x-v1.3.0-r2"（当时切换器只有一个目标），
                    # r3 上线后立刻误判 —— 又是一次「把当时为真的快照写死」。
                    t = rec.get("target_shape")
                    switched.add(f"x-v1.3.0-{t}" if t else "x-v1.3.0-r2")
        unexplained = revs - {"x-v1.3.0-r1"} - switched
        self.assertEqual(
            unexplained, set(),
            f"这些 revision 没有对应的切换记录，疑似候选件自动上线：{unexplained}",
        )

    def test_live_x_resolves_to_the_newest_switched_revision(self):
        m = effective_manifest("x", "20260908-999")
        self.assertIsNotNone(m)
        revs = sorted(x.rule_revision for x in list_published("x"))
        self.assertEqual(m.rule_revision, revs[-1],
                         "生效解析必须落在最新已发布 revision 上")


class CandidateContentIsHonest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads(CANDIDATE_X.read_text(encoding="utf-8"))

    def test_switch_time_fields_are_left_as_placeholders(self):
        """生效边界与部署 commit 必须在切换那一刻才填，不能预先钉死。"""
        for k in ("effective_from_utc", "effective_from_scan_id", "code_commit"):
            self.assertTrue(
                str(self.doc[k]).startswith("REPLACE"),
                f"{k} 不应预先填值：{self.doc[k]!r}",
            )

    def test_execution_locks_are_off_in_the_candidate_too(self):
        self.assertIs(self.doc["dmr_executable"], False)
        self.assertIs(self.doc["consumable_by_dmr"], False)

    def test_identity_differs_from_published_r1(self):
        r1 = json.loads(
            (manifest_dir() / "x-v1.3.0-r1.json").read_text(encoding="utf-8")
        )
        self.assertNotEqual(self.doc["param_hash"], r1["param_hash"])
        self.assertNotEqual(self.doc["config_hash"], r1["config_hash"])
        self.assertEqual(self.doc["rule_revision"], "x-v1.3.0-r2")
        # 映射身份不变：本轮换的是 DMR 谓词，不是 216 表本身。
        self.assertEqual(self.doc["mapping_hash"], r1["mapping_hash"])
        self.assertEqual(self.doc["mcap_mapping_version"], r1["mcap_mapping_version"])

    def test_candidate_param_hash_matches_the_adapted_overrides(self):
        """候选 manifest 的 param_hash 必须真的由候选 overrides 算出来。

        否则切换时会拿一个对不上号的身份去标数据 —— 比不写还糟。
        """
        import importlib
        import os
        import tempfile

        frag = json.loads(
            (CANDIDATES / "board-variants.x-adapted.json").read_text(encoding="utf-8")
        )["overrides"]
        reg = json.loads(
            (ROOT / "packages/config/board-variants.json").read_text(encoding="utf-8")
        )
        for b in reg["boards"]:
            if b["key"] == "x":
                b["overrides"] = frag
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "board-variants.json"
            p.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
            prev = os.environ.get("BOARD_VARIANTS_CONFIG")
            os.environ["BOARD_VARIANTS_CONFIG"] = str(p)
            try:
                import coin_selection.board_variants as bv
                importlib.reload(bv)
                from coin_selection.scan import SelectionSettings

                v = bv.get_variant("x")
                got = bv.param_fingerprint(
                    v, bv.variant_settings(SelectionSettings(), v),
                    bv.variant_state_config(v),
                )
            finally:
                if prev is None:
                    os.environ.pop("BOARD_VARIANTS_CONFIG", None)
                else:
                    os.environ["BOARD_VARIANTS_CONFIG"] = prev
                import coin_selection.board_variants as bv2
                importlib.reload(bv2)
        self.assertEqual(got, self.doc["param_hash"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
