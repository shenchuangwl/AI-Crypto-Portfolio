"""v1.3参数一致性审计器：不把X的v1.4克隆通过误报为旧dual-path通过。
只读工具不运行历史内核，不写生产数据；未来X独立演进前仍需原始输入/身份完整。
"""
from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "services/coin-selection/src"))
from test_review_ledger import _board, _row
from unittest.mock import patch


class XAuditTests(unittest.TestCase):
    def test_clone_success_cannot_pass_legacy_acceptance(self):
        import audit_screener_x_v13 as audit
        result = {"integrity_errors": [], "legacy_alignment": "DIFFERENT"}
        self.assertEqual(audit.exit_status(result, require_legacy=False), 0)
        self.assertEqual(audit.exit_status(result, require_legacy=True), 2)

    def test_entry_audit_checks_membership_price_and_identity(self):
        """DMR入价必须是入点last，不借CONF停留价；缺节点不能当作通过。"""
        import audit_screener_x_v13 as audit
        board = _board("20260821-069", "2026-08-21T17:28:25Z", [
            _row("AAAUSDT", "up", "CONFIRMED", last=12, enter=10, dmr=True),
        ])
        board["meta"]["parameter_version"] = "param-v1.3.0-dual-path-sticky"
        trade = dict(trade_id="a", symbol="AAAUSDT", direction="up", zone="DMR",
                     enter_scan_id="20260821-069", enter_price=12,
                     parameter_version=board["meta"]["parameter_version"], param_hash=None)
        result = audit.audit_entries({"20260821-069": board}, [trade])
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["errors"], [])
        for changes in ({"enter_price": 10}, {"zone": "WATCH"},
                        {"enter_scan_id": "20260821-070"}, {"parameter_version": "wrong"},
                        {"param_hash": "invented"}):
            with self.subTest(changes=changes):
                self.assertTrue(audit.audit_entries({"20260821-069": board}, [{**trade, **changes}])["errors"])

    def test_empty_evidence_cannot_pass(self):
        import audit_screener_x_v13 as audit
        result = audit.audit_entries({}, [])
        self.assertEqual(result["checked"], 0)
        self.assertTrue(result["errors"])

    def test_missing_production_evidence_fails_closed_without_creating_data(self):
        """空目录不能有“看起来全绿”的报告，CLI也不得自动创建生产替身。"""
        import audit_screener_x_v13 as audit
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = audit.collect(root)
            self.assertTrue(result["integrity_errors"])
            self.assertNotEqual(audit.exit_status(result, require_legacy=True), 0)
            self.assertFalse((root / "data").exists())

    def test_requested_scan_id_is_validated_before_artifact_lookup(self):
        """保留源token，不把无效日期/序号修正为另一个扫描节点。"""
        import audit_screener_x_v13 as audit
        for sid in ("20260230-001", "20260906-096", "20260906-1", "../latest"):
            with self.subTest(scan_id=sid), patch.object(Path, "read_text") as read:
                result = audit.collect(ROOT, scan_id=sid)
                self.assertTrue(result["integrity_errors"])
                read.assert_not_called()

    def test_cli_missing_evidence_returns_json_and_nonzero(self):
        """CLI本体也真实执行，输出明确失败，而不是一个空stub的退出0。"""
        import contextlib
        import io
        import audit_screener_x_v13 as audit
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "argv", ["audit", "--root", tmp]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(audit.main(), 1)
            self.assertEqual(json.loads(output.getvalue())["exit_code"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
