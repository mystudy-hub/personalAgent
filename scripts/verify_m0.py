"""Exercise the Personal Agent M0 CSV workflow in temporary WSL repositories."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import tomllib
from contextlib import closing
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX = "/usr/local/bin/codex"
DELIVERY = "refs/heads/pa-m0-delivery"
IDENTITY = ["-c", "user.name=PA M0", "-c", "user.email=pa-m0@example.invalid",
            "-c", "core.hooksPath=.git/hooks"]


class RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def git(repo, *args, binary=False):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=not binary,
        check=True, timeout=15,
    ).stdout


def snapshot(repo):
    return (git(repo, "symbolic-ref", "HEAD"), git(repo, "rev-parse", "HEAD"),
            git(repo, "status", "--porcelain"), (repo / ".git/index").read_bytes())


def materialize(repo, revision, destination):
    destination.mkdir()
    contents = git(repo, "archive", revision, binary=True)
    with tarfile.open(fileobj=BytesIO(contents)) as archive:
        for member in archive.getmembers():
            require(member.isfile() and Path(member.name).name == member.name,
                    "Unexpected file in the fixed CSV fixture")
            with archive.extractfile(member) as stream:
                (destination / member.name).write_bytes(stream.read())


class ModelProvider:
    def __init__(self):
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        self.model = data["model"]
        self.config = data["model_providers"][data["model_provider"]]
        require(not self.config.get("requires_openai_auth"), "The M0 fixture expects API authentication")
        self.endpoint = self.config["base_url"].rstrip("/")
        self.token = (self.config.get("experimental_bearer_token")
                      or os.environ.get(self.config.get("env_key", "OPENAI_API_KEY")))
        self.headers = {
            key: os.environ[var] for key, var in self.config.get("env_http_headers", {}).items()
            if var in os.environ
        }
        self.headers.update(self.config.get("http_headers", {}))

    def ask(self, prompt):
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        headers.update(self.headers)
        payload = json.dumps({"model": self.model, "input": prompt, "stream": True, "store": False})
        request = Request(self.endpoint + "/responses", data=payload.encode(), headers=headers)
        chunks = []
        try:
            with build_opener(RefuseRedirect()).open(request, timeout=30) as response:
                for line in response:
                    if line.startswith(b"data: "):
                        event = json.loads(line[6:]) if line.strip() != b"data: [DONE]" else {}
                        if event.get("type") == "response.output_text.delta":
                            chunks.append(event["delta"])
                        elif event.get("type") == "response.completed":
                            return "".join(chunks).strip()
                        elif event.get("type") in ("response.failed", "error"):
                            raise RuntimeError("Model provider did not complete the request")
        except HTTPError as error:
            raise RuntimeError(f"Model provider returned HTTP {error.code}") from None
        raise RuntimeError("Model provider closed the stream before completion")

    def worker(self, root):
        config = root / "codex"
        config.mkdir()
        env = {key: os.environ[key] for key in (
            "PATH", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY",
            "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        ) if key in os.environ}
        env.update(HOME=str(root), CODEX_HOME=str(config))
        fields = [
            "model = " + json.dumps(self.model), 'model_provider = "pa_worker"',
            'web_search = "disabled"', 'default_permissions = "pa_check"',
            '[permissions.pa_check.filesystem]', '":root" = "deny"', '":minimal" = "read"',
            '"/proc" = "deny"', '[permissions.pa_check.filesystem.":workspace_roots"]',
            '"." = "write"', "[permissions.pa_check.network]", "enabled = false",
            "[shell_environment_policy]", 'inherit = "none"', "experimental_use_profile = false",
            "[shell_environment_policy.set]", 'PATH = "/usr/local/bin:/usr/bin:/bin"',
            "[skills.bundled]", "enabled = false", "[model_providers.pa_worker]",
        ]
        for key in ("name", "base_url", "wire_api"):
            if key in self.config:
                fields.append(f"{key} = {json.dumps(self.config[key])}")
        if self.token:
            env["PA_WORKER_API_KEY"] = self.token
            fields.append('env_key = "PA_WORKER_API_KEY"')
        if self.headers:
            fields.append("[model_providers.pa_worker.env_http_headers]")
            for index, (key, value) in enumerate(self.headers.items()):
                name = f"PA_WORKER_HEADER_{index}"
                env[name] = value
                fields.append(f"{json.dumps(key)} = {json.dumps(name)}")
        (config / "config.toml").write_text("\n".join(fields) + "\n", encoding="utf-8")
        return env


def worker_command(work):
    return [
        "/usr/bin/setpriv", "--pdeathsig", "KILL",
        "/usr/bin/unshare", "--pid", "--fork", "--kill-child=SIGKILL", "--mount-proc",
        CODEX, "-a", "never", "exec", "--ephemeral",
        "--skip-git-repo-check", "--ignore-rules", "--disable", "hooks", "--json", "-C",
        str(work), "-",
    ]


def run_worker(work, env):
    result = subprocess.run(
        worker_command(work), cwd=work, env=env, capture_output=True, text=True, timeout=100,
        input=("Fix the extra empty record produced by a physically empty CSV line. "
               "Preserve delimited empty fields and multiline quoted values. "
               "Edit only csv_import.py and do not change the tests or fixture. "
               "If you run tests, use python3 -B -m unittest -v.\n"),
    )
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    require(result.returncode == 0 and any(event.get("type") == "turn.completed" for event in events),
            f"Codex attempt did not complete (exit {result.returncode})")
    for event in events:
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "command_execution":
            output = item.get("aggregated_output", "").lower()
            hints = [text for text in ("permission denied", "operation not permitted",
                                       "read-only file system", "bwrap:", "seccomp") if text in output]
            if hints:
                print(f"Worker command exit={item.get('exit_code')}, hints={hints}", flush=True)


def test_revision(work):
    return subprocess.run([
        "/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--bind", str(work), "/work", "--chdir", "/work",
        "/usr/bin/python3", "-B", "-m", "unittest", "-v",
    ], capture_output=True, text=True, timeout=15)


def deliver(repo, ledger, base, candidate, digest, expires_at, decision):
    before = snapshot(repo)
    target = DELIVERY
    binding = (base, candidate, target, digest, expires_at, decision)
    with closing(sqlite3.connect(ledger)) as db, db:
        db.execute("CREATE TABLE decision (base TEXT, candidate TEXT, ref TEXT, digest TEXT, "
                   "expires_at INTEGER, decision TEXT, receipt TEXT)")
        db.execute("INSERT INTO decision VALUES (?, ?, ?, ?, ?, ?, NULL)", binding)
    if decision == "approve":
        require(time.time() < expires_at, "Approval expired")
        current_diff = git(repo, "diff", base, candidate, "--", "csv_import.py")
        require(hashlib.sha256(current_diff.encode()).hexdigest() == digest, "Candidate diff changed")
        transaction = f"start\nverify refs/heads/main {base}\ncreate {target} {candidate}\nprepare\ncommit\n"
        subprocess.run(["git", "-C", str(repo), "update-ref", "--stdin"], input=transaction,
                       check=True, text=True, capture_output=True, timeout=10)
    # Reopen the ledger to emulate recovery after the external write.
    with closing(sqlite3.connect(ledger)) as db, db:
        recorded = db.execute(
            "SELECT base, candidate, ref, digest, expires_at, decision FROM decision"
        ).fetchone()
        require(recorded == binding, "Approval binding changed")
        reference = git(repo, "for-each-ref", "--format=%(objectname)", target).strip()
        if decision == "approve":
            require(reference == candidate, "Delivery reference does not match approval")
            db.execute("UPDATE decision SET receipt = ?", (reference,))
        else:
            require(not reference, "A rejected candidate created a delivery reference")
    require(snapshot(repo) == before, "Delivery modified the original checkout")
    return reference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exercise", action="store_true",
                        help="Replay scripted approve and reject decisions on disposable repositories")
    args = parser.parse_args()
    model = ModelProvider()
    require(model.ask("Reply exactly PA_CHAT_OK.") == "PA_CHAT_OK", "Chat answer did not match")
    plan = json.loads(model.ask(
        'Return only JSON with key "template" and value "csv_blank_line", and key "test" '
        'with value "unittest". The user requests a CSV blank line fix.'
    ).removeprefix("```json").removesuffix("```").strip())
    require(plan == {"template": "csv_blank_line", "test": "unittest"}, "Invalid Main template parameters")
    print("PASS Chat and Main via the same API adapter", flush=True)

    with TemporaryDirectory(prefix="pa-m0-workflow-", dir="/var/tmp") as directory:
        root = Path(directory)
        repo, work = root / "repo", root / "coding"
        subprocess.run([sys.executable, "-B", str(SCRIPT_DIR / "init_csv_fixture.py"), str(repo)],
                       check=True, capture_output=True, timeout=15)
        base = git(repo, "rev-parse", "HEAD").strip()
        materialize(repo, base, root / "baseline")
        baseline = test_revision(root / "baseline")
        require(baseline.returncode == 1 and "Ran 4 tests" in baseline.stderr
                and "FAILED (failures=1)" in baseline.stderr
                and "FAIL: test_skips_empty_line_between_records" in baseline.stderr, "Unexpected CSV baseline")
        materialize(repo, base, work)
        original = {path.name: path.read_bytes() for path in work.iterdir()}
        research = model.ask(
            "Locate the line that produces empty records in this synthetic importer. "
            "Reply only csv_import.py when identified.\n" + original["csv_import.py"].decode()
        )
        require(research == "csv_import.py", "Research did not identify the importer")
        run_worker(work, model.worker(root))
        require({path.name for path in work.iterdir()} == set(original), "Worker changed the fixture inventory")
        for path in work.iterdir():
            require(path.is_file() and not path.is_symlink(), "Worker produced an unexpected file type")
            if path.name == "csv_import.py":
                require(path.read_bytes() != original[path.name], "Worker did not change the importer")
            else:
                require(path.read_bytes() == original[path.name], f"Worker changed {path.name}")
        oid = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                             input=(work / "csv_import.py").read_bytes(), capture_output=True,
                             check=True, timeout=10).stdout.decode().strip()
        env = dict(os.environ, GIT_INDEX_FILE=str(root / "candidate.index"))
        for command in (["read-tree", base], ["update-index", "--cacheinfo", f"100644,{oid},csv_import.py"]):
            subprocess.run(["git", "-C", str(repo), *command], env=env, check=True, timeout=10)
        tree = subprocess.check_output(["git", "-C", str(repo), "write-tree"], env=env, text=True, timeout=10).strip()
        candidate = git(repo, *IDENTITY, "commit-tree", tree, "-p", base, "-m", "M0 CSV candidate").strip()
        materialize(repo, candidate, root / "regression")
        fixed = test_revision(root / "regression")
        require(fixed.returncode == 0 and "Ran 4 tests" in fixed.stderr, "Candidate regression failed")
        diff = git(repo, "diff", base, candidate, "--", "csv_import.py")
        digest = hashlib.sha256(diff.encode()).hexdigest()
        review = model.ask(
            "Review this synthetic CSV patch. Tests: baseline 3 pass and 1 expected failure; candidate 4 pass. "
            "Reply exactly PASS if the patch skips empty records and preserves delimited fields, otherwise FAIL.\n" + diff
        )
        require(review == "PASS", "Review did not approve this candidate")
        expires_at = int(time.time()) + 300
        print(f"Review PASS. Target {repo}, branch {DELIVERY}\nBase {base}\nCandidate {candidate}"
              f"\nDiff SHA256 {digest}\nApproval expires in 300 seconds"
              f"\nBaseline 3/4, candidate 4/4\n{diff}", flush=True)
        if args.exercise:
            reject_repo = root / "reject-repo"
            git(repo, "clone", "--no-local", "--quiet", str(repo), str(reject_repo))
            git(reject_repo, "remote", "remove", "origin")
            require(not deliver(reject_repo, root / "rejected.db", base, candidate, digest,
                                expires_at, "reject"), "Rejection failed")
            require(deliver(repo, root / "approved.db", base, candidate, digest,
                            expires_at, "approve") == candidate, "Approval failed")
            print("PASS scripted rejection, approval, reference readback, receipt recovery and unchanged checkout")
        else:
            decision = input("Enter the candidate SHA to approve, or press Enter to reject: ").strip()
            require(decision in ("", candidate), "Decision did not match the candidate shown")
            delivered = deliver(repo, root / "operator.db", base, candidate, digest, expires_at,
                                "approve" if decision else "reject")
            print("Temporary delivery result:", delivered or "rejected")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, RuntimeError):
            print(error, file=sys.stderr)
        else:
            print(f"M0 verification failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1)
