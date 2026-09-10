"""X/复盘 v1.3.0 对齐前的只读边界：不得借审计重写 v1.4.0 克隆账本。
未来独立演进也只由选币写者更新账本；这些测试仅使用临时 SQLite，不触及生产。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from coin_selection.review_ledger import ReviewLedger


class ReviewReadonlyTests(unittest.TestCase):
    def test_readonly_connection_rejects_sql_write(self):
        """readonly 不仅跳过 rehydrate：必须由 SQLite 拒绝实际写入。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.sqlite"
            writer = ReviewLedger(path)
            writer.close()
            reader = ReviewLedger(path, readonly=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    reader._conn.execute("CREATE TABLE forbidden_write (id INTEGER)")
            finally:
                reader.close()

    def test_missing_readonly_database_does_not_create_directory(self):
        """旧v1.3基线缺库必须报错，不能创建空库冒充复盘就绪。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing" / "ledger.sqlite"
            with self.assertRaises(sqlite3.OperationalError):
                ReviewLedger(path, readonly=True)
            self.assertFalse(path.parent.exists())

    def test_readonly_preserves_existing_journal_mode(self):
        """查询旧账本不作隐式WAL迁移；v1.4写者逻辑不变。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "旧 v1.3 ?库.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE audit (id INTEGER)")
            conn.commit()
            conn.close()
            before = path.read_bytes()
            reader = ReviewLedger(path, readonly=True)
            try:
                self.assertEqual(reader._conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(reader._conn.execute("PRAGMA query_only").fetchone()[0], 1)
            finally:
                reader.close()
            self.assertEqual(path.read_bytes(), before)

    def test_readonly_sees_wal_writer_commits(self):
        """不能用immutable伪只读而漏掉X实时写者的新节点。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.sqlite"
            writer = ReviewLedger(path)
            reader = ReviewLedger(path, readonly=True)
            try:
                self.assertEqual(reader._conn.execute("SELECT COUNT(*) FROM watermarks").fetchone()[0], 0)
                writer._conn.execute("INSERT INTO watermarks(k,json) VALUES('scan','{}')")
                writer._conn.commit()
                self.assertEqual(reader._conn.execute("SELECT COUNT(*) FROM watermarks").fetchone()[0], 1)
            finally:
                reader.close()
                writer.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
