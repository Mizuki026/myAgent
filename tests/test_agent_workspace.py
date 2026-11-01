"""Deterministic self-tests for the agent's guarded file editing tools."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.workspace import WorkspaceTools


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-workspace-tools-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.events = []
        self.state = {
            "acceptance_paths": ["checks/test_requirement.py"],
            "frozen_acceptance": {},
            "modified_paths": [],
            "latest_hashes": {},
            "edits": [],
            "implementation_paths": [],
        }
        self.tools = WorkspaceTools(
            self.workspace, self.state, lambda **event: self.events.append(event), lambda text: text
        )

    def test_exact_edit_preserves_utf8_bom_and_crlf(self):
        target = self.workspace / "sample.py"
        original = b"\xef\xbb\xbfdef answer():\r\n    return 1\r\n"
        target.write_bytes(original)
        result = self.tools.call(
            "edit_file",
            {
                "path": "sample.py",
                "old_text": "def answer():\n    return 1\n",
                "new_text": "def answer():\n    return 2\n",
                "expected_hash": hashlib.sha256(original).hexdigest(),
            },
            "implementation",
            1,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfdef answer():\r\n    return 2\r\n")
        self.assertEqual(self.state["implementation_paths"], ["sample.py"])
        self.assertEqual(self.events[0]["type"], "implementation_changed")

    def test_edit_preserves_mixed_newlines_outside_the_replaced_span(self):
        target = self.workspace / "mixed.py"
        original = b"before = 1\r\nanswer = 1\n after = 2\r\n"
        target.write_bytes(original)
        result = self.tools.call(
            "edit_file",
            {
                "path": "mixed.py",
                "old_text": "answer = 1\n",
                "new_text": "answer = 3\n",
                "expected_hash": hashlib.sha256(original).hexdigest(),
            },
            "implementation",
            1,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(target.read_bytes(), b"before = 1\r\nanswer = 3\n after = 2\r\n")

    def test_edit_preserves_python_source_encoding_cookie(self):
        target = self.workspace / "latin1.py"
        original = b"# -*- coding: latin-1 -*-\nname = 'caf\xe9'\n"
        target.write_bytes(original)
        result = self.tools.call(
            "edit_file",
            {
                "path": "latin1.py",
                "old_text": "name = 'café'\n",
                "new_text": "name = 'crème'\n",
                "expected_hash": hashlib.sha256(original).hexdigest(),
            },
            "implementation",
            1,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(target.read_bytes(), b"# -*- coding: latin-1 -*-\nname = 'cr\xe8me'\n")

    def test_stale_version_is_rejected_without_overwriting_external_change(self):
        target = self.workspace / "sample.py"
        target.write_text("value = 1\n", encoding="utf-8")
        stale_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        target.write_text("value = 3\n", encoding="utf-8")
        result = self.tools.call(
            "edit_file",
            {
                "path": "sample.py",
                "old_text": "value = 1\n",
                "new_text": "value = 2\n",
                "expected_hash": stale_hash,
            },
            "implementation",
            1,
        )
        self.assertEqual(result["error"]["code"], "STALE_FILE")
        self.assertEqual(target.read_text(encoding="utf-8"), "value = 3\n")

    def test_nonunique_text_and_frozen_acceptance_are_rejected(self):
        target = self.workspace / "sample.py"
        target.write_text("x = 1\nx = 1\n", encoding="utf-8")
        result = self.tools.call(
            "edit_file",
            {
                "path": "sample.py",
                "old_text": "x = 1",
                "new_text": "x = 2",
                "expected_hash": hashlib.sha256(target.read_bytes()).hexdigest(),
            },
            "implementation",
            1,
        )
        self.assertEqual(result["error"]["code"], "NON_UNIQUE_MATCH")
        frozen_path = self.workspace / "checks" / "test_requirement.py"
        frozen_path.parent.mkdir()
        frozen_path.write_text("assert True\n", encoding="utf-8")
        self.state["frozen_acceptance"]["checks/test_requirement.py"] = hashlib.sha256(
            frozen_path.read_bytes()
        ).hexdigest()
        result = self.tools.call(
            "edit_file",
            {
                "path": "checks/test_requirement.py",
                "old_text": "assert True\n",
                "new_text": "assert False\n",
                "expected_hash": self.state["frozen_acceptance"]["checks/test_requirement.py"],
            },
            "implementation",
            1,
        )
        self.assertEqual(result["error"]["code"], "PROTECTED_ACCEPTANCE")
        self.assertEqual(frozen_path.read_text(encoding="utf-8"), "assert True\n")

    def test_outside_workspace_path_is_rejected(self):
        outside_directory = tempfile.TemporaryDirectory(prefix="agent-outside-")
        self.addCleanup(outside_directory.cleanup)
        outside = Path(outside_directory.name) / "private.txt"
        outside.write_text("private\n", encoding="utf-8")
        result = self.tools.call(
            "read_file",
            {"path": "../agent-outside.txt", "start_line": 1, "end_line": 1},
            "preparation",
            0,
        )
        self.assertEqual(result["error"]["code"], "OUTSIDE_WORKSPACE")
        self.assertEqual(outside.read_text(encoding="utf-8"), "private\n")


if __name__ == "__main__":
    unittest.main()
