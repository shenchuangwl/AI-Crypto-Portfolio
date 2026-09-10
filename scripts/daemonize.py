#!/usr/bin/env python3
"""Detach long-running services so they survive the agent shell session."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def daemonize(cmd: list[str], *, cwd: Path, env: dict, pid_file: Path, log_file: Path) -> int:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    if pid_file.is_file():
        try:
            old = int(pid_file.read_text().strip())
            os.kill(old, 0)
            print(f"already running pid={old}")
            return 0
        except (ValueError, OSError, ProcessLookupError):
            pid_file.unlink(missing_ok=True)

    log_f = open(log_file, "a", encoding="utf-8")
    log_f.write(f"\n--- start {' '.join(cmd)} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
    log_f.flush()

    # Start fully detached
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.6)
    if proc.poll() is not None:
        print(f"process exited early code={proc.returncode}", file=sys.stderr)
        print(log_file.read_text(encoding="utf-8")[-2000:], file=sys.stderr)
        return 1
    print(f"started pid={proc.pid} log={log_file}")
    return 0


def stop(pid_file: Path) -> int:
    if not pid_file.is_file():
        print("not running")
        return 0
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.4)
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        print(f"stopped pid={pid}")
    except Exception as e:
        print(f"stop error: {e}")
    pid_file.unlink(missing_ok=True)
    return 0


def main() -> int:
    root = Path(os.environ.get("HERMES_ROOT", Path(__file__).resolve().parents[1]))
    p = argparse.ArgumentParser()
    p.add_argument("service", choices=["gateway", "selection-loop", "market-ingest", "web-dev"])
    p.add_argument("action", choices=["start", "stop", "status"], default="start", nargs="?")
    args = p.parse_args()

    data = root / "data" / "coin-selection"
    logs = data / "logs"
    env = os.environ.copy()
    env["HERMES_ROOT"] = str(root)
    env.pop("SM_FAST", None)
    # Inject gitignored .env (existing process env wins). Never print values.
    dotenv = root / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in env:
                env[k] = v
    py = root / ".venv" / "bin" / "python"
    python = str(py) if py.is_file() else sys.executable
    cwd = root

    if args.service == "gateway":
        pid_file = data / "gateway.pid"
        log_file = logs / "gateway.log"
        cmd = [
            python,
            str(root / "services" / "api-gateway" / "mock_server.py"),
            "--host",
            os.environ.get("HOST", "127.0.0.1"),
            "--port",
            os.environ.get("PORT", "18080"),
        ]
    elif args.service == "selection-loop":
        pid_file = data / "loop.pid"
        log_file = logs / "loop.log"
        env["PYTHONPATH"] = str(root / "services" / "coin-selection" / "src") + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        cmd = [python, "-m", "coin_selection", "--loop", "--force"]
    elif args.service == "web-dev":
        pid_file = data / "web_dev.pid"
        log_file = logs / "web_dev.log"
        cwd = root / "apps" / "web"
        cmd = [
            "npm",
            "run",
            "dev",
            "--",
            "--host",
            os.environ.get("HOST", "127.0.0.1"),
            "--port",
            os.environ.get("VITE_PORT", "5173"),
            "--strictPort",
        ]
    else:
        pid_file = data / "market_ingest.pid"
        log_file = logs / "market_ingest.log"
        env["PYTHONPATH"] = str(root / "services" / "market-ingest" / "src") + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        cmd = [python, "-m", "market_ingest"]

    if args.action == "stop":
        return stop(pid_file)
    if args.action == "status":
        if pid_file.is_file():
            pid = pid_file.read_text().strip()
            try:
                os.kill(int(pid), 0)
                print(f"running pid={pid}")
                return 0
            except Exception:
                print("stale pid file")
                return 1
        print("not running")
        return 1
    return daemonize(cmd, cwd=cwd, env=env, pid_file=pid_file, log_file=log_file)


if __name__ == "__main__":
    raise SystemExit(main())
