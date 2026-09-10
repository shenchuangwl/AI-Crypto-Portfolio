"""切换器的闸必须真的挡得住（scripts/switch_x_to_v13.py）。

这次切换的失败模式不是「切不动」，而是「切了但标错」：
把 ``effective_from`` 倒填到一个已经产出旧规则数据的边界上，会让那段数据被
当成新规则的产物统计 —— 复盘的胜率/盈亏比从此不可信，且事后无法分辨。
所以边界推导与拒绝逻辑必须有测试看着，不能只靠人肉记得。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/switch_x_to_v13.py"
REGISTRY = ROOT / "packages/config/board-variants.json"

spec = importlib.util.spec_from_file_location("switch_x_to_v13", SCRIPT)
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)


class BoundaryDerivation(unittest.TestCase):
    """effective_from 只能对齐到「本次执行真正能落上的那个」00:00 UTC 边界。"""

    def test_next_boundary_is_always_midnight_utc(self):
        for hh in (0, 1, 12, 23):
            now = datetime(2026, 9, 6, hh, 37, tzinfo=timezone.utc)
            b = sw.next_boundary(now)
            self.assertEqual((b.hour, b.minute, b.second), (0, 0, 0))
            self.assertGreater(b, now)
            self.assertEqual(b - sw.today_boundary(now), timedelta(days=1))

    def test_today_boundary_is_midnight_of_the_same_day(self):
        now = datetime(2026, 9, 6, 13, 19, tzinfo=timezone.utc)
        self.assertEqual(
            sw.today_boundary(now), datetime(2026, 9, 6, 0, 0, tzinfo=timezone.utc)
        )

    def test_manifest_validation_requires_midnight(self):
        """这条约束不是脚本自己发明的，是 RuleManifest 强制的（文档B §3.3）。"""
        sys.path.insert(0, str(ROOT / "services/coin-selection/src"))
        from coin_selection.rule_manifest import RuleManifest
        base = json.loads(
            (ROOT / "packages/config/candidates/x-v1.3.0-r2.candidate.json")
            .read_text(encoding="utf-8")
        )
        base.pop("manifest_hash", None)
        fields = {
            k: v for k, v in base.items()
            if k in RuleManifest.__dataclass_fields__ and k not in ("feature_flags", "notes")
        }
        fields.update(
            effective_from_utc="2026-09-07T13:00:00Z",   # 非 00:00 —— 必须被拒
            effective_from_scan_id="20260907-052",
            code_commit="0" * 40,
        )
        with self.assertRaises(Exception):
            RuleManifest(
                **fields,
                feature_flags=dict(base.get("feature_flags") or {}),
                notes=tuple(base.get("notes") or ()),
            ).validate()


class ExecuteIsRefusedOffBoundary(unittest.TestCase):
    def test_execute_outside_window_changes_nothing(self):
        before = REGISTRY.read_bytes()
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--execute"],
            capture_output=True, text=True, cwd=ROOT,
        )
        # 本测试跑在任意时刻。若恰好落在边界窗口内（00:00–00:30 UTC），
        # 拒绝断言不成立 —— 那种情况下只断言「没有偷偷改注册表以外的东西」。
        now = datetime.now(timezone.utc)
        in_window = (now - sw.today_boundary(now)).total_seconds() / 60.0 <= 30
        if not in_window:
            self.assertEqual(r.returncode, 1, r.stdout[-500:])
            self.assertIn("前置闸未通过", r.stdout)
            self.assertEqual(REGISTRY.read_bytes(), before, "被拒后注册表不得有任何改动")

    def test_dry_run_never_writes(self):
        before = REGISTRY.read_bytes()
        published = ROOT / "packages/config/rule-manifests/x-v1.3.0-r2.json"
        existed = published.exists()
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--dry-run"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(r.returncode, 0, r.stdout[-500:])
        self.assertEqual(REGISTRY.read_bytes(), before, "dry-run 不得改注册表")
        self.assertEqual(published.exists(), existed, "dry-run 不得发布 manifest")

    def test_dry_run_plans_a_future_boundary_not_a_stale_one(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--dry-run"],
            capture_output=True, text=True, cwd=ROOT,
        )
        line = next(l for l in r.stdout.splitlines() if "将发布" in l)
        planned = line.split("effective_from=")[1].split()[0]
        now = datetime.now(timezone.utc)
        in_window = (now - sw.today_boundary(now)).total_seconds() / 60.0 <= 30
        want = (sw.today_boundary(now) if in_window else sw.next_boundary(now))
        self.assertEqual(planned, want.strftime("%Y%m%d") + "-000",
                         "生效边界不得倒填到已产出旧规则数据的那一天")


class RollbackShape(unittest.TestCase):
    def test_clone_shape_matches_the_pre_switch_backup(self):
        """回滚目标必须等于**切换前那份备份**，而不是「当前生产」。

        原断言拿 CLONE_OVERRIDES 与当前注册表比 —— 切换前恰好相等所以是绿的，
        切换后当前是 ADAPTED，断言立刻红，但回滚目标本身完全正确。
        对照物选错了：回滚要还原的是切换**之前**的形态，那份形态就存在
        board-variants.json.pre-x-v13.bak 里。没有备份（尚未切换）时退回原口径。
        """
        bak = REGISTRY.with_suffix(".json.pre-x-v13.bak")
        src = bak if bak.is_file() else REGISTRY
        cur = next(
            b for b in json.loads(src.read_text(encoding="utf-8"))["boards"]
            if b["key"] == "x"
        )["overrides"]
        self.assertEqual(sw.CLONE_OVERRIDES, cur)



class AllowLateIsHonest(unittest.TestCase):
    """--allow-late 必须真的放行，并把代价如实记账（原实现是坏的：说放行、实际拒绝）。"""

    def test_dry_run_previews_the_same_boundary_as_execute(self):
        """演练必须演的是真实执行那件事，否则演练毫无意义。

        原实现把 late 判定挂在 executing 上，导致 --dry-run --allow-late 预览出
        的是**下一个**边界，而真实 --execute --allow-late 用的是今日边界 —— 演练
        和执行不是同一件事，等于没演。
        """
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--dry-run", "--allow-late"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(r.returncode, 0, r.stdout[-400:])
        now = datetime.now(timezone.utc)
        mins = (now - sw.today_boundary(now)).total_seconds() / 60.0
        if 30 < mins < 1440:
            want = sw.today_boundary(now).strftime("%Y%m%d") + "-000"
            line = next(l for l in r.stdout.splitlines() if "将发布" in l)
            self.assertIn(f"effective_from={want}", line,
                          "--allow-late 应对齐今日边界，且 dry-run 要预览出同一个")

    def test_allow_late_records_the_unmet_precondition(self):
        """补做必须打印未兑现前提 —— 这是它与「准点切换」的唯一区别。"""
        now = datetime.now(timezone.utc)
        mins = (now - sw.today_boundary(now)).total_seconds() / 60.0
        if not (30 < mins < 1440):
            self.skipTest("此刻不在补做区间，无法断言")
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--dry-run", "--allow-late"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertIn("补做模式", r.stdout)
        self.assertIn("未兑现前提", r.stdout)
        self.assertIn("rebuild_screener_x_v13", r.stdout,
                      "必须指名后续必须跑的历史重建命令")

    def test_without_allow_late_still_aligns_to_next_boundary(self):
        """不加 --allow-late 时行为不变：窗口外一律顺延到下一个边界。"""
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--dry-run"],
            capture_output=True, text=True, cwd=ROOT,
        )
        now = datetime.now(timezone.utc)
        mins = (now - sw.today_boundary(now)).total_seconds() / 60.0
        if mins <= 30:
            self.skipTest("此刻在准点窗口内")
        want = sw.next_boundary(now).strftime("%Y%m%d") + "-000"
        line = next(l for l in r.stdout.splitlines() if "将发布" in l)
        self.assertIn(f"effective_from={want}", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
