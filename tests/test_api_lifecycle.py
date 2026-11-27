"""Exercise M1 over loopback, including a forced service restart."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


class ApiLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.cleanup_directory, directory)
        self.root = Path(directory.name)
        with socket.socket() as address:
            address.bind(("127.0.0.1", 0))
            self.port = address.getsockname()[1]
        self.token = "m1-integration-test-token"
        self.client = build_opener(ProxyHandler({}))
        self.server = None
        self.server_pid = None
        self.addCleanup(self.stop)

    @staticmethod
    def cleanup_directory(directory):
        deadline = time.monotonic() + 3
        while True:
            try:
                directory.cleanup()
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def start(self):
        pid_file = self.root / "service.pid"
        pid_file.unlink(missing_ok=True)
        env = dict(os.environ, PA_SESSION_TOKEN=self.token, PA_PORT=str(self.port),
                   PA_DB_PATH=str(self.root / "m1.sqlite3"), PA_SIM_WORKERS="2",
                   PA_SIM_STEP_SECONDS="1.5", PA_SIM_LEASE_SECONDS="0.75")
        launch = ("import os, runpy, sys; from pathlib import Path; "
                  "Path(sys.argv[1]).write_text(str(os.getpid())); "
                  "runpy.run_module('core.api', run_name='__main__')")
        self.server = subprocess.Popen([sys.executable, "-B", "-c", launch, str(pid_file)], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_for(lambda: pid_file.is_file() and self.request("/health")[0] == 200, timeout=15)
        self.server_pid = int(pid_file.read_text())

    def stop(self):
        if self.server is not None and self.server.poll() is None:
            if self.server_pid in (None, self.server.pid):
                self.server.kill()
            else:
                try:
                    os.kill(self.server_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            self.server.wait(timeout=10)
        self.server_pid = None

    def wait_for(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except URLError:
                if self.server is not None and self.server.poll() is not None:
                    self.fail(f"Service stopped with exit code {self.server.returncode}")
            time.sleep(0.03)
        self.fail("API did not reach the expected state before the deadline")

    def request(self, route, data=None, *, post=False, headers=None):
        request_headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        request_headers.update(headers or {})
        payload = json.dumps(data).encode() if data is not None else None
        request = Request(f"http://127.0.0.1:{self.port}{route}", payload, request_headers,
                          method="POST" if post else None)
        try:
            with self.client.open(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            with response:
                return response.code, json.load(response)

    def task(self, route):
        status, task = self.request(route)
        self.assertEqual(status, 200)
        return task

    def test_pause_survives_restart_and_interrupted_attempt_is_replaced(self):
        project_root = self.root / "project"
        subprocess.run(["git", "init", "--quiet", "--template=", str(project_root)], check=True)
        self.start()
        self.assertEqual(self.request("/health", headers={"Authorization": ""})[0], 401)
        self.assertEqual(self.request("/health", headers={"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.request("/health", headers={"Host": "example.invalid"})[0], 403)
        status, project = self.request("/projects", {"name": "Test", "path": str(project_root)})
        self.assertEqual(status, 200)

        def submit(key):
            body = {"project_id": project["id"], "submission_key": key, "title": "CSV", "goal": "Repair"}
            status, task = self.request("/tasks", body)
            self.assertEqual(status, 201)
            self.assertEqual(task["status"], "CREATED")
            again_status, again = self.request("/tasks", body)
            self.assertEqual((again_status, again["id"]), (200, task["id"]))
            return "/tasks/" + task["id"]

        paused = submit("paused")
        other = submit("interrupted")
        self.wait_for(lambda: all(self.task(route)["status"] == "RUNNING" for route in (paused, other)))
        status, state = self.request(paused + "/pause", post=True)
        self.assertEqual((status, state["pending_action"]), (200, "pause"))
        self.wait_for(lambda: self.task(paused)["status"] == "PAUSED")
        self.assertEqual(self.task(paused)["checkpoint"], 1)
        self.wait_for(lambda: (self.task(other)["checkpoint"], self.task(other)["status"]) == (1, "RUNNING"))
        active = self.task(other)["active_attempt_id"]
        previous_pid = self.server_pid
        self.stop()
        self.start()
        self.assertNotEqual(self.server_pid, previous_pid)
        self.assertEqual(self.task(paused)["status"], "PAUSED")
        self.wait_for(lambda: self.task(other)["status"] == "SUCCEEDED")
        status, events = self.request(other + "/events")
        self.assertEqual(status, 200)
        expired = [event for event in events if event["kind"] == "attempt.expired"]
        self.assertEqual([event["payload"]["attempt_id"] for event in expired], [active])
        completed = [event["payload"]["checkpoint"] for event in events if event["kind"] == "attempt.completed"]
        self.assertEqual(completed, [1, 2])
        self.assertEqual(self.request(other + "/events?after=" + str(events[-1]["sequence"])), (200, []))
        self.assertEqual(self.request(paused + "/resume", post=True)[0], 200)
        self.wait_for(lambda: self.task(paused)["status"] == "SUCCEEDED")

        cancelled = submit("cancelled")
        self.wait_for(lambda: self.task(cancelled)["status"] == "RUNNING")
        self.assertEqual(self.request(cancelled + "/cancel", post=True)[0], 200)
        self.wait_for(lambda: self.task(cancelled)["status"] == "CANCELLED")
        self.assertEqual(self.task(cancelled)["checkpoint"], 0)
        self.assertEqual(self.request(other + "/cancel", post=True)[1]["status"], "SUCCEEDED")
        self.assertEqual(len(self.request("/tasks")[1]), 3)
