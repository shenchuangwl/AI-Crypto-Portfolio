"""保留策略执行器的护栏测试（scripts/prune_data.py）。

删数据的脚本不能靠「看起来对」上线。这里对着一个合成目录树逐条验证：
过期才删、地板保住最近的、账本没消费的不删、never_delete 名单不动、
dry-run 一个字节都不掉、日志轮转用 copytruncate 不换 inode。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PRUNE = ROOT / "scripts" / "prune_data.py"
TMP = Path("/tmp/hermes_retention_test")


def _age(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    import os

    os.utime(path, (t, t))


def _fixture(watermark: str | None = "20260824-045") -> tuple[Path, Path]:
    """造一棵与生产同构的小树 + 一个只有 watermarks 表的账本。"""
    if TMP.exists():
        shutil.rmtree(TMP)
    snaps = TMP / "data" / "coin-selection" / "snapshots"
    inbox = TMP / "data" / "dmr-adapter" / "inbox"
    logs = TMP / "data" / "coin-selection" / "logs"
    cache = TMP / "data" / "coin-selection" / "kline_tf_cache" / "30m"
    for d in (snaps, inbox, logs, cache, TMP / "data" / "coin-selection" / "review"):
        d.mkdir(parents=True, exist_ok=True)

    # 老节点（2026-05-01，>90 天前）与新节点（2026-08-24）
    for seq in range(1, 6):
        (snaps / f"20260501-{seq:03d}.json").write_text("{}", encoding="utf-8")
        (snaps / f"20260501-{seq:03d}.full.json").write_text("{}", encoding="utf-8")
        (inbox / f"20260501-{seq:03d}.candidates.json").write_text("{}", encoding="utf-8")
        (inbox / f"20260501-{seq:03d}.candidates.json.done").write_text("", encoding="utf-8")
    for seq in range(44, 48):
        (snaps / f"20260824-{seq:03d}.json").write_text("{}", encoding="utf-8")
        (inbox / f"20260824-{seq:03d}.candidates.json").write_text("{}", encoding="utf-8")

    # 永不删名单里的文件
    (TMP / "data" / "coin-selection" / "latest.json").write_text("{}", encoding="utf-8")

    # 可再生缓存：一个 30 天前的、一个刚写的
    old_cache = cache / "OLDUSDT.json"
    old_cache.write_text("{}", encoding="utf-8")
    _age(old_cache, 30)
    (cache / "NEWUSDT.json").write_text("{}", encoding="utf-8")

    # 账本：只需要 watermarks 表
    db = TMP / "data" / "coin-selection" / "review" / "ledger.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE watermarks (k TEXT PRIMARY KEY, scan_id TEXT, ts TEXT, json TEXT)")
    if watermark:
        conn.execute("INSERT INTO watermarks VALUES('scan',?,?,?)", (watermark, "", "{}"))
    conn.commit()
    conn.close()

    cfg = {
        "version": "retention-test",
        "disk": {"mount": str(TMP), "warn_free_gb": 0, "min_free_gb": 0},
        "observed": {"total_mb_per_day": 1},
        "rules": [
            {
                "name": "snapshots_board",
                "path": "data/coin-selection/snapshots",
                "glob": "*.json",
                "exclude_glob": "*.full.json",
                "age_from": "scan_id",
                "keep_days": 90,
                "min_keep_files": 0,
                "guard_review_ledger": True,
            },
            {
                "name": "snapshots_full",
                "path": "data/coin-selection/snapshots",
                "glob": "*.full.json",
                "age_from": "scan_id",
                "keep_days": 30,
                "min_keep_files": 0,
            },
            {
                "name": "dmr_inbox",
                "path": "data/dmr-adapter/inbox",
                "glob": "*.candidates.json*",
                "age_from": "scan_id",
                "keep_days": 90,
                "min_keep_files": 0,
                "guard_review_ledger": True,
            },
            {
                "name": "cache_kline_tf",
                "path": "data/coin-selection/kline_tf_cache",
                "glob": "**/*",
                "age_from": "mtime",
                "keep_days": 7,
                "min_keep_files": 0,
                "recursive": True,
            },
        ],
        "logs": {"paths": ["data/coin-selection/logs/*.log"], "rotate_at_mb": 0.001, "keep_rotations": 2, "compress": True},
        "never_delete": ["data/coin-selection/latest.json"],
    }
    cfg_path = TMP / "retention.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return TMP, cfg_path


def _run(cfg_path: Path, *extra: str) -> subprocess.CompletedProcess:
    # prune_data.py 的 ROOT 由自身位置推导，测试时把脚本复制进夹具树。
    dst = TMP / "scripts"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(PRUNE, dst / "prune_data.py")
    return subprocess.run(
        [sys.executable, str(dst / "prune_data.py"), "--config", str(cfg_path), *extra],
        capture_output=True,
        text=True,
    )


def test_dry_run_deletes_nothing():
    root, cfg = _fixture()
    snaps = root / "data" / "coin-selection" / "snapshots"
    before = sorted(p.name for p in snaps.iterdir())
    r = _run(cfg)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    assert sorted(p.name for p in snaps.iterdir()) == before, "dry-run 不得动任何文件"
    assert "would delete" in r.stdout


def test_apply_deletes_only_expired():
    root, cfg = _fixture()
    snaps = root / "data" / "coin-selection" / "snapshots"
    r = _run(cfg, "--apply")
    assert r.returncode == 0, r.stderr
    left = sorted(p.name for p in snaps.iterdir())
    # 2026-05-01 的板面全过期；.full.json 30 天也过期
    assert not any(n.startswith("20260501") for n in left), left
    # 新节点一个都不能少
    for seq in range(44, 48):
        assert f"20260824-{seq:03d}.json" in left, left


def test_never_delete_list_is_untouched():
    root, cfg = _fixture()
    keep = root / "data" / "coin-selection" / "latest.json"
    _age(keep, 400)  # 老到不能再老
    _run(cfg, "--apply")
    assert keep.exists(), "never_delete 名单里的文件被删了"


def test_ledger_guard_blocks_unconsumed_nodes():
    """账本只消费到 -045 时，-046/-047 属于「还没入账」，绝不能删。"""
    root, cfg = _fixture(watermark="20260824-045")
    snaps = root / "data" / "coin-selection" / "snapshots"
    # 把新节点也弄成「过期」：keep_days 改成 0，只留账本护栏挡着
    data = json.loads(cfg.read_text(encoding="utf-8"))
    for rule in data["rules"]:
        rule["keep_days"] = 0
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    r = _run(cfg, "--apply")
    assert r.returncode == 0, r.stderr
    left = sorted(p.name for p in snaps.iterdir())
    assert "20260824-046.json" in left, f"账本未消费的节点被删了：{left}"
    assert "20260824-047.json" in left, left
    assert "20260824-045.json" not in left, "已消费的节点应可回收"
    assert "尚未消费" in r.stdout


def test_missing_ledger_skips_guarded_rules_entirely():
    """水位读不到时宁可不删——不能因为账本坏了就把事实源清掉。"""
    root, cfg = _fixture(watermark=None)
    snaps = root / "data" / "coin-selection" / "snapshots"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    for rule in data["rules"]:
        rule["keep_days"] = 0
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = _run(cfg, "--apply")
    left = sorted(p.name for p in snaps.iterdir())
    assert any(n.endswith(".json") and not n.endswith(".full.json") for n in left), left
    assert "水位不可读" in r.stdout
    # 没有护栏的规则照常执行
    assert not any(n.endswith(".full.json") for n in left), left


def test_min_keep_files_floor():
    """keep_days=0 也要留住最近 N 个——防止误配一次清空事实源。"""
    root, cfg = _fixture(watermark="20260824-047")
    snaps = root / "data" / "coin-selection" / "snapshots"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    for rule in data["rules"]:
        rule["keep_days"] = 0
        if rule["name"] == "snapshots_board":
            rule["min_keep_files"] = 3
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = _run(cfg, "--apply")
    boards = sorted(p.name for p in snaps.iterdir() if not p.name.endswith(".full.json"))
    assert len(boards) == 3, boards
    assert boards == ["20260824-045.json", "20260824-046.json", "20260824-047.json"], boards
    assert "min_keep_files" in r.stdout


def test_inbox_marker_pairs_go_together():
    root, cfg = _fixture()
    inbox = root / "data" / "dmr-adapter" / "inbox"
    _run(cfg, "--apply")
    left = sorted(p.name for p in inbox.iterdir())
    assert not any(n.startswith("20260501") for n in left), left
    # 成对删除：不能只剩孤立的 .done
    assert not any(n.endswith(".done") and n.startswith("20260501") for n in left), left


def test_log_rotation_is_copytruncate():
    """必须原地清空而不是改名：写日志的进程仍持有同一个 fd。"""
    root, cfg = _fixture()
    log = root / "data" / "coin-selection" / "logs" / "loop.log"
    log.write_text("x" * 5000, encoding="utf-8")
    inode_before = log.stat().st_ino
    with log.open("a", encoding="utf-8") as holder:  # 模拟仍在写的进程
        r = _run(cfg, "--apply")
        assert r.returncode == 0, r.stderr
        assert log.exists(), "活动日志不能消失"
        assert log.stat().st_ino == inode_before, "inode 变了 = 用了 rename，进程会写进孤儿文件"
        assert log.stat().st_size == 0, "原文件应被就地清空"
        assert (root / "data" / "coin-selection" / "logs" / "loop.log.1.gz").exists()
        holder.write("still writing\n")
    assert "still writing" in log.read_text(encoding="utf-8")


def test_cache_rule_reaches_nested_dirs():
    root, cfg = _fixture()
    cache = root / "data" / "coin-selection" / "kline_tf_cache" / "30m"
    _run(cfg, "--apply")
    left = sorted(p.name for p in cache.iterdir())
    assert "OLDUSDT.json" not in left, left
    assert "NEWUSDT.json" in left, left


def test_ceiling_clamps_an_over_long_rule():
    """max_retention_days 必须是结构性上限，不是注释里的君子协定。

    把某条规则改成 90 天，执行器要把它夹回 31 并告警——否则「不超过 1 个月」
    只是一句话，随手一改就破了。
    """
    root, cfg = _fixture(watermark="20260824-047")
    snaps = root / "data" / "coin-selection" / "snapshots"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["max_retention_days"] = 31
    for rule in data["rules"]:
        rule["keep_days"] = 90          # 蓄意越界
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    r = _run(cfg, "--apply")
    assert r.returncode == 0, r.stderr
    assert "强制夹到 31 天" in r.stdout, r.stdout
    # 2026-05-01 距 2026-08-24 约 115 天：90 天不该删它，夹到 31 天就该删。
    left = sorted(p.name for p in snaps.iterdir())
    assert not any(n.startswith("20260501") for n in left), left


def test_live_policy_never_exceeds_one_month():
    """对**真实**的 packages/config/retention.json 断言：没有任何一条超过 31 天。"""
    live = ROOT / "packages" / "config" / "retention.json"
    cfg = json.loads(live.read_text(encoding="utf-8"))
    cap = cfg["max_retention_days"]
    assert cap == 31, cap
    over = [(r["name"], r["keep_days"]) for r in cfg["rules"] if r["keep_days"] > cap]
    assert not over, f"这些规则超过了 31 天上限：{over}"
    # 栏目最长窗口是「近 28 天」，事实源必须覆盖得住
    board = next(r for r in cfg["rules"] if r["name"] == "snapshots_board")
    assert board["keep_days"] >= 28, board["keep_days"]


def test_t20_research_products_are_not_under_retention():
    """``data/research/**`` 不进 ``retention.json`` 的 rules（测试 T20）。

    研究产物由研究者自行清理：31 天上限是给「复盘选币」的事实源用的，
    把实验目录也纳进去，只会在下一次 prune 时把正在分析的实验删掉。
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    doc = json.loads((root / "packages" / "config" / "retention.json").read_text(encoding="utf-8"))
    paths = [str(r.get("path") or "") for r in doc.get("rules") or []]
    assert not any("research" in p for p in paths), paths
    # 反过来也要成立：现网四个目录必须**在**规则里，别把它们漏掉
    for must in (
        "data/coin-selection/snapshots",
        "data/coin-selection-y/snapshots",
        "data/dmr-adapter/inbox",
        "data/dmr-adapter-y/inbox",
    ):
        assert must in paths, must


def test_t20_max_retention_days_is_still_31():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    doc = json.loads((root / "packages" / "config" / "retention.json").read_text(encoding="utf-8"))
    assert doc.get("max_retention_days") == 31, doc.get("max_retention_days")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    if TMP.exists():
        shutil.rmtree(TMP)
    print("all", len(tests))
