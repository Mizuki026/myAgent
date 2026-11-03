"""Web-task adapter for the existing verified coding workflow."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app.execution import ProcessManager
from app.git_ops import GitService
from app.git_ops.service import GitSnapshot
from app.models import AgentRequest, CheckSpec, Limits, ModelConfig
from app.storage import StateStore
from app.tasks.service import TaskRunConfig, TaskRunOutcome
from app.tooling import ExecutionLimits, ToolContext, ToolRegistry
from app.tooling.types import ArtifactRef, ToolError
from app.workflow import AgentWorkflow, WorkflowFailure
from app.workspaces import WorkspaceService


class AgentRuntime:
    def __init__(
        self,
        env_file: Path,
        store: StateStore | None = None,
        process_manager: ProcessManager | None = None,
    ) -> None:
        self.env_file = env_file.resolve()
        self.store = store
        self.process_manager = process_manager or ProcessManager()

    def validate_configuration(self) -> None:
        values = self._configuration()
        api_key_env = "LLM_API_KEY"
        if not (os.environ.get(api_key_env) or values.get(api_key_env)):
            raise WorkflowFailure(
                "MODEL_CONFIG", f"Missing {api_key_env} in the application environment."
            )
        if not values.get("LLM_BASE_URL") and not os.environ.get("LLM_BASE_URL"):
            raise WorkflowFailure("MODEL_CONFIG", "LLM_BASE_URL is not configured.")
        if not values.get("LLM_MODEL_ID") and not os.environ.get("LLM_MODEL_ID"):
            raise WorkflowFailure("MODEL_CONFIG", "LLM_MODEL_ID is not configured.")

    def start(self, config: TaskRunConfig, emit: Any) -> TaskRunOutcome:
        request = self._request(config, operation="run")
        workflow, _git = self._workflow(request, config, emit)
        try:
            report = workflow.run()
            self._register_report_artifacts(config, report)
            state = workflow.state
            return TaskRunOutcome(
                status=report.status
                if report.status in {"succeeded", "failed", "no_changes", "needs_clarification"}
                else "failed",
                summary=report.summary,
                attempt=report.implementation_attempts,
                model_requests=state.get("model_requests", 0),
                questions=tuple(report.questions),
                commit=report.commit,
                error=self._error_from_report(report) if report.status == "failed" else None,
            )
        finally:
            workflow.store.close()

    def continue_with_answer(self, config: TaskRunConfig, answer: str, emit: Any) -> TaskRunOutcome:
        request = self._request(config, operation="resume")
        workflow, _git = self._workflow(request, config, emit)
        try:
            report = workflow.continue_with_answer(answer)
            self._register_report_artifacts(config, report)
            state = workflow.state
            return TaskRunOutcome(
                status=report.status
                if report.status in {"succeeded", "failed", "no_changes", "needs_clarification"}
                else "failed",
                summary=report.summary,
                attempt=report.implementation_attempts,
                model_requests=state.get("model_requests", 0),
                questions=tuple(report.questions),
                commit=report.commit,
                error=self._error_from_report(report) if report.status == "failed" else None,
            )
        finally:
            workflow.store.close()

    def _request(self, config: TaskRunConfig, operation: str) -> AgentRequest:
        self.validate_configuration()
        project = config.project
        acceptance_relative = f"tests/agent_acceptance/{config.task.id}/test_requirement.py"
        expected_regression = self.collect_expected_test_ids(
            project.python_executable,
            Path(project.workspace),
            project.pytest_args,
        )
        acceptance_id = f"tests.agent_acceptance.{config.task.id}.test_requirement.test_requirement"
        regression_args = [
            project.python_executable,
            "-m",
            "pytest",
            "-q",
            *project.pytest_args,
            "--junitxml",
            "{report}",
        ]
        acceptance_args = [
            project.python_executable,
            "-m",
            "pytest",
            "-q",
            acceptance_relative,
            "--junitxml",
            "{report}",
        ]
        values = self._configuration()
        return AgentRequest(
            schema_version=1,
            operation=operation,
            task_id=config.task.id,
            workspace=project.workspace,
            state_dir=str(config.task_dir),
            env_file=str(self.env_file),
            requirement=(
                config.task.requirement + "\n\n内部验收约束：在指定验收文件中创建一个名为 "
                "test_requirement 的 pytest 测试函数，并覆盖用户提出的可观察行为。"
            ),
            model=ModelConfig(
                base_url=os.environ.get("LLM_BASE_URL") or values["LLM_BASE_URL"],
                name=os.environ.get("LLM_MODEL_ID") or values["LLM_MODEL_ID"],
                api_key_env="LLM_API_KEY",
                stream=False,
            ),
            limits=Limits(
                max_implementation_attempts=3,
                max_model_requests=project.limits.max_model_requests,
                command_timeout_seconds=project.limits.command_timeout_seconds,
                output_limit_bytes=project.limits.output_limit_bytes,
            ),
            checks=[
                CheckSpec(
                    name="regression",
                    argv=regression_args,
                    report_format="junit_xml",
                    expected_test_ids=expected_regression,
                ),
                CheckSpec(
                    name="acceptance",
                    argv=acceptance_args,
                    report_format="junit_xml",
                    expected_test_ids=[acceptance_id],
                ),
            ],
            acceptance_paths=[acceptance_relative],
        )

    def _workflow(
        self, request: AgentRequest, config: TaskRunConfig, emit: Any
    ) -> tuple[AgentWorkflow, GitService]:
        workspace = Path(config.project.workspace).resolve()
        git = GitService(workspace)
        workflow = AgentWorkflow(
            request, config.task_dir / "agent-report.json", event_callback=emit
        )
        prior_state = workflow.store.get(request.task_id)
        snapshot = (
            GitSnapshot.model_validate(prior_state["initial_git_snapshot"])
            if prior_state and prior_state.get("initial_git_snapshot")
            else git.snapshot()
        )
        if prior_state is None:
            emit(
                {
                    "task_id": config.task.id,
                    "type": "git_snapshot",
                    "snapshot": snapshot.model_dump(mode="json"),
                }
            )
        workspace_tools = WorkspaceService()
        registry = ToolRegistry(
            [
                *workspace_tools.registrations(),
                *self.process_manager.registrations(),
                *git.registrations(),
            ]
        )

        def context_factory(phase: str, attempt: int) -> ToolContext:
            return self._tool_context(workflow, config, phase, attempt, git, snapshot)

        workflow.tool_registry = registry
        workflow.tool_context_factory = context_factory
        workflow.initial_git_snapshot = snapshot.model_dump(mode="json")
        workflow.delivery_guard = lambda: git.ensure_safe_delivery(
            snapshot,
            {
                str(item[key])
                for item in workflow.state.get("task_changes", [])
                for key in ("path", "target")
                if item.get(key)
            },
        )
        if config.project.extra_checks:
            workflow.extra_check_runner = lambda attempt: self._run_extra_checks(
                workflow, config, git, snapshot, attempt
            )
        return workflow, git

    def _run_extra_checks(
        self,
        workflow: AgentWorkflow,
        config: TaskRunConfig,
        git: GitService,
        snapshot: GitSnapshot,
        attempt: int,
    ) -> list[dict[str, Any]]:
        context = self._tool_context(workflow, config, "verification", attempt, git, snapshot)
        outcomes: list[dict[str, Any]] = []
        for index, spec in enumerate(config.project.extra_checks, 1):
            result = self.process_manager.run_command(spec, context)
            outcomes.append(
                {
                    "name": f"extra_check_{index}",
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "output_truncated": result.output_truncated,
                    "stdout_tail": result.stdout_tail,
                    "stderr_tail": result.stderr_tail,
                }
            )
        return outcomes

    def _tool_context(
        self,
        workflow: AgentWorkflow,
        config: TaskRunConfig,
        phase: str,
        attempt: int,
        git: GitService,
        snapshot: GitSnapshot,
    ) -> ToolContext:
        state = workflow.state
        task_changes = state.setdefault("task_changes", [])

        def record_change(change: dict[str, Any]) -> None:
            task_changes.append(change)
            operation = change["operation"]
            path = str(change["path"])
            target = change.get("target")
            modified = state.setdefault("modified_paths", [])
            paths = [path, target] if target else [path]
            for value in paths:
                if value not in modified:
                    modified.append(value)
            if operation == "edit":
                state.setdefault("edits", []).append(
                    {
                        "path": path,
                        "old_text": change["old_text"],
                        "new_text": change["new_text"],
                    }
                )
                state.setdefault("latest_hashes", {})[path] = change["after_sha256"]
            elif operation == "create":
                created = state.setdefault("created_paths", [])
                if path not in created:
                    created.append(path)
                state.setdefault("latest_hashes", {})[path] = change["after_sha256"]
            elif operation == "delete":
                deleted = state.setdefault("deleted_paths", [])
                if path not in deleted:
                    deleted.append(path)
                state.setdefault("deleted_hashes", {})[path] = change["before_sha256"]
                state.setdefault("latest_hashes", {}).pop(path, None)
            elif operation == "move":
                deleted = state.setdefault("deleted_paths", [])
                created = state.setdefault("created_paths", [])
                if path not in deleted:
                    deleted.append(path)
                if target not in created:
                    created.append(target)
                state.setdefault("deleted_hashes", {})[path] = change["before_sha256"]
                state.setdefault("latest_hashes", {})[target] = change["after_sha256"]
                state.setdefault("latest_hashes", {}).pop(path, None)
            if phase == "implementation" and path not in state.get("acceptance_paths", []):
                implementation = state.setdefault("implementation_paths", [])
                for value in paths:
                    if value not in implementation:
                        implementation.append(value)

        def register_artifact(artifact: Any, path: Path, **kwargs: Any) -> None:
            if self.store:
                self.store.register_artifact(artifact, path, **kwargs)

        def save_service(service: Any) -> None:
            if self.store:
                self.store.save_service(service)

        protected = {
            str((Path(config.project.workspace) / path).resolve())
            for path in state.get("frozen_acceptance", {})
        }
        services = {
            "git_snapshot": snapshot,
            "task_changes": task_changes,
            "validate_write": lambda relative, operation: git.validate_write(
                relative, operation, snapshot
            ),
            "register_artifact": register_artifact,
            "save_service": save_service,
        }
        return ToolContext(
            task_id=config.task.id,
            project_id=config.project.id,
            workspace=Path(config.project.workspace),
            artifact_dir=config.task_dir / "artifacts",
            phase=phase,
            attempt=attempt,
            limits=ExecutionLimits(
                max_model_requests=config.project.limits.max_model_requests,
                command_timeout_seconds=config.project.limits.command_timeout_seconds,
                output_limit_bytes=config.project.limits.output_limit_bytes,
            ),
            protected_paths=protected,
            emit=lambda **event: workflow._emit(**event),
            redact=workflow._redact,
            record_change=record_change,
            services=services,
        )

    @classmethod
    def collect_expected_test_ids(
        cls, python: str, workspace: Path, pytest_args: list[str]
    ) -> list[str]:
        result = subprocess.run(
            [python, "-m", "pytest", "--collect-only", "-q", *pytest_args],
            cwd=workspace,
            capture_output=True,
            timeout=120,
            check=False,
            env=cls._safe_environment(),
        )
        stdout = result.stdout.decode("utf-8", errors="replace")[:1_000_000]
        if result.returncode != 0:
            raise WorkflowFailure(
                "ENVIRONMENT_NOT_READY", "Unable to collect the project's pytest suite."
            )
        ids = sorted(
            {item for line in stdout.splitlines() if (item := cls._junit_id(line.strip()))}
        )
        if not ids:
            raise WorkflowFailure(
                "EXPECTED_TESTS_MISSING", "Project pytest configuration discovered no tests."
            )
        return ids

    @staticmethod
    def _junit_id(node_id: str) -> str | None:
        if "::" not in node_id:
            return None
        parts = node_id.replace("\\", "/").split("::")
        path = parts[0]
        if not path.endswith(".py"):
            return None
        module = Path(path).with_suffix("").as_posix().strip("./").replace("/", ".")
        if not module or not parts[-1]:
            return None
        return ".".join([module, *parts[1:]])

    def _configuration(self) -> dict[str, str]:
        if not self.env_file.is_file():
            return {}
        return {
            key: value
            for key, value in dotenv_values(self.env_file).items()
            if isinstance(value, str)
        }

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        sensitive = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        return {
            key: value
            for key, value in os.environ.items()
            if not any(word in key.upper() for word in sensitive)
        }

    def _register_report_artifacts(self, config: TaskRunConfig, report: Any) -> None:
        if self.store is None:
            return
        artifact_dir = config.task_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        contents = {
            "diff": Path(next(item.path for item in report.artifacts if item.kind == "diff")),
            "events": artifact_dir / "events.web.json",
            "verification": artifact_dir / "verification.web.json",
        }
        contents["events"].write_text(
            json.dumps(
                [_sanitize_public(event.model_dump(mode="json")) for event in report.events],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        contents["verification"].write_text(
            json.dumps(
                [
                    {
                        "name": check.name,
                        "attempt": check.attempt,
                        "returncode": check.returncode,
                        "timed_out": check.timed_out,
                        "output_truncated": check.output_truncated,
                    }
                    for check in report.checks
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        for kind, path in contents.items():
            path = path.resolve()
            if not path.is_file():
                continue
            raw = path.read_bytes()
            reference = ArtifactRef(
                id=str(uuid.uuid4()),
                task_id=config.task.id,
                kind=kind,
                media_type="text/x-diff" if kind == "diff" else "application/json",
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
            self.store.register_artifact(reference, path)

    @staticmethod
    def _error_from_report(report: Any) -> ToolError | None:
        for event in reversed(report.events):
            if event.type in {
                "task_error",
                "model_error",
                "protocol_error",
                "verification_error",
            }:
                return ToolError(
                    code=event.code or "TASK_FAILED",
                    message=event.reason or "Task failed.",
                )
        return None


def _sanitize_public(value: Any) -> Any:
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
            key: _sanitize_public(item)
            for key, item in value.items()
            if key.casefold() not in hidden
        }
    if isinstance(value, list):
        return [_sanitize_public(item) for item in value]
    if (
        isinstance(value, str)
        and len(value) > 2
        and (value.startswith(("C:\\", "D:\\", "E:\\", "/")) or "AppData\\Local\\MyAgent" in value)
    ):
        return "[internal path]"
    return value
