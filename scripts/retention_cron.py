#!/usr/bin/env python3
"""数据保留策略的定时任务 —— 生成 / 检查 / 安装 cron 行。

    python3 scripts/retention_cron.py render                 # 打印本检出目录对应的正确 cron 行
    python3 scripts/retention_cron.py check                  # 只读：检查 crontab 与 retention.log
    python3 scripts/retention_cron.py install                # 只显示将要做的改动（默认不写）
    python3 scripts/retention_cron.py install --apply        # 真的写入 crontab，并读回核对

为什么需要它：保留任务曾被写成

    CRON_TZ=Asia/Shanghai 20 4 * * * …/scripts/run-retention.sh

这一整行在 cron 眼里是一条**环境变量赋值**（`名字=值`），不是任务，所以一次都
不会执行，也不会报错 —— 31 天保留策略因此停摆了近一个月，快照一直累积。
另外，Debian / Ubuntu 的 cron（3.0pl1）**不支持** `CRON_TZ`：即使单独写一行，
调度也仍按主机本地时区解释。唯一可靠的写法是直接用本机时区写时刻。

本脚本只处理指向**本检出目录** `scripts/run-retention.sh` 的行；crontab 里的其他
行（包括其他项目的任务）逐字节保持不变。任务外面套一层 `flock -n`，避免上一次
还没跑完时再起一次。

退出码：0 正常 · 1 发现问题或执行失败
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
RETENTION_LOG = Path("data/coin-selection/logs/retention.log")
LOCK_FILE = Path("data/coin-selection/logs/retention.lock")
#: 默认每天本机 03:20 执行。在 UTC+7 的主机上正好是北京时间 04:20。
DEFAULT_TIME = "03:20"
#: retention.log 最后一次运行超过这么多小时就判为「没在按日执行」（24h + 2h 余量）。
DEFAULT_MAX_AGE_HOURS = 26.0

_ENV_ASSIGN = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=")
_SCHEDULE = re.compile(r"^\s*(@\w+|(\S+\s+){4}\S+)\s+(?P<cmd>\S.*)$")
_LOG_EXIT = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\] exit=(?P<rc>-?\d+)\s*$")


def job_script(root: Path) -> str:
    return str(root / "scripts" / "run-retention.sh")


def render_line(root: Path, at: str = DEFAULT_TIME, flock: Optional[str] = None) -> str:
    """本检出目录对应的正确 cron 行（主机本地时区）。"""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", at)
    if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
        raise ValueError(f"--time 必须是 HH:MM（主机本地时区），收到 {at!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    # cron 用 /bin/sh 执行命令：路径里有空格等字符时必须转义（普通路径 quote 后原样不变）。
    cmd = shlex.quote(job_script(root))
    if flock:
        cmd = f"{shlex.quote(flock)} -n {shlex.quote(str(root / LOCK_FILE))} {cmd}"
    return f"{minute} {hour} * * * {cmd} >/dev/null 2>&1"


def _mentions_job(line: str, root: Path) -> bool:
    # 必须是「本目录的脚本」且后面紧跟（可选的引号和）空白或行尾，
    # 避免把 run-retention.sh.bak 之类当成它；也认得被 shlex.quote 包起来的路径。
    return re.search(re.escape(job_script(root)) + r"['\"]?(\s|$)", line) is not None


@dataclass
class Finding:
    lineno: int
    kind: str  # VALID / BROKEN_INLINE_ENV / UNPARSEABLE
    text: str
    notes: list[str] = field(default_factory=list)


def classify(crontab: str, root: Path) -> list[Finding]:
    """找出所有指向本目录 run-retention.sh 的行并判定是否是 cron 真正会执行的任务。"""
    out: list[Finding] = []
    cron_tz_seen = False
    for i, raw in enumerate(crontab.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^CRON_TZ\s*=", line) and not _SCHEDULE.match(line.split("=", 1)[1]):
            cron_tz_seen = True
        if not _mentions_job(line, root):
            continue
        if _ENV_ASSIGN.match(line):
            out.append(Finding(i, "BROKEN_INLINE_ENV", raw, [
                "整行以 `名字=` 开头，cron 把它当成环境变量赋值，任务永远不会执行",
            ]))
            continue
        if not _SCHEDULE.match(line):
            out.append(Finding(i, "UNPARSEABLE", raw, ["不是「5 个时间字段 + 命令」的格式"]))
            continue
        notes = []
        if "flock" not in line:
            notes.append("没有 flock -n，上一次未结束时可能重入")
        if cron_tz_seen:
            notes.append("前面有 CRON_TZ= 行；Debian/Ubuntu cron 不支持它，时刻按主机本地时区解释")
        out.append(Finding(i, "VALID", raw, notes))
    return out


def merged(crontab: str, root: Path, desired: str) -> str:
    """把本目录的保留任务替换为 `desired`（无则追加），其余行逐字节保留。"""
    lines = crontab.splitlines()
    kept: list[str] = []
    placed = False
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and _mentions_job(stripped, root):
            if not placed:
                kept.append(desired)
                placed = True
            continue  # 重复的旧行一并收掉
        kept.append(raw)
    if not placed:
        hhmm = desired.split()[1].zfill(2) + ":" + desired.split()[0].zfill(2)
        kept.append(f"# 数据保留策略（packages/config/retention.json）：每天主机本地 {hhmm} 执行。")
        kept.append(desired)
    return "\n".join(kept) + "\n"  # cron 要求最后一行以换行结尾


def last_run(log_text: str) -> Optional[tuple[datetime, int]]:
    """retention.log 里最后一条 `[时间] exit=N`。"""
    for line in reversed(log_text.splitlines()):
        m = _LOG_EXIT.match(line.strip())
        if m:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return ts, int(m.group("rc"))
    return None


# ---------------------------------------------------------------- I/O ----

def _crontab_cmd() -> str:
    return os.environ.get("RETENTION_CRONTAB", "crontab")


def read_crontab() -> str:
    r = subprocess.run([_crontab_cmd(), "-l"], capture_output=True, text=True)
    if r.returncode != 0:
        if "no crontab" in (r.stderr or "").lower():
            return ""
        raise RuntimeError(f"crontab -l 失败：{(r.stderr or '').strip()}")
    return r.stdout


def write_crontab(text: str) -> None:
    r = subprocess.run([_crontab_cmd(), "-"], input=text, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"crontab - 写入失败：{(r.stderr or '').strip()}")


# ---------------------------------------------------------------- CLI ----

def cmd_check(root: Path, max_age_hours: float, skip_log: bool, now: Optional[datetime] = None) -> int:
    problems = 0
    findings = classify(read_crontab(), root)
    valid = [f for f in findings if f.kind == "VALID"]
    print(f"检出目录：{root}")
    if not findings:
        print("  ✗ crontab 里没有指向本目录 scripts/run-retention.sh 的任务")
        problems += 1
    for f in findings:
        mark = "✓" if f.kind == "VALID" else "✗"
        print(f"  {mark} 第 {f.lineno} 行 {f.kind}: {f.text.strip()}")
        for n in f.notes:
            print(f"      · {n}")
        if f.kind != "VALID":
            problems += 1
    if len(valid) > 1:
        print(f"  ✗ 有 {len(valid)} 条有效任务，会重复执行")
        problems += 1

    if not skip_log:
        log = root / RETENTION_LOG
        run = last_run(log.read_text(encoding="utf-8", errors="replace")) if log.is_file() else None
        clock = now or datetime.now(timezone.utc)
        if run is None:
            print(f"  ✗ {RETENTION_LOG} 里没有任何运行记录")
            problems += 1
        else:
            ts, rc = run
            age_h = (clock - ts).total_seconds() / 3600
            fresh = age_h <= max_age_hours
            print(f"  {'✓' if fresh and rc == 0 else '✗'} 最近一次运行 {ts:%Y-%m-%dT%H:%M:%SZ}"
                  f"（{age_h:.1f} 小时前）exit={rc}")
            if not fresh:
                print(f"      · 超过 {max_age_hours:g} 小时没有新记录：定时任务可能没在执行")
                problems += 1
            if rc != 0:
                print("      · 上次退出码非 0（2 = 磁盘可用空间低于 min_free_gb）")
                problems += 1
    print("结果：" + ("正常" if problems == 0 else f"发现 {problems} 个问题"))
    return 0 if problems == 0 else 1


def cmd_install(root: Path, at: str, apply: bool) -> int:
    flock = shutil.which("flock")
    desired = render_line(root, at, flock)
    if not flock:
        print("  ! 未找到 flock，任务将不带重入锁")
    current = read_crontab()
    target = merged(current, root, desired)
    if target == (current if current.endswith("\n") or not current else current + "\n"):
        print("crontab 已是期望状态，无需改动：")
        print(f"  {desired}")
        return 0
    diff = difflib.unified_diff(current.splitlines(), target.splitlines(),
                                "crontab（当前）", "crontab（安装后）", lineterm="")
    print("\n".join(diff))
    if not apply:
        print("\n以上为预览；加 --apply 才会写入 crontab。")
        return 0
    write_crontab(target)
    if read_crontab() != target:
        print("✗ 写入后读回的 crontab 与预期不一致", file=sys.stderr)
        return 1
    print("\n✓ 已写入并读回核对一致")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="action", required=True)
    r = sub.add_parser("render", help="打印正确的 cron 行")
    r.add_argument("--time", default=DEFAULT_TIME, help="主机本地时区的 HH:MM（默认 %(default)s）")
    c = sub.add_parser("check", help="只读检查 crontab 与 retention.log")
    c.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    c.add_argument("--skip-log", action="store_true", help="刚安装、还没到第一次触发时用")
    i = sub.add_parser("install", help="安装 / 修正本目录的保留任务（默认只预览）")
    i.add_argument("--time", default=DEFAULT_TIME, help="主机本地时区的 HH:MM（默认 %(default)s）")
    i.add_argument("--apply", action="store_true", help="真的写入 crontab")
    a = p.parse_args(argv)
    root = a.root.resolve()
    try:
        if a.action == "render":
            print(render_line(root, a.time, shutil.which("flock")))
            return 0
        if a.action == "check":
            return cmd_check(root, a.max_age_hours, a.skip_log)
        return cmd_install(root, a.time, a.apply)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
