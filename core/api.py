"""Authenticated localhost API for the M1 task lifecycle."""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from core.model_adapter import ModelError, ResponsesModel
from core.orchestrator import PlanParameters, build_plan
from core.task_manager import Conflict, TaskManager
from core.worker_runtime import run_worker


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ProjectInput(BaseModel):
    name: ShortText
    path: Annotated[str, StringConstraints(min_length=1, max_length=32767)]


class TaskFields(BaseModel):
    project_id: ShortText
    title: ShortText
    goal: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]


class DraftInput(TaskFields):
    plan_parameters: PlanParameters


class TaskInput(TaskFields):
    submission_key: ShortText
    plan_parameters: PlanParameters | None = None
    model_plan: bool = False


class ChatInput(BaseModel):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
    mode: Literal["answer", "task"] = "answer"
    project_id: ShortText | None = None


class ModelDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: ShortText
    goal: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]


def validated_plan(project_id: str, parameters: PlanParameters) -> dict:
    try:
        return build_plan(project_id, parameters)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


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
    review_limit = int(os.environ.get("PA_REVIEW_LIMIT", "2"))
    if not 1 <= review_limit <= 10:
        raise RuntimeError("PA_REVIEW_LIMIT must be between 1 and 10")
    origin = f"http://127.0.0.1:{port}"
    hosts = {"127.0.0.1", f"127.0.0.1:{port}"}
    manager = TaskManager(Path(os.environ.get("PA_DB_PATH", "data/database/pa.sqlite3")),
                          review_limit=review_limit)
    model = ResponsesModel.from_environment()

    @asynccontextmanager
    async def lifespan(_app):
        manager.migrate()
        stop = asyncio.Event()
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(workers):
                    group.create_task(run_worker(
                        manager, stop, step_seconds=step_seconds, lease_seconds=lease_seconds, model=model,
                    ))
                try:
                    yield
                finally:
                    stop.set()
        finally:
            if model is not None:
                await model.close()

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

    @app.post("/chat")
    async def chat(body: ChatInput):
        if model is None:
            raise HTTPException(503, "Model adapter is not configured")
        if body.mode == "task" and (
            body.project_id is None or not await asyncio.to_thread(manager.project_exists, body.project_id)
        ):
            raise HTTPException(404, "Project not found")
        instructions = "Answer the user's question. You have no tools or repository access."
        if body.mode == "task":
            instructions = (
                "Summarize the request as an editable draft. Return only JSON with title and goal. "
                "Preserve the user's requested scope. Do not select a project or invent files or actions."
            )
        try:
            answer = await model.ask(instructions, body.message)
            if body.mode == "answer":
                return {"kind": "answer", "answer": answer}
            draft = ModelDraft.model_validate_json(answer.removeprefix("```json").removesuffix("```").strip())
        except ModelError as error:
            raise HTTPException(502, str(error)) from error
        except ValidationError as error:
            raise HTTPException(502, "Chat model returned an invalid draft") from error
        return {"kind": "draft", "draft": draft.model_dump() | {
            "project_id": body.project_id, "submission_key": uuid4().hex, "model_plan": True,
        }}

    @app.post("/chat/drafts")
    def create_draft(body: DraftInput):
        if not manager.project_exists(body.project_id):
            raise HTTPException(404, "Project not found")
        plan = validated_plan(body.project_id, body.plan_parameters)
        return {"draft": body.model_dump() | {"submission_key": uuid4().hex}, "plan": plan}

    @app.post("/tasks", status_code=201)
    def submit_task(body: TaskInput, response: Response):
        if body.model_plan and body.plan_parameters:
            raise HTTPException(422, "Choose a submitted plan or Main planning")
        plan = validated_plan(body.project_id, body.plan_parameters) if body.plan_parameters else None
        try:
            task, created = manager.submit(body.project_id, body.submission_key, body.title, body.goal,
                                           plan=plan, model_plan=body.model_plan)
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

    @app.get("/tasks/{task_id}/summary")
    def task_summary(task_id: str):
        try:
            return manager.summary(task_id)
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
