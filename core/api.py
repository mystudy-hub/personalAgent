"""Authenticated localhost API for the M1 task lifecycle."""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StringConstraints

from core.task_manager import Conflict, TaskManager
from core.worker_runtime import run_worker


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ProjectInput(BaseModel):
    name: ShortText
    path: Annotated[str, StringConstraints(min_length=1, max_length=32767)]


class TaskInput(BaseModel):
    project_id: ShortText
    submission_key: ShortText
    title: ShortText
    goal: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]


def create_app() -> FastAPI:
    token = os.environ.get("PA_SESSION_TOKEN", "")
    if not token:
        raise RuntimeError("PA_SESSION_TOKEN is required")
    port = int(os.environ.get("PA_PORT", "8000"))
    if not 1 <= port <= 65535:
        raise RuntimeError("PA_PORT must be between 1 and 65535")
    workers = int(os.environ.get("PA_SIM_WORKERS", "2"))
    step_seconds = float(os.environ.get("PA_SIM_STEP_SECONDS", "0.75"))
    lease_seconds = float(os.environ.get("PA_SIM_LEASE_SECONDS", "5"))
    if not (1 <= workers <= 16 and 0.05 <= step_seconds <= 60 and 0.5 <= lease_seconds <= 120):
        raise RuntimeError("Invalid simulated worker configuration")
    origin = f"http://127.0.0.1:{port}"
    hosts = {"127.0.0.1", f"127.0.0.1:{port}"}
    manager = TaskManager(Path(os.environ.get("PA_DB_PATH", "data/database/pa.sqlite3")))

    @asynccontextmanager
    async def lifespan(_app):
        manager.migrate()
        stop = asyncio.Event()
        async with asyncio.TaskGroup() as group:
            for _ in range(workers):
                group.create_task(run_worker(
                    manager, stop, step_seconds=step_seconds, lease_seconds=lease_seconds,
                ))
            try:
                yield
            finally:
                stop.set()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authorize(request: Request, call_next):
        supplied_origin = request.headers.get("origin")
        if request.headers.get("host") not in hosts or supplied_origin not in (None, origin):
            return JSONResponse({"detail": "Invalid host or origin"}, status_code=403)
        supplied = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(supplied, f"Bearer {token}".encode()):
            return JSONResponse({"detail": "Invalid session token"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/projects")
    def register_project(body: ProjectInput):
        path = Path(body.path)
        try:
            if not path.is_absolute() or not path.is_dir() or not (path / ".git").exists():
                raise HTTPException(422, "Path must be an absolute local Git working directory")
            return manager.register_project(body.name, path)
        except (OSError, ValueError) as error:
            raise HTTPException(422, "Project path is unavailable") from error

    @app.get("/projects")
    def list_projects():
        return manager.list_projects()

    @app.post("/tasks", status_code=201)
    def submit_task(body: TaskInput, response: Response):
        try:
            task, created = manager.submit(body.project_id, body.submission_key, body.title, body.goal)
        except KeyError as error:
            raise HTTPException(404, "Project not found") from error
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        if not created:
            response.status_code = 200
        return task

    @app.get("/tasks")
    def list_tasks(limit: Annotated[int, Query(ge=1, le=500)] = 100):
        return manager.list_tasks(limit)

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str):
        try:
            return manager.get_task(task_id)
        except KeyError as error:
            raise HTTPException(404, "Task not found") from error

    @app.get("/tasks/{task_id}/events")
    def get_events(task_id: str, after: Annotated[int, Query(ge=0, le=2**63-1)] = 0,
                   limit: Annotated[int, Query(ge=1, le=500)] = 100):
        try:
            return manager.events(task_id, after, limit)
        except KeyError as error:
            raise HTTPException(404, "Task not found") from error

    @app.post("/tasks/{task_id}/{action}")
    def control_task(task_id: str, action: Literal["pause", "resume", "cancel"]):
        try:
            return manager.control(task_id, action)
        except KeyError as error:
            raise HTTPException(404, "Task not found") from error
        except Conflict as error:
            raise HTTPException(409, str(error)) from error

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("core.api:create_app", factory=True, host="127.0.0.1",
                port=int(os.environ.get("PA_PORT", "8000")), proxy_headers=False)
