"""executor 侧二次守卫：不信任 accepted 目录。

权威：ChatGpt_SOL5.6 文档 A §14（``executor_accept``）、文档 B §18.6、§20（执行负例
含「裸 accepted、篡改 hash」）、§21.1。

要点：executor 重跑**同一个** ``execution_allowed``，并额外校验 envelope 的
payload hash。任何直接写进 ``accepted/`` 的裸 candidate 都不执行。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "contracts" / "python"))
sys.path.insert(0, str(ROOT / "services" / "dmr-executor" / "src"))

from execution_guard import (  # noqa: E402
    build_accepted_envelope,
    executor_accept,
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


def candidate(**over) -> dict:
    c = dict(MANIFEST)
    c.update(
        {
            "message_id": "m1",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "state": "CONFIRMED",
            "total_score": 80.0,
        }
    )
    c.update(over)
    return c


def envelope(**over) -> dict:
    return build_accepted_envelope(
        dict(MANIFEST), candidate(**over), deployed_manifest=MANIFEST
    )


def test_positive_envelope_is_accepted():
    assert bool(executor_accept(envelope(), MANIFEST))


def test_bare_candidate_without_envelope_is_never_executed():
    r = executor_accept(candidate(), MANIFEST)
    assert not r and r.reason == "ENVELOPE_SCHEMA"
    assert not executor_accept(None, MANIFEST)


def test_tampered_payload_hash_is_rejected():
    env = envelope()
    env["candidate"]["total_score"] = 99.0
    r = executor_accept(env, MANIFEST)
    assert not r and r.reason == "ENVELOPE_HASH_TAMPERED"


def test_envelope_for_board_y_is_rejected_even_if_hash_is_valid():
    y = dict(MANIFEST)
    y.update({"board_key": "y", "dmr_executable": False, "consumable_by_dmr": False})
    env = build_accepted_envelope(y, {**candidate(), **y}, deployed_manifest=y)
    r = executor_accept(env, y)
    assert not r, r.reason


def test_identity_drift_between_envelope_and_deployment_is_rejected():
    env = envelope()
    redeployed = dict(MANIFEST)
    redeployed["config_hash"] = "sha256:bbbb"
    r = executor_accept(env, redeployed)
    assert not r and r.reason.startswith("IDENTITY_MISMATCH")


def test_missing_deployed_manifest_blocks_everything():
    r = executor_accept(envelope(), None)
    assert not r and r.reason == "NO_DEPLOYED_MANIFEST"


def test_paper_decide_skips_when_guard_fails():
    from dmr_executor.paper import paper_decide

    bad = envelope()
    bad["candidate"]["symbol"] = "ETHUSDT"  # 破坏 hash
    dec = paper_decide(
        bad["candidate"], envelope=bad, manifest=MANIFEST, guard=executor_accept
    )
    assert dec["action"] == "SKIP"
    assert dec["guard_ok"] is False
    assert any(str(r).startswith("guard:") for r in dec["reasons"])


def test_paper_decide_opens_on_valid_envelope():
    from dmr_executor.paper import paper_decide

    env = envelope()
    dec = paper_decide(
        env["candidate"], envelope=env, manifest=MANIFEST, guard=executor_accept
    )
    assert dec["action"] == "PAPER_OPEN", dec
    assert dec["guard_ok"] is True


def test_run_paper_once_refuses_bare_files():
    """把裸 candidate 直接扔进 accepted/ —— run_paper_once 必须一个都不 PAPER_OPEN。"""
    import os

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        acc = tmp / "accepted"
        acc.mkdir(parents=True)
        (acc / "m1.json").write_text(json.dumps(candidate()), encoding="utf-8")
        (tmp / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        os.environ["DMR_ACCEPTED_DIR"] = str(acc)
        os.environ["DMR_EXECUTOR_DATA"] = str(tmp / "out")
        os.environ["DMR_DEPLOYED_MANIFEST"] = str(tmp / "manifest.json")
        for m in list(sys.modules):
            if m.startswith("dmr_executor"):
                del sys.modules[m]
        from dmr_executor.paper import run_paper_once

        run = run_paper_once(limit=10)
        assert run["counts"]["paper_open"] == 0, run["decisions"]
        assert run["guard"]["blocked"] == 1


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
