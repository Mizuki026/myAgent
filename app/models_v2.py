"""Public, strict data contracts for the phase-two web application."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt, StrictStr

from app.execution.process_manager import CommandSpec, ProcessArtifact, ServiceSnapshot
from app.tooling.types import ArtifactRef, ExecutionLimits, StrictModel, ToolError


class ProjectInput(StrictModel):
    name: StrictStr = Field(min_length=1, max_length=100)
    workspace: StrictStr = Field(min_length=1)
    python_executable: StrictStr = Field(min_length=1)
    pytest_args: list[StrictStr] = Field(default_factory=list, max_length=100)
    extra_checks: list[CommandSpec] = Field(default_factory=list, max_length=20)
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)


class ProjectProfile(ProjectInput):
    id: StrictStr
    revision: StrictInt = Field(ge=1)
    created_at: StrictStr
    updated_at: StrictStr


class ProjectInspection(StrictModel):
    workspace: StrictStr
    is_git_repository: bool
    head: StrictStr | None = None
    candidate_interpreters: list[StrictStr]
    selected_interpreter: StrictStr | None = None
    pytest_available: bool
    suggested_pytest_args: list[StrictStr] = Field(default_factory=list)
    ready: bool
    issues: list[ToolError] = Field(default_factory=list)


class TaskSnapshot(StrictModel):
    id: StrictStr
    project_id: StrictStr
    requirement: StrictStr
    status: Literal[
        "running",
        "needs_clarification",
        "succeeded",
        "failed",
        "no_changes",
        "dismissed",
        "interrupted",
    ]
    phase: Literal[
        "preflight",
        "baseline",
        "preparation",
        "implementation",
        "verification",
        "delivery",
        "finished",
    ] = "preflight"
    attempt: StrictInt = Field(default=0, ge=0, le=3)
    model_requests: StrictInt = Field(default=0, ge=0)
    summary: StrictStr = ""
    questions: list[StrictStr] = Field(default_factory=list)
    commit: StrictStr | None = None
    last_event_seq: StrictInt = Field(default=0, ge=0)
    created_at: StrictStr
    updated_at: StrictStr
    error: ToolError | None = None


class TaskEvent(StrictModel):
    task_id: StrictStr
    seq: StrictInt = Field(ge=1)
    kind: StrictStr
    timestamp: StrictStr
    payload: dict[str, object] = Field(default_factory=dict)


class ArtifactContent(StrictModel):
    artifact: ArtifactRef
    offset: StrictInt = Field(ge=0)
    next_offset: StrictInt = Field(ge=0)
    size_bytes: StrictInt = Field(ge=0)
    text: StrictStr
    eof: bool
    truncated: bool


class ListResponse[T](StrictModel):
    items: list[T]
    total: StrictInt = Field(ge=0)
    offset: StrictInt = Field(ge=0)
    limit: StrictInt = Field(ge=1, le=100)


class DiffFile(StrictModel):
    path: StrictStr
    source: StrictStr | None = None
    change: Literal["added", "modified", "deleted", "renamed", "unknown"]


class DiffResult(StrictModel):
    scope: Literal["task", "workspace"]
    text: StrictStr
    files: list[DiffFile]
    truncated: bool
    artifact: ArtifactRef | None = None


__all__ = [
    "ArtifactContent",
    "DiffFile",
    "DiffResult",
    "ListResponse",
    "ProcessArtifact",
    "ProjectInput",
    "ProjectInspection",
    "ProjectProfile",
    "ServiceSnapshot",
    "TaskEvent",
    "TaskSnapshot",
]
