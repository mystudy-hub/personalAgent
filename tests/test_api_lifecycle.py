"""Exercise task recovery and model handoff over loopback."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from core.model_adapter import ModelError, ResponsesModel
from core.task_manager import TaskManager
from core.worker_runtime import run_worker


class TestModelHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        instructions, message = body["instructions"], body["input"]
        scenario = json.loads(message)["goal"] if instructions.startswith("Classify") else message
        if scenario == "truncated" and not instructions.startswith("Summarize"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", "1024")
            self.end_headers()
            self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
            self.close_connection = True
            return
        if scenario == "heartbeat" and not instructions.startswith("Summarize"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.server.stream_started.set()
            try:
                while not self.server.stop_fixture.wait(0.04):
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except OSError:
                self.server.stream_closed.set()
            return
        if instructions.startswith("Answer"):
            answer = "Test answer"
        elif instructions.startswith("Summarize"):
            answer = json.dumps({"title": "CSV", "goal": message})
        else:
            task = json.loads(message)
            answer = "{}"
            if task["goal"] == "correctable" and "previous output failed" in instructions:
                answer = json.dumps({"project_id": task["project_id"], "template": "csv_blank_line",
                                     "source_file": "csv_import.py", "test": "unittest"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in ({"type": "response.output_text.delta", "delta": answer},
                      {"type": "response.completed"}):
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())


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

    def start(self, *, step_seconds="1.5", model_url=None, workers="2"):
        pid_file = self.root / "service.pid"
        pid_file.unlink(missing_ok=True)
        env = dict(os.environ, PA_SESSION_TOKEN=self.token, PA_PORT=str(self.port),
                   PA_DB_PATH=str(self.root / "m1.sqlite3"), PA_SIM_WORKERS=workers,
                   PA_SIM_STEP_SECONDS=step_seconds, PA_SIM_LEASE_SECONDS="0.75")
        for key in ("PA_MODEL_BASE_URL", "PA_MODEL_NAME", "PA_MODEL_API_KEY"):
            env.pop(key, None)
        if model_url is not None:
            env.update(PA_MODEL_BASE_URL=model_url, PA_MODEL_NAME="fixture")
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

    def start_model(self):
        model = ThreadingHTTPServer(("127.0.0.1", 0), TestModelHandler)
        model.stream_started = Event()
        model.stream_closed = Event()
        model.stop_fixture = Event()
        thread = Thread(target=model.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(model.server_close)
        self.addCleanup(model.shutdown)
        self.addCleanup(model.stop_fixture.set)
        return model, f"http://127.0.0.1:{model.server_port}"

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

    def test_draft_is_editable_and_only_a_validated_plan_is_dispatched(self):
        project_root = self.root / "project"
        subprocess.run(["git", "init", "--quiet", "--template=", str(project_root)], check=True)
        self.start(step_seconds="0.1")
        status, project = self.request("/projects", {"name": "Test", "path": str(project_root)})
        self.assertEqual(status, 200)
        parameters = {"project_id": project["id"], "template": "csv_blank_line",
                      "source_file": "csv_import.py", "test": "unittest"}
        request = {"project_id": project["id"], "title": "CSV", "goal": "Repair",
                   "plan_parameters": parameters}
        self.assertEqual(self.request("/chat/drafts", request | {
            "plan_parameters": parameters | {"nodes": []},
        })[0], 422)
        self.assertEqual(self.request("/chat/drafts", request | {
            "plan_parameters": parameters | {"project_id": "another-project"},
        })[0], 422)
        self.assertEqual(self.request("/chat/drafts", request | {
            "plan_parameters": parameters | {"source_file": "../outside.py"},
        })[0], 422)

        status, result = self.request("/chat/drafts", request)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/tasks"), (200, []))
        body = result["draft"] | {"goal": "Skip empty physical rows"}
        status, task = self.request("/tasks", body)
        self.assertEqual(status, 201)
        self.assertEqual(task["goal"], "Skip empty physical rows")
        self.assertEqual(task["plan"], result["plan"])
        self.assertEqual(self.request("/tasks", body)[1]["id"], task["id"])
        self.assertEqual(self.request("/tasks", body | {"plan_parameters": None})[0], 409)
        path = "/tasks/" + task["id"]
        self.wait_for(lambda: self.task(path)["status"] == "WAITING_APPROVAL")
        events = self.request(path + "/events")[1]
        completed = [event["payload"]["node_id"] for event in events if event["kind"] == "attempt.completed"]
        self.assertEqual(completed, ["research", "coding", "review"])

    def test_chat_and_main_model_handoff_retries_and_blocks_invalid_plan(self):
        _, model_url = self.start_model()
        self.start(step_seconds="0.1", model_url=model_url)
        self.assertEqual(self.request("/chat", {"message": "Hello"}), (
            200, {"kind": "answer", "answer": "Test answer"},
        ))
        project_root = self.root / "project"
        subprocess.run(["git", "init", "--quiet", "--template=", str(project_root)], check=True)
        _, project = self.request("/projects", {"name": "Test", "path": str(project_root)})
        for goal, expected in (("correctable", "WAITING_APPROVAL"), ("unsupported", "BLOCKED")):
            status, reply = self.request("/chat", {"mode": "task", "project_id": project["id"], "message": goal})
            self.assertEqual(status, 200)
            status, task = self.request("/tasks", reply["draft"])
            self.assertEqual(status, 201)
            path = "/tasks/" + task["id"]
            self.wait_for(lambda: self.task(path)["status"] == expected)
            summary = self.request(path + "/summary")[1]
            self.assertEqual(summary["model_calls"], 2)
            self.assertEqual(self.request("/tasks", reply["draft"])[1]["id"], task["id"])
            if expected == "WAITING_APPROVAL":
                self.assertEqual([artifact["node_id"] for artifact in summary["artifacts"]],
                                 ["research", "coding", "review"])
            else:
                self.assertTrue(summary["blocked_reason"])
                self.assertEqual(summary["artifacts"], [])

    def test_total_model_deadline_closes_stream_even_with_heartbeats(self):
        model, model_url = self.start_model()

        async def ask():
            client = ResponsesModel(model_url, "fixture", "", timeout_seconds=0.5)
            try:
                with self.assertRaisesRegex(ModelError, "total time limit"):
                    await asyncio.wait_for(client.ask("Answer the question", "heartbeat"), timeout=3)
            finally:
                await client.close()

        asyncio.run(ask())
        self.assertTrue(model.stream_started.is_set())
        self.assertTrue(model.stream_closed.wait(2), "Model stream was not closed at the deadline")

    def test_main_cancel_closes_model_stream_and_truncated_stream_does_not_stop_worker(self):
        model, model_url = self.start_model()
        self.start(step_seconds="0.1", model_url=model_url, workers="1")
        self.assertEqual(self.request("/chat", {"message": "truncated"})[0], 502)
        project_root = self.root / "project"
        subprocess.run(["git", "init", "--quiet", "--template=", str(project_root)], check=True)
        status, project = self.request("/projects", {"name": "Test", "path": str(project_root)})
        self.assertEqual(status, 200)

        def submit(goal):
            status, task = self.request("/tasks", {
                "project_id": project["id"], "submission_key": goal, "title": "CSV",
                "goal": goal, "model_plan": True,
            })
            self.assertEqual(status, 201)
            return "/tasks/" + task["id"]

        slow = submit("heartbeat")
        self.assertTrue(model.stream_started.wait(3))
        self.assertEqual(self.request(slow + "/cancel", post=True)[0], 200)
        self.wait_for(lambda: self.task(slow)["status"] == "CANCELLED", timeout=3)
        self.assertTrue(model.stream_closed.wait(2), "Cancelled Main still has an open model stream")
        broken = submit("truncated")
        self.wait_for(lambda: self.task(broken)["status"] == "BLOCKED")
        self.assertIn("Model connection closed", self.task(broken)["blocked_reason"])
        healthy = submit("correctable")
        self.wait_for(lambda: self.task(healthy)["status"] == "WAITING_APPROVAL")

    def test_worker_shutdown_closes_model_stream_and_releases_attempt(self):
        model, model_url = self.start_model()
        manager = TaskManager(self.root / "shutdown.sqlite3")
        manager.migrate()
        project = manager.register_project("Test", self.root)
        task, _ = manager.submit(project["id"], "shutdown", "CSV", "heartbeat", model_plan=True)

        async def exercise():
            stop = asyncio.Event()
            client = ResponsesModel(model_url, "fixture", "")
            worker = asyncio.create_task(run_worker(
                manager, stop, step_seconds=0.1, lease_seconds=2, model=client,
            ))
            try:
                self.assertTrue(await asyncio.to_thread(model.stream_started.wait, 3))
                stop.set()
                await asyncio.wait_for(worker, timeout=3)
                self.assertTrue(await asyncio.to_thread(model.stream_closed.wait, 2))
            finally:
                stop.set()
                try:
                    await asyncio.wait_for(worker, timeout=3)
                finally:
                    await client.close()

        asyncio.run(exercise())
        current = manager.get_task(task["id"])
        self.assertEqual((current["status"], current["active_attempt_id"], current["model_calls"]),
                         ("CREATED", None, 1))
        self.assertEqual(manager.events(task["id"], 0, 100)[-1]["kind"], "attempt.released")
