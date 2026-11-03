"""Project environment checks and revision handling tests."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from app.models_v2 import ProjectInput
from app.storage import StateStore
from app.tasks.projects import ProjectService, ProjectServiceError
from app.tooling import ExecutionLimits


def make_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Phase Two Test"], cwd=path, check=True)
    (path / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


class ProjectServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase-two-projects-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "project"
        make_repository(self.workspace)
        self.store = StateStore(self.root / "data")
        self.service = ProjectService(self.store)

    def profile(self, name: str = "sample") -> ProjectInput:
        inspection = self.service.inspect_project(str(self.workspace))
        assert inspection.selected_interpreter
        return ProjectInput(
            name=name,
            workspace=str(self.workspace),
            python_executable=inspection.selected_interpreter,
            pytest_args=[],
            limits=ExecutionLimits(),
        )

    def test_inspection_reports_real_git_and_pytest_readiness(self) -> None:
        result = self.service.inspect_project(str(self.workspace))
        self.assertTrue(result.is_git_repository)
        self.assertIsNotNone(result.head)
        self.assertTrue(result.pytest_available)
        self.assertTrue(result.ready)

    def test_save_and_patch_use_expected_revision_and_protect_test_configuration(self) -> None:
        profile = self.service.save_project(self.profile())
        self.assertEqual(profile.revision, 1)
        revised = self.service.patch_project(profile.id, {"name": "updated"}, expected_revision=1)
        self.assertEqual(revised.revision, 2)
        self.assertEqual(revised.name, "updated")
        unchanged = self.service.patch_project(profile.id, {"name": "updated"}, expected_revision=2)
        self.assertEqual(unchanged.revision, 2)
        with self.assertRaises(ProjectServiceError) as error:
            self.service.patch_project(profile.id, {"name": "conflict"}, expected_revision=1)
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")
        reset = self.service.save_project(
            self.profile(), project_id=profile.id, expected_revision=2
        )
        self.assertEqual(reset.revision, 3)

    def test_unready_repository_and_reserved_pytest_options_are_rejected(self) -> None:
        non_repository = self.root / "plain"
        non_repository.mkdir()
        self.assertFalse(self.service.inspect_project(str(non_repository)).ready)
        profile = self.profile()
        invalid = profile.model_copy(update={"pytest_args": ["--collect-only"]})
        with self.assertRaises(ProjectServiceError) as error:
            self.service.save_project(invalid)
        self.assertEqual(error.exception.code, "INVALID_ARGUMENTS")


if __name__ == "__main__":
    unittest.main()
