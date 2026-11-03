"""Persistent state tests for web tasks, events, and registered artifacts."""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.models_v2 import ProjectProfile, TaskSnapshot
from app.storage import StateStore
from app.storage.state_store import StoreError
from app.tooling import ArtifactRef, ExecutionLimits


def now() -> str:
    return datetime.now(UTC).isoformat()


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase-two-storage-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.store = StateStore(self.root / "data")

    def project(
        self, project_id: str = "project-1", workspace: Path | None = None
    ) -> ProjectProfile:
        timestamp = now()
        return ProjectProfile(
            id=project_id,
            name="sample",
            workspace=str(workspace or self.workspace),
            python_executable="python.exe",
            limits=ExecutionLimits(),
            revision=1,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def task(self, task_id: str = "task-1") -> TaskSnapshot:
        timestamp = now()
        return TaskSnapshot(
            id=task_id,
            project_id="project-1",
            requirement="Add a helper",
            status="running",
            created_at=timestamp,
            updated_at=timestamp,
        )

    def test_project_revision_and_duplicate_workspace_are_enforced(self) -> None:
        profile = self.project()
        self.store.save_project(profile)
        revised = profile.model_copy(update={"revision": 2, "name": "renamed", "updated_at": now()})
        self.store.save_project(revised, expected_revision=1)
        self.assertEqual(self.store.get_project(profile.id).revision, 2)
        with self.assertRaises(StoreError) as error:
            self.store.save_project(self.project("project-2"))
        self.assertEqual(error.exception.code, "PROJECT_CONFLICT")

    def test_event_sequence_is_atomic_and_active_task_slot_is_unique(self) -> None:
        self.store.save_project(self.project())
        self.store.create_task(self.task(), {"secret": "not an API field"})
        first = self.store.append_event("task-1", "tool_started", {"tool": "read_file"})
        second = self.store.append_event("task-1", "tool_completed", {"tool": "read_file"})
        self.assertEqual((first.seq, second.seq), (2, 3))
        self.assertEqual(self.store.get_task("task-1").last_event_seq, 3)
        with self.assertRaises(StoreError) as error:
            self.store.create_task(self.task("task-2"), {})
        self.assertEqual(error.exception.code, "TASK_BUSY")
        self.assertEqual([event.seq for event in self.store.events_after("task-1", 0)], [1, 2, 3])

    def test_artifacts_are_read_by_id_with_utf8_byte_offsets(self) -> None:
        self.store.save_project(self.project())
        self.store.create_task(self.task(), {})
        task_dir = self.root / "data" / "runs" / "task-1"
        task_dir.mkdir(parents=True)
        artifact_path = task_dir / "result.txt"
        artifact_path.write_text("A雪B", encoding="utf-8")
        reference = ArtifactRef(
            id="artifact-1",
            task_id="task-1",
            kind="result",
            media_type="text/plain",
            size_bytes=artifact_path.stat().st_size,
        )
        self.store.register_artifact(reference, artifact_path)
        initial = self.store.read_artifact(reference.id, limit=2)
        self.assertEqual(initial.text, "A")
        following = self.store.read_artifact(reference.id, offset=initial.next_offset, limit=4)
        self.assertEqual(following.text, "雪B")
        self.assertTrue(following.eof)
        with self.assertRaises(StoreError) as error:
            self.store.read_artifact(reference.id, offset=100)
        self.assertEqual(error.exception.code, "INVALID_OFFSET")

    def test_restart_marks_active_tasks_interrupted_without_resuming(self) -> None:
        self.store.save_project(self.project())
        self.store.create_task(self.task(), {})
        self.assertEqual(self.store.mark_unfinished_interrupted(), ["task-1"])
        self.assertEqual(self.store.get_task("task-1").status, "interrupted")


if __name__ == "__main__":
    unittest.main()
