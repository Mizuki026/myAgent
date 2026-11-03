"""Application wiring and loopback-only HTTP security."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.execution import ProcessManager
from app.execution.process_manager import ExecutionFailure
from app.git_ops.service import GitFailure, GitSnapshot
from app.models_v2 import TaskSnapshot
from app.storage import InstanceLock, InstanceLockError, StateStore
from app.storage.state_store import StoreError
from app.tasks import ProjectService, TaskService
from app.tasks.projects import ProjectServiceError
from app.tasks.runtime import AgentRuntime
from app.tasks.service import TaskServiceError
from app.tooling import ExecutionLimits, ToolContext
from app.web.api import ApiFailure, bind_api, error_status, exception_code


def create_app(
    *,
    data_dir: Path | None = None,
    env_file: Path | None = None,
    runtime=None,
    process_manager: ProcessManager | None = None,
) -> FastAPI:
    app_home = data_dir or _application_home()
    app_home = Path(app_home).resolve()
    app_env = env_file or (Path.cwd() / ".env")
    instance_lock = InstanceLock(app_home)
    store = StateStore(app_home)
    store.mark_unfinished_interrupted()
    manager = process_manager or ProcessManager()
    projects = ProjectService(store)
    task_runtime = runtime or AgentRuntime(Path(app_env), store, manager)
    tasks = TaskService(store, projects, app_home, task_runtime)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        tasks.shutdown()
        manager.shutdown()
        store.close()
        instance_lock.close()

    app = FastAPI(title="My Agent", version="0.2.0", lifespan=lifespan)
    app.state.data_dir = app_home
    app.state.store = store
    app.state.process_manager = manager
    app.state.project_service = projects
    app.state.task_service = tasks
    app.state.make_context = lambda task, profile, snapshot, changes: _make_context(
        task, profile, app_home, snapshot, changes
    )

    @app.middleware("http")
    async def restrict_to_loopback(request: Request, call_next):
        host_header = request.headers.get("host", "")
        host = urlsplit(f"//{host_header}").hostname
        host = host.casefold() if host else ""
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "LOCAL_HOST_REQUIRED",
                        "message": "Only local requests are accepted.",
                        "retryable": False,
                    }
                },
            )
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
            "testserver",
        }:
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "ORIGIN_NOT_ALLOWED",
                        "message": "Request origin is not allowed.",
                        "retryable": False,
                    }
                },
            )
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            content_type = request.headers.get("content-type", "")
            if not content_type.lower().startswith("application/json"):
                return JSONResponse(
                    status_code=415,
                    content={
                        "error": {
                            "code": "JSON_REQUIRED",
                            "message": "Modifying requests require JSON.",
                            "retryable": False,
                        }
                    },
                )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_ARGUMENTS",
                    "message": "Request fields are invalid.",
                    "retryable": True,
                }
            },
        )

    @app.exception_handler(Exception)
    async def service_error(_request: Request, error: Exception):
        code, message, retryable = exception_code(error)
        status = error_status(code)
        if code == "INTERNAL_ERROR":
            message = "An internal error occurred."
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message[:2000], "retryable": retryable}},
        )

    for error_type in (
        ApiFailure,
        ProjectServiceError,
        TaskServiceError,
        StoreError,
        GitFailure,
        ExecutionFailure,
        InstanceLockError,
    ):
        app.add_exception_handler(error_type, service_error)

    api_router = APIRouter(prefix="/api")
    bind_api(api_router)
    app.include_router(api_router)
    frontend_dist = _frontend_distribution()
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app


def _frontend_distribution() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _application_home() -> Path:
    configured = os.environ.get("MY_AGENT_HOME")
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        local = str(Path.home() / "AppData" / "Local")
    return Path(local) / "MyAgent"


def _make_context(
    task: TaskSnapshot,
    profile,
    data_dir: Path,
    snapshot: GitSnapshot | None,
    changes: list[dict[str, object]],
) -> ToolContext:
    return ToolContext(
        task_id=task.id,
        project_id=profile.id,
        workspace=Path(profile.workspace),
        artifact_dir=data_dir / "runs" / task.id / "artifacts",
        limits=ExecutionLimits(
            max_model_requests=profile.limits.max_model_requests,
            command_timeout_seconds=profile.limits.command_timeout_seconds,
            output_limit_bytes=profile.limits.output_limit_bytes,
        ),
        services={"git_snapshot": snapshot, "task_changes": changes},
    )
