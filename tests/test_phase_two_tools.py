"""Behavior tests for the second-stage registered workspace tools."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.tooling import ToolContext, ToolRegistry
from app.workspaces import WorkspaceService
from app.workspaces.service import EditFileArgs, ReadFileArgs, WorkspaceFailure


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase-two-workspace-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.changes: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.context = ToolContext(
            task_id="task-1",
            project_id="project-1",
            workspace=self.workspace,
            artifact_dir=self.workspace / "artifacts",
            phase="implementation",
            record_change=self.changes.append,
            emit=lambda **event: self.events.append(event),
        )
        self.service = WorkspaceService()

    def test_edit_preserves_bom_and_crlf_and_records_hashes(self) -> None:
        target = self.workspace / "source.py"
        original = b"\xef\xbb\xbfvalue = 1\r\nkeep = 2\n"
        target.write_bytes(original)
        result = self.service.edit_file(
            EditFileArgs(
                path="source.py",
                old_text="value = 1\n",
                new_text="value = 3\n",
                expected_hash=hashlib.sha256(original).hexdigest(),
            ),
            self.context,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfvalue = 3\r\nkeep = 2\n")
        self.assertEqual(self.changes[0]["operation"], "edit")
        self.assertNotEqual(self.changes[0]["before_sha256"], self.changes[0]["after_sha256"])

    def test_read_returns_version_and_rejects_credential_and_git_paths(self) -> None:
        target = self.workspace / "sample.ts"
        target.write_text("const answer = 42\n", encoding="utf-8")
        result = self.service.read_file(ReadFileArgs(path="sample.ts"), self.context)
        self.assertEqual(result["content"], "const answer = 42\r\n")
        self.assertEqual(result["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        for path in (".env", ".env.local", ".git/config"):
            with self.subTest(path=path), self.assertRaises(WorkspaceFailure) as error:
                self.service.read_file(ReadFileArgs(path=path), self.context)
            self.assertEqual(error.exception.code, "PROTECTED_PATH")

    def test_stale_or_ambiguous_edits_make_no_change(self) -> None:
        target = self.workspace / "sample.py"
        target.write_text("x = 1\nx = 1\n", encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        with self.assertRaises(WorkspaceFailure) as error:
            self.service.edit_file(
                EditFileArgs(
                    path="sample.py", old_text="x = 1", new_text="x = 2", expected_hash=digest
                ),
                self.context,
            )
        self.assertEqual(error.exception.code, "NON_UNIQUE_MATCH")
        with self.assertRaises(WorkspaceFailure) as error:
            self.service.edit_file(
                EditFileArgs(
                    path="sample.py", old_text="x = 1\n", new_text="x = 2\n", expected_hash="0" * 64
                ),
                self.context,
            )
        self.assertEqual(error.exception.code, "STALE_FILE")
        self.assertEqual(target.read_text(encoding="utf-8"), "x = 1\nx = 1\n")

    def test_file_operations_do_not_overwrite_or_recursively_delete(self) -> None:
        from app.workspaces.service import CreateFileArgs, DirectoryArgs, MoveFileArgs

        (self.workspace / "parent").mkdir()
        target = self.workspace / "parent" / "original.txt"
        target.write_text("content", encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        with self.assertRaises(WorkspaceFailure):
            self.service.create_file(
                CreateFileArgs(path="parent/original.txt", content="overwrite"),
                self.context,
            )
        moved = self.service.move_file(
            MoveFileArgs(
                source="parent/original.txt", target="parent/moved.txt", expected_hash=digest
            ),
            self.context,
        )
        self.assertEqual(moved["target"], "parent/moved.txt")
        (self.workspace / "parent" / "nested.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(WorkspaceFailure) as error:
            self.service.remove_empty_directory(DirectoryArgs(path="parent"), self.context)
        self.assertEqual(error.exception.code, "DIRECTORY_NOT_EMPTY")
        self.assertTrue((self.workspace / "parent" / "nested.txt").exists())


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase-two-registry-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.events: list[dict[str, object]] = []
        self.context = ToolContext(
            task_id="task-1",
            project_id="project-1",
            workspace=self.workspace,
            artifact_dir=self.workspace / "artifacts",
            emit=lambda **event: self.events.append(event),
        )
        self.service = WorkspaceService()
        self.registry = ToolRegistry(self.service.registrations())

    def test_schemas_match_strict_parameter_models(self) -> None:
        schemas = self.registry.schemas()
        self.assertEqual(len(schemas), 9)
        edit = next(
            schema["function"] for schema in schemas if schema["function"]["name"] == "edit_file"
        )
        self.assertEqual(edit["parameters"]["additionalProperties"], False)
        self.assertIn("expected_hash", edit["parameters"]["required"])

    def test_unknown_tool_and_invalid_arguments_have_no_side_effects(self) -> None:
        unknown = self.registry.invoke("not_registered", {}, self.context)
        invalid = self.registry.invoke(
            "create_file", {"path": "new.py", "content": "x", "extra": 1}, self.context
        )
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.error.code, "UNKNOWN_TOOL")
        self.assertFalse(invalid.ok)
        self.assertEqual(invalid.error.code, "INVALID_ARGUMENTS")
        self.assertFalse((self.workspace / "new.py").exists())

    def test_registered_create_invokes_real_file_operation(self) -> None:
        result = self.registry.invoke(
            "create_file", {"path": "new.ts", "content": "const x = 1;\n"}, self.context
        )
        self.assertTrue(result.ok)
        self.assertEqual((self.workspace / "new.ts").read_text(encoding="utf-8"), "const x = 1;\n")
        self.assertEqual(
            [event["type"] for event in self.events],
            ["tool_started", "file_changed", "tool_completed"],
        )


if __name__ == "__main__":
    unittest.main()
