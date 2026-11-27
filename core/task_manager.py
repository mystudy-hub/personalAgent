"""SQLite persistence for projects, tasks and their ordered events."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from core.worker_protocol import CodingResult, RESULT_TYPES, evidence_problem


SCHEMA = (
    """CREATE TABLE projects (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
        path_key TEXT NOT NULL UNIQUE
    )""",
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        submission_key TEXT NOT NULL, title TEXT NOT NULL, goal TEXT NOT NULL,
        status TEXT NOT NULL, version INTEGER NOT NULL,
        UNIQUE (project_id, submission_key)
    )""",
    """CREATE TABLE events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        kind TEXT NOT NULL, payload TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )""",
    "CREATE INDEX events_by_task ON events(task_id, sequence)",
)

EXECUTION_SCHEMA = (
    "ALTER TABLE tasks ADD COLUMN checkpoint INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN pending_action TEXT",
    "ALTER TABLE tasks ADD COLUMN resume_status TEXT",
    "ALTER TABLE tasks ADD COLUMN active_attempt_id TEXT",
    """CREATE TABLE attempts (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
        worker TEXT NOT NULL, checkpoint INTEGER NOT NULL, lease_until REAL NOT NULL,
        started_at REAL NOT NULL, finished_at REAL, outcome TEXT
    )""",
    "CREATE UNIQUE INDEX current_attempt ON attempts(task_id) WHERE outcome IS NULL",
    "CREATE INDEX tasks_queue ON tasks(status)",
    "CREATE INDEX attempts_expiry ON attempts(lease_until) WHERE outcome IS NULL",
)
PLAN_SCHEMA = "ALTER TABLE tasks ADD COLUMN plan TEXT"
ROLE_SCHEMA = (
    "ALTER TABLE tasks ADD COLUMN model_plan INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN model_calls INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN review_round INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN blocked_reason TEXT",
    """CREATE TABLE artifacts (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        node_id TEXT NOT NULL, payload TEXT NOT NULL
    )""",
    "CREATE INDEX artifacts_by_task ON artifacts(task_id, node_id)",
)
TERMINAL = {"SUCCEEDED", "CANCELLED", "FAILED"}
ACTIVE = {"RUNNING", "PLANNING", "REVIEWING"}
SIMULATED_STEPS = 2


@dataclass(frozen=True)
class Attempt:
    id: str
    task_id: str
    node_id: str | None = None


class Conflict(Exception):
    """The request conflicts with the current task state."""


