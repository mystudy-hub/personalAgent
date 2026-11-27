"""Verify cancellation, timeouts and supervisor death for a real M0 Codex Worker."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from verify_m0 import ModelProvider, require, worker_command


COMMAND = """
from pathlib import Path
import time
with Path('heartbeat').open('ab', buffering=0) as stream:
    while True:
        stream.write(b'.')
        time.sleep(0.05)
"""

SUPERVISOR = """
import subprocess
import sys
with subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, text=True) as attempt:
    print(attempt.pid, flush=True)
    attempt.communicate(
        'Run exactly this synthetic command with your command tool now: '
        '/usr/bin/python3 -B ./heartbeat.py. '
        'The attempt supervisor will stop it. Do not run any other command.\\n'
    )
"""


def process_start(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    return fields[19] if fields[0] != "Z" else None


def processes(pid):
    started = process_start(pid)
    if started is None:
        return {}
    found = {pid: started}
    for file in Path(f"/proc/{pid}/task").glob("*/children"):
        try:
            children = file.read_text().split()
        except FileNotFoundError:
            continue
        for child in children:
            found.update(processes(int(child)))
    return found


def await_condition(condition, timeout, error):
    deadline = time.monotonic() + timeout
    while not condition():
        require(time.monotonic() < deadline, error)
        time.sleep(0.05)


def main():
    model = ModelProvider()
    with TemporaryDirectory(prefix="pa-m0-cleanup-", dir="/var/tmp") as directory:
        root = Path(directory)
        env = model.worker(root)
        for scenario in ("cancel", "timeout", "supervisor-killed"):
            work = root / scenario
            work.mkdir()
            (work / "heartbeat.py").write_text(COMMAND, encoding="utf-8")
            heartbeat = work / "heartbeat"
            with subprocess.Popen(
                [sys.executable, "-B", "-c", SUPERVISOR, *worker_command(work)],
                cwd=work, env=env, stdout=subprocess.PIPE, text=True,
            ) as supervisor:
                try:
                    launcher = int(supervisor.stdout.readline())
                    await_condition(lambda: heartbeat.exists() and heartbeat.stat().st_size > 0,
                                    35, f"{scenario}: real Worker did not start the heartbeat")
                    tracked = processes(launcher)
                    require(len(tracked) >= 3, f"{scenario}: missing Worker descendants")
                    if scenario == "supervisor-killed":
                        supervisor.kill()
                    elif scenario == "timeout":
                        try:
                            supervisor.wait(timeout=0.25)
                        except subprocess.TimeoutExpired:
                            os.kill(launcher, signal.SIGKILL)
                        else:
                            raise RuntimeError("Worker exited before the timeout")
                    else:
                        os.kill(launcher, signal.SIGKILL)
                    supervisor.wait(timeout=10)
                    await_condition(
                        lambda: all(process_start(pid) != identity for pid, identity in tracked.items()),
                        10, f"{scenario}: Worker descendants remain active",
                    )
                    size = heartbeat.stat().st_size
                    time.sleep(0.2)
                    require(heartbeat.stat().st_size == size, f"{scenario}: heartbeat continued")
                    print(f"PASS {scenario}: {len(tracked)} Worker processes stopped", flush=True)
                finally:
                    if supervisor.poll() is None:
                        supervisor.kill()
                        supervisor.wait(timeout=10)


if __name__ == "__main__":
    main()
