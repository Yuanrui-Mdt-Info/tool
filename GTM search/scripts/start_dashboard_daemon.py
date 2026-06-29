#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PID_PATH = PROJECT_ROOT / "logs" / "dashboard_server_8787.pid"
OUT_PATH = PROJECT_ROOT / "logs" / "dashboard_server_8787.log"
ERR_PATH = PROJECT_ROOT / "logs" / "dashboard_server_8787.err.log"


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main() -> int:
    PROJECT_ROOT.joinpath("logs").mkdir(parents=True, exist_ok=True)
    if PID_PATH.exists():
        try:
            existing_pid = int(PID_PATH.read_text(encoding="utf-8").strip())
        except ValueError:
            existing_pid = 0
        if existing_pid and pid_is_alive(existing_pid):
            print(existing_pid)
            return 0

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    out = OUT_PATH.open("ab", buffering=0)
    err = ERR_PATH.open("ab", buffering=0)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "dashboard_server.py"),
            "--share-lan",
            "--port",
            "8787",
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=out,
        stderr=err,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    PID_PATH.write_text(str(proc.pid) + "\n", encoding="utf-8")
    time.sleep(0.5)
    if proc.poll() is not None:
        raise SystemExit(proc.returncode or 1)
    print(proc.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
