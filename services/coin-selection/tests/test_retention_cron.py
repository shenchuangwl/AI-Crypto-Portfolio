"""保留任务 cron 行的生成 / 检查 / 安装（scripts/retention_cron.py）。

回归对象是一次真实事故：保留任务被写成 `CRON_TZ=… 20 4 * * * …run-retention.sh`
单行，cron 把整行当成环境变量赋值，31 天保留策略一次都没执行过。
这里不碰真实 crontab：纯函数直接测，CLI 用一个假的 `crontab` 可执行文件测。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "retention_cron.py"

_spec = importlib.util.spec_from_file_location("retention_cron", SCRIPT)
rc = importlib.util.module_from_spec(_spec)
sys.modules["retention_cron"] = rc
_spec.loader.exec_module(rc)

FAKE = Path("/srv/app")  # 合成的检出目录，与真实主机路径无关
JOB = f"{FAKE}/scripts/run-retention.sh"
FLOCK = "/usr/bin/flock"
GOOD = f"20 3 * * * {FLOCK} -n {FAKE}/data/coin-selection/logs/retention.lock {JOB} >/dev/null 2>&1"

#: 事故现场的形态（其他项目的行换成了合成路径）：同样写法的第三方任务必须原样保留。
BEFORE = (
    "CRON_TZ=Asia/Shanghai 0 8 * * * /srv/other/run.sh >> /srv/other/a.log 2>&1\n"
    "CRON_TZ=Asia/Shanghai 10 8 * * * /srv/other2/run.sh >> /srv/other2/b.log 2>&1\n"
    "# 数据保留策略。每日 04:20 执行。\n"
    f"CRON_TZ=Asia/Shanghai 20 4 * * * {JOB} >/dev/null 2>&1\n"
)
AFTER = BEFORE.replace(f"CRON_TZ=Asia/Shanghai 20 4 * * * {JOB} >/dev/null 2>&1", GOOD)


# ---------------------------------------------------------------- render ----

def test_render_is_local_time_with_flock():
    assert rc.render_line(FAKE, "03:20", FLOCK) == GOOD
    assert rc.render_line(FAKE, "4:05", None) == f"5 4 * * * {JOB} >/dev/null 2>&1"


def test_render_rejects_bad_time():
    for bad in ("25:00", "03:60", "3:7", "0320", "abc", ""):
        try:
            rc.render_line(FAKE, bad, FLOCK)
        except ValueError:
            continue
        raise AssertionError(f"应拒绝 {bad!r}")


def test_render_quotes_paths_with_spaces():
    line = rc.render_line(Path("/srv/my app"), "03:20", FLOCK)
    assert "'/srv/my app/scripts/run-retention.sh'" in line
    assert "'/srv/my app/data/coin-selection/logs/retention.lock'" in line
    # 带引号的路径仍能被识别为本目录的任务，否则安装会重复追加。
    assert [f.kind for f in rc.classify(line + "\n", Path("/srv/my app"))] == ["VALID"]


# -------------------------------------------------------------- classify ----

def test_inline_cron_tz_is_detected_as_never_running():
    found = rc.classify(BEFORE, FAKE)
    assert [f.kind for f in found] == ["BROKEN_INLINE_ENV"], found
    assert found[0].lineno == 4


def test_valid_line_has_no_notes():
    found = rc.classify(AFTER, FAKE)
    assert [(f.kind, f.notes) for f in found] == [("VALID", [])], found


def test_valid_without_flock_is_flagged():
    found = rc.classify(f"20 3 * * * {JOB}\n", FAKE)
    assert found[0].kind == "VALID" and any("flock" in n for n in found[0].notes)


def test_standalone_cron_tz_is_warned_as_unsupported():
    found = rc.classify(f"CRON_TZ=Asia/Shanghai\n20 4 * * * {FLOCK} -n x {JOB}\n", FAKE)
    assert found[0].kind == "VALID" and any("CRON_TZ" in n for n in found[0].notes)


def test_other_checkouts_backups_and_comments_are_ignored():
    text = (
        f"# 20 3 * * * {JOB}\n"
        f"20 3 * * * /srv/elsewhere/scripts/run-retention.sh\n"
        f"20 3 * * * {JOB}.bak\n"
    )
    assert rc.classify(text, FAKE) == []


def test_non_schedule_line_is_unparseable():
    assert [f.kind for f in rc.classify(f"@reboot\n* * * {JOB}\n", FAKE)] == ["UNPARSEABLE"]


# ---------------------------------------------------------------- merged ----

def test_merge_fixes_the_incident_and_leaves_other_lines_byte_identical():
    assert rc.merged(BEFORE, FAKE, GOOD) == AFTER


def test_merge_is_idempotent_and_dedupes():
    once = rc.merged(BEFORE, FAKE, GOOD)
    assert rc.merged(once, FAKE, GOOD) == once
    doubled = AFTER + GOOD + "\n" + f"CRON_TZ=Asia/Shanghai 20 4 * * * {JOB}\n"
    assert rc.merged(doubled, FAKE, GOOD) == AFTER


def test_merge_into_empty_crontab_appends_comment_and_job():
    out = rc.merged("", FAKE, GOOD)
    lines = out.splitlines()
    assert out.endswith("\n") and lines[-1] == GOOD
    assert lines[0].startswith("#") and "03:20" in lines[0]


# --------------------------------------------------------------- last_run ----

def test_last_run_reads_the_final_exit_line():
    log = "[2026-09-20T20:20:01Z] retention --apply\n[2026-09-20T20:20:03Z] exit=0\n" \
          "[2026-09-21T20:20:01Z] retention --apply\n[2026-09-21T20:20:04Z] exit=2\n"
    ts, code = rc.last_run(log)
    assert ts == datetime(2026, 9, 21, 20, 20, 4, tzinfo=timezone.utc) and code == 2
    assert rc.last_run("nothing here\n") is None


# -------------------------------------------------------------------- CLI ----

def _sandbox() -> tuple[Path, dict]:
    """临时检出目录 + 假 crontab（存到文件里），绝不触碰真实 crontab。"""
    tmp = Path(tempfile.mkdtemp(prefix="retention_cron_"))
    (tmp / "scripts").mkdir()
    (tmp / "data/coin-selection/logs").mkdir(parents=True)
    store = tmp / "crontab.store"
    fake = tmp / "fake-crontab"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-l" ]; then\n'
        '  [ -f "$STORE" ] && exec cat "$STORE"\n'
        '  echo "no crontab for tester" >&2; exit 1\n'
        'elif [ "$1" = "-" ]; then cat > "$STORE"; fi\n',
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, RETENTION_CRONTAB=str(fake), STORE=str(store))
    return tmp, env


def _cli(tmp: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp), *args],
        env=env, capture_output=True, text=True,
    )


def test_cli_check_install_roundtrip_with_fake_crontab():
    tmp, env = _sandbox()
    try:
        job = f"{tmp}/scripts/run-retention.sh"
        store = Path(env["STORE"])
        store.write_text(f"MAILTO=\nCRON_TZ=Asia/Shanghai 20 4 * * * {job} >/dev/null 2>&1\n", encoding="utf-8")
        before = store.read_text(encoding="utf-8")

        r = _cli(tmp, env, "check", "--skip-log")
        assert r.returncode == 1 and "BROKEN_INLINE_ENV" in r.stdout, r.stdout

        r = _cli(tmp, env, "install")  # 预览不写
        assert r.returncode == 0 and "--apply" in r.stdout, r.stdout
        assert store.read_text(encoding="utf-8") == before

        r = _cli(tmp, env, "install", "--apply")
        assert r.returncode == 0 and "读回核对一致" in r.stdout, r.stdout + r.stderr
        after = store.read_text(encoding="utf-8")
        assert after.startswith("MAILTO=\n") and "CRON_TZ" not in after
        assert after.splitlines()[1].startswith("20 3 * * * ")

        assert _cli(tmp, env, "check", "--skip-log").returncode == 0
        r = _cli(tmp, env, "install", "--apply")
        assert r.returncode == 0 and "无需改动" in r.stdout
        assert store.read_text(encoding="utf-8") == after
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_check_flags_missing_stale_and_failed_runs():
    tmp, env = _sandbox()
    try:
        Path(env["STORE"]).write_text(rc.render_line(tmp, "03:20", FLOCK) + "\n", encoding="utf-8")
        log = tmp / "data/coin-selection/logs/retention.log"

        r = _cli(tmp, env, "check")
        assert r.returncode == 1 and "没有任何运行记录" in r.stdout, r.stdout

        stale = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.write_text(f"[{stale}] exit=0\n", encoding="utf-8")
        r = _cli(tmp, env, "check")
        assert r.returncode == 1 and "没有新记录" in r.stdout, r.stdout

        fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.write_text(f"[{fresh}] exit=2\n", encoding="utf-8")
        assert _cli(tmp, env, "check").returncode == 1

        log.write_text(f"[{fresh}] exit=0\n", encoding="utf-8")
        r = _cli(tmp, env, "check")
        assert r.returncode == 0 and "正常" in r.stdout, r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_without_any_crontab_reports_missing_job():
    tmp, env = _sandbox()
    try:
        r = _cli(tmp, env, "check", "--skip-log")
        assert r.returncode == 1 and "没有指向本目录" in r.stdout, r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
