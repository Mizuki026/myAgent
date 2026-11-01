"""JSON command-line interface for the local coding agent."""

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .models import AgentReport
from .workflow import AgentWorkflow, WorkflowFailure

EXIT_CODES = {
    "succeeded": 0,
    "no_changes": 0,
    "failed": 1,
    "needs_clarification": 2,
    "paused": 3,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one local coding-agent task.")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    arguments = parser.parse_args()
    report_path = arguments.report.resolve()
    workflow: AgentWorkflow | None = None
    task_id = "unknown"
    try:
        raw = json.loads(arguments.request.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("task_id"), str):
            task_id = raw["task_id"]
        from .models import AgentRequest

        request = AgentRequest.model_validate(raw)
        if {check.name for check in request.checks} != {"regression", "acceptance"} or len(
            request.checks
        ) != 2:
            raise ValueError("Exactly one regression and one acceptance check must be configured.")
        workflow = AgentWorkflow(request, report_path)
        report = workflow.run()
        return EXIT_CODES[report.status]
    except Exception as error:
        if workflow is not None:
            state = workflow.store.get(workflow.request.task_id)
            if state and workflow.state:
                workflow.state = state
                if isinstance(error, WorkflowFailure):
                    code, message = error.code, str(error)
                elif isinstance(error, ValidationError):
                    code, message = "INVALID_ARGUMENTS", str(error)
                else:
                    code, message = "INTERNAL_ERROR", str(error)
                workflow._emit(
                    type="task_error", code=code, reason=workflow._redact(message)[:2000]
                )
                report = workflow._finish(
                    "failed", workflow._redact(message)[:2000] or "任务执行失败。"
                )
                return EXIT_CODES[report.status]
        message = _safe_error(error)
        report = AgentReport(
            schema_version=1,
            task_id=task_id,
            status="failed",
            implementation_attempts=0,
            summary=message,
            events=[],
            checks=[],
            artifacts=[],
        )
        _write_report(report_path, report)
        print(message, file=sys.stderr)
        return EXIT_CODES[report.status]
    finally:
        if workflow is not None:
            workflow.store.close()


def _safe_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "Invalid request: " + str(error)[:2000]
    if isinstance(error, json.JSONDecodeError):
        return f"Invalid request JSON at line {error.lineno}, column {error.colno}."
    if isinstance(error, FileNotFoundError):
        return f"Input file was not found: {error.filename}"
    return f"Task failed: {type(error).__name__}: {error}"[:2000]


def _write_report(path: Path, report: AgentReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
