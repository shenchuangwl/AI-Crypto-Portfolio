"""X v1.3.0 对齐前先保护复盘数据身份；v1.4克隆不改参，未来换段不复用旧缓存。
全部用临时账本/快照，同水位换库不能返回旧币种，main/y无业务规则变化。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/coin-selection/src"))
sys.path.insert(0, str(ROOT / "services/api-gateway"))
import review_api
from coin_selection.review_ledger import ReviewLedger
from test_review_ledger import _board, _row


class ReviewCacheIdentityTests(unittest.TestCase):
    def setUp(self):
        review_api.clear_review_cache()
        review_api._latest_cache.clear()
        review_api._PHYSICAL_CACHE.clear()

    def _ledger(self, path, symbol):
        """仅币种不同，扫描水位/笔数/时刻完全相同的两份真实账本。"""
        led = ReviewLedger(path)
        try:
            for sid, ts, state in [
                ("20260906-001", "2026-09-06T00:15:00Z", "WATCH"),
                ("20260906-002", "2026-09-06T00:30:00Z", "ELIMINATED"),
            ]:
                led.ingest_board(_board(sid, ts, [_row(symbol, "up", state, last=1, enter=1)]))
        finally:
            led.close()

    def test_same_watermark_database_replacement_invalidates_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "ledger.sqlite"
            replacement = root / "replacement.sqlite"
            self._ledger(db, "AAAUSDT")
            self._ledger(replacement, "BBBUSDT")
            qs = {"board": ["x"], "zones": ["WATCH"]}
            paths = (db, root / "latest.json", "param-v1.3.0-screener-x")
            with patch.object(review_api, "board_paths", return_value=paths):
                code, before = review_api.review_payload(qs, kind="trades")
                self.assertEqual(code, 200)
                self.assertEqual([r["symbol"] for r in before["trades"]], ["AAAUSDT"])
                os.replace(replacement, db)
                code, after = review_api.review_payload(qs, kind="trades")
                self.assertEqual(code, 200)
                self.assertEqual([r["symbol"] for r in after["trades"]], ["BBBUSDT"])

    def test_latest_replacement_with_same_mtime_is_not_stale(self):
        """X换段保留mtime时仍必须读取新身份，不能静默回显v1.4克隆头。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = root / "latest.json"
            latest.write_text(json.dumps({"meta": {"scan_id": "20260906-001"}}))
            st = latest.stat()
            with patch.object(review_api, "board_paths", return_value=(root / "db", latest, "x")):
                self.assertEqual(review_api._latest_scan("x")["scan_id"], "20260906-001")
                replacement = root / "replacement.json"
                replacement.write_text(json.dumps({"meta": {"scan_id": "20260906-002"}}))
                os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
                os.replace(replacement, latest)
                self.assertEqual(review_api._latest_scan("x")["scan_id"], "20260906-002")

    def test_same_count_snapshots_with_different_gap_invalidate_coverage(self):
        """缺口身份依scan_id集合，不依文件数；仅临时文件模拟修复后的新段。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snaps = root / "snapshots"
            snaps.mkdir()
            for sid in ("20260906-001", "20260906-003"):
                (snaps / f"{sid}.json").write_text("{}")
            with patch.object(review_api, "board_paths", return_value=(root / "db", root / "latest.json", "x")):
                self.assertEqual(review_api.snapshot_coverage("x")["missing_nodes"], 1)
                (snaps / "20260906-003.json").rename(snaps / "20260906-002.json")
                self.assertEqual(review_api.snapshot_coverage("x")["missing_nodes"], 0)

    def test_sanitize_drops_foreign_parameter_version(self):
        params = {
            "parameter_versions": ["param-v1.4.0-staircase-confirm-dmr"],
            "param_hashes": None,
        }
        cov = {
            "parameter_segments": [{"version": "param-v2.0.0-screener-y"}],
            "param_hash_segments": [{"param_hash": "pf1_fcea251fa94122fe"}],
        }
        dropped = review_api.sanitize_param_filters(params, cov)
        self.assertEqual(dropped, ["pv:param-v1.4.0-staircase-confirm-dmr"])
        self.assertIsNone(params["parameter_versions"])

    def test_sanitize_keeps_historical_version_on_same_ledger(self):
        params = {
            "parameter_versions": ["param-v1.3.0-dual-path-sticky"],
            "param_hashes": None,
        }
        cov = {
            "parameter_segments": [
                {"version": "param-v1.3.0-dual-path-sticky"},
                {"version": "param-v1.4.0-staircase-confirm-dmr"},
            ],
            "param_hash_segments": [],
        }
        dropped = review_api.sanitize_param_filters(params, cov)
        self.assertEqual(dropped, [])
        self.assertEqual(params["parameter_versions"], ["param-v1.3.0-dual-path-sticky"])

    def test_y_payload_ignores_main_parameter_version(self):
        """``?board=y&pv=param-v1.4.0-…`` must not AND the Y ledger to 0 笔."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "ledger.sqlite"
            led = ReviewLedger(db)
            try:
                board = _board(
                    "20260910-048",
                    "2026-09-10T12:00:00Z",
                    [_row("HYPEUSDT", "up", "CONFIRMED", last=1.1, enter=1.0, dmr=True)],
                )
                board["meta"]["parameter_version"] = "param-v2.0.0-screener-y"
                led.ingest_board(board)
                board2 = _board(
                    "20260910-049",
                    "2026-09-10T12:15:00Z",
                    [_row("HYPEUSDT", "up", "WATCH", last=1.2, enter=1.2)],
                )
                board2["meta"]["parameter_version"] = "param-v2.0.0-screener-y"
                led.ingest_board(board2)
            finally:
                led.close()
            qs = {
                "board": ["y"],
                "zones": ["CONFIRMED"],
                "pv": ["param-v1.4.0-staircase-confirm-dmr"],
            }
            paths = (db, root / "latest.json", "param-v2.0.0-screener-y")
            with patch.object(review_api, "board_paths", return_value=paths):
                with patch.object(review_api, "board_cycle", return_value=None):
                    with patch.object(review_api, "snapshot_coverage", return_value={"available": False}):
                        with patch.object(review_api, "_latest_scan", return_value={}):
                            code, body = review_api.review_payload(qs, kind="trades")
            self.assertEqual(code, 200)
            self.assertGreaterEqual(body["summary"]["trades"], 1)
            self.assertIsNone(body["filters"]["parameter_versions"])
            self.assertTrue(
                any(str(x).startswith("pv:") for x in body["filters"]["dropped_params"])
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
