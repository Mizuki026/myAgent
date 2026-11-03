"""Task mutual exclusion, clarification, and lifecycle behavior tests."""

import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.models_v2 import ProjectProfile
from app.storage import StateStore
from app.tasks.service import TaskRunOutcome, TaskService, TaskServiceError
from app.tooling import ExecutionLimits


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class FakeRuntime:
    def __init__(self) -> None:
        self.start_result = TaskRunOutcome(
            status="needs_clarification",
            summary="需要澄清。",
            attempt=0,
            model_requests=1,
            questions=("哪种格式？",),
        )
        self.continue_result = TaskRunOutcome(
            status="succeeded", summary="完成。", attempt=1, model_requests=2, commit="abc123"
        )
        self.answers: list[str] = []

    def validate_configuration(self) -> None:
        return None

    def start(self, config, emit) -> TaskRunOutcome:
        emit({"task_id": config.task.id, "type": "model_request", "phase": "preparation"})
        emit({"task_id": config.task.id, "type": "tool_started", "tool": "search_code"})
        return self.start_result

    def continue_with_answer(self, config, answer, emit) -> TaskRunOutcome:
        self.answers.append(answer)
        return self.continue_result


class FakeProjects:
    def __init__(self, store: StateStore, profile: ProjectProfile) -> None:
        self.store = store
        self.profile = profile
        self.is_project_active = lambda _project_id: False

    def get_project(self, project_id: str) -> ProjectProfile:
        if project_id != self.profile.id:
            raise AssertionError(project_id)
        return self.profile

    def inspect_project(self, _workspace: str):
        return SimpleNamespace(ready=True, candidate_interpreters=[self.profile.python_executable])


class TaskServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase-two-tasks-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.data_dir = self.root / "data"
        self.store = StateStore(self.data_dir)
        now = timestamp()
        self.profile = ProjectProfile(
            id="project-1",
            name="project",
            workspace=str(self.workspace),
            python_executable="python.exe",
            limits=ExecutionLimits(),
            revision=1,
            created_at=now,
            updated_at=now,
        )
        self.store.save_project(self.profile)
        self.projects = FakeProjects(self.store, self.profile)
        self.runtime = FakeRuntime()
        self.tasks = TaskService(self.store, self.projects, self.data_dir, self.runtime)
        self.addCleanup(self.tasks.shutdown)

    def wait_for_status(self, task_id: str, status: str):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            task = self.tasks.get_task(task_id)
            if task.status == status:
                return task
            time.sleep(0.01)
        self.fail(f"Task did not reach {status}.")

    def test_single_active_task_waits_for_clarification_and_continues(self) -> None:
        initial = self.tasks.start_task("project-1", "Add the requested behavior")
        waiting = self.wait_for_status(initial.id, "needs_clarification")
        self.assertEqual(waiting.questions, ["哪种格式？"])
        self.assertEqual(waiting.model_requests, 1)
        with self.assertRaises(TaskServiceError) as error:
            self.tasks.start_task("project-1", "another task")
        self.assertEqual(error.exception.code, "TASK_BUSY")
        self.tasks.answer_clarification(initial.id, "Use JSON")
        succeeded = self.wait_for_status(initial.id, "succeeded")
        self.assertEqual(self.runtime.answers, ["Use JSON"])
        self.assertEqual(succeeded.attempt, 1)
        self.assertEqual(succeeded.commit, "abc123")
        self.assertEqual(
            [event.seq for event in self.store.events_after(initial.id, 0)], list(range(1, 8))
        )

    def test_dismiss_only_releases_a_waiting_task_and_rejects_empty_answers(self) -> None:
        initial = self.tasks.start_task("project-1", "Clarify before editing")
        self.wait_for_status(initial.id, "needs_clarification")
        with self.assertRaises(TaskServiceError) as error:
            self.tasks.answer_clarification(initial.id, "  ")
        self.assertEqual(error.exception.code, "INVALID_ARGUMENTS")
        dismissed = self.tasks.dismiss_waiting_task(initial.id)
        self.assertEqual(dismissed.status, "dismissed")
        next_task = self.tasks.start_task("project-1", "A new task")
        self.assertEqual(next_task.status, "running")


if __name__ == "__main__":
    unittest.main()
