#!/usr/bin/env python3
"""复盘账本迁移 v2 —— 文档B §10.2 的 5 个新列（幂等，对 main 与 y 分别执行）。

    python3 scripts/migrate_ledger_v2.py --plan            # 只看要做什么，不写
    python3 scripts/migrate_ledger_v2.py --board main --board y
    python3 scripts/migrate_ledger_v2.py --all             # 两本一起

新增列（全部 nullable、无 DEFAULT）::

    param_hash    TEXT   参数指纹（文档B §3.1）
    combo_code    TEXT   'AAB' 等三字母；判不出级为 NULL
    combo_zone    TEXT   该组合在该方向的名义天花板（216 查表值）
    zone_ceiling  TEXT   实际生效的天花板（含弃权 / P1–P3 / SS 背离调整后）
    z_score       REAL   方向分 Z_direction

以及两条索引 ``idx_trades_param_hash`` / ``idx_trades_combo``。

**不回填历史行**：``combo_code`` 理论上能从快照重算，但 ``param_hash`` 在阶段 0
之前的快照里根本不存在，回填出来的是假的。旧行一律保持 NULL，UI 上显示为「—」。

幂等：先查 ``PRAGMA table_info(trades)``，已存在的列跳过；重复执行是 no-op。
默认在改动前把 sqlite 复制一份到 ``<name>.pre-v2.bak``（``--no-backup`` 可关）。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.review_ledger import ReviewLedger, default_ledger_path  # noqa: E402


def ledger_paths(boards: list[str]) -> list[tuple[str, Path]]:
    from coin_selection.board_variants import load_variants  # noqa: E402

    out: list[tuple[str, Path]] = []
    by_key = {v.key: v for v in load_variants()}
    for b in boards:
        v = by_key.get(b)
        if v is None:
            print(f"[SKIP] unknown board: {b}")
            continue
        out.append((b, default_ledger_path(v.data_path(ROOT))))
    return out


def existing_columns(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    finally:
        conn.close()


def migrate_one(board: str, path: Path, *, plan: bool, backup: bool) -> int:
    if not path.is_file():
        print(f"[SKIP] {board}: ledger missing ({path})")
        return 0
    have = existing_columns(path)
    want = [c for c, _ in ReviewLedger.MIGRATED_COLUMNS if c not in have]
    n_rows = 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        n_rows = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    finally:
        conn.close()
    if not want:
        print(f"[OK]   {board}: already migrated (rows={n_rows}, columns={len(have)})")
        return 0
    print(f"[PLAN] {board}: rows={n_rows} add columns -> {', '.join(want)}")
    if plan:
        return 0
    if backup:
        bak = path.with_suffix(path.suffix + ".pre-v2.bak")
        shutil.copy2(str(path), str(bak))
        print(f"       backup -> {bak.name}")
    # ReviewLedger.__init__ 本身就跑迁移（幂等），复用它就不会出现第二份 DDL。
    led = ReviewLedger(path)
    try:
        led._conn.commit()
        now = {r[1] for r in led._conn.execute("PRAGMA table_info(trades)")}
        idx = {
            r[0]
            for r in led._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        led.close()
    missing = [c for c, _ in ReviewLedger.MIGRATED_COLUMNS if c not in now]
    if missing:
        print(f"[FAIL] {board}: columns still missing after migrate: {missing}")
        return 1
    for want_idx in ("idx_trades_param_hash", "idx_trades_combo"):
        if want_idx not in idx:
            print(f"[FAIL] {board}: index missing after migrate: {want_idx}")
            return 1
    # 旧行必须保持 NULL —— 迁移不许回填
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        n_ph = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE param_hash IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"[OK]   {board}: migrated (rows={n_rows}, param_hash non-null={n_ph})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Review ledger v2 migration (idempotent)")
    ap.add_argument("--board", action="append", default=[], help="main | y (repeatable)")
    ap.add_argument("--all", action="store_true", help="both main and y")
    ap.add_argument("--plan", action="store_true", help="dry run")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    boards = list(args.board)
    if args.all or not boards:
        boards = ["main", "y"]

    rc = 0
    for board, path in ledger_paths(boards):
        rc |= migrate_one(board, path, plan=args.plan, backup=not args.no_backup)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
