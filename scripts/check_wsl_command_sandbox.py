"""Check Codex's Linux command sandbox using only temporary WSL fixtures."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


PROBE = """
import json
import socket
import subprocess
import sys
from pathlib import Path

work, outside, marker, port, remote = sys.argv[1:]
work, outside, marker = Path(work), Path(outside), Path(marker)

def readable(path):
    try:
        path.read_bytes()
    except OSError:
        return False
    return True

def writable(path):
    try:
        path.write_text("sandbox-write")
    except OSError:
        return False
    return True

try:
    with socket.create_connection(("127.0.0.1", int(port)), timeout=2):
        reachable = True
except OSError:
    reachable = False

subprocess.run(
    ["git", "-C", str(work), "rev-parse", "--verify", "HEAD"],
    check=True, capture_output=True, timeout=5,
)
push = subprocess.run(
    ["git", "-C", str(work), "push", remote, "HEAD:refs/heads/sandbox-probe"],
    capture_output=True, timeout=5,
)
print(json.dumps({
    "allowed_read": readable(work / "input.txt"),
    "allowed_write": writable(work / "output.txt"),
    "outside_read": readable(outside),
    "outside_write_reported": writable(outside.with_name("unexpected-write")),
    "fake_credential_read": readable(marker),
    "host_loopback": reachable,
    "remote_push": push.returncode == 0,
}))
"""


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True, capture_output=True,
        text=True, timeout=5,
    ).stdout.strip()


@contextmanager
def synthetic_remote(root: Path):
    git(root, "init", "--bare", "--quiet", "--template=", "remote.git")
    with socket.socket() as port_picker:
        port_picker.bind(("127.0.0.1", 0))
        port = port_picker.getsockname()[1]
    daemon = subprocess.Popen(
        ["git", "daemon", "--listen=127.0.0.1", f"--port={port}", "--reuseaddr",
         "--export-all", "--enable=receive-pack", f"--base-path={root}", str(root)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            if daemon.poll() is not None:
                raise RuntimeError("Temporary Git daemon exited before accepting connections")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Temporary Git daemon did not start")
                time.sleep(0.05)
        yield port, f"git://127.0.0.1:{port}/remote.git"
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex", help="Codex executable on WSL")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="pa-command-sandbox-", dir="/var/tmp") as folder:
        root = Path(folder)
        work = root / "work"
        config = root / "codex"
        work.mkdir()
        config.mkdir()
        (work / "input.txt").write_text("authorized fixture", encoding="utf-8")
        outside = root / "outside.txt"
        outside.write_text("outside fixture", encoding="utf-8")
        marker = config / "synthetic-credential"
        marker.write_text("test data only", encoding="utf-8")
        git(work, "init", "--quiet", "--template=", "--initial-branch=main")
        git(work, "add", "input.txt")
        git(work, "-c", "user.name=PA fixture", "-c", "user.email=pa@example.invalid",
            "commit", "--no-gpg-sign", "--quiet", "-m", "Synthetic check")
        (config / "config.toml").write_text(
            '[permissions.pa_check.filesystem]\n":root" = "deny"\n'
            '":minimal" = "read"\n'
            '[permissions.pa_check.filesystem.":workspace_roots"]\n"." = "write"\n'
            '[permissions.pa_check.network]\nenabled = false\n',
            encoding="utf-8",
        )

        with synthetic_remote(root) as (port, remote):
            git(work, "push", remote, "HEAD:refs/heads/control")
            remote_repo = root / "remote.git"
            refs_before = git(remote_repo, "for-each-ref", "--format=%(refname) %(objectname)")
            if not refs_before.startswith("refs/heads/control "):
                raise RuntimeError("Positive control did not create the remote branch")
            command = [
                args.codex, "sandbox", "-P", "pa_check", "-C", str(work), "--",
                sys.executable, "-B", "-c", PROBE, str(work), str(outside), str(marker),
                str(port), remote,
            ]
            result = subprocess.run(
                command, cwd=work, env={
                    "PATH": os.environ["PATH"], "HOME": str(root), "CODEX_HOME": str(config),
                }, text=True, capture_output=True, timeout=20,
            )
            refs_after = git(remote_repo, "for-each-ref", "--format=%(refname) %(objectname)")
        if result.returncode:
            print(result.stderr, file=sys.stderr, end="")
            return result.returncode
        checks = json.loads(result.stdout)
        checks["outside_write_persisted"] = outside.with_name("unexpected-write").exists()
        checks["remote_refs_unchanged"] = refs_before == refs_after
        print(json.dumps(checks, sort_keys=True))
        required = {
            "allowed_read": True, "allowed_write": True, "outside_read": False,
            "outside_write_persisted": False, "fake_credential_read": False,
            "host_loopback": False, "remote_push": False, "remote_refs_unchanged": True,
        }
        return 0 if all(checks.get(key) is value for key, value in required.items()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
