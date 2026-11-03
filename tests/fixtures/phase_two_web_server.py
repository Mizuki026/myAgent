"""Deterministic HTTP task runtime used by the browser acceptance test."""

from __future__ import annotations

import tempfile
from pathlib import Path

import uvicorn

from app.tasks.service import TaskRunOutcome
from app.web.application import create_app


class BrowserRuntime:
    def validate_configuration(self) -> None:
        return None

    def start(self, config, emit) -> TaskRunOutcome:
        emit({"task_id": config.task.id, "type": "tool_started", "tool": "search_code"})
        return TaskRunOutcome(
            status="needs_clarification",
            summary="需要确认验收格式。",
            questions=("请选择输出格式。",),
            model_requests=1,
        )

    def continue_with_answer(self, config, answer, emit) -> TaskRunOutcome:
        if not answer:
            return TaskRunOutcome(status="failed", summary="未收到补充说明。")
        return TaskRunOutcome(
            status="succeeded",
            summary="已按补充说明完成。",
            attempt=1,
            model_requests=2,
            commit="browser-test-commit",
        )


def main() -> None:
    data_dir = tempfile.TemporaryDirectory(prefix="my-agent-browser-test-")
    app = create_app(
        data_dir=Path(data_dir.name),
        env_file=Path(data_dir.name) / ".env",
        runtime=BrowserRuntime(),
    )
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
