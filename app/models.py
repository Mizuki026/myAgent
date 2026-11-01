"""Validated input and report models for the JSON CLI contract."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelConfig(StrictModel):
    base_url: StrictStr
    name: StrictStr
    api_key_env: StrictStr
    stream: StrictBool


class Limits(StrictModel):
    max_implementation_attempts: StrictInt = Field(ge=0, le=3)
    max_model_requests: StrictInt = Field(ge=1, le=1000)
    command_timeout_seconds: StrictInt = Field(ge=1, le=3600)
    output_limit_bytes: StrictInt = Field(ge=256, le=10_000_000)


class CheckSpec(StrictModel):
    name: Literal["regression", "acceptance"]
    argv: list[StrictStr] = Field(min_length=1)
    report_format: Literal["junit_xml"]
    expected_test_ids: list[StrictStr] = Field(min_length=1)


class AgentRequest(StrictModel):
    schema_version: Literal[1]
    operation: Literal["run", "resume"]
    task_id: StrictStr = Field(min_length=1, max_length=200)
    workspace: StrictStr = Field(min_length=1)
    state_dir: StrictStr = Field(min_length=1)
    env_file: StrictStr = Field(min_length=1)
    requirement: StrictStr = Field(min_length=1)
    model: ModelConfig
    limits: Limits
    checks: list[CheckSpec] = Field(min_length=2)
    acceptance_paths: list[StrictStr] = Field(min_length=1)
    pause_after_attempt: StrictInt | None = Field(default=None, ge=1, le=3)


class Event(StrictModel):
    seq: StrictInt
    type: StrictStr
    tool: StrictStr | None = None
    path: StrictStr | None = None
    code: StrictStr | None = None
    reason: StrictStr | None = None
    sha256: StrictStr | None = None
    attempt: StrictInt | None = None


class CheckResult(StrictModel):
    name: StrictStr
    attempt: StrictInt
    argv: list[StrictStr]
    cwd: StrictStr
    returncode: StrictInt
    timed_out: StrictBool
    output_truncated: StrictBool
    report_path: StrictStr
    stdout_path: StrictStr
    stderr_path: StrictStr


class Artifact(StrictModel):
    kind: Literal["diff", "events", "verification"]
    path: StrictStr


class AgentReport(StrictModel):
    schema_version: Literal[1]
    task_id: StrictStr
    status: Literal["succeeded", "no_changes", "failed", "needs_clarification", "paused"]
    implementation_attempts: StrictInt = Field(ge=0, le=3)
    summary: StrictStr
    questions: list[StrictStr] = Field(default_factory=list)
    commit: StrictStr | None = None
    events: list[Event]
    checks: list[CheckResult]
    artifacts: list[Artifact]
