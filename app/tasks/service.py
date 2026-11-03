"""Single-active-task lifecycle and asynchronous task execution."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from app.models_v2 import ProjectProfile, TaskSnapshot
from app.storage.state_store import StateStore, StoreError
from app.tasks.projects import ProjectService
from app.tooling.types import ToolError


class TaskServiceError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class TaskRunConfig:
    task: TaskSnapshot
    project: ProjectProfile
    task_dir: Path


@dataclass(frozen=True)
class TaskRunOutcome:
    status: Literal["succeeded", "failed", "no_changes", "needs_clarification"]
    summary: str
    attempt: int = 0
    model_requests: int = 0
    questions: tuple[str, ...] = ()
    commit: str | None = None
    error: ToolError | None = None


class TaskRuntime(Protocol):
    def validate_configuration(self) -> None: ...

    def start(self, config: TaskRunConfig, emit: Any) -> TaskRunOutcome: ...

    def continue_with_answer(
        self, config: TaskRunConfig, answer: str, emit: Any
    ) -> TaskRunOutcome: ...


class TaskService:
    def __init__(
        self,
        store: StateStore,
        projects: ProjectService,
        data_dir: Path,
        runtime: TaskRuntime,
    ) -> None:
        self.store = store
        self.projects = projects
        self.data_dir = data_dir.resolve()
        self.runtime = runtime
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="my-agent-task")
        self._lock = threading.RLock()
        self._active_task_id: str | None = None
        self._execution_active = False
        self._accepting = True
        self._output_event_count: dict[str, int] = {}
        self._output_overflow_notified: set[str] = set()
        self.projects.is_project_active = self.is_project_active
        active = self.store.list_tasks(limit=100)[0]
        for task in active:
            if task.status in {"running", "needs_clarification"}:
                self._active_task_id = task.id
                break

    def start_task(self, project_id: str, requirement: str) -> TaskSnapshot:
        normalized = requirement.strip()
        if not normalized or len(normalized) > 20_000:
            raise TaskServiceError(
                "INVALID_ARGUMENTS", "requirement must contain 1 to 20000 characters."
            )
        with self._lock:
            if not self._accepting:
                raise TaskServiceError("APPLICATION_CLOSING", "The application is shutting down.")
            if self._active_task_id is not None:
                raise TaskServiceError("TASK_BUSY", "Another task is active.")
            profile = self.projects.get_project(project_id)
            inspection = self.projects.inspect_project(profile.workspace)
            if (
                not inspection.ready
                or profile.python_executable not in inspection.candidate_interpreters
            ):
                raise TaskServiceError(
                    "ENVIRONMENT_NOT_READY", "Project environment is no longer ready."
                )
            try:
                self.runtime.validate_configuration()
            except TaskServiceError:
                raise
            except Exception as error:
                raise TaskServiceError("MODEL_CONFIG", str(error)) from error
            now = _now()
            task = TaskSnapshot(
                id=str(uuid.uuid4()),
                project_id=project_id,
                requirement=normalized,
                status="running",
                phase="preflight",
                created_at=now,
                updated_at=now,
            )
            task_dir = self.data_dir / "runs" / task.id
            task_dir.mkdir(parents=True, exist_ok=False)
            config = TaskRunConfig(task=task, project=profile, task_dir=task_dir)
            try:
                self.store.create_task(task, {"project": profile.model_dump(mode="json")})
            except StoreError as error:
                task_dir.rmdir()
                raise TaskServiceError(error.code, str(error)) from error
            self._active_task_id = task.id
            self._execution_active = True
            try:
                self._executor.submit(self._run, config, False, None)
            except RuntimeError as error:
                failed = task.model_copy(
                    update={
                        "status": "failed",
                        "phase": "finished",
                        "summary": "无法启动后台任务。",
                        "updated_at": _now(),
                    }
                )
                self.store.transition_task(
                    failed, "task_status_changed", {"status": "failed"}, {"running"}
                )
                self._active_task_id = None
                self._execution_active = False
                raise TaskServiceError(
                    "EXECUTION_FAILED", "Unable to schedule task execution."
                ) from error
            return self.store.get_task(task.id)

    def answer_clarification(self, task_id: str, answer: str) -> TaskSnapshot:
        normalized = answer.strip()
        if not normalized or len(normalized) > 20_000:
            raise TaskServiceError(
                "INVALID_ARGUMENTS", "answer must contain 1 to 20000 characters."
            )
        with self._lock:
            task = self.get_task(task_id)
            if task.status != "needs_clarification" or self._active_task_id != task_id:
                raise TaskServiceError(
                    "INVALID_TASK_STATE", "Task is not waiting for clarification."
                )
            if self._execution_active:
                raise TaskServiceError("INVALID_TASK_STATE", "Task is already being continued.")
            profile = self.projects.get_project(task.project_id)
            updated = task.model_copy(
                update={
                    "status": "running",
                    "questions": [],
                    "summary": "继续处理补充说明。",
                    "updated_at": _now(),
                }
            )
            try:
                self.store.transition_task(
                    updated,
                    "task_status_changed",
                    {"status": "running"},
                    {"needs_clarification"},
                )
            except StoreError as error:
                raise TaskServiceError(error.code, str(error)) from error
            self.store.append_event(task_id, "clarification_answered", {"received": True})
            self._execution_active = True
            config = TaskRunConfig(
                task=updated,
                project=profile,
                task_dir=self.data_dir / "runs" / task_id,
            )
            self._executor.submit(self._run, config, True, normalized)
            return self.store.get_task(task_id)

    def dismiss_waiting_task(self, task_id: str) -> TaskSnapshot:
        with self._lock:
            task = self.get_task(task_id)
            if task.status != "needs_clarification" or self._active_task_id != task_id:
                raise TaskServiceError(
                    "INVALID_TASK_STATE", "Only a waiting task can be dismissed."
                )
            updated = task.model_copy(
                update={
                    "status": "dismissed",
                    "phase": "finished",
                    "summary": "已放弃等待补充说明。",
                    "updated_at": _now(),
                }
            )
            try:
                self.store.transition_task(
                    updated,
                    "task_status_changed",
                    {"status": "dismissed"},
                    {"needs_clarification"},
                )
            except StoreError as error:
                raise TaskServiceError(error.code, str(error)) from error
            self._active_task_id = None
            return self.store.get_task(task_id)

    def get_task(self, task_id: str) -> TaskSnapshot:
        try:
            return self.store.get_task(task_id)
        except StoreError as error:
            raise TaskServiceError(error.code, str(error)) from error

    def list_tasks(
        self, project_id: str | None = None, offset: int = 0, limit: int = 20
    ) -> tuple[list[TaskSnapshot], int]:
        if offset < 0 or not 1 <= limit <= 100:
            raise TaskServiceError("INVALID_ARGUMENTS", "Invalid pagination range.")
        if project_id:
            self.projects.get_project(project_id)
        return self.store.list_tasks(project_id, offset, limit)

    def is_project_active(self, project_id: str) -> bool:
        with self._lock:
            if self._active_task_id is None:
                return False
            try:
                return self.store.get_task(self._active_task_id).project_id == project_id
            except StoreError:
                return False

    def shutdown(self) -> None:
        with self._lock:
            self._accepting = False
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run(self, config: TaskRunConfig, continuing: bool, answer: str | None) -> None:
        try:
            if continuing:
                outcome = self.runtime.continue_with_answer(
                    config, answer or "", self._event_callback
                )
            else:
                outcome = self.runtime.start(config, self._event_callback)
        except Exception as error:
            outcome = TaskRunOutcome(
                status="failed",
                summary="任务执行失败。",
                error=ToolError(
                    code=getattr(error, "code", "INTERNAL_ERROR"), message=str(error)[:2000]
                ),
            )
        with self._lock:
            current = self.store.get_task(config.task.id)
            updated = current.model_copy(
                update={
                    "status": outcome.status,
                    "phase": "finished"
                    if outcome.status != "needs_clarification"
                    else current.phase,
                    "attempt": max(current.attempt, outcome.attempt),
                    "model_requests": max(current.model_requests, outcome.model_requests),
                    "summary": outcome.summary,
                    "questions": list(outcome.questions),
                    "commit": outcome.commit,
                    "error": outcome.error,
                    "updated_at": _now(),
                }
            )
            self.store.transition_task(
                updated,
                "task_status_changed",
                {
                    "status": outcome.status,
                    "summary": outcome.summary,
                    "error": outcome.error.model_dump(mode="json") if outcome.error else None,
                },
                {"running"},
            )
            self._execution_active = False
            if outcome.status != "needs_clarification":
                self._active_task_id = None

    def _event_callback(self, event: dict[str, Any]) -> None:
        task_id = event.get("task_id") or self._active_task_id
        if not task_id:
            return
        kind = str(event.get("type", event.get("kind", "event")))
        if kind == "command_output":
            count = self._output_event_count.get(task_id, 0)
            if count >= 10_000:
                if task_id in self._output_overflow_notified:
                    return
                self._output_overflow_notified.add(task_id)
                kind = "command_output_merged"
                payload = {"message": "Further command output notifications were merged."}
            else:
                self._output_event_count[task_id] = count + 1
                payload = {
                    key: value
                    for key, value in event.items()
                    if key not in {"seq", "type", "task_id"}
                }
        else:
            payload = {
                key: value for key, value in event.items() if key not in {"seq", "type", "task_id"}
            }
        payload = _sanitize_event_payload(payload)
        allowed_phases = {
            "preflight",
            "baseline",
            "preparation",
            "implementation",
            "verification",
            "delivery",
            "finished",
        }
        task = self.store.get_task(task_id)
        changes: dict[str, Any] = {}
        phase = event.get("phase")
        if phase in allowed_phases:
            changes["phase"] = phase
        attempt = event.get("attempt")
        if type(attempt) is int and 0 <= attempt <= 3:
            changes["attempt"] = max(task.attempt, attempt)
        if kind == "model_request":
            changes["model_requests"] = task.model_requests + 1
        if changes:
            task = task.model_copy(update={**changes, "updated_at": _now()})
            self.store.save_task(task)
        self.store.append_event(task_id, kind, payload)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sanitize_event_payload(value: Any) -> Any:
    hidden = {
        "cwd",
        "report_path",
        "stdout_path",
        "stderr_path",
        "env_file",
        "state_dir",
        "workspace",
        "process_id",
        "pid",
        "api_key",
        "authorization",
    }
    if isinstance(value, dict):
        return {
            key: _sanitize_event_payload(item)
            for key, item in value.items()
            if key.casefold() not in hidden
        }
    if isinstance(value, list):
        return [_sanitize_event_payload(item) for item in value]
    if (
        isinstance(value, str)
        and len(value) > 2
        and (value.startswith(("C:\\", "D:\\", "E:\\", "/")) or "AppData\\Local\\MyAgent" in value)
    ):
        return "[internal path]"
    return value
