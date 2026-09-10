"""选币榜X v1.3 适配的**规则身份**闭合性（提示词 D2 / D4）。

要证明两件互相拉扯的事：

  1. 任何改变 X 选币结果的新开关，都必须改变 X 的 param_hash 与 config_hash。
     否则复盘按身份分段统计时会把两个体制的成交混算 —— D2 明令禁止
     「用同一 hash 制造一致性」。
  2. 同时 main / 选币榜Y 的已发布身份必须逐字节不变。
     这就是为什么两个字段都走**条件式省略**而不是加进白名单元组：
     实测直接加进 FINGERPRINT_SETTINGS_FIELDS 会让 main 的
     pf1_ddc6f8d09708942c 与 Y 的 pf1_fcea251fa94122fe 一起变号。

本模块只算 hash，不读也不写任何生产数据。
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/coin-selection/src"))

REGISTRY = ROOT / "packages/config/board-variants.json"
X_MANIFEST = ROOT / "packages/config/rule-manifests/x-v1.3.0-r1.json"

#: 现网 X（v1.4 克隆）的已发布身份 —— 权威来源是已生效的 manifest 文件本身。
#: 克隆配置算出来的身份必须与它逐字符相同，否则就是给已发布规则制造漂移。
PUBLISHED_X = json.loads(X_MANIFEST.read_text(encoding="utf-8"))


def identities(x_state: dict, x_settings: dict) -> dict:
    """在一份临时注册表上算三块板的身份。绝不改仓库里的 board-variants.json。"""
    doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
    for b in doc["boards"]:
        if b["key"] == "x":
            b["overrides"]["state_config"] = dict(x_state)
            # **替换而非合并**：原先是 merge，于是 CLONE=({},{}) 会继承生产当时
            # 的 settings。X 切到 ADAPTED 之后，生产 settings 里已经有
            # dmr_selection_mode，CLONE 就不再是 clone 了，断言随之失真。
            # 测试要描述的是「这四种配置各自的身份」，必须自带完整 settings。
            b["overrides"]["settings"] = dict(x_settings)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "board-variants.json"
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        prev = os.environ.get("BOARD_VARIANTS_CONFIG")
        os.environ["BOARD_VARIANTS_CONFIG"] = str(p)
        try:
            import coin_selection.board_variants as bv
            import coin_selection.rule_manifest as rm
            importlib.reload(bv)
            importlib.reload(rm)
            from coin_selection.scan import SelectionSettings

            out = {}
            for key in ("main", "x", "y"):
                v = bv.get_variant(key)
                st = bv.variant_state_config(v)
                se = bv.variant_settings(SelectionSettings(), v)
                flags = {
                    "ENABLE_MCAP_ZONE": bv.mcap_zone_mode_of(se) != "off",
                    "ENABLE_TARGET_WEIGHTS": False,
                    "ENABLE_MCAP_EFFECTIVE_ZONE": False,
                }
                out[key] = (
                    bv.param_fingerprint(v, se, st),
                    rm.config_hash(se, st, cycle=v.cycle, feature_flags=flags),
                )
            return out
        finally:
            if prev is None:
                os.environ.pop("BOARD_VARIANTS_CONFIG", None)
            else:
                os.environ["BOARD_VARIANTS_CONFIG"] = prev
            import coin_selection.board_variants as bv2
            import coin_selection.rule_manifest as rm2
            importlib.reload(bv2)
            importlib.reload(rm2)


CLONE = {}, {"mcap_zone_mode": "shadow"}
DMR_ONLY = {}, {"mcap_zone_mode": "shadow", "dmr_selection_mode": "confirmed-basic-216-v1.3"}
RULES_ONLY = {"selection_semantics": "dual-path-v1.3"}, {"mcap_zone_mode": "shadow"}
CANDIDATE = (
    {"selection_semantics": "dual-path-v1.3"},
    {"mcap_zone_mode": "shadow", "dmr_selection_mode": "confirmed-basic-216-v1.3"},
)


class MainYFrozen(unittest.TestCase):
    def test_main_and_y_identity_invariant_under_every_x_config(self):
        base = identities(*CLONE)
        for label, cfg in (
            ("dmr-only", DMR_ONLY), ("rules-only", RULES_ONLY), ("candidate", CANDIDATE)
        ):
            got = identities(*cfg)
            for key in ("main", "y"):
                self.assertEqual(
                    got[key], base[key],
                    f"{label}: {key} 的 param_hash/config_hash 必须逐字节不变",
                )

    def test_clone_config_still_matches_published_manifest(self):
        pf, ch = identities(*CLONE)["x"]
        self.assertEqual(pf, PUBLISHED_X["param_hash"])
        self.assertEqual(ch, PUBLISHED_X["config_hash"])


class NewSwitchesEnterIdentity(unittest.TestCase):
    """每个改变选币结果的开关都必须单独把身份改掉。"""

    def test_each_switch_alone_changes_x_identity(self):
        clone = identities(*CLONE)["x"]
        for label, cfg in (("dmr-only", DMR_ONLY), ("rules-only", RULES_ONLY)):
            got = identities(*cfg)["x"]
            self.assertNotEqual(got[0], clone[0], f"{label}: param_hash 未变")
            self.assertNotEqual(got[1], clone[1], f"{label}: config_hash 未变")

    def test_four_configs_are_four_distinct_identities(self):
        seen = {}
        for label, cfg in (
            ("clone", CLONE), ("dmr-only", DMR_ONLY),
            ("rules-only", RULES_ONLY), ("candidate", CANDIDATE),
        ):
            ident = identities(*cfg)["x"]
            self.assertNotIn(
                ident, seen,
                f"{label} 与 {seen.get(ident)} 撞身份 —— 两套规则一个 hash",
            )
            seen[ident] = label
        self.assertEqual(len(seen), 4)


class ConfigTimeValidation(unittest.TestCase):
    def test_illegal_dmr_mode_rejected_at_registry_boundary(self):
        for bad in ("confirmed-216", "basic-216", "", "STRICT-V1.4"):
            with self.assertRaises(ValueError, msg=bad):
                identities({}, {"dmr_selection_mode": bad})

    def test_illegal_semantics_rejected_at_registry_boundary(self):
        with self.assertRaises(ValueError):
            identities({"selection_semantics": "dual-path"}, {})

    def test_default_constant_is_duplicated_consistently(self):
        """rule_manifest 为避免 import 环留了一份字面量副本，两处必须一致。"""
        import coin_selection.board_variants as bv
        import coin_selection.rule_manifest as rm
        self.assertEqual(rm._DMR_MODE_DEFAULT, bv.DMR_SELECTION_MODE_DEFAULT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