class TaskManager:
    def __init__(self, database: Path, *, clock=time.time, review_limit=2):
        self.database = database
        self.clock = clock
        self.review_limit = review_limit

    @contextmanager
    def connection(self, *, write=False):
        if write:
            database = self.database
        else:
            database = f"{self.database.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(database, uri=not write, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection
        finally:
            connection.close()

    def migrate(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in SCHEMA:
                    db.execute(statement)
                version = 1
            if version == 1:
                for statement in EXECUTION_SCHEMA:
                    db.execute(statement)
                version = 2
            if version == 2:
                db.execute(PLAN_SCHEMA)
                version = 3
            if version == 3:
                for statement in ROLE_SCHEMA:
                    db.execute(statement)
                db.execute("PRAGMA user_version = 4")
            elif version != 4:
                raise RuntimeError(f"Unsupported database schema version: {version}")

    @staticmethod
    def _task(row):
        return dict(row) | {"plan": json.loads(row["plan"]) if row["plan"] is not None else None}

    @staticmethod
    def _load(db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TaskManager._task(row)

    @staticmethod
    def _event(db, task, kind, **details):
        payload = {key: task[key] for key in ("status", "version", "checkpoint", "pending_action")}
        db.execute("INSERT INTO events (task_id, kind, payload) VALUES (?, ?, ?)",
                   (task["id"], kind, json.dumps(payload | details)))

    def _transition(self, db, task, kind, *, detail=None, **updates):
        task = task | updates | {"version": task["version"] + 1}
        db.execute(
            """UPDATE tasks SET status=:status, version=:version, checkpoint=:checkpoint,
            pending_action=:pending_action, resume_status=:resume_status,
            active_attempt_id=:active_attempt_id, plan=:stored_plan, model_calls=:model_calls,
            review_round=:review_round, blocked_reason=:blocked_reason WHERE id=:id""",
            task | {"stored_plan": json.dumps(task["plan"]) if task["plan"] is not None else None},
        )
        self._event(db, task, kind, **(detail or {}))
        return task

    def register_project(self, name: str, path: Path) -> dict:
        canonical = str(path.resolve(strict=True))
        key = os.path.normcase(canonical)
        with self.connection(write=True) as db:
            db.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?) ON CONFLICT(path_key) DO NOTHING",
                (uuid4().hex, name, canonical, key),
            )
            row = db.execute(
                "SELECT id, name, path FROM projects WHERE path_key = ?", (key,),
            ).fetchone()
            return dict(row)

    def list_projects(self) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT id, name, path FROM projects ORDER BY name, id")]

    def project_exists(self, project_id: str) -> bool:
        with self.connection() as db:
            return db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is not None

    def submit(self, project_id: str, key: str, title: str, goal: str, *,
               plan=None, model_plan: bool = False) -> tuple[dict, bool]:
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise KeyError(project_id)
            current = db.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND submission_key = ?",
                (project_id, key),
            ).fetchone()
            if current is not None:
                task = self._task(current)
                comparable_plan = None if task["model_plan"] else task["plan"]
                if (task["title"], task["goal"], comparable_plan, bool(task["model_plan"])) != (
                    title, goal, plan, model_plan
                ):
                    raise Conflict("Submission key already binds a different task")
                return task, False
            task = {
                "id": uuid4().hex, "project_id": project_id, "submission_key": key,
                "title": title, "goal": goal, "status": "CREATED", "version": 1,
                "plan": json.dumps(plan) if plan is not None else None,
                "model_plan": model_plan,
            }
            db.execute(
                """INSERT INTO tasks (id, project_id, submission_key, title, goal, status, version, plan, model_plan)
                VALUES (:id, :project_id, :submission_key, :title, :goal, :status, :version, :plan, :model_plan)""",
                task,
            )
            task = self._load(db, task["id"])
            self._event(db, task, "task.created")
            return task, True

    def get_task(self, task_id: str) -> dict:
        with self.connection() as db:
            return self._load(db, task_id)

    def list_tasks(self, limit: int) -> list[dict]:
        with self.connection() as db:
            return [self._task(row) for row in db.execute("SELECT * FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,))]

    def control(self, task_id: str, action: str) -> dict:
        with self.connection(write=True) as db:
            task = self._load(db, task_id)
            status, pending = task["status"], task["pending_action"]
            if status in TERMINAL:
                return task
            if action == "cancel":
                if status in ACTIVE:
                    if pending == "cancel":
                        return task
                    return self._transition(db, task, "task.cancel_requested", pending_action="cancel")
                return self._transition(db, task, "task.cancelled", status="CANCELLED",
                                        pending_action=None, resume_status=None)
            if action == "pause":
                if pending == "cancel":
                    raise Conflict("Task cancellation is pending")
                if status == "PAUSED" or pending == "pause":
                    return task
                if status in ACTIVE:
                    return self._transition(db, task, "task.pause_requested", pending_action="pause")
                if status in ("CREATED", "READY", "WAITING_APPROVAL"):
                    return self._transition(db, task, "task.paused", status="PAUSED", resume_status=status)
            if action == "resume" and status == "PAUSED":
                return self._transition(db, task, "task.resumed", status=task["resume_status"], resume_status=None)
            raise Conflict(f"Cannot {action} task in {status}")

    @staticmethod
    def _node_id(task):
        if task["status"] == "PLANNING":
            return "main"
        if task["plan"]:
            return task["plan"]["nodes"][task["checkpoint"]]["id"]
        return None

    @staticmethod
    def _artifact(db, task_id, node_id):
        row = db.execute(
            """SELECT attempt_id, node_id, payload FROM artifacts WHERE task_id=? AND node_id=?
            ORDER BY rowid DESC LIMIT 1""", (task_id, node_id),
        ).fetchone()
        return dict(row) | {"payload": json.loads(row["payload"])} if row is not None else None

    def role_input(self, attempt: Attempt) -> dict | None:
        dependency = {"coding": "research", "review": "coding"}.get(attempt.node_id)
        if dependency is None:
            return None
        with self.connection() as db:
            return self._artifact(db, attempt.task_id, dependency)

    def summary(self, task_id: str) -> dict:
        with self.connection() as db:
            task = self._load(db, task_id)
            rows = db.execute(
                "SELECT attempt_id, node_id, payload FROM artifacts WHERE task_id=? ORDER BY rowid",
                (task_id,),
            )
            artifacts = [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]
            return {key: task[key] for key in ("id", "status", "checkpoint", "review_round",
                                               "blocked_reason", "model_calls")} | {"artifacts": artifacts}

    def _close_attempt(self, db, task, attempt_id, outcome, now, *, checkpoint=None, **overrides):
        db.execute("UPDATE attempts SET outcome=?, finished_at=? WHERE id=?", (outcome, now, attempt_id))
        pending = task["pending_action"]
        next_checkpoint = task["checkpoint"] if checkpoint is None else checkpoint
        status = "CREATED" if task["status"] == "PLANNING" else "READY"
        if pending == "cancel":
            status = "CANCELLED"
        elif task["plan"] and next_checkpoint == len(task["plan"]["nodes"]):
            status = "WAITING_APPROVAL"
        elif not task["plan"] and next_checkpoint == SIMULATED_STEPS:
            status = "SUCCEEDED"
        elif pending == "pause":
            overrides.setdefault("resume_status", status)
            status = "PAUSED"
        detail = {"attempt_id": attempt_id}
        node_id = self._node_id(task)
        if node_id:
            detail["node_id"] = node_id
        updates = {"status": status, "checkpoint": next_checkpoint, "pending_action": None,
                   "active_attempt_id": None, "resume_status": None}
        return self._transition(
            db, task, f"attempt.{outcome}", detail=detail, **(updates | overrides),
        )

    def claim(self, worker: str, lease_seconds: float) -> Attempt | None:
        with self.connection(write=True) as db:
            now = self.clock()
            expired = db.execute(
                "SELECT id, task_id FROM attempts WHERE outcome IS NULL AND lease_until <= ?", (now,),
            ).fetchall()
            for row in expired:
                task = self._load(db, row["task_id"])
                self._close_attempt(db, task, row["id"], "expired", now)
            row = db.execute(
                "SELECT * FROM tasks WHERE status IN ('CREATED', 'READY') ORDER BY rowid LIMIT 1",
            ).fetchone()
            if row is None:
                return None
            task = self._task(row)
            if task["status"] == "CREATED":
                task = self._transition(db, task, "task.planning", status="PLANNING")
                if not task["model_plan"] or task["plan"] is not None:
                    task = self._transition(db, task, "task.ready", status="READY")
            node_id = self._node_id(task)
            attempt = Attempt(uuid4().hex, task["id"], node_id)
            db.execute(
                """INSERT INTO attempts (id, task_id, worker, checkpoint, lease_until, started_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (attempt.id, attempt.task_id, worker, task["checkpoint"], now + lease_seconds, now),
            )
            detail = {"attempt_id": attempt.id, "worker": worker}
            if node_id:
                detail["node_id"] = node_id
            status = {"main": "PLANNING", "review": "REVIEWING"}.get(node_id, "RUNNING")
            self._transition(db, task, "attempt.claimed", status=status, active_attempt_id=attempt.id,
                             detail=detail)
            return attempt

    def _live_task(self, db, attempt, now):
        task = self._load(db, attempt.task_id)
        if task["status"] not in ACTIVE or task["active_attempt_id"] != attempt.id:
            return None
        lease = db.execute("SELECT lease_until FROM attempts WHERE id = ?", (attempt.id,)).fetchone()
        return task if lease["lease_until"] > now else None

    def reserve_main_call(self, attempt: Attempt) -> bool:
        with self.connection(write=True) as db:
            task = self._live_task(db, attempt, self.clock())
            if (task is None or task["status"] != "PLANNING" or task["pending_action"] == "cancel"
                    or task["model_calls"] >= 2):
                return False
            self._transition(db, task, "main.call_reserved", model_calls=task["model_calls"] + 1)
            return True

    def _finish_role(self, db, task, attempt, result, now):
        node_id = self._node_id(task)
        if node_id == "main":
            if result is None or "plan" not in result:
                reason = result.get("error", "Main did not produce a plan") if result else "Main did not produce a plan"
                self._close_attempt(db, task, attempt.id, "blocked", now, status="BLOCKED",
                                    blocked_reason=reason, resume_status=None)
            else:
                paused = task["pending_action"] == "pause"
                self._close_attempt(db, task, attempt.id, "completed", now, plan=result["plan"],
                                    status="PAUSED" if paused else "READY",
                                    resume_status="READY" if paused else None)
            return
        if node_id is None:
            self._close_attempt(db, task, attempt.id, "completed", now, checkpoint=task["checkpoint"] + 1)
            return
        try:
            output = RESULT_TYPES[node_id].model_validate(result)
        except ValidationError:
            self._close_attempt(db, task, attempt.id, "blocked", now, status="BLOCKED",
                                blocked_reason=f"Invalid {node_id} result", resume_status=None)
            return
        if node_id == "review":
            candidate = self._artifact(db, task["id"], "coding")
            if candidate is None or output.candidate_id != candidate["attempt_id"]:
                self._close_attempt(db, task, attempt.id, "blocked", now, status="BLOCKED",
                                    blocked_reason="Review did not identify the current candidate", resume_status=None)
                return
            problem = evidence_problem(CodingResult.model_validate(candidate["payload"]))
            if problem and output.verdict == "pass":
                output = output.model_copy(update={"verdict": "revise", "reason": problem})
        db.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?)",
                   (attempt.id, task["id"], node_id, output.model_dump_json()))
        if node_id == "review" and output.verdict == "revise":
            rounds = task["review_round"] + 1
            blocked = rounds >= self.review_limit
            paused = not blocked and task["pending_action"] == "pause"
            self._close_attempt(
                db, task, attempt.id, "returned", now, checkpoint=1, review_round=rounds,
                status="BLOCKED" if blocked else "PAUSED" if paused else "READY",
                resume_status="READY" if paused else None,
                blocked_reason=output.reason if blocked else None,
            )
        else:
            self._close_attempt(db, task, attempt.id, "completed", now, checkpoint=task["checkpoint"] + 1)

    def advance(self, attempt: Attempt, lease_seconds: float, *, finished=False, release=False,
                result=None, settling=False) -> bool:
        """Advance a live attempt. A settling caller closes its stream before acknowledging cancellation."""
        with self.connection(write=True) as db:
            now = self.clock()
            task = self._live_task(db, attempt, now)
            if task is None:
                return False
            if task["pending_action"] == "cancel":
                if settling:
                    return False
                self._close_attempt(db, task, attempt.id, "cancelled", now)
            elif finished:
                self._finish_role(db, task, attempt, result, now)
            elif release:
                self._close_attempt(db, task, attempt.id, "released", now)
            else:
                db.execute("UPDATE attempts SET lease_until = ? WHERE id = ?", (now + lease_seconds, attempt.id))
                return True
            return False

    def events(self, task_id: str, after: int, limit: int) -> list[dict]:
        with self.connection() as db:
            if db.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
                raise KeyError(task_id)
            rows = db.execute(
                "SELECT * FROM events WHERE task_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (task_id, after, limit),
            )
            return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]
