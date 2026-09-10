"""RuleManifest / config_hash / legacy 身份迁移单测。

权威：ChatGpt_SOL5.6 文档 A §13.1、§14；文档 B §3.1–§3.2、§5.1、§19.1 步骤 3、
§20「身份」行（同 parameter_version 不同 config_hash 必须拒绝混算）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import rule_manifest as rm  # noqa: E402
from coin_selection.scan import SelectionSettings  # noqa: E402
from coin_selection.state_machine import default_state_config  # noqa: E402


def _sc():
    return default_state_config()


# ---------------------------------------------------------------------------
# config_hash
# ---------------------------------------------------------------------------
def test_config_hash_is_stable_and_prefixed():
    s, c = SelectionSettings(), _sc()
    a = rm.config_hash(s, c)
    b = rm.config_hash(SelectionSettings(), default_state_config())
    assert a == b
    assert a.startswith("sha256:") and len(a) == 7 + 64


def test_every_scoring_weight_changes_the_config_hash():
    base = rm.config_hash(SelectionSettings(), _sc())
    for k in ("w_ss", "w_mom", "w_liq", "w_mcap", "w_cons", "w_rank", "w_risk"):
        s = SelectionSettings()
        setattr(s, k, float(getattr(s, k)) + 0.01)
        assert rm.config_hash(s, _sc()) != base, k


def test_runtime_fields_do_not_change_the_config_hash():
    """换个目录跑离线重放不该改变规则身份（文档 B §5.1）。"""
    base = rm.config_hash(SelectionSettings(), _sc())
    s = SelectionSettings()
    s.data_dir = "/tmp/elsewhere"
    s.dmr_inbox = "/tmp/inbox"
    s.gate1_workers = 99
    s.gate2_workers = 99
    assert rm.config_hash(s, _sc()) == base


def test_feature_flags_are_part_of_the_config_hash():
    s, c = SelectionSettings(), _sc()
    off = rm.config_hash(s, c, feature_flags={k: False for k in rm.FEATURE_FLAGS})
    for f in rm.FEATURE_FLAGS:
        flags = {k: False for k in rm.FEATURE_FLAGS}
        flags[f] = True
        assert rm.config_hash(s, c, feature_flags=flags) != off, f


def test_frozen_redline_block_is_in_the_hash():
    s, c = SelectionSettings(), _sc()
    base = rm.config_hash(s, c)
    old = rm.FROZEN_BLOCK["g1_floor_usd"]
    try:
        rm.FROZEN_BLOCK["g1_floor_usd"] = 1_000_000
        assert rm.config_hash(s, c) != base
    finally:
        rm.FROZEN_BLOCK["g1_floor_usd"] = old


def test_float_representation_noise_does_not_shake_the_hash():
    s1, s2 = SelectionSettings(), SelectionSettings()
    s2.w_ss = 0.1 + 0.2 - 0.3 + s1.w_ss  # 同值不同二进制路径
    assert rm.config_hash(s1, _sc()) == rm.config_hash(s2, _sc())


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def _manifest(**over):
    kw = dict(
        board_key="y",
        settings=SelectionSettings(),
        state_cfg=_sc(),
        mcap_mapping_version="mcap-216-v2.0.0-r1",
        mapping_hash="sha256:da14429d",
        code_commit="abc123",
    )
    kw.update(over)
    return rm.build_manifest(**kw)


def test_manifest_carries_all_thirteen_identity_fields():
    m = _manifest()
    ident = m.identity()
    for k in (
        "parameter_version",
        "rule_revision",
        "config_hash",
        "asset_mapping_version",
        "mcap_mapping_version",
        "mapping_hash",
        "code_commit",
        "indicator_version",
        "data_contract_version",
        "universe_policy_version",
        "effective_from_utc",
        "effective_from_scan_id",
        "feature_flags",
    ):
        assert k in ident and ident[k] not in (None, ""), k
    # 引用常量而不是字面量：rule_revision 每次逻辑变更都会递增（当前 r3），
    # 断言字面量会让每一次合法的版本递增都误报为测试失败。
    assert m.rule_revision == rm.RULE_REVISION


def test_effective_from_must_be_a_cycle_boundary():
    m = _manifest(effective_from_utc="2026-09-01T12:15:00Z")
    try:
        m.assert_publishable()
        raise AssertionError("mid-cycle effective_from must be refused")
    except rm.ManifestError:
        pass
    _manifest(effective_from_utc="2026-09-01T00:00:00Z").assert_publishable()


def test_missing_code_commit_blocks_strict_publish():
    m = _manifest(code_commit=rm.CODE_COMMIT_UNAVAILABLE)
    m.assert_publishable()  # 非严格模式允许，但字段是诚实的占位值
    assert not rm.code_commit_available(m.code_commit)
    try:
        m.assert_publishable(require_code_commit=True)
        raise AssertionError("strict publish must be blocked without code_commit")
    except rm.ManifestError:
        pass


def test_mapping_version_without_hash_is_refused():
    m = _manifest(mapping_hash=None)
    try:
        m.assert_publishable()
        raise AssertionError("mcap_mapping_version without mapping_hash must fail")
    except rm.ManifestError:
        pass


def test_publish_is_append_only():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m = _manifest()
        p = rm.publish(m, root=root)
        assert p.is_file()
        rm.publish(m, root=root)  # 同内容重复发布 = 幂等
        m2 = _manifest(code_commit="different")
        try:
            rm.publish(m2, root=root)
            raise AssertionError("overwriting a published manifest must be refused")
        except rm.ManifestError:
            pass
        loaded = rm.load(root=root)
        assert loaded.config_hash == m.config_hash
        assert loaded.manifest_hash() == m.manifest_hash()


def test_manifest_hash_detects_hand_editing():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        p = rm.publish(_manifest(), root=root)
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["code_commit"] = "tampered"
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        try:
            rm.load(root=root)
            raise AssertionError("hand-edited manifest must fail its hash check")
        except rm.ManifestError:
            pass


# ---------------------------------------------------------------------------
# 一致性等式：同 parameter_version 不同 config_hash 必须可区分
# ---------------------------------------------------------------------------
def test_same_parameter_version_different_config_hash_is_a_different_segment():
    a = _manifest()
    s = SelectionSettings()
    s.w_mcap = 0.25
    b = _manifest(settings=s)
    assert a.parameter_version == b.parameter_version
    assert a.config_hash != b.config_hash
    assert not rm.identities_match(a.identity(), b.identity(), keys=("config_hash",))


# ---------------------------------------------------------------------------
# legacy 迁移（文档 B §19.1 步骤 3）
# ---------------------------------------------------------------------------
def test_legacy_mapping_version_migrates_one_way_only():
    row = rm.migrate_legacy_identity({"mapping_version": "cg-map"})
    assert row["asset_mapping_version"] == "cg-map"
    assert row["identity_status"] == rm.LEGACY_INCOMPLETE
    # 缺的规则身份保留 null，不得用当前值补贴
    for k in ("rule_revision", "config_hash", "mapping_hash", "code_commit"):
        assert row.get(k) is None, k


def test_complete_identity_is_not_flagged_legacy():
    row = rm.migrate_legacy_identity(
        {
            "mapping_version": "cg-map",
            "rule_revision": "y-v2.0.0-r1",
            "config_hash": "sha256:x",
            "mcap_mapping_version": "mcap-216-v2.0.0-r1",
            "mapping_hash": "sha256:y",
            "code_commit": "abc",
            "data_contract_version": "raw-input-contract-v1",
            "universe_policy_version": "binance-usdt-perp-v1",
        }
    )
    assert row["identity_status"] == "COMPLETE"


def test_asset_and_mcap_mapping_are_never_aliases():
    m = _manifest()
    assert m.asset_mapping_version == "cg-map"
    assert m.mcap_mapping_version == "mcap-216-v2.0.0-r1"
    assert m.asset_mapping_version != m.mcap_mapping_version


def test_target_weights_sum_to_one():
    assert abs(sum(rm.TARGET_WEIGHTS.values()) - 1.0) < 1e-12
    assert abs(sum(rm.CURRENT_WEIGHTS.values()) - 1.0) < 1e-12
    assert rm.TARGET_WEIGHTS["w_mcap"] > rm.TARGET_WEIGHTS["w_mom"]


# ---------------------------------------------------------------------------
# 生效边界解析（文档 B §3.3）
# ---------------------------------------------------------------------------
def _publish_pair(root: Path):
    """在 tmp 里发两份 manifest：r1 从 0901-000 生效，r2 从 0902-000 生效。"""
    r1 = _manifest(
        rule_revision="y-v2.0.0-r1",
        code_commit=rm.CODE_COMMIT_UNAVAILABLE,
        effective_from_utc="2026-09-01T00:00:00Z",
        effective_from_scan_id="20260901-000",
    )
    r2 = _manifest(
        rule_revision="y-v2.0.0-r2",
        code_commit="85c20b2b0831a22cbc0e5c3d0a0c66577253eb18",
        effective_from_utc="2026-09-02T00:00:00Z",
        effective_from_scan_id="20260902-000",
    )
    rm.publish(r1, root=root)
    rm.publish(r2, root=root)
    return r1, r2


def test_effective_manifest_switches_exactly_at_the_cycle_boundary():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _publish_pair(root)
        for sid, want in (
            ("20260831-095", None),          # r1 生效之前：没有任何 manifest
            ("20260901-000", "y-v2.0.0-r1"),  # 边界当刻即生效
            ("20260901-095", "y-v2.0.0-r1"),  # 周期中途绝不切换
            ("20260902-000", "y-v2.0.0-r2"),  # 下一个边界切换
            ("20260903-042", "y-v2.0.0-r2"),
        ):
            got = rm.effective_manifest("y", sid, root=root)
            got = got.rule_revision if got else None
            assert got == want, (sid, got, want)


def test_effective_manifest_is_board_scoped():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _publish_pair(root)
        assert rm.effective_manifest("main", "20260902-000", root=root) is None
        assert len(rm.list_published("y", root=root)) == 2
        assert rm.list_published("main", root=root) == []


def test_list_published_skips_unreadable_manifest_without_failing():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _publish_pair(root)
        (rm.manifest_dir(root) / "broken.json").write_text("{not json", encoding="utf-8")
        # 坏文件只跳过，不抛 —— 一份损坏的历史 manifest 不该让在线扫描失败
        assert len(rm.list_published("y", root=root)) == 2


def test_identity_drift_flags_changed_fields_only():
    m = _manifest()
    live = dict(m.identity())
    assert rm.identity_drift(m, live) == []
    live["config_hash"] = "sha256:changed"
    live["code_commit"] = "another"
    assert sorted(rm.identity_drift(m, live)) == ["code_commit", "config_hash"]


def test_published_manifests_in_repo_are_consistent():
    """仓库里已发布的 manifest 必须自洽：hash 可校验、边界合法、r2 带真实 commit。"""
    pubs = rm.list_published()
    assert pubs, "packages/config/rule-manifests/ 为空"
    for m in pubs:
        m.assert_publishable()                       # 边界 / 必填 / mapping 配对
        assert m.manifest_hash().startswith("sha256:")
        if m.rule_revision.endswith("-r2"):
            assert rm.code_commit_available(m.code_commit), m.rule_revision
    # 同一板面的 revision 不得共用同一个生效边界（否则「哪份生效」有歧义）
    for board in {m.board_key for m in pubs}:
        sids = [m.effective_from_scan_id for m in pubs if m.board_key == board]
        assert len(sids) == len(set(sids)), (board, sids)


# ---------------------------------------------------------------------------
# 部署提交解析（第三轮实测缺陷的回归钉子，验收报告 §7.9.8）
# ---------------------------------------------------------------------------
def test_deployed_commit_file_beats_git_head():
    """部署提交文件优先于 git HEAD —— 否则 loop 与 adapter 会算出两个身份。"""
    import os

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        f = root / rm.DEPLOYED_COMMIT_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("deadbeefcafe\n", encoding="utf-8")
        old_env = {k: os.environ.pop(k, None) for k in
                   ("HERMES_CODE_COMMIT", "GIT_COMMIT", "CI_COMMIT_SHA", "IMAGE_DIGEST",
                    "HERMES_DEPLOY_COMMIT_FILE")}
        try:
            assert rm.resolve_code_commit(root) == "deadbeefcafe"
            # 环境变量仍然最高优先（CI / 容器注入）
            os.environ["HERMES_CODE_COMMIT"] = "from-ci"
            assert rm.resolve_code_commit(root) == "from-ci"
        finally:
            for k, v in old_env.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


def test_missing_deployed_commit_file_falls_back_without_raising():
    import os

    with tempfile.TemporaryDirectory() as d:
        old_env = {k: os.environ.pop(k, None) for k in
                   ("HERMES_CODE_COMMIT", "GIT_COMMIT", "CI_COMMIT_SHA", "IMAGE_DIGEST",
                    "HERMES_DEPLOY_COMMIT_FILE")}
        try:
            # 目录里既无部署文件也无 .git → 诚实占位，不抛
            assert rm.resolve_code_commit(Path(d)) == rm.CODE_COMMIT_UNAVAILABLE
        finally:
            for k, v in old_env.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


def test_all_processes_agree_on_code_commit():
    """同一台机器上任何进程解析出的 code_commit 必须一致（正向 allow-only 的前提）。"""
    from coin_selection.scan import SelectionSettings, main_exec_identity

    assert main_exec_identity(SelectionSettings())["code_commit"] == rm.resolve_code_commit()


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"ok {t.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"all {len(tests)}" if not fails else f"{fails} failed")
    raise SystemExit(1 if fails else 0)
