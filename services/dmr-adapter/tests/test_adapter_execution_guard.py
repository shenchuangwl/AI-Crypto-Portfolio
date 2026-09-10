"""adapter 侧正向 allow-only 执行守卫的正负例矩阵。

权威：ChatGpt_SOL5.6 文档 A §17.1（必过验收倒数第 3 条）、文档 B §20（执行正例 / 执行负例）、
文档 B §21.1（上线验收门槛最后一条）。

验收判据（文档 B §20）
----------------------
* **唯一正例**：batch 与 candidate 都是有效 ``main``、两个严格 bool ``true``、
  完整已登记身份 → adapter ACCEPT；
* **负例**：board / 两个 bool / 每个身份字段的 missing、null、false、字符串 ``"true"``、
  ``y``、unknown；身份不一致、误白名单、合同不可用、裸 accepted、篡改 hash
  → 两边均不执行。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "contracts" / "python"))
sys.path.insert(0, str(ROOT / "services" / "dmr-adapter" / "src"))

from execution_guard import (  # noqa: E402
    REASON_CONTRACT_UNAVAILABLE,
    accepted_envelope_integrity_ok,
    adapter_accept,
    build_accepted_envelope,
    execution_allowed,
    payload_hash,
    strict_required_non_null,
)

MANIFEST = {
    "board_key": "main",
    "dmr_executable": True,
    "consumable_by_dmr": True,
    "parameter_version": "param-v1.4.0-staircase-confirm-dmr",
    "rule_revision": "main-v1.4.0-r1",
    "config_hash": "sha256:aaaa",
    "mcap_mapping_version": "NOT_APPLICABLE",
    "mapping_hash": "NOT_APPLICABLE",
    "code_commit": "deadbeef",
}
WHITELIST = ["param-v1.4.0-staircase-confirm-dmr"]


def good_batch() -> dict:
    return dict(MANIFEST)


def good_candidate() -> dict:
    c = dict(MANIFEST)
    c.update({"message_id": "m1", "symbol": "BTCUSDT", "direction": "LONG", "state": "CONFIRMED"})
    return c


# ---------------------------------------------------------------------------
# 唯一正例
# ---------------------------------------------------------------------------
def test_the_single_positive_case_is_accepted():
    r = adapter_accept(good_batch(), good_candidate(), MANIFEST, whitelist=WHITELIST)
    assert bool(r) is True, r.reason
    assert execution_allowed(good_batch(), good_candidate(), MANIFEST)


# ---------------------------------------------------------------------------
# 负例：board_key
# ---------------------------------------------------------------------------
def test_board_key_y_is_rejected_on_both_sides():
    b, c = good_batch(), good_candidate()
    b["board_key"] = "y"
    assert not execution_allowed(b, c, MANIFEST)
    b, c = good_batch(), good_candidate()
    c["board_key"] = "y"
    assert not execution_allowed(b, c, MANIFEST)


def test_board_key_unknown_or_missing_or_null():
    for bad in ("unknown", "MAIN", "", None):
        b = good_batch()
        b["board_key"] = bad
        assert not execution_allowed(b, good_candidate(), MANIFEST), bad
    b = good_batch()
    del b["board_key"]
    r = execution_allowed(b, good_candidate(), MANIFEST)
    assert not r and r.reason == "BATCH_MISSING:board_key"


# ---------------------------------------------------------------------------
# 负例：两个严格布尔
# ---------------------------------------------------------------------------
def test_strict_true_rejects_string_true_and_one_and_false():
    for field in ("dmr_executable", "consumable_by_dmr"):
        for bad in ("true", "True", 1, 1.0, "yes", False, None):
            b = good_batch()
            b[field] = bad
            assert not execution_allowed(b, good_candidate(), MANIFEST), (field, bad)
            c = good_candidate()
            c[field] = bad
            assert not execution_allowed(good_batch(), c, MANIFEST), (field, bad)


def test_missing_bool_field_is_rejected():
    for field in ("dmr_executable", "consumable_by_dmr"):
        b = good_batch()
        del b[field]
        r = execution_allowed(b, good_candidate(), MANIFEST)
        assert not r and r.reason == f"BATCH_MISSING:{field}"


# ---------------------------------------------------------------------------
# 负例：每个身份字段的 missing / null / 不一致
# ---------------------------------------------------------------------------
IDENTITY_FIELDS = (
    "parameter_version",
    "rule_revision",
    "config_hash",
    "mcap_mapping_version",
    "mapping_hash",
    "code_commit",
)


def test_every_identity_field_missing_null_or_mismatched_is_rejected():
    for f in IDENTITY_FIELDS:
        b = good_batch()
        del b[f]
        assert not execution_allowed(b, good_candidate(), MANIFEST), f"batch missing {f}"

        c = good_candidate()
        c[f] = None
        assert not execution_allowed(good_batch(), c, MANIFEST), f"candidate null {f}"

        c = good_candidate()
        c[f] = "tampered"
        r = execution_allowed(good_batch(), c, MANIFEST)
        assert not r and r.reason.startswith("IDENTITY_MISMATCH"), f

        m = dict(MANIFEST)
        m[f] = "different-deployment"
        assert not execution_allowed(good_batch(), good_candidate(), m), f


def test_no_deployed_manifest_means_no_execution():
    r = execution_allowed(good_batch(), good_candidate(), None)
    assert not r and r.reason == "NO_DEPLOYED_MANIFEST"


def test_wrong_types_are_rejected():
    b = good_batch()
    b["config_hash"] = 12345
    assert not execution_allowed(b, good_candidate(), MANIFEST)
    assert not execution_allowed(None, good_candidate(), MANIFEST)
    assert not execution_allowed(good_batch(), None, MANIFEST)
    assert not strict_required_non_null(["not", "a", "mapping"])


# ---------------------------------------------------------------------------
# 负例：白名单只能收紧 / 合同不可用
# ---------------------------------------------------------------------------
def test_whitelist_can_only_tighten_never_loosen():
    # 误把 Y 的 parameter_version 加进白名单，也不能让 board_key=y 通过。
    b, c = good_batch(), good_candidate()
    b["board_key"] = c["board_key"] = "y"
    b["parameter_version"] = c["parameter_version"] = "param-v2.0.0-screener-y"
    m = dict(MANIFEST)
    m["parameter_version"] = "param-v2.0.0-screener-y"
    r = adapter_accept(b, c, m, whitelist=["param-v2.0.0-screener-y"])
    assert not r, r.reason

    # 反向：身份全对但 parameter_version 不在白名单 → 拒绝（收紧生效）。
    r = adapter_accept(good_batch(), good_candidate(), MANIFEST, whitelist=["other"])
    assert not r and r.reason == "PARAMETER_VERSION_NOT_WHITELISTED"


def test_contract_unavailable_rejects_whole_batch():
    r = adapter_accept(
        good_batch(), good_candidate(), MANIFEST,
        whitelist=WHITELIST, contract_available=False,
    )
    assert not r and r.reason == REASON_CONTRACT_UNAVAILABLE


# ---------------------------------------------------------------------------
# accepted envelope
# ---------------------------------------------------------------------------
def test_envelope_roundtrip_and_tamper_detection():
    env = build_accepted_envelope(good_batch(), good_candidate(), deployed_manifest=MANIFEST)
    assert bool(accepted_envelope_integrity_ok(env))
    tampered = json.loads(json.dumps(env))
    tampered["candidate"]["symbol"] = "ETHUSDT"
    r = accepted_envelope_integrity_ok(tampered)
    assert not r and r.reason == "ENVELOPE_HASH_TAMPERED"
    bare = dict(good_candidate())
    assert not accepted_envelope_integrity_ok(bare)
    assert not accepted_envelope_integrity_ok(None)
    assert payload_hash({"a": 1}) == payload_hash({"a": 1})


# ---------------------------------------------------------------------------
# 端到端：真实 DmrAdapter 处理一个 Y 批次必须一个都不 accept
# ---------------------------------------------------------------------------
def _run_adapter(batch: dict, tmp: Path):
    import os

    os.environ["DMR_DEPLOYED_MANIFEST"] = str(tmp / "manifest.json")
    (tmp / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    from dmr_adapter.adapter import DmrAdapter

    inbox, outbox, accepted = tmp / "inbox", tmp / "outbox", tmp / "accepted"
    a = DmrAdapter(inbox=inbox, outbox=outbox, accepted_dir=accepted)
    a.rejected_dir = tmp / "rejected"
    a.rejected_dir.mkdir(parents=True, exist_ok=True)
    a.deployed_manifest = MANIFEST
    a.param_whitelist = WHITELIST
    path = inbox / f"{batch['scan_id']}.candidates.json"
    path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
    return a.process_file(path)


def _minimal_candidate(**over) -> dict:
    c = {
        "message_id": "mid-1",
        "system_version": "v1.2",
        "anchor_date": "2026-09-01",
        "scan_id": "20260901-001",
        "scan_sequence": 1,
        "scan_timestamp_utc": "2026-09-01T00:15:00Z",
        "generated_at_utc": "2026-09-01T00:15:00Z",
        "expires_at_utc": "2099-01-01T00:00:00Z",
        "symbol": "BTCUSDT",
        "underlying_asset": "BTC",
        "canonical_asset_id": "bitcoin",
        "contract_multiplier": 1,
        "direction": "LONG",
        "state": "CONFIRMED",
        "state_duration_minutes": 60.0,
        "direction_confidence": 1.0,
        "total_score": 80.0,
        "liquidity_score": 90.0,
        "momentum_score": 70.0,
        "market_cap_momentum_score": 70.0,
        "staircase_score": 60.0,
        "trend_consistency_score": 100.0,
        "rank_velocity_score": 50.0,
        "risk_score": 100.0,
        "data_confidence": 95.0,
        "market_rank": 1,
        "tier_rank": 1,
        "market_cap_tier": "T1",
        "coingecko_coin_id": "bitcoin",
        "circulating_supply": 19_000_000.0,
        "market_cap_calculated": 1.0e12,
        "market_cap_coingecko": 1.0e12,
        "risk_flags": [],
        "data_mode": "LIVE",
        "reason_codes": ["G1_PASS"],
        "indicator_version": "g1g2g3g4-sm-dual-path",
        "mapping_version": "cg-map",
        "data_version": "data-20260901-001",
    }
    c.update(MANIFEST)
    c.update(over)
    return c


def test_end_to_end_y_batch_accepts_nothing():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        y_identity = dict(MANIFEST)
        y_identity.update(
            {
                "board_key": "y",
                "dmr_executable": False,
                "consumable_by_dmr": False,
                "parameter_version": "param-v2.0.0-screener-y",
            }
        )
        batch = {
            "scan_id": "20260901-001",
            **y_identity,
            "candidates": [_minimal_candidate(**y_identity)],
        }
        out = _run_adapter(batch, tmp)
        assert out["accepted"] == [], out["accepted"]
        assert len(out["rejected"]) == 1
        assert not list((tmp / "accepted").glob("*.json"))


def test_end_to_end_main_batch_accepts_and_writes_envelope():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        batch = {
            "scan_id": "20260901-002",
            **MANIFEST,
            "candidates": [_minimal_candidate()],
        }
        out = _run_adapter(batch, tmp)
        assert len(out["accepted"]) == 1, out["rejected"]
        files = list((tmp / "accepted").glob("*.json"))
        assert len(files) == 1
        env = json.loads(files[0].read_text(encoding="utf-8"))
        assert env["schema"] == "dmr-accepted-envelope-v1"
        assert bool(accepted_envelope_integrity_ok(env))


def test_end_to_end_main_batch_without_identity_is_rejected():
    """旧格式批次（没有 board_key / 身份）不得被 accepted —— 只能进归档。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cand = _minimal_candidate()
        for k in MANIFEST:
            cand.pop(k, None)
        cand["parameter_version"] = MANIFEST["parameter_version"]
        batch = {"scan_id": "20260901-003", "candidates": [cand]}
        out = _run_adapter(batch, tmp)
        assert out["accepted"] == []
        assert out["rejected"][0]["reason"].startswith("GUARD:")


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
