"""Check that acceptance fixtures distinguish correct and incorrect behavior."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROJECT = FIXTURES / "projects" / "average_project"
CHECKS = FIXTURES / "checks"


class AcceptanceFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-fixture-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "project"
        shutil.copytree(PROJECT, self.workspace)
        self.implementations = json.loads(
            (FIXTURES / "model_outputs" / "implementations.json").read_text(
                encoding="utf-8"
            )
        )

    def run_checks(self, start_directory, pattern):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                str(start_directory),
                "-p",
                pattern,
                "-v",
            ],
            cwd=self.workspace,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    def use_implementation(self, name):
        target = self.workspace / "sample_stats" / "statistics.py"
        with target.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(self.implementations[name])

    def test_original_project_passes_all_six_regression_tests(self):
        result = self.run_checks("tests", "test_*.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 6 tests", result.stderr)

    def test_original_project_exposes_the_missing_empty_input_requirement(self):
        result = self.run_checks(CHECKS, "average_acceptance.py")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("test_empty_input_returns_none", result.stderr)
        self.assertIn("ZeroDivisionError", result.stderr)
        self.assertIn("FAILED (errors=1)", result.stderr)

    def test_correct_implementation_passes_acceptance_and_regression(self):
        self.use_implementation("correct")
        for directory, pattern in [(CHECKS, "average_acceptance.py"), ("tests", "test_*.py")]:
            with self.subTest(directory=str(directory)):
                result = self.run_checks(directory, pattern)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_oracle_rejects_typical_incorrect_implementations(self):
        cases = {
            "empty_zero": "test_empty_input_returns_none",
            "all_none": "test_existing_numeric_examples",
            "wrong_mean": "test_existing_numeric_examples",
            "mutates_input": "test_input_is_not_modified",
        }
        for implementation, failing_test in cases.items():
            with self.subTest(implementation=implementation):
                self.use_implementation(implementation)
                result = self.run_checks(CHECKS, "average_acceptance.py")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(failing_test, result.stderr)
                self.assertIn("FAILED", result.stderr)

    def test_requirement_data_contains_known_expected_examples(self):
        data = json.loads(
            (FIXTURES / "requirements" / "average_empty.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(data["examples"][0], {"input": [], "expected": None})
        self.assertIn({"input": [2, 4], "expected": 3.0}, data["examples"])


if __name__ == "__main__":
    unittest.main()
