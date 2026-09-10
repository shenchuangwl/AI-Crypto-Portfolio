"""板面停更 / 身份漂移必须对运维可见（本轮裁决四）。

受约束板面（选币榜Y）在身份漂移或未授权时会**拒绝出数**。此时 latest.json
仍然躺在磁盘上，于是：

  * ``loop_status.json`` 曾照旧写 ``status: "ok"``；
  * ``/health`` 曾只报 ``latest_exists: true``。

两者合起来把「板面已经黑了」说成健康。这套用例把相反的行为钉死。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))
sys.path.insert(0, str(ROOT / "services" / "api-gateway"))


def _load_gateway():
    import importlib

    return importlib.import_module("mock_server")


class _V:
    """board_variants() 里一条变体的最小替身。"""

    def __init__(self, key, latest: Path, ledger: Path):
        self.key = key
        self.label = f"board-{key}"
        self.parameter_version = "param-v2.0.0-screener-y"
        self.api_prefix = f"/api/v1/{key}"
        self.web_route = f"/{key}"
        self.dmr_executable = False
        self._latest = latest
        self._ledger = ledger

    def ledger_path(self, _root):
        return self._ledger


def _write_snapshot(p: Path, *, identity_status, drift=None, scan_id="20260902-064"):
    ri = {"rule_revision": "y-v2.0.0-r2", "identity_status": identity_status}
    if drift:
        ri["identity_drift"] = drift
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"meta": {"scan_id": scan_id, "rule_identity": ri}}),
        encoding="utf-8",
    )


def _health_for(gw, tmp, **kw):
    latest = tmp / "latest.json"
    _write_snapshot(latest, **kw)
    v = _V("y", latest, tmp / "ledger.sqlite")
    orig = gw.board_latest_path
    gw.board_latest_path = lambda key: latest
    try:
        return gw.board_health(v)
    finally:
        gw.board_latest_path = orig


def test_drift_snapshot_is_reported_as_drift_not_ok():
    gw = _load_gateway()
    tmp = Path(tempfile.mkdtemp(prefix="bh-drift-"))
    info = _health_for(
        gw, tmp, identity_status="DRIFT", drift=["config_hash", "mapping_hash"]
    )
    assert info["latest_exists"] is True, "文件确实在 —— 这正是旧口径会说 ok 的原因"
    assert info["status"] == "drift", info
    assert info["identity_drift"] == ["config_hash", "mapping_hash"], info


def test_match_snapshot_is_ok():
    gw = _load_gateway()
    tmp = Path(tempfile.mkdtemp(prefix="bh-ok-"))
    info = _health_for(gw, tmp, identity_status="MATCH")
    assert info["status"] == "ok", info
    assert info["stale"] is False, info


def test_stale_snapshot_is_reported_even_when_identity_matches():
    """板面停更 = 文件不再更新。身份对得上也救不了它。"""
    gw = _load_gateway()
    tmp = Path(tempfile.mkdtemp(prefix="bh-stale-"))
    latest = tmp / "latest.json"
    _write_snapshot(latest, identity_status="MATCH")
    old = time.time() - (gw.BOARD_STALE_AFTER_SEC + 60)
    import os

    os.utime(latest, (old, old))
    v = _V("y", latest, tmp / "ledger.sqlite")
    orig = gw.board_latest_path
    gw.board_latest_path = lambda key: latest
    try:
        info = gw.board_health(v)
    finally:
        gw.board_latest_path = orig
    assert info["stale"] is True, info
    assert info["status"] == "stale", info


def test_missing_snapshot_is_no_data():
    gw = _load_gateway()
    tmp = Path(tempfile.mkdtemp(prefix="bh-none-"))
    v = _V("y", tmp / "nope.json", tmp / "ledger.sqlite")
    orig = gw.board_latest_path
    gw.board_latest_path = lambda key: tmp / "nope.json"
    try:
        info = gw.board_health(v)
    finally:
        gw.board_latest_path = orig
    assert info["status"] == "no_data", info


def test_loop_status_marks_degraded_when_a_board_refuses():
    """``project_secondary_boards`` 返回 refused 时，循环状态不得再写 ok。"""
    out = {
        "scan_id": "20260902-064",
        "alerts": ["CONFIRM_OVERFLOW"],
        "variant_boards": [
            {"board": "y", "status": "refused", "reason": "MCAP_DOMINANCE_UNAVAILABLE"}
        ],
    }
    vb = out.get("variant_boards") or []
    board_status = [
        {
            "board": b.get("board"),
            "status": b.get("status") or "ok",
            **({"reason": b.get("reason")} if b.get("reason") else {}),
            "scan_id": b.get("scan_id") or out.get("scan_id"),
        }
        for b in vb
    ]
    degraded = [b for b in board_status if b["status"] not in ("ok", "skipped")]
    assert degraded, "refused 必须被算作降级"
    assert ("degraded" if degraded else "ok") == "degraded"
    alerts = (out.get("alerts") or []) + (["BOARD_REFUSED"] if degraded else [])
    assert "BOARD_REFUSED" in alerts
    assert [b["board"] for b in degraded] == ["y"]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
