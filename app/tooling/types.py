"""Strict contracts shared by tools, tasks, and the web API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

T = TypeVar("T")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ArtifactRef(StrictModel):
    id: StrictStr
    task_id: StrictStr
    service_id: StrictStr | None = None
    kind: StrictStr
    media_type: StrictStr
    size_bytes: StrictInt = Field(ge=0)
    mutable: StrictBool = False
    sha256: StrictStr | None = None
    created_at: StrictStr = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ToolError(StrictModel):
    code: StrictStr
    message: StrictStr
    retryable: StrictBool = False


class ToolResult[T](StrictModel):
    ok: StrictBool
    data: T | None = None
    error: ToolError | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    duration_ms: StrictInt = Field(ge=0)


class ExecutionLimits(StrictModel):
    max_model_requests: StrictInt = Field(default=40, ge=1, le=1000)
    command_timeout_seconds: StrictInt = Field(default=120, ge=1, le=3600)
    output_limit_bytes: StrictInt = Field(default=1_048_576, ge=256, le=10_000_000)


class ToolContext:
    """Backend-owned execution scope; model-supplied arguments cannot alter it."""

    def __init__(
        self,
        *,
        task_id: str,
        project_id: str,
        workspace: Path,
        artifact_dir: Path,
        phase: str = "implementation",
        attempt: int = 0,
        limits: ExecutionLimits | None = None,
        protected_paths: set[str] | None = None,
        emit: Callable[..., None] | None = None,
        redact: Callable[[str], str] | None = None,
        record_change: Callable[[dict[str, Any]], None] | None = None,
        services: dict[str, Any] | None = None,
    ) -> None:
        self.task_id = task_id
        self.project_id = project_id
        self.workspace = workspace.resolve()
        self.artifact_dir = artifact_dir.resolve()
        self.phase = phase
        self.attempt = attempt
        self.limits = limits or ExecutionLimits()
        self.protected_paths = {path.casefold() for path in (protected_paths or set())}
        self.emit = emit or (lambda **_: None)
        self.redact = redact or (lambda text: text)
        self.record_change = record_change or (lambda _change: None)
        self.services = services or {}
