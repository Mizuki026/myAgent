"""HTTP contract tests using the real FastAPI app and a deterministic runtime."""

import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.tasks.service import TaskRunOutcome
from app.tooling import ArtifactRef
from app.web.application import create_app


def now() -> str:
    return datetime.now(UTC).isoformat()


class FakeRuntime:
    def validate_configuration(self) -> None:
        return None

    def start(self, config, emit):
        emit({"task_id": config.task.id, "type": "tool_started", "tool": "search_code"})
        return TaskRunOutcome(
            status="needs_clarification",
            summary="需要说明。",
            questions=("选择格式",),
            model_requests=1,
        )

    def continue_with_answer(self, config, answer, emit):
        return TaskRunOutcome(status="succeeded", summary="已完成。", attempt=1, commit="123abc")


def make_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "web@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Web Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase-two-web-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "project"
        make_repository(self.workspace)
        self.app = create_app(
            data_dir=self.root / "data", env_file=self.root / ".env", runtime=FakeRuntime()
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)

    def save_project(self) -> dict:
        inspection = self.client.post(
            "/api/projects/inspect", json={"workspace": str(self.workspace)}
        )
        self.assertEqual(inspection.status_code, 200, inspection.text)
        self.assertTrue(inspection.json()["ready"])
        saved = self.client.post(
            "/api/projects",
            json={
                "name": "sample project",
                "workspace": str(self.workspace),
                "python_executable": inspection.json()["selected_interpreter"],
                "pytest_args": [],
                "extra_checks": [],
                "limits": {
                    "max_model_requests": 40,
                    "command_timeout_seconds": 120,
                    "output_limit_bytes": 1048576,
                },
            },
        )
        self.assertEqual(saved.status_code, 201, saved.text)
        return saved.json()

    def wait_status(self, task_id: str, status: str) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/tasks/{task_id}")
            if response.status_code == 200 and response.json()["status"] == status:
                return response.json()
            time.sleep(0.01)
        self.fail(f"Task did not reach {status}.")

    def test_project_to_task_clarification_events_and_result(self) -> None:
        profile = self.save_project()
        accepted = self.client.post(
            "/api/tasks", json={"project_id": profile["id"], "requirement": "Add behavior"}
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        task_id = accepted.json()["id"]
        waiting = self.wait_status(task_id, "needs_clarification")
        self.assertEqual(waiting["questions"], ["选择格式"])
        with self.client.stream("GET", f"/api/tasks/{task_id}/events?after=0") as events:
            self.assertEqual(events.status_code, 200)
            lines = []
            for line in events.iter_lines():
                lines.append(line)
                if line == "event: tool_started":
                    break
            event_text = "\n".join(lines)
            self.assertIn("event: tool_started", event_text)
            self.assertNotIn("workspace", event_text)
        answered = self.client.post(f"/api/tasks/{task_id}/answers", json={"answer": "JSON"})
        self.assertEqual(answered.status_code, 202)
        succeeded = self.wait_status(task_id, "succeeded")
        self.assertEqual(succeeded["commit"], "123abc")
        stream = self.client.get(f"/api/tasks/{task_id}/events?after=0")
        self.assertIn("event: task_status_changed", stream.text)
        self.assertIn("id: 6", stream.text)
        self.assertNotIn("JSON", stream.text)

    def test_project_revision_conflict_and_strict_error_contract(self) -> None:
        profile = self.save_project()
        conflict = self.client.patch(
            f"/api/projects/{profile['id']}",
            json={"expected_revision": 99, "name": "changed"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "REVISION_CONFLICT")
        invalid = self.client.post(
            "/api/tasks",
            json={"project_id": profile["id"], "requirement": "hello", "api_key": "secret"},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_ARGUMENTS")
        self.assertNotIn("secret", invalid.text)

    def test_local_origin_and_json_content_type_are_required(self) -> None:
        bad_host = self.client.get("/api/projects", headers={"host": "remote.example"})
        self.assertEqual(bad_host.status_code, 403)
        bad_origin = self.client.post(
            "/api/projects/inspect",
            json={"workspace": str(self.workspace)},
            headers={"origin": "https://remote.example"},
        )
        self.assertEqual(bad_origin.status_code, 403)
        no_json = self.client.post(
            "/api/projects/inspect", content="{}", headers={"content-type": "text/plain"}
        )
        self.assertEqual(no_json.status_code, 415)

    def test_production_app_serves_the_built_frontend(self) -> None:
        distribution = self.root / "frontend-dist"
        distribution.mkdir()
        (distribution / "index.html").write_text(
            "<!doctype html><title>My Agent</title>", encoding="utf-8"
        )
        with patch("app.web.application._frontend_distribution", return_value=distribution):
            app = create_app(
                data_dir=self.root / "static-data",
                env_file=self.root / ".env",
                runtime=FakeRuntime(),
            )
        with TestClient(app) as client:
            response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("My Agent", response.text)

    def test_artifact_content_is_accessed_by_registered_id(self) -> None:
        profile = self.save_project()
        task = self.client.post(
            "/api/tasks", json={"project_id": profile["id"], "requirement": "Add behavior"}
        ).json()
        self.wait_status(task["id"], "needs_clarification")
        task_dir = self.root / "data" / "runs" / task["id"]
        artifact_path = task_dir / "artifacts" / "sample.txt"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("approved", encoding="utf-8")
        reference = ArtifactRef(
            id="artifact-1",
            task_id=task["id"],
            kind="log",
            media_type="text/plain",
            size_bytes=8,
        )
        self.app.state.store.register_artifact(reference, artifact_path)
        content = self.client.get("/api/artifacts/artifact-1")
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.json()["text"], "approved")
        guessed = self.client.get("/api/artifacts/../../outside")
        self.assertIn(guessed.status_code, {400, 404})


if __name__ == "__main__":
    unittest.main()
