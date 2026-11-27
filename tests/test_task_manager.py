import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from core.task_manager import SCHEMA, Conflict, TaskManager


class TaskManagerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.now = 100.0
        self.manager = TaskManager(self.path / "pa.sqlite3", clock=lambda: self.now)

    def new_task(self):
        self.manager.migrate()
        project = self.manager.register_project("Fixture", self.path)
        task, _ = self.manager.submit(project["id"], "draft-1", "CSV", "Skip empty lines")
        return task["id"]

    def test_concurrent_submission_and_restart_keep_one_task_and_event(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory)
            manager = TaskManager(path / "pa.sqlite3")
            manager.migrate()
            project = manager.register_project("Fixture", path)

            def submit(_):
                return manager.submit(project["id"], "draft-1", "Repair CSV", "Skip blank lines")

            with ThreadPoolExecutor(max_workers=6) as pool:
                submissions = list(pool.map(submit, range(12)))
            task_id = submissions[0][0]["id"]
            self.assertEqual({task["id"] for task, _ in submissions}, {task_id})
            self.assertEqual(sum(created for _, created in submissions), 1)
            with self.assertRaises(Conflict):
                manager.submit(project["id"], "draft-1", "Different request", "Skip blank lines")

            restarted = TaskManager(manager.database)
            restarted.migrate()
            self.assertEqual(restarted.get_task(task_id)["status"], "CREATED")
            events = restarted.events(task_id, 0, 100)
            self.assertEqual([event["kind"] for event in events], ["task.created"])
            self.assertEqual(restarted.events(task_id, events[0]["sequence"], 100), [])

    def test_atomic_claim_pause_and_resume_without_repeating_checkpoint(self):
        task_id = self.new_task()
        with ThreadPoolExecutor(max_workers=6) as pool:
            claimed = list(pool.map(lambda n: self.manager.claim(f"worker-{n}", 5), range(12)))
        attempts = [attempt for attempt in claimed if attempt is not None]
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(self.manager.control(task_id, "pause")["pending_action"], "pause")
        self.assertFalse(self.manager.advance(attempt, 5, finished=True))
        self.assertIsNone(self.manager.claim("waiting", 5))
        paused = self.manager.get_task(task_id)
        self.assertEqual((paused["status"], paused["checkpoint"]), ("PAUSED", 1))
        restarted = TaskManager(self.manager.database, clock=lambda: self.now)
        restarted.migrate()
        self.assertIsNone(restarted.claim("waiting", 5))
        self.assertEqual(restarted.control(task_id, "resume")["status"], "READY")
        resumed = restarted.claim("resumed", 5)
        self.assertNotEqual(resumed.id, attempt.id)
        self.assertFalse(restarted.advance(attempt, 5, finished=True))
        restarted.advance(resumed, 5, finished=True)
        final = restarted.control(task_id, "cancel")
        self.assertEqual((final["status"], final["checkpoint"]), ("SUCCEEDED", 2))
        events = restarted.events(task_id, 0, 100)
        self.assertEqual([event["payload"]["version"] for event in events], list(range(1, len(events) + 1)))

    def test_expired_attempt_is_fenced_and_accepted_cancellation_wins(self):
        task_id = self.new_task()
        original = self.manager.claim("crashed", 5)
        self.now += 4
        self.assertIsNone(self.manager.claim("restart", 5))
        self.now += 1
        self.assertFalse(self.manager.advance(original, 5, finished=True))
        restarted = TaskManager(self.manager.database, clock=lambda: self.now)
        restarted.migrate()
        replacement = restarted.claim("restart", 5)
        self.assertNotEqual(replacement.id, original.id)
        self.assertEqual(restarted.get_task(task_id)["checkpoint"], 0)
        restarted.control(task_id, "cancel")
        restarted.advance(replacement, 5, finished=True)
        task = restarted.get_task(task_id)
        self.assertEqual((task["status"], task["checkpoint"]), ("CANCELLED", 0))
        self.assertIsNone(restarted.claim("later", 5))
        self.assertIn("attempt.expired", [event["kind"] for event in restarted.events(task_id, 0, 100)])

    def test_v1_migration_preserves_task_and_creation_event(self):
        with closing(sqlite3.connect(self.manager.database)) as db, db:
            for statement in SCHEMA:
                db.execute(statement)
            db.execute("PRAGMA user_version = 1")
            db.execute("INSERT INTO projects VALUES ('project', 'Example', 'local', 'local')")
            db.execute("INSERT INTO tasks VALUES ('old', 'project', 'draft', 'CSV', 'Repair', 'CREATED', 1)")
            db.execute("INSERT INTO events (task_id, kind, payload) VALUES ('old', 'task.created', '{}')")
        self.manager.migrate()
        task = self.manager.get_task("old")
        self.assertEqual((task["status"], task["checkpoint"], task["version"]), ("CREATED", 0, 1))
        self.assertEqual(self.manager.events("old", 0, 100)[0]["kind"], "task.created")


if __name__ == "__main__":
    unittest.main()
