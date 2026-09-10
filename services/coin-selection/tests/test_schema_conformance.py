"""The published board must validate against the published schema.

Contract drift is silent: a scan happily writes `ret_15m: null` for months while
`screener-snapshot.schema.json` says `number`, and nothing notices until a
consumer written from the schema crashes. This test pins the two together, using
the *live* snapshot when one exists and the committed example otherwise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = ROOT / "contracts" / "json-schema"
EXAMPLES = ROOT / "contracts" / "examples"
LIVE = ROOT / "data" / "coin-selection" / "latest.json"
INBOX = ROOT / "data" / "dmr-adapter" / "inbox"
# 「选币榜Y」(param-v2.0.0-screener-y) 走同一份 schema。它是选币榜的复刻，
# 契约上不允许有自己的方言 —— 一旦分叉，「两块板面结构一致」就只是嘴上说说。
LIVE_Y = ROOT / "data" / "coin-selection-y" / "latest.json"
INBOX_Y = ROOT / "data" / "dmr-adapter-y" / "inbox"

try:
    from jsonschema import Draft202012Validator
except ImportError:  # optional dep — skip rather than fail the stdlib suite
    print("skip: jsonschema not installed")
    sys.exit(0)


def _validate(schema_name: str, doc: dict, label: str) -> None:
    schema = json.loads((SCHEMA_DIR / schema_name).read_text(encoding="utf-8"))
    errs = list(Draft202012Validator(schema).iter_errors(doc))
    assert not errs, f"{label}: {len(errs)} errors, first = " + " / ".join(
        f"{list(e.path)[:3]} {e.message[:120]}" for e in errs[:3]
    )


def test_snapshot_example_matches_schema():
    doc = json.loads((EXAMPLES / "screener-latest.example.json").read_text(encoding="utf-8"))
    _validate("screener-snapshot.schema.json", doc, "example snapshot")


def test_live_snapshot_matches_schema():
    if not LIVE.is_file():
        print("  (no live snapshot; example-only)")
        return
    doc = json.loads(LIVE.read_text(encoding="utf-8"))
    _validate("screener-snapshot.schema.json", doc, "live snapshot")


def test_live_board_y_snapshot_matches_schema():
    """选币榜Y 的板面必须过与选币榜完全相同的 schema。"""
    if not LIVE_Y.is_file():
        print("  (选币榜Y 尚未产出；跑一轮扫描或 scripts/replay_screener_y.py)")
        return
    doc = json.loads(LIVE_Y.read_text(encoding="utf-8"))
    _validate("screener-snapshot.schema.json", doc, "board-y snapshot")
    meta = doc.get("meta") or {}
    assert meta.get("board_key") == "y", meta.get("board_key")
    assert meta.get("parameter_version") == "param-v2.0.0-screener-y", meta.get("parameter_version")
    # 硬约束：选币榜Y 的候选不可被执行层消费。
    assert meta.get("dmr_executable") is False, meta.get("dmr_executable")


def test_dmr_example_matches_schema():
    doc = json.loads((EXAMPLES / "dmr-candidate.example.json").read_text(encoding="utf-8"))
    _validate("dmr-candidate-message.schema.json", doc, "example dmr message")


def test_live_dmr_candidates_match_schema():
    files = sorted(p for p in INBOX.glob("*.candidates.json") if not p.name.startswith("smoke-"))
    if not files:
        print("  (no live inbox)")
        return
    doc = json.loads(files[-1].read_text(encoding="utf-8"))
    for m in doc.get("candidates") or []:
        _validate("dmr-candidate-message.schema.json", m, f"{files[-1].name}:{m.get('symbol')}")


def test_live_board_y_dmr_candidates_match_schema():
    files = sorted(p for p in INBOX_Y.glob("*.candidates.json")) if INBOX_Y.is_dir() else []
    if not files:
        print("  (选币榜Y inbox 为空)")
        return
    doc = json.loads(files[-1].read_text(encoding="utf-8"))
    # 这份 inbox 存在的唯一理由是让复盘能还原 DMR 区；它必须自报「不可执行」。
    assert doc.get("consumable_by_dmr") is False, doc.get("consumable_by_dmr")
    assert doc.get("parameter_version") == "param-v2.0.0-screener-y", doc.get("parameter_version")
    for m in doc.get("candidates") or []:
        _validate("dmr-candidate-message.schema.json", m, f"y:{files[-1].name}:{m.get('symbol')}")


def test_t17_new_and_old_snapshots_both_validate():
    """新字段全部 optional：31 天窗口内必然同时存在「有指纹」与「无指纹」的快照，
    schema 必须两者都过（文档B §3.2 铁律 2 / 测试 T17）。"""
    src = LIVE_Y if LIVE_Y.is_file() else (LIVE if LIVE.is_file() else None)
    if src is None:
        print("  (没有可用的现网快照)")
        return
    doc = json.loads(src.read_text(encoding="utf-8"))

    # (a) 旧快照：一个新键都没有
    old = json.loads(json.dumps(doc))
    old["meta"].pop("param_hash", None)
    old["meta"].pop("mcap_zone", None)
    for pool in ("long_pool", "short_pool"):
        for r in old.get(pool) or []:
            for k in ("combo_code", "z_score", "combo_zone", "zone_ceiling"):
                r.pop(k, None)
    _validate("screener-snapshot.schema.json", old, "old-style snapshot")

    # (b) 新快照：全部新键都在
    new = json.loads(json.dumps(doc))
    new["meta"]["param_hash"] = "pf1_deadbeefdeadbeef"
    new["meta"]["mcap_zone"] = {
        "mode": "shadow",
        "enabled": False,
        "cuts": {"dmr": 2.1, "confirmed": 1.5, "qualified": 0.8, "watch": -0.5},
        "abstain_demote": True,
    }
    for pool in ("long_pool", "short_pool"):
        for r in new.get(pool) or []:
            r["combo_code"] = "ABC"
            r["z_score"] = 1.25
            r["combo_zone"] = "CONFIRMED"
            r["zone_ceiling"] = "QUALIFIED"
    _validate("screener-snapshot.schema.json", new, "new-style snapshot")

    # (c) 判不出级的行：三个新字段为 null 也合法
    nulls = json.loads(json.dumps(new))
    for pool in ("long_pool", "short_pool"):
        for r in nulls.get(pool) or []:
            r["combo_code"] = None
            r["z_score"] = None
            r["combo_zone"] = None
            r["zone_ceiling"] = None
    _validate("screener-snapshot.schema.json", nulls, "null-combo snapshot")


def test_t17_review_trade_schema_accepts_new_and_old_rows():
    schema_path = SCHEMA_DIR / "review-trade.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    props = schema.get("properties") or {}
    for k in ("param_hash", "combo_code", "combo_zone", "zone_ceiling", "z_score"):
        assert k in props, k
        types = props[k].get("type")
        assert isinstance(types, list) and "null" in types, (k, types)
    required = set(schema.get("required") or [])
    assert not (required & {"param_hash", "combo_code", "combo_zone", "zone_ceiling", "z_score"})


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
