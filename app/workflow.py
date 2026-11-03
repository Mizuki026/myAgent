"""Task orchestration for the local coding-agent CLI."""

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from openai import OpenAI

from app.tooling.registry import ToolRegistry
from app.tooling.types import ToolContext

from .checks import CheckOutcome, CheckRunner
from .models import AgentReport, AgentRequest, Artifact, CheckResult
from .persistence import TaskStore
from .workspace import (
    TOOLS,
    ToolFailure,
    WorkspaceTools,
    _decode,
    _encode,
    _replace_preserving_newlines,
)


class AgentWorkflow:
    def __init__(
        self,
        request: AgentRequest,
        report_path: Path,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_context_factory: Callable[[str, int], ToolContext] | None = None,
        delivery_guard: Callable[[], None] | None = None,
        initial_git_snapshot: dict[str, Any] | None = None,
    ) -> None:
        self.request = request
        self.workspace = Path(request.workspace).resolve()
        self.state_dir = Path(request.state_dir).resolve()
        self.report_path = report_path.resolve()
        self.store = TaskStore(self.state_dir)
        self.state: dict[str, Any] = {}
        self.client: OpenAI | None = None
        self.secrets: list[str] = []
        self.event_callback = event_callback
        self.current_phase = "preflight"
        self.tool_registry = tool_registry
        self.tool_context_factory = tool_context_factory
        self.delivery_guard = delivery_guard
        self.initial_git_snapshot = initial_git_snapshot
        self.extra_check_runner: Callable[[int], list[dict[str, Any]]] | None = None

    def run(self) -> AgentReport:
        if not self.workspace.is_dir():
            raise WorkflowFailure("INVALID_ARGUMENTS", "workspace must be an existing directory.")
        self._load_configuration()
        if self.request.operation == "run":
            if self.store.get(self.request.task_id):
                raise WorkflowFailure(
                    "TASK_EXISTS", "task_id already exists in this state directory."
                )
            baseline = self._git(["rev-parse", "HEAD"]).strip()
            self.state = {
                "task_id": self.request.task_id,
                "request": self._request_payload(),
                "baseline_commit": baseline,
                "phase": "baseline",
                "attempts": 0,
                "model_requests": 0,
                "messages": [],
                "checks": [],
                "modified_paths": [],
                "latest_hashes": {},
                "edits": [],
                "implementation_paths": [],
                "acceptance_paths": [
                    self._relative_acceptance(path) for path in self.request.acceptance_paths
                ],
                "frozen_acceptance": {},
                "frozen_backups": {},
                "initial_acceptance": self._snapshot_initial_acceptance(),
                "initial_index_clean": self._initial_index_clean(),
                "initial_git_snapshot": self.initial_git_snapshot,
                "status": "running",
            }
            self.store.create(self.request.task_id, self._redact_value(self.state))
        else:
            self.state = self.store.get(self.request.task_id) or {}
            if not self.state:
                raise WorkflowFailure("TASK_NOT_FOUND", "No saved task exists for this task_id.")
            self._validate_resume_request()
            if self.state.get("status") != "paused":
                raise WorkflowFailure("TASK_NOT_PAUSED", "Only a paused task can be resumed.")

        self.client = self._model_client()
        tools = WorkspaceTools(self.workspace, self.state, self._emit, self._redact)
        runner = CheckRunner(self.workspace, self.state_dir, self.request.limits, self._redact)
        if self.request.operation == "run":
            if not self._run_baseline_regression(runner):
                return self._finish("failed", "原有回归测试基线未通过，任务未开始修改。")
            self.state["messages"] = self._initial_messages()
            self.state["phase"] = "preparation"
            self._save()
            final = self._model_turn(tools, runner, "preparation", 0)
            if final["kind"] == "needs_clarification":
                questions = self._questions(final)
                if not questions:
                    return self._finish("failed", "模型返回了空的澄清问题。")
                return self._finish(
                    "needs_clarification", self._summary(final, "需要先明确需求。"), questions
                )
            if final["kind"] != "acceptance_ready":
                self._emit(
                    type="protocol_error",
                    code="PROTOCOL_ERROR",
                    reason="Expected acceptance_ready before implementation.",
                )
                return self._finish("failed", "模型未先生成并固定验收测试。")
            if not self._freeze_acceptance(tools):
                return self._finish("failed", "无法读取或固定生成的验收测试。")
            acceptance_baseline = self._run_check(runner, self._check_spec("acceptance"), 0)
            self.state["acceptance_baseline"] = acceptance_baseline.record.model_dump(mode="json")
            valid_baseline, baseline_codes = runner.validate_acceptance_baseline(
                acceptance_baseline
            )
            if baseline_codes:
                self._emit_check_codes(baseline_codes, attempt=0)
                return self._finish("failed", "生成的验收测试未能形成有效、可执行的测试报告。")
            if not valid_baseline:
                self._emit(
                    type="verification_error",
                    code="REPORT_INVALID",
                    attempt=0,
                    reason=(
                        "Acceptance baseline did not provide test failures or a passing suite."
                    ),
                )
                return self._finish("failed", "生成的验收测试无法证明原始实现是否满足需求。")
            if acceptance_baseline.passed:
                self._restore_initial_acceptance()
                self.state["modified_paths"] = [
                    path
                    for path in self.state["modified_paths"]
                    if path not in self.state["acceptance_paths"]
                ]
                self.state["latest_hashes"] = {
                    path: digest
                    for path, digest in self.state.get("latest_hashes", {}).items()
                    if path not in self.state["acceptance_paths"]
                }
                return self._finish(
                    "no_changes", "原始实现已满足冻结验收和回归测试，无需修改代码。"
                )
            self.state["phase"] = "implementation"
            self._append_message(
                "user",
                "验收测试已固定。它在原始实现上实际失败，回归基线通过。现在按需求修改目标函数；"
                "实现后以 implementation_ready 结束本轮。修复时不得改动或跳过固定验收文件。",
            )
            self._save()

        return self._implementation_loop(tools, runner)

    def _implementation_loop(self, tools: WorkspaceTools, runner: CheckRunner) -> AgentReport:
        while self.state["attempts"] < self.request.limits.max_implementation_attempts:
            attempt = self.state["attempts"] + 1
            self.state["attempts"] = attempt
            self._save()
            final = self._model_turn(tools, runner, "implementation", attempt)
            if final["kind"] == "needs_clarification":
                questions = self._questions(final)
                return self._finish(
                    "needs_clarification", self._summary(final, "需要先明确需求。"), questions
                )
            result = self._process_implementation_result(tools, runner, final, attempt)
            if result is not None:
                return result
        return self._finish("failed", "三次实现与修复预算已用尽，验证仍未全部通过。")

    def _process_implementation_result(
        self,
        tools: WorkspaceTools,
        runner: CheckRunner,
        final: dict[str, Any],
        attempt: int,
    ) -> AgentReport | None:
        if final.get("kind") != "implementation_ready":
            self._emit(
                type="protocol_error",
                code="PROTOCOL_ERROR",
                attempt=attempt,
                reason="Expected implementation_ready at the end of an implementation round.",
            )
            return self._finish("failed", "模型没有按协议结束实现轮次。")
        self._restore_frozen_acceptance_if_needed()
        outcomes = [
            self._run_check(runner, self._check_spec("acceptance"), attempt),
            self._run_check(runner, self._check_spec("regression"), attempt),
        ]
        self._restore_frozen_acceptance_if_needed()
        passed = all(outcome.passed for outcome in outcomes)
        for outcome in outcomes:
            self._emit_check_codes(outcome.codes, attempt=attempt)
        extra_results: list[dict[str, Any]] = []
        if passed and self.extra_check_runner is not None:
            try:
                extra_results = self.extra_check_runner(attempt)
            except Exception as error:
                extra_results = [
                    {
                        "name": "extra_check",
                        "returncode": -1,
                        "timed_out": False,
                        "output_truncated": False,
                        "error": self._redact(str(error))[:1000],
                    }
                ]
            for result in extra_results:
                self._emit(type="extra_check_completed", attempt=attempt, **result)
            passed = all(
                result.get("returncode") == 0
                and not result.get("timed_out")
                and not result.get("output_truncated")
                and not result.get("error")
                for result in extra_results
            )
        if passed:
            if not self.state.get("implementation_paths"):
                return self._finish("failed", "测试通过但没有记录任何实现文件变更，未创建提交。")
            if self.delivery_guard is not None:
                try:
                    self.delivery_guard()
                except Exception as error:
                    code = getattr(error, "code", "USER_CHANGE_CONFLICT")
                    self._emit(type="commit_error", code=code, reason=str(error))
                    return self._finish("failed", "验证通过，但工作区出现未归属变化，未创建提交。")
            commit = self._commit_agent_changes()
            if not commit:
                return self._finish("failed", "验证通过，但 agent 自有改动无法安全提交。")
            self.state["commit"] = commit
            return self._finish(
                "succeeded", self._summary(final, "修改已通过验收与回归测试，并创建了提交。")
            )
        self._append_verification_feedback(outcomes, attempt)
        if extra_results:
            self._append_message(
                "user",
                "项目额外检查未全部通过：\n"
                + json.dumps(extra_results, ensure_ascii=False, sort_keys=True),
            )
        self._save()
        if self.request.pause_after_attempt == attempt:
            self.state["status"] = "paused"
            self._save()
            return self._finish(
                "paused", f"已完成第 {attempt} 次实现尝试并保存状态，可使用相同 task_id 恢复。"
            )
        return None

    def continue_with_answer(self, answer: str) -> AgentReport:
        """Continue a saved Web task after its user answered a clarification."""
        if not answer.strip():
            raise WorkflowFailure("INVALID_ARGUMENTS", "Clarification answer cannot be empty.")
        self._load_configuration()
        self.state = self.store.get(self.request.task_id) or {}
        if not self.state or self.state.get("status") != "needs_clarification":
            raise WorkflowFailure("INVALID_TASK_STATE", "Task is not waiting for clarification.")
        self._validate_resume_request()
        self._verify_continuation_workspace()
        if self.delivery_guard is not None:
            try:
                self.delivery_guard()
            except Exception as error:
                raise WorkflowFailure(
                    getattr(error, "code", "WORKSPACE_CHANGED"), str(error)
                ) from error
        self.client = self._model_client()
        self._append_message("user", "补充说明：" + answer.strip())
        self.state["status"] = "running"
        self._save()
        tools = WorkspaceTools(self.workspace, self.state, self._emit, self._redact)
        runner = CheckRunner(self.workspace, self.state_dir, self.request.limits, self._redact)
        phase = self.state.get("phase")
        if phase == "preparation":
            final = self._model_turn(tools, runner, "preparation", 0)
            if final["kind"] == "needs_clarification":
                questions = self._questions(final)
                return self._finish(
                    "needs_clarification", self._summary(final, "需要先明确需求。"), questions
                )
            if final["kind"] != "acceptance_ready":
                return self._finish("failed", "模型未按协议完成验收准备。")
            if not self._freeze_acceptance(tools):
                return self._finish("failed", "无法读取或固定生成的验收测试。")
            baseline = self._run_check(runner, self._check_spec("acceptance"), 0)
            self.state["acceptance_baseline"] = baseline.record.model_dump(mode="json")
            valid, codes = runner.validate_acceptance_baseline(baseline)
            if codes:
                self._emit_check_codes(codes, attempt=0)
                return self._finish("failed", "生成的验收测试未形成有效报告。")
            if not valid:
                return self._finish("failed", "生成的验收测试无法判断原始实现。")
            if baseline.passed:
                self._restore_initial_acceptance()
                return self._finish("no_changes", "原始实现已满足验收与回归测试，无需修改。")
            self.state["phase"] = "implementation"
            self._append_message(
                "user",
                "固定验收在原始实现上失败，回归基线通过。现在按需求实现修改；"
                "每轮以 implementation_ready 结束。",
            )
            self._save()
            return self._implementation_loop(tools, runner)
        if phase == "implementation":
            attempt = self.state.get("attempts", 0)
            final = self._model_turn(tools, runner, "implementation", attempt)
            if final["kind"] == "needs_clarification":
                return self._finish(
                    "needs_clarification",
                    self._summary(final, "需要先明确需求。"),
                    self._questions(final),
                )
            result = self._process_implementation_result(tools, runner, final, attempt)
            return result or self._implementation_loop(tools, runner)
        raise WorkflowFailure("INVALID_TASK_STATE", "Saved task phase cannot accept clarification.")

    def _verify_continuation_workspace(self) -> None:
        head = self._git(["rev-parse", "HEAD"]).strip()
        if head != self.state.get("baseline_commit"):
            raise WorkflowFailure(
                "WORKSPACE_CHANGED", "Git HEAD changed while waiting for clarification."
            )
        expected_hashes = dict(self.state.get("read_hashes", {}))
        expected_hashes.update(self.state.get("latest_hashes", {}))
        for relative, expected in expected_hashes.items():
            path = (self.workspace / relative).resolve()
            if not path.is_relative_to(self.workspace) or not path.is_file():
                raise WorkflowFailure("WORKSPACE_CHANGED", f"Task file disappeared: {relative}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise WorkflowFailure(
                    "WORKSPACE_CHANGED", f"Task file changed while waiting: {relative}"
                )

    def _model_turn(
        self,
        tools: WorkspaceTools,
        runner: CheckRunner,
        phase: str,
        attempt: int,
    ) -> dict[str, Any]:
        self.current_phase = phase
        while True:
            if self.state["model_requests"] >= self.request.limits.max_model_requests:
                raise WorkflowFailure("MODEL_REQUEST_LIMIT", "Model request limit reached.")
            assert self.client is not None
            self.state["model_requests"] += 1
            self._save()
            self._notify(
                {
                    "task_id": self.request.task_id,
                    "type": "model_request",
                    "phase": phase,
                    "attempt": attempt,
                }
            )
            try:
                response = self.client.chat.completions.create(
                    model=self.request.model.name,
                    messages=self.state["messages"],
                    tools=self._tool_schemas(),
                    tool_choice="auto",
                    stream=False,
                )
            except Exception as error:
                reason = self._redact(str(error))[:1500]
                self._emit(
                    type="model_error", code="MODEL_ERROR", attempt=attempt or None, reason=reason
                )
                raise WorkflowFailure("MODEL_ERROR", reason) from error
            message = response.choices[0].message.model_dump(exclude_none=True)
            message = self._redact_value(message)
            self.state["messages"].append(message)
            self._save()
            tool_calls = message.get("tool_calls", [])
            if tool_calls:
                for call in tool_calls:
                    call_id = call.get("id", "")
                    function = call.get("function", {})
                    name = function.get("name", "")
                    try:
                        arguments = json.loads(function.get("arguments", "{}"))
                    except (TypeError, json.JSONDecodeError):
                        arguments = None
                    if name == "run_checks":
                        result = self._run_checks_tool(arguments, runner, phase, attempt)
                    elif self.tool_registry is not None and self.tool_context_factory is not None:
                        context = self.tool_context_factory(phase, attempt)
                        result = self.tool_registry.invoke(name, arguments, context).model_dump(
                            mode="json"
                        )
                    else:
                        result = tools.call(name, arguments, phase, attempt)
                    if name == "read_file" and isinstance(result, dict):
                        payload = result.get("data") if result.get("ok") is True else result
                        if isinstance(payload, dict):
                            path = payload.get("path")
                            digest = payload.get("sha256")
                            if isinstance(path, str) and isinstance(digest, str):
                                self.state.setdefault("read_hashes", {})[path] = digest
                                self._save()
                    self._append_message(
                        "tool",
                        json.dumps(result, ensure_ascii=False, sort_keys=True),
                        tool_call_id=call_id,
                    )
                self._save()
                continue
            content = message.get("content")
            if not isinstance(content, str):
                raise WorkflowFailure(
                    "PROTOCOL_ERROR", "Model response must contain a JSON phase result."
                )
            try:
                final = json.loads(content)
            except json.JSONDecodeError as error:
                raise WorkflowFailure(
                    "PROTOCOL_ERROR", "Model phase result is not valid JSON."
                ) from error
            if not isinstance(final, dict) or not isinstance(final.get("kind"), str):
                raise WorkflowFailure(
                    "PROTOCOL_ERROR", "Model phase result must contain a kind field."
                )
            self.state["last_model_summary"] = self._summary(final, "")
            self._save()
            return final

    def _run_checks_tool(
        self, arguments: Any, runner: CheckRunner, phase: str, attempt: int
    ) -> dict[str, Any]:
        if (
            not isinstance(arguments, dict)
            or arguments.get("scope") != "all"
            or set(arguments) != {"scope"}
        ):
            self._emit(
                type="tool_error",
                tool="run_checks",
                code="INVALID_ARGUMENTS",
                reason="run_checks requires scope='all'.",
            )
            return {"error": {"code": "INVALID_ARGUMENTS", "reason": "scope must be all."}}
        if not self.state.get("frozen_acceptance"):
            self._emit(
                type="tool_error",
                tool="run_checks",
                code="INVALID_ARGUMENTS",
                reason="Acceptance must be frozen before checks can run.",
            )
            return {"error": {"code": "INVALID_ARGUMENTS", "reason": "Acceptance is not frozen."}}
        outcomes = [self._run_check(runner, spec, attempt) for spec in self.request.checks]
        self._restore_frozen_acceptance_if_needed()
        return {
            "passed": all(item.passed for item in outcomes),
            "checks": [item.record.model_dump(mode="json") for item in outcomes],
            "failures": [code for item in outcomes for code, _ in item.codes],
        }

    def _run_baseline_regression(self, runner: CheckRunner) -> bool:
        outcome = self._run_check(runner, self._check_spec("regression"), 0)
        if outcome.passed:
            return True
        codes = outcome.codes or [("BASELINE_FAILED", "Original regression checks failed.")]
        self._emit_check_codes(codes, attempt=0)
        self._emit(
            type="baseline_failed",
            code="BASELINE_FAILED",
            attempt=0,
            reason="Original regression tests did not all pass before the task.",
        )
        return False

    def _run_check(self, runner: CheckRunner, spec, attempt: int) -> CheckOutcome:
        outcome = runner.run(spec, attempt)
        outcome.record = CheckResult.model_validate(
            self._redact_value(outcome.record.model_dump(mode="json"))
        )
        self.state["checks"].append(outcome.record.model_dump(mode="json"))
        self._save()
        return outcome

    def _emit_check_codes(self, codes: list[tuple[str, str]], attempt: int) -> None:
        for code, reason in codes:
            self._emit(type="verification_error", code=code, attempt=attempt, reason=reason)

    def _freeze_acceptance(self, tools: WorkspaceTools) -> bool:
        frozen: dict[str, str] = {}
        backups: dict[str, str] = {}
        destination = self.state_dir / "frozen"
        destination.mkdir(parents=True, exist_ok=True)
        for relative in self.state["acceptance_paths"]:
            try:
                path = tools.resolve_acceptance_path(relative)
                if not path.is_file():
                    self._emit(
                        type="tool_error",
                        tool="edit_file",
                        path=relative,
                        code="FILE_NOT_FOUND",
                        reason="Generated acceptance file does not exist.",
                    )
                    return False
                raw = path.read_bytes()
                if any(secret.encode() in raw for secret in self.secrets):
                    self._emit(
                        type="tool_error",
                        path=relative,
                        code="CREDENTIAL_PROTECTION",
                        reason="Acceptance file contains a configured credential.",
                    )
                    return False
                digest = hashlib.sha256(raw).hexdigest()
                backup = destination / (hashlib.sha256(relative.encode()).hexdigest() + ".bin")
                backup.write_bytes(raw)
                frozen[relative] = digest
                backups[relative] = str(backup)
            except (OSError, ToolFailure) as error:
                self._emit(
                    type="tool_error",
                    path=relative,
                    code=error.code if isinstance(error, ToolFailure) else "IO_ERROR",
                    reason=str(error),
                )
                return False
        self.state["frozen_acceptance"] = frozen
        self.state["frozen_backups"] = backups
        if frozen:
            first_path = next(iter(frozen))
            self._emit(type="acceptance_frozen", path=first_path, sha256=frozen[first_path])
        self._save()
        return True

    def _restore_frozen_acceptance_if_needed(self) -> None:
        for relative, digest in self.state.get("frozen_acceptance", {}).items():
            path = (self.workspace / relative).resolve(strict=False)
            if not path.is_relative_to(self.workspace):
                self._emit(
                    type="verification_error",
                    code="OUTSIDE_WORKSPACE",
                    path=relative,
                    reason="Frozen acceptance path resolved outside the workspace.",
                )
                continue
            try:
                current_hash = (
                    hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
                )
                if current_hash != digest:
                    backup = Path(self.state["frozen_backups"][relative])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(backup.read_bytes())
                    self._emit(
                        type="verification_error",
                        code="PROTECTED_ACCEPTANCE",
                        path=relative,
                        reason=(
                            "A command changed frozen acceptance; the frozen version was restored."
                        ),
                    )
            except OSError as error:
                self._emit(
                    type="verification_error", code="IO_ERROR", path=relative, reason=str(error)
                )

    def _snapshot_initial_acceptance(self) -> dict[str, str | None]:
        snapshots: dict[str, str | None] = {}
        directory = self.state_dir / "initial-acceptance"
        directory.mkdir(parents=True, exist_ok=True)
        for value in self.request.acceptance_paths:
            relative = self._relative_acceptance(value)
            path = (self.workspace / relative).resolve(strict=False)
            if not path.is_relative_to(self.workspace):
                raise WorkflowFailure("OUTSIDE_WORKSPACE", "Acceptance path escapes the workspace.")
            if path.exists():
                raw = path.read_bytes()
                if any(secret.encode() in raw for secret in self.secrets):
                    raise WorkflowFailure(
                        "CREDENTIAL_PROTECTION", "Acceptance file contains a configured credential."
                    )
                destination = directory / (hashlib.sha256(relative.encode()).hexdigest() + ".bin")
                destination.write_bytes(raw)
                snapshots[relative] = str(destination)
            else:
                snapshots[relative] = None
        return snapshots

    def _restore_initial_acceptance(self) -> None:
        for relative, snapshot in self.state.get("initial_acceptance", {}).items():
            path = (self.workspace / relative).resolve(strict=False)
            if not path.is_relative_to(self.workspace):
                continue
            if snapshot is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(Path(snapshot).read_bytes())

    def _append_verification_feedback(self, outcomes: list[CheckOutcome], attempt: int) -> None:
        entries = []
        for outcome in outcomes:
            record = outcome.record
            stdout = Path(record.stdout_path).read_text(encoding="utf-8", errors="replace")
            stderr = Path(record.stderr_path).read_text(encoding="utf-8", errors="replace")
            report_summary = self._junit_failure_summary(Path(record.report_path))
            entries.append(
                {
                    "name": record.name,
                    "returncode": record.returncode,
                    "timed_out": record.timed_out,
                    "output_truncated": record.output_truncated,
                    "codes": [{"code": code, "reason": reason} for code, reason in outcome.codes],
                    "test_failures": report_summary,
                    "stdout": stdout[-5000:],
                    "stderr": stderr[-5000:],
                }
            )
        self._append_message(
            "user",
            "实际检查未全部通过。以下是第 "
            + str(attempt)
            + " 次的执行证据，请据此修复实现，保持冻结验收测试不变：\n"
            + json.dumps(entries, ensure_ascii=False, sort_keys=True),
        )

    @staticmethod
    def _junit_failure_summary(path: Path) -> list[str]:
        if not path.is_file():
            return []
        try:
            root = __import__("xml.etree.ElementTree", fromlist=["fromstring"]).fromstring(
                path.read_text(encoding="utf-8", errors="replace")
            )
            return [
                f"{case.get('classname')}.{case.get('name')}: "
                + (
                    case.findtext("failure")
                    or case.findtext("error")
                    or case.findtext("skipped")
                    or ""
                )[:1200]
                for case in root.iter("testcase")
                if any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
            ]
        except Exception:
            return []

    def _commit_agent_changes(self) -> str | None:
        paths = sorted(set(self.state.get("modified_paths", [])))
        if not paths:
            return None
        current_head = self._git(["rev-parse", "HEAD"]).strip()
        baseline = self.state["baseline_commit"]
        if current_head != baseline:
            self._emit(
                type="commit_error",
                code="GIT_BASE_CHANGED",
                reason="HEAD changed during the task; refusing to commit against a new base.",
            )
            return None
        temp_index = self.state_dir / "agent-index"
        temp_index.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(temp_index)
        try:
            self._git(["read-tree", baseline], env=environment)
            for relative in paths:
                current_path = (self.workspace / relative).resolve(strict=False)
                if not current_path.is_relative_to(self.workspace):
                    self._emit(
                        type="commit_error",
                        code="OUTSIDE_WORKSPACE",
                        path=relative,
                        reason="A changed path resolved outside the workspace.",
                    )
                    return None
                if relative in self.state.get("deleted_paths", []):
                    if current_path.exists():
                        self._emit(
                            type="commit_error",
                            code="STALE_FILE",
                            path=relative,
                            reason="A deleted task file reappeared before commit.",
                        )
                        return None
                    baseline_entry = self._git(["ls-tree", baseline, "--", relative]).strip()
                    if baseline_entry:
                        self._git(
                            ["update-index", "--force-remove", "--", relative], env=environment
                        )
                    continue
                expected = self.state.get("latest_hashes", {}).get(relative)
                if not current_path.is_relative_to(self.workspace) or not current_path.is_file():
                    self._emit(
                        type="commit_error",
                        code="STALE_FILE",
                        path=relative,
                        reason="A changed file disappeared or resolved outside the workspace.",
                    )
                    return None
                current_raw = current_path.read_bytes()
                if expected and hashlib.sha256(current_raw).hexdigest() != expected:
                    self._emit(
                        type="commit_error",
                        code="STALE_FILE",
                        path=relative,
                        reason="A file changed after the agent last edited it; refusing to commit.",
                    )
                    return None
                committed_raw = (
                    current_raw
                    if relative in self.state.get("created_paths", [])
                    else self._replay_edits_on_baseline(relative)
                )
                if committed_raw is None:
                    self._emit(
                        type="commit_error",
                        code="USER_CHANGE_CONFLICT",
                        path=relative,
                        reason=(
                            "Agent edits cannot be isolated from the user's pre-existing changes."
                        ),
                    )
                    return None
                blob = (
                    self._git_bytes(["hash-object", "-w", "--stdin"], committed_raw)
                    .decode()
                    .strip()
                )
                try:
                    entry = self._git(["ls-tree", baseline, "--", relative]).strip()
                    mode = entry.split(maxsplit=1)[0].split()[0] if entry else "100644"
                except (WorkflowFailure, IndexError):
                    mode = "100644"
                self._git(
                    ["update-index", "--add", "--cacheinfo", f"{mode},{blob},{relative}"],
                    env=environment,
                )
            tree = self._git(["write-tree"], env=environment).strip()
            baseline_tree = self._git(["rev-parse", f"{baseline}^{{tree}}"]).strip()
            if tree == baseline_tree:
                self._emit(
                    type="commit_error", code="NO_CHANGES", reason="Task has no net Git changes."
                )
                return None
            author_env = environment.copy()
            if not self._git_optional(["config", "user.name"]):
                author_env["GIT_AUTHOR_NAME"] = "Local Coding Agent"
                author_env["GIT_COMMITTER_NAME"] = "Local Coding Agent"
            if not self._git_optional(["config", "user.email"]):
                author_env["GIT_AUTHOR_EMAIL"] = "local-agent@localhost"
                author_env["GIT_COMMITTER_EMAIL"] = "local-agent@localhost"
            commit = self._git(
                ["commit-tree", tree, "-p", baseline, "-m", f"agent: {self.request.task_id}"],
                env=author_env,
            ).strip()
            self._git(["update-ref", "HEAD", commit, baseline])
            if self.state.get("initial_index_clean"):
                try:
                    self._git(["read-tree", commit])
                except WorkflowFailure as error:
                    self._emit(
                        type="commit_warning",
                        code="GIT_INDEX_UPDATE_FAILED",
                        reason=str(error),
                    )
            self._emit(type="commit_created", sha256=commit)
            return commit
        except WorkflowFailure as error:
            self._emit(type="commit_error", code="GIT_ERROR", reason=str(error))
            return None
        finally:
            temp_index.unlink(missing_ok=True)

    def _replay_edits_on_baseline(self, relative: str) -> bytes | None:
        result = subprocess.run(
            ["git", "show", f"{self.state['baseline_commit']}:{relative}"],
            cwd=self.workspace,
            capture_output=True,
            check=False,
        )
        original = result.stdout if result.returncode == 0 else b""
        try:
            text = _decode(original)
        except UnicodeError:
            return None
        for edit in (item for item in self.state.get("edits", []) if item["path"] == relative):
            updated = _replace_preserving_newlines(text, edit["old_text"], edit["new_text"])
            if updated is None:
                return None
            text = updated
        return _encode(text, original)

    def _initial_messages(self) -> list[dict[str, Any]]:
        prompt = {
            "task_id": self.request.task_id,
            "workspace": str(self.workspace),
            "requirement": self.request.requirement,
            "acceptance_paths": self.state["acceptance_paths"],
            "checks": [spec.model_dump(mode="json") for spec in self.request.checks],
            "instructions": [
                "First inspect relevant code and callers using search_code and read_file.",
                (
                    "Before implementation, write independent acceptance tests to "
                    "acceptance_paths using edit_file."
                ),
                'End the preparation phase with JSON {"kind":"acceptance_ready"}.',
                (
                    "If behavior is ambiguous, ask concise needs_clarification questions "
                    "without editing files."
                ),
                (
                    "edit_file requires the SHA-256 from read_file and an old_text span "
                    "that appears exactly once."
                ),
                "Do not read or write credential configuration files.",
            ],
        }
        return [
            {
                "role": "system",
                "content": (
                    "You are a coding agent in a local Python repository. Use the provided "
                    "tools for workspace operations. Return phase results as JSON content. "
                    "The program, not your summary, determines success from actual test reports."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, sort_keys=True)},
        ]

    def _load_configuration(self) -> None:
        if self.request.model.stream:
            raise WorkflowFailure(
                "INVALID_ARGUMENTS", "Streaming model responses are not supported in phase one."
            )
        env_values = {
            key: value
            for key, value in dotenv_values(self.request.env_file).items()
            if value is not None
        }
        name = self.request.model.api_key_env
        api_key = os.environ.get(name) or env_values.get(name)
        if not api_key:
            raise WorkflowFailure(
                "MODEL_CONFIG", f"Missing API key in environment variable {name} or env_file."
            )
        self.secrets = sorted(
            {value for key, value in env_values.items() if self._secret_name(key) and value}
            | {value for key, value in os.environ.items() if self._secret_name(key) and value},
            key=len,
            reverse=True,
        )
        self.secrets = [secret for secret in self.secrets if len(secret) >= 4]
        if api_key not in self.secrets:
            self.secrets.insert(0, api_key)

    def _model_client(self) -> OpenAI:
        env_values = {
            key: value
            for key, value in dotenv_values(self.request.env_file).items()
            if value is not None
        }
        api_key = os.environ.get(self.request.model.api_key_env) or env_values.get(
            self.request.model.api_key_env
        )
        if not api_key:
            raise WorkflowFailure(
                "MODEL_CONFIG", "API key disappeared before model client creation."
            )
        return OpenAI(
            api_key=api_key,
            base_url=self.request.model.base_url.rstrip("/"),
            max_retries=0,
            timeout=45,
        )

    def _request_payload(self) -> dict[str, Any]:
        payload = self.request.model_dump(mode="json")
        payload["operation"] = "run"
        payload["pause_after_attempt"] = self.request.pause_after_attempt
        return payload

    def _validate_resume_request(self) -> None:
        stored = self.state.get("request", {})
        current = self._request_payload()
        current["operation"] = "run"
        current["pause_after_attempt"] = None
        stored["pause_after_attempt"] = None
        if current != stored:
            raise WorkflowFailure(
                "RESUME_MISMATCH", "Resume request differs from the original task configuration."
            )

    def _check_spec(self, name: str):
        matches = [spec for spec in self.request.checks if spec.name == name]
        if len(matches) != 1:
            raise WorkflowFailure(
                "INVALID_ARGUMENTS", f"Exactly one {name} check must be configured."
            )
        return matches[0]

    def _relative_acceptance(self, value: str) -> str:
        supplied = Path(value)
        absolute = (supplied if supplied.is_absolute() else self.workspace / supplied).resolve(
            strict=False
        )
        if not absolute.is_relative_to(self.workspace):
            raise WorkflowFailure(
                "OUTSIDE_WORKSPACE", "Acceptance path resolves outside the workspace."
            )
        return absolute.relative_to(self.workspace).as_posix()

    def _append_message(self, role: str, content: str, **fields: Any) -> None:
        message = {"role": role, "content": self._redact(content), **fields}
        self.state["messages"].append(message)

    def _emit(self, **fields: Any) -> None:
        event = {"seq": len(self.store.events(self.request.task_id)) + 1}
        event_type = fields.pop("type", "event")
        event["type"] = event_type
        for key, value in fields.items():
            if value is not None:
                event[key] = self._redact_value(value)
        self.store.add_event(self.request.task_id, event)
        self._notify({**event, "task_id": self.request.task_id, "phase": self.current_phase})

    def _notify(self, event: dict[str, Any]) -> None:
        if self.event_callback is None:
            return
        try:
            self.event_callback(self._redact_value(event))
        except Exception:
            return

    def _tool_schemas(self) -> list[dict[str, object]]:
        if self.tool_registry is None:
            return TOOLS
        check_schema = next(
            schema for schema in TOOLS if schema.get("function", {}).get("name") == "run_checks"
        )
        return [*self.tool_registry.schemas(), check_schema]

    def _save(self) -> None:
        self.store.save(self.request.task_id, self._redact_value(self.state))

    def _finish(
        self,
        status: str,
        summary: str,
        questions: list[str] | None = None,
    ) -> AgentReport:
        self.state["status"] = status
        self.state["summary"] = self._redact(summary)
        self._save()
        artifacts = self._write_artifacts()
        report = AgentReport(
            schema_version=1,
            task_id=self.request.task_id,
            status=status,
            implementation_attempts=self.state.get("attempts", 0),
            summary=self._redact(summary),
            questions=[self._redact(question) for question in questions or []],
            commit=self.state.get("commit"),
            events=self.store.events(self.request.task_id),
            checks=[CheckResult.model_validate(item) for item in self.state.get("checks", [])],
            artifacts=artifacts,
        )
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
        temporary.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, self.report_path)
        return report

    def _write_artifacts(self) -> list[Artifact]:
        artifact_dir = self.state_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        diff_path = artifact_dir / "diff.patch"
        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff", self.state.get("baseline_commit", "HEAD"), "--", "."],
            cwd=self.workspace,
            capture_output=True,
            check=False,
        )
        diff_path.write_text(self._redact(diff.stdout.decode("utf-8", "replace")), encoding="utf-8")
        events_path = artifact_dir / "events.json"
        events_path.write_text(
            json.dumps(self.store.events(self.request.task_id), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        verification_path = artifact_dir / "verification.json"
        verification_path.write_text(
            json.dumps(self.state.get("checks", []), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return [
            Artifact(kind="diff", path=str(diff_path.resolve())),
            Artifact(kind="events", path=str(events_path.resolve())),
            Artifact(kind="verification", path=str(verification_path.resolve())),
        ]

    def _summary(self, final: dict[str, Any], default: str) -> str:
        value = final.get("summary")
        return self._redact(value) if isinstance(value, str) and value.strip() else default

    @staticmethod
    def _questions(final: dict[str, Any]) -> list[str]:
        questions = final.get("questions")
        if not isinstance(questions, list):
            return []
        return [
            question for question in questions if isinstance(question, str) and question.strip()
        ]

    def _redact(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
        return value

    @staticmethod
    def _secret_name(name: str) -> bool:
        return name.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))

    def _git(self, arguments: list[str], env: dict[str, str] | None = None) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=self.workspace, env=env, capture_output=True, check=False
        )
        if result.returncode:
            raise WorkflowFailure(
                "GIT_ERROR", self._redact(result.stderr.decode("utf-8", "replace")[-2000:])
            )
        return result.stdout.decode("utf-8", "replace")

    def _git_bytes(self, arguments: list[str], content: bytes) -> bytes:
        result = subprocess.run(
            ["git", *arguments], cwd=self.workspace, input=content, capture_output=True, check=False
        )
        if result.returncode:
            raise WorkflowFailure(
                "GIT_ERROR", self._redact(result.stderr.decode("utf-8", "replace")[-2000:])
            )
        return result.stdout

    def _git_optional(self, arguments: list[str]) -> str | None:
        result = subprocess.run(
            ["git", *arguments], cwd=self.workspace, capture_output=True, check=False
        )
        return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else None

    def _initial_index_clean(self) -> bool:
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=self.workspace,
            capture_output=True,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise WorkflowFailure("GIT_ERROR", "Unable to inspect the user's staged changes.")
        return result.returncode == 0


class WorkflowFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
