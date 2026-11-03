"""HTTP and SSE contracts for projects, tasks, artifacts, and services."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field, StrictInt, StrictStr

from app.execution.process_manager import ExecutionFailure, ProcessManager
from app.git_ops import GitService
from app.git_ops.service import GitSnapshot
from app.models_v2 import (
    ArtifactContent,
    DiffResult,
    ListResponse,
    ProjectInput,
    ProjectInspection,
    ProjectProfile,
    TaskEvent,
    TaskSnapshot,
)
from app.tasks.service import TaskService
from app.tooling.types import StrictModel


class InspectProjectRequest(StrictModel):
    workspace: StrictStr = Field(min_length=1)


class CreateTaskRequest(StrictModel):
    project_id: StrictStr = Field(min_length=1)
    requirement: StrictStr = Field(min_length=1, max_length=20_000)


class ClarificationRequest(StrictModel):
    answer: StrictStr = Field(min_length=1, max_length=20_000)


class EmptyRequest(StrictModel):
    pass


class PatchProjectRequest(StrictModel):
    expected_revision: StrictInt = Field(ge=1)
    name: StrictStr | None = None
    workspace: StrictStr | None = None
    python_executable: StrictStr | None = None
    pytest_args: list[StrictStr] | None = None
    extra_checks: list[Any] | None = None
    limits: Any = None


router = APIRouter(prefix="/api")


def bind_routes(router: APIRouter) -> None:
    @router.post("/projects/inspect", response_model=ProjectInspection)
    def inspect_project(body: InspectProjectRequest, request: Request) -> ProjectInspection:
        return request.app.state.project_service.inspect_project(body.workspace)

    @router.get("/projects", response_model=ListResponse[ProjectProfile])
    def list_projects(
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> ListResponse[ProjectProfile]:
        items, total = request.app.state.project_service.list_projects(offset, limit)
        return ListResponse(items=items, total=total, offset=offset, limit=limit)

    @router.post("/projects", response_model=ProjectProfile, status_code=201)
    def create_project(body: ProjectInput, request: Request) -> ProjectProfile:
        return request.app.state.project_service.save_project(body)

    @router.get("/projects/{project_id}", response_model=ProjectProfile)
    def get_project(project_id: str, request: Request) -> ProjectProfile:
        return request.app.state.project_service.get_project(project_id)

    @router.patch("/projects/{project_id}", response_model=ProjectProfile)
    def patch_project(
        project_id: str, body: PatchProjectRequest, request: Request
    ) -> ProjectProfile:
        fields = body.model_dump(exclude_unset=True)
        expected_revision = fields.pop("expected_revision")
        return request.app.state.project_service.patch_project(
            project_id, fields, expected_revision
        )

    @router.post("/tasks", response_model=TaskSnapshot, status_code=202)
    def create_task(body: CreateTaskRequest, request: Request) -> TaskSnapshot:
        return request.app.state.task_service.start_task(body.project_id, body.requirement)

    @router.get("/tasks", response_model=ListResponse[TaskSnapshot])
    def list_tasks(
        request: Request,
        project_id: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> ListResponse[TaskSnapshot]:
        items, total = request.app.state.task_service.list_tasks(project_id, offset, limit)
        return ListResponse(items=items, total=total, offset=offset, limit=limit)

    @router.get("/tasks/{task_id}", response_model=TaskSnapshot)
    def get_task(task_id: str, request: Request) -> TaskSnapshot:
        return request.app.state.task_service.get_task(task_id)

    @router.post("/tasks/{task_id}/answers", response_model=TaskSnapshot, status_code=202)
    def answer_task(task_id: str, body: ClarificationRequest, request: Request) -> TaskSnapshot:
        return request.app.state.task_service.answer_clarification(task_id, body.answer)

    @router.post("/tasks/{task_id}/dismiss", response_model=TaskSnapshot)
    def dismiss_task(
        task_id: str, request: Request, _body: Annotated[EmptyRequest, Body()]
    ) -> TaskSnapshot:
        return request.app.state.task_service.dismiss_waiting_task(task_id)

    @router.get("/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        task_service: TaskService = request.app.state.task_service
        snapshot = task_service.get_task(task_id)
        cursor = after
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError as error:
                raise ApiFailure("INVALID_CURSOR", "Last-Event-ID must be an integer.") from error
        if cursor < 0 or cursor > snapshot.last_event_seq:
            raise ApiFailure("INVALID_CURSOR", "Event cursor is outside the task event range.")

        async def stream():
            nonlocal cursor
            last_heartbeat = asyncio.get_running_loop().time()
            while not await request.is_disconnected():
                events = request.app.state.store.events_after(task_id, cursor, 100)
                for event in events:
                    cursor = event.seq
                    yield _sse(event)
                current = task_service.get_task(task_id)
                if not events and current.status not in {"running", "needs_clarification"}:
                    break
                now = asyncio.get_running_loop().time()
                if now - last_heartbeat >= 15:
                    yield ": keep-alive\n\n"
                    last_heartbeat = now
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/tasks/{task_id}/diff", response_model=DiffResult)
    def task_diff(
        task_id: str,
        request: Request,
        scope: str = Query(default="task", pattern="^(task|workspace)$"),
    ) -> DiffResult:
        task = request.app.state.task_service.get_task(task_id)
        profile = request.app.state.project_service.get_project(task.project_id)
        events = request.app.state.store.events_after(task_id, 0, 10_000)
        snapshot = _snapshot_from_events(events, profile.workspace)
        changes = [event.payload for event in events if event.kind == "file_changed"]
        context = request.app.state.make_context(task, profile, snapshot, changes)
        result = GitService(Path(profile.workspace)).diff(scope, context)
        return DiffResult.model_validate(result)

    @router.get("/tasks/{task_id}/artifacts", response_model=ListResponse)
    def task_artifacts(
        task_id: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> ListResponse[Any]:
        request.app.state.task_service.get_task(task_id)
        items, total = request.app.state.store.list_artifacts(task_id, offset, limit)
        return ListResponse(items=items, total=total, offset=offset, limit=limit)

    @router.get("/artifacts/{artifact_id}", response_model=ArtifactContent)
    def read_artifact(
        artifact_id: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=65_536, ge=1, le=65_536),
    ) -> ArtifactContent:
        return request.app.state.store.read_artifact(artifact_id, offset, limit)

    @router.get("/services", response_model=ListResponse)
    def list_services(
        request: Request,
        project_id: str | None = None,
        task_id: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> ListResponse[Any]:
        if project_id:
            request.app.state.project_service.get_project(project_id)
        if task_id:
            task = request.app.state.task_service.get_task(task_id)
            if project_id and task.project_id != project_id:
                raise ApiFailure("INVALID_ARGUMENTS", "task_id does not belong to project_id.")
        items, total = request.app.state.store.list_services(project_id, task_id, offset, limit)
        live = {
            item.id: item
            for item in request.app.state.process_manager.list_all_services()
            if (not project_id or item.project_id == project_id)
            and (not task_id or item.task_id == task_id)
        }
        items = [live.get(item.id, item) for item in items]
        return ListResponse(items=items, total=total, offset=offset, limit=limit)

    @router.get("/services/{service_id}")
    def get_service(service_id: str, request: Request) -> dict[str, Any]:
        service = request.app.state.store.get_service(service_id)
        try:
            profile = request.app.state.project_service.get_project(service.project_id)
            task = request.app.state.task_service.get_task(service.task_id)
            context = request.app.state.make_context(task, profile, None, [])
            context.services["save_service"] = request.app.state.store.save_service
            service = request.app.state.process_manager.get_service(service_id, context)
        except ExecutionFailure:
            pass
        return service.model_dump(mode="json")

    @router.post("/services/{service_id}/stop")
    def stop_service(
        service_id: str,
        request: Request,
        _body: Annotated[EmptyRequest, Body()],
    ) -> dict[str, Any]:
        manager: ProcessManager = request.app.state.process_manager
        service = request.app.state.store.get_service(service_id)
        profile = request.app.state.project_service.get_project(service.project_id)
        task = request.app.state.task_service.get_task(service.task_id)
        context = request.app.state.make_context(task, profile, None, [])
        context.services["save_service"] = request.app.state.store.save_service
        return manager.stop_service(service_id, context).model_dump(mode="json")


class ApiFailure(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def error_status(code: str) -> int:
    if code in {
        "OUTSIDE_WORKSPACE",
        "PROTECTED_PATH",
        "PROTECTED_ACCEPTANCE",
        "CREDENTIAL_PROTECTION",
    }:
        return 403
    if code.endswith("_NOT_FOUND") or code in {
        "FILE_NOT_FOUND",
        "ARTIFACT_NOT_FOUND",
        "TASK_NOT_FOUND",
        "PROJECT_NOT_FOUND",
        "SERVICE_NOT_FOUND",
    }:
        return 404
    if code in {
        "TASK_BUSY",
        "INVALID_TASK_STATE",
        "REVISION_CONFLICT",
        "PROJECT_CONFLICT",
        "PROJECT_IN_USE",
        "STALE_FILE",
        "NON_UNIQUE_MATCH",
        "WORKSPACE_CHANGED",
        "USER_CHANGE_CONFLICT",
        "GIT_BASE_CHANGED",
        "GIT_INDEX_CHANGED",
        "DIRECTORY_NOT_EMPTY",
    }:
        return 409
    if code in {"INVALID_CURSOR", "INVALID_OFFSET"}:
        return 400
    if code == "JSON_REQUIRED":
        return 415
    if code in {"INVALID_ARGUMENTS", "ENVIRONMENT_NOT_READY", "MODEL_CONFIG", "PROGRAM_NOT_FOUND"}:
        return 422
    return 500


def exception_code(error: Exception) -> tuple[str, str, bool]:
    code = getattr(error, "code", "INTERNAL_ERROR")
    retryable = bool(getattr(error, "retryable", False))
    message = str(error) if code != "INTERNAL_ERROR" else "An internal error occurred."
    return code, message, retryable


def _sse(event: TaskEvent) -> str:
    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {data}\n\n"


def _snapshot_from_events(events: list[TaskEvent], workspace: str) -> GitSnapshot:
    for event in events:
        if event.kind == "git_snapshot":
            try:
                return GitSnapshot.model_validate(event.payload["snapshot"])
            except Exception:
                break
    return GitService(Path(workspace)).snapshot()


def bind_api(router: APIRouter) -> None:
    bind_routes(router)
