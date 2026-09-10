#!/usr/bin/env python3
"""数据保留策略执行器 —— 按 packages/config/retention.json 清理与轮转。

    python3 scripts/prune_data.py            # dry-run：只报告，什么都不删（默认）
    python3 scripts/prune_data.py --plan     # 只看容量规划与磁盘水位，不列文件
    python3 scripts/prune_data.py --apply    # 真的删
    python3 scripts/prune_data.py --apply --only snapshots_full,cache_kline1h

**默认永远是 dry-run。** 删数据必须是一次显式的、打了 `--apply` 的动作。

退出码：0 正常 · 1 策略/配置错误 · 2 磁盘低于 min_free_gb（供 cron 告警）

安全护栏（都会让该规则整条跳过，而不是删一半）：
  1. `guard_review_ledger` 的规则绝不删复盘账本尚未消费的节点（scan_id > 水位）。
  2. `min_keep_files` 是硬地板：无论 keep_days 配成多少，最近 N 个文件永远保留。
  3. `never_delete` 名单里的路径永不参与。
  4. 删除会缩短「可全量重建」窗口时，先告警说明重建只能回到哪一天。

日志轮转用 copytruncate（先拷贝再原地清空），写日志的进程无需重启。
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "packages" / "config" / "retention.json"
LEDGER = ROOT / "data" / "coin-selection" / "review" / "ledger.sqlite"

SCAN_ID_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{3,})")
GB = 1024**3
MB = 1024**2


def human(n: float) -> str:
    return f"{n / GB:.2f} GB" if n >= GB else f"{n / MB:.1f} MB"


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def ledger_watermark(ledger: Path | None = None) -> str | None:
    """复盘账本已消费到哪个 scan_id。取不到就返回 None（护栏会因此更保守）。

    每块板面一本账本：「选币榜」是 data/coin-selection/review/ledger.sqlite，
    「选币榜Y」是 data/coin-selection-y/review/ledger.sqlite。规则可以用
    ``ledger_path`` 指定自己那本 —— 拿错账本的水位会删掉另一块板面还没消费的节点。
    """
    ledger = ledger or LEDGER
    if not ledger.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
        row = conn.execute("SELECT scan_id FROM watermarks WHERE k='scan'").fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def scan_id_date(name: str) -> datetime | None:
    m = SCAN_ID_RE.match(name)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def scan_id_of(path: Path) -> str | None:
    m = SCAN_ID_RE.match(path.name)
    return m.group(0) if m else None


DEFAULT_MAX_RETENTION_DAYS = 31


def effective_keep_days(rule: dict, cap: Optional[int]) -> tuple[int, Optional[str]]:
    """规则的实际保留天数，受策略上限强制约束。

    上限做成**结构性**的而不是只写在注释里：把某条规则的 keep_days 改成 90，
    这里会把它夹回 31 并告警。策略说「不超过 1 个月」，那就真的越不过去。
    """
    want = int(rule["keep_days"])
    if cap is None or want <= cap:
        return want, None
    return cap, f"keep_days={want} 超过策略上限 max_retention_days={cap}，已强制夹到 {cap} 天"


def collect(
    rule: dict, now: datetime, never: set[Path], cap: Optional[int] = None
) -> tuple[list[Path], list[Path], list[str]]:
    """→ (要删的, 保留的, 备注)。备注解释为什么某些文件被豁免。"""
    notes: list[str] = []
    base = ROOT / rule["path"]
    if not base.is_dir():
        return [], [], [f"目录不存在：{rule['path']}"]

    excl = rule.get("exclude_glob")
    # 有些缓存目录按周期分子目录（kline_tf_cache/{30m,2h,6h}），非递归 glob 一个都匹配不到。
    it = base.rglob(rule.get("glob", "*")) if rule.get("recursive") else base.glob(rule.get("glob", "*"))
    files = [
        p
        for p in it
        if p.is_file()
        and p.resolve() not in never
        and not (excl and p.match(excl))
    ]

    keep_days, cap_note = effective_keep_days(rule, cap)
    if cap_note:
        notes.append(cap_note)
    cutoff = now - timedelta(days=keep_days)
    age_from = rule.get("age_from", "mtime")

    def keys(p: Path) -> tuple[datetime, str]:
        """(用于比较保留期的时间, 用于排序的次键)。

        `scan_id` 只精确到天，同一天的 96 个节点时间键完全相同。排序必须再带上
        scan_id 本身（零填充 → 字典序即时间序），否则 `min_keep_files` 的
        「保留最近 N 个」会在同一天内随机挑，把最新的节点删掉。
        """
        if age_from == "scan_id":
            d = scan_id_date(p.name)
            if d is not None:
                return d, (scan_id_of(p) or p.name)
        st = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        return st, st.isoformat()

    dated = sorted(((*keys(p), p) for p in files), key=lambda x: (x[0], x[1]))
    stale = [p for d, _, p in dated if d < cutoff]
    fresh = [p for d, _, p in dated if d >= cutoff]

    # 硬地板：最近 N 个文件永不删，哪怕 keep_days 被配成 0。
    floor = int(rule.get("min_keep_files") or 0)
    if floor and stale:
        survivors = len(files) - len(stale)
        if survivors < floor:
            rescue = min(floor - survivors, len(stale))
            if rescue:
                notes.append(f"min_keep_files={floor} 保住了最近 {rescue} 个本已过期的文件")
                fresh = stale[-rescue:] + fresh   # stale 已按时间升序
                stale = stale[:-rescue]

    return stale, fresh, notes


def rule_ledger(rule: dict) -> Path:
    """这条规则该看哪一本复盘账本（默认主板面的）。"""
    rel = rule.get("ledger_path")
    return (ROOT / rel) if rel else LEDGER


def guard_ledger(rule: dict, victims: list[Path], watermarks: dict[str, str | None]) -> tuple[list[Path], list[str]]:
    """绝不删账本还没消费的节点。"""
    if not rule.get("guard_review_ledger"):
        return victims, []
    key = str(rule_ledger(rule))
    if key not in watermarks:
        watermarks[key] = ledger_watermark(Path(key))
    watermark = watermarks[key]
    if watermark is None:
        return [], ["复盘账本水位不可读 → 整条规则跳过（宁可不删）"]
    ahead = [p for p in victims if (scan_id_of(p) or "") > watermark]
    if ahead:
        kept = sorted({scan_id_of(p) for p in ahead})
        return (
            [p for p in victims if p not in set(ahead)],
            [f"跳过 {len(ahead)} 个账本尚未消费的节点（水位 {watermark}，最早未消费 {kept[0]}）"],
        )
    return victims, []


def rebuild_window_note(rule: dict, victims: list[Path], fresh: list[Path]) -> list[str]:
    """删完之后还能回放到哪一天——这是快照被删掉真正会失去的东西。"""
    # 「选币榜」与「选币榜Y」的板面规则都适用（snapshots_board / snapshots_board_y）。
    if not rule["name"].startswith("snapshots_board") or not victims:
        return []
    remaining = sorted(scan_id_of(p) or "" for p in fresh)
    if not remaining:
        return ["⚠ 板面将被清空，复盘账本从此无法全量重建"]
    return [
        f"⚠ 删除后 build_review_ledger.py --reset 只能回放到 {remaining[0]}；"
        f"更早的交易仍留在账本里，但无法再从原始快照重算"
    ]


def rotate_logs(cfg: dict, apply: bool) -> list[str]:
    """copytruncate：先拷再原地清空，写日志的进程不必重启。"""
    out: list[str] = []
    logs = cfg.get("logs") or {}
    limit = float(logs.get("rotate_at_mb", 64)) * MB
    keep = int(logs.get("keep_rotations", 5))
    compress = bool(logs.get("compress", True))
    for pattern in logs.get("paths", []):
        for path in sorted(ROOT.glob(pattern)):
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size < limit:
                out.append(f"  keep   {path.relative_to(ROOT)}  {human(size)} < {human(limit)}")
                continue
            out.append(f"  ROTATE {path.relative_to(ROOT)}  {human(size)} ≥ {human(limit)}")
            if not apply:
                continue
            ext = ".gz" if compress else ""
            oldest = Path(f"{path}.{keep}{ext}")
            if oldest.exists():
                oldest.unlink()
            for i in range(keep - 1, 0, -1):
                src = Path(f"{path}.{i}{ext}")
                if src.exists():
                    src.rename(Path(f"{path}.{i + 1}{ext}"))
            dst = Path(f"{path}.1{ext}")
            if compress:
                with path.open("rb") as fin, gzip.open(dst, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
            else:
                shutil.copyfile(path, dst)
            # 原地清空：绝不 rename 活动日志，否则进程会继续往被改名的 inode 里写。
            with path.open("r+b") as f:
                f.truncate(0)
            out.append(f"         → {dst.relative_to(ROOT)} ({human(dst.stat().st_size)})，原文件已清空")
    return out


def disk_report(cfg: dict) -> tuple[int, list[str]]:
    d = cfg.get("disk") or {}
    mount = d.get("mount", "/")
    try:
        st = os.statvfs(mount)
    except OSError as e:
        return 0, [f"磁盘信息不可读 {mount}: {e}"]
    free = st.f_bavail * st.f_frsize
    total = st.f_blocks * st.f_frsize
    warn = float(d.get("warn_free_gb", 0)) * GB
    hard = float(d.get("min_free_gb", 0)) * GB
    obs = cfg.get("observed") or {}
    per_day = float(obs.get("total_mb_per_day") or 0) * MB
    lines = [
        f"挂载点 {mount}：可用 {human(free)} / 总 {human(total)}"
        f"（warn<{d.get('warn_free_gb')}GB · min<{d.get('min_free_gb')}GB）"
    ]
    if per_day:
        lines.append(
            f"当前写入速率 {per_day / MB:.1f} MB/日 → 不清理可再撑 {free / per_day:.0f} 天；"
            f"按本策略稳态占用约 {obs.get('projected_steady_state_gb')} GB"
        )
    code = 0
    if free < hard:
        lines.append(f"❌ 可用空间低于 min_free_gb={d.get('min_free_gb')}GB —— 需要立即处理")
        code = 2
    elif free < warn:
        lines.append(f"⚠ 可用空间低于 warn_free_gb={d.get('warn_free_gb')}GB")
    return code, lines


def main() -> int:
    ap = argparse.ArgumentParser(description="Enforce packages/config/retention.json")
    ap.add_argument("--apply", action="store_true", help="真的删除/轮转（默认只报告）")
    ap.add_argument("--plan", action="store_true", help="只看磁盘与容量规划")
    ap.add_argument("--only", default=None, help="逗号分隔的规则名，只跑这几条")
    ap.add_argument("--config", default=str(CONFIG))
    args = ap.parse_args()

    cfg_path = Path(args.config)
    try:
        cfg = load_config(cfg_path)
    except Exception as e:
        print(f"[FAIL] 读不了保留策略 {cfg_path}: {e}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    try:
        shown = cfg_path.relative_to(ROOT)
    except ValueError:
        shown = cfg_path
    print(f"保留策略 {cfg.get('version')}  ({shown})")
    print(f"模式：{'APPLY（会真的删）' if args.apply else 'DRY-RUN（只报告）'}\n")

    disk_code, disk_lines = disk_report(cfg)
    for line in disk_lines:
        print(f"  {line}")
    if args.plan:
        cap = cfg.get("max_retention_days", DEFAULT_MAX_RETENTION_DAYS)
        print(f"\n策略上限 max_retention_days = {cap} 天（超过的 keep_days 一律强制夹到上限）")
        print("\n规则一览：")
        for r in cfg.get("rules", []):
            eff, note = effective_keep_days(r, cap)
            mark = "  ← 已夹紧" if note else ""
            print(f"  {r['name']:22} {r['path']:38} keep {eff:>3}d  floor {r.get('min_keep_files', 0)}{mark}")
        lg = cfg.get("logs") or {}
        print(f"  {'logs':22} {', '.join(lg.get('paths', [])):38} rotate ≥{lg.get('rotate_at_mb')}MB × {lg.get('keep_rotations')}")
        return disk_code

    never = set()
    for rel in cfg.get("never_delete", []):
        p = ROOT / rel
        try:
            never.add(p.resolve())
        except OSError:
            pass

    watermarks: dict[str, str | None] = {str(LEDGER): ledger_watermark()}
    print(f"\n复盘账本水位：{watermarks[str(LEDGER)] or '(不可读)'}")
    for rule in cfg.get("rules", []):
        lp = rule_ledger(rule)
        if str(lp) not in watermarks:
            watermarks[str(lp)] = ledger_watermark(lp)
            print(
                f"  次级板面账本 {lp.relative_to(ROOT)} 水位："
                f"{watermarks[str(lp)] or '(不可读)'}"
            )

    cap = cfg.get("max_retention_days", DEFAULT_MAX_RETENTION_DAYS)
    only = {s.strip() for s in args.only.split(",")} if args.only else None
    freed = 0
    deleted = 0
    print("\n=== 清理规则 ===")
    for rule in cfg.get("rules", []):
        if only and rule["name"] not in only:
            continue
        victims, fresh, notes = collect(rule, now, never, cap)
        victims, gnotes = guard_ledger(rule, victims, watermarks)
        notes += gnotes + rebuild_window_note(rule, victims, fresh)
        size = sum(p.stat().st_size for p in victims if p.exists())
        verb = "DELETE" if args.apply else "would delete"
        status = "—" if not victims else f"{verb} {len(victims)} 个 · {human(size)}"
        eff, _ = effective_keep_days(rule, cap)
        print(f"  {rule['name']:22} keep {eff:>3}d  保留 {len(fresh):5} 个 · {status}")
        for n in notes:
            print(f"      {n}")
        if victims and args.apply:
            for p in victims:
                try:
                    p.unlink()
                except OSError as e:
                    print(f"      删除失败 {p.name}: {e}")
            deleted += len(victims)
        freed += size

    print("\n=== 日志轮转 ===")
    for line in rotate_logs(cfg, args.apply):
        print(line)

    print()
    if args.apply:
        print(f"已删除 {deleted} 个文件，释放 {human(freed)}")
    else:
        print(f"DRY-RUN：本次将释放 {human(freed)}（加 --apply 才会真的执行）")
    return disk_code


if __name__ == "__main__":
    raise SystemExit(main())
