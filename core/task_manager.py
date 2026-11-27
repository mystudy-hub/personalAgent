"""SQLite persistence for projects, tasks and their ordered events."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


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
TERMINAL = {"SUCCEEDED", "CANCELLED", "FAILED"}
SIMULATED_STEPS = 2


@dataclass(frozen=True)
class Attempt:
    id: str
    task_id: str


class Conflict(Exception):
    """The request conflicts with the current task state."""


class TaskManager:
    def __init__(self, database: Path, *, clock=time.time):
        self.database = database
        self.clock = clock

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
                db.execute("PRAGMA user_version = 2")
            elif version != 2:
                raise RuntimeError(f"Unsupported database schema version: {version}")

    @staticmethod
    def _load(db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

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
            active_attempt_id=:active_attempt_id WHERE id=:id""",
            task,
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

    def submit(self, project_id: str, key: str, title: str, goal: str) -> tuple[dict, bool]:
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise KeyError(project_id)
            current = db.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND submission_key = ?",
                (project_id, key),
            ).fetchone()
            if current is not None:
                if (current["title"], current["goal"]) != (title, goal):
                    raise Conflict("Submission key already binds a different task")
                return dict(current), False
            task = {
                "id": uuid4().hex, "project_id": project_id, "submission_key": key,
                "title": title, "goal": goal, "status": "CREATED", "version": 1,
            }
            db.execute(
                """INSERT INTO tasks (id, project_id, submission_key, title, goal, status, version)
                VALUES (:id, :project_id, :submission_key, :title, :goal, :status, :version)""",
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
            return [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,))]

    def control(self, task_id: str, action: str) -> dict:
        with self.connection(write=True) as db:
            task = self._load(db, task_id)
            status, pending = task["status"], task["pending_action"]
            if status in TERMINAL:
                return task
            if action == "cancel":
                if status == "RUNNING":
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
                if status == "RUNNING":
                    return self._transition(db, task, "task.pause_requested", pending_action="pause")
                if status in ("CREATED", "READY"):
                    return self._transition(db, task, "task.paused", status="PAUSED", resume_status=status)
            if action == "resume" and status == "PAUSED":
                return self._transition(db, task, "task.resumed", status=task["resume_status"], resume_status=None)
            raise Conflict(f"Cannot {action} task in {status}")

    def _close_attempt(self, db, task, attempt_id, outcome, now, *, checkpoint=None):
        db.execute("UPDATE attempts SET outcome=?, finished_at=? WHERE id=?", (outcome, now, attempt_id))
        pending = task["pending_action"]
        next_checkpoint = task["checkpoint"] if checkpoint is None else checkpoint
        status = "READY"
        if pending == "cancel":
            status = "CANCELLED"
        elif next_checkpoint == SIMULATED_STEPS:
            status = "SUCCEEDED"
        elif pending == "pause":
            status = "PAUSED"
        return self._transition(
            db, task, f"attempt.{outcome}", detail={"attempt_id": attempt_id}, status=status,
            checkpoint=next_checkpoint, pending_action=None, active_attempt_id=None,
            resume_status="READY" if status == "PAUSED" else None,
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
            task = dict(row)
            if task["status"] == "CREATED":
                task = self._transition(db, task, "task.planning", status="PLANNING")
                task = self._transition(db, task, "task.ready", status="READY")
            attempt = Attempt(uuid4().hex, task["id"])
            db.execute(
                """INSERT INTO attempts (id, task_id, worker, checkpoint, lease_until, started_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (attempt.id, attempt.task_id, worker, task["checkpoint"], now + lease_seconds, now),
            )
            self._transition(db, task, "attempt.claimed", status="RUNNING", active_attempt_id=attempt.id,
                             detail={"attempt_id": attempt.id, "worker": worker})
            return attempt

    def advance(self, attempt: Attempt, lease_seconds: float, *, finished=False, release=False) -> bool:
        """Advance a live attempt. Return whether the Worker should keep running."""
        with self.connection(write=True) as db:
            now = self.clock()
            task = self._load(db, attempt.task_id)
            if task["status"] != "RUNNING" or task["active_attempt_id"] != attempt.id:
                return False
            lease = db.execute("SELECT lease_until FROM attempts WHERE id = ?", (attempt.id,)).fetchone()
            if lease["lease_until"] <= now:
                return False
            if task["pending_action"] == "cancel":
                self._close_attempt(db, task, attempt.id, "cancelled", now)
            elif finished:
                self._close_attempt(db, task, attempt.id, "completed", now, checkpoint=task["checkpoint"] + 1)
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
