"""Runtime test discovery and Git task-scope behavior."""

import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from app.execution import ProcessManager
from app.git_ops import GitService
from app.tasks.runtime import AgentRuntime
from app.tooling import ToolContext, ToolRegistry
from app.workspaces import WorkspaceService


def make_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "runtime@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Runtime Test"], cwd=path, check=True)
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase-two-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        make_repository(self.workspace)

    def test_discovered_node_ids_match_the_actual_junit_report(self) -> None:
        tests = self.workspace / "tests"
        tests.mkdir()
        (tests / "test_values.py").write_text(
            "class TestValue:\n    def test_answer(self):\n        assert 42 == 42\n\n"
            "def test_plain():\n    assert True\n",
            encoding="utf-8",
        )
        expected = AgentRuntime.collect_expected_test_ids(sys.executable, self.workspace, [])
        report = self.workspace / "report.xml"
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", f"--junitxml={report}"],
            cwd=self.workspace,
            check=True,
            capture_output=True,
        )
        root = ET.parse(report).getroot()
        actual = {
            f"{case.get('classname', '')}.{case.get('name', '')}" for case in root.iter("testcase")
        }
        self.assertEqual(set(expected), actual)

    def test_registered_web_tool_surface_has_all_seventeen_operations(self) -> None:
        registrations = [
            *WorkspaceService().registrations(),
            *ProcessManager().registrations(),
            *GitService(self.workspace).registrations(),
        ]
        names = {item.name for item in registrations}
        names.add("run_checks")
        self.assertEqual(
            names,
            {
                "list_files",
                "search_code",
                "read_file",
                "create_file",
                "edit_file",
                "move_file",
                "delete_file",
                "create_directory",
                "remove_empty_directory",
                "run_command",
                "start_service",
                "get_service",
                "list_services",
                "stop_service",
                "git_status",
                "get_diff",
                "run_checks",
            },
        )
        self.assertEqual(len(ToolRegistry(registrations).schemas()), 16)

    def test_git_write_guards_preserve_initial_user_staged_files(self) -> None:
        staged = self.workspace / "README.md"
        staged.write_text("user staged\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.workspace, check=True)
        git = GitService(self.workspace)
        snapshot = git.snapshot()
        with self.assertRaises(Exception) as error:
            git.validate_write("README.md", "edit", snapshot)
        self.assertEqual(error.exception.code, "USER_CHANGE_CONFLICT")
        self.assertEqual(staged.read_text(encoding="utf-8"), "user staged\n")

    def test_task_diff_includes_new_file_without_staging_it(self) -> None:
        git = GitService(self.workspace)
        snapshot = git.snapshot()
        new_file = self.workspace / "new.ts"
        new_file.write_text("export const answer = 42;\n", encoding="utf-8")
        context = ToolContext(
            task_id="task",
            project_id="project",
            workspace=self.workspace,
            artifact_dir=self.workspace / "artifacts",
            services={
                "git_snapshot": snapshot,
                "task_changes": [{"operation": "create", "path": "new.ts"}],
            },
        )
        result = git.diff("task", context)
        self.assertIn("new.ts", result["text"])
        self.assertIn("export const answer", result["text"])
        self.assertEqual(result["files"][0].change, "added")
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "new.ts"],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("??", status.stdout)


if __name__ == "__main__":
    unittest.main()
