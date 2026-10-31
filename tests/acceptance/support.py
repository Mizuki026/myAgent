"""Temporary projects, CLI invocation and independent acceptance assertions."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.acceptance.fake_model import ScriptedModelServer, final_message, tool_message


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
TARGET = "sample_stats/statistics.py"
ACCEPTANCE = "checks/test_requirement.py"
REGRESSION_IDS = [
    "test_statistics.OriginalBehaviorTests." + name
    for name in [
        "test_mean_of_positive_numbers", "test_single_value",
        "test_negative_and_positive_values", "test_input_is_not_modified",
        "test_existing_caller", "test_unrelated_same_named_function",
    ]
]
ACCEPTANCE_IDS = [
    "test_requirement.AverageAcceptanceTests." + name
    for name in [
        "test_empty_input_returns_none", "test_single_zero",
        "test_existing_numeric_examples", "test_input_is_not_modified",
        "test_caller_still_formats_result",
    ]
]


def digest(content):
    return hashlib.sha256(content).hexdigest()


def step(message, **fields):
    return {"message": message, **fields}


def command(arguments, cwd, timeout=15, environment=None):
    return subprocess.run(
        arguments, cwd=cwd, env=environment, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )


class AgentAcceptanceCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-acceptance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "project"
        shutil.copytree(FIXTURES / "projects" / "average_project", self.workspace)
        (self.workspace / ".gitignore").write_text(
            ".env\n__pycache__/\n*.pyc\n", encoding="utf-8"
        )
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")
        self.git("add", ".")
        self.git(
            "-c", "user.name=Acceptance Test", "-c", "user.email=test@example.invalid",
            "commit", "--quiet", "-m", "fixture baseline",
        )
        self.baseline = self.git("rev-parse", "HEAD").strip()
        self.original = (self.workspace / TARGET).read_bytes()
        self.unrelated = (self.workspace / "sample_stats/text.py").read_bytes()
        self.implementations = json.loads(
            (FIXTURES / "model_outputs/implementations.json").read_text(encoding="utf-8")
        )
        self.acceptance_source = (FIXTURES / "checks/average_acceptance.py").read_text(
            encoding="utf-8"
        )
        self.task_id = self._testMethodName
        self.secret = "acceptance-only-secret-4f98a1"
        self.invocations = 0

    def git(self, *arguments):
        result = command(["git", *arguments], self.workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def write_source(self, relative_path, content):
        with (self.workspace / relative_path).open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)

    def model(self, steps):
        server = ScriptedModelServer(steps, expected_api_key=self.secret).start()
        self.addCleanup(server.close)
        return server

    def edit(self, relative_path, before, after, call_id, **fields):
        return step(tool_message("edit_file", {
            "path": relative_path,
            "old_text": before,
            "new_text": after,
            "expected_hash": digest(before.encode("utf-8")),
        }, call_id), **fields)

    def preparation(self):
        steps = [step(tool_message("search_code", {"pattern": "average"}, "search"))]
        for number, path in enumerate([TARGET, "sample_stats/summary.py", "tests/test_statistics.py"]):
            steps.append(step(tool_message("read_file", {
                "path": path, "start_line": 1, "end_line": 200,
            }, f"read-{number}")))
        before = (self.workspace / ACCEPTANCE).read_text(encoding="utf-8")
        steps.append(self.edit(ACCEPTANCE, before, self.acceptance_source, "write-acceptance"))
        steps.append(step(final_message("acceptance_ready", summary="验收测试已写好，请先运行并固定版本。")))
        return steps

    def implementation_steps(self, candidates):
        steps = self.preparation()
        previous = (self.workspace / TARGET).read_text(encoding="utf-8")
        for index, name in enumerate(candidates, 1):
            candidate = self.implementations[name]
            steps.append(self.edit(
                TARGET, previous, candidate, f"implementation-{index}", implementation=True,
            ))
            steps.append(step(final_message(
                "implementation_ready", summary="本轮实现已完成，请执行实际验证。",
            )))
            previous = candidate
        return steps

    def default_request(self, server):
        requirement = json.loads(
            (FIXTURES / "requirements/average_empty.json").read_text(encoding="utf-8")
        )["requirement"]
        suite_runner = str(FIXTURES / "commands/run_suite.py")
        check_specs = []
        for name, directory, pattern, identifiers in [
            ("regression", "tests", "test_*.py", REGRESSION_IDS),
            ("acceptance", "checks", "test_requirement.py", ACCEPTANCE_IDS),
        ]:
            check_specs.append({
                "name": name,
                "argv": [sys.executable, "-B", suite_runner, "--start", directory,
                         "--pattern", pattern, "--report", "{report}"],
                "report_format": "junit_xml",
                "expected_test_ids": identifiers,
            })
        return {
            "schema_version": 1, "operation": "run", "task_id": self.task_id,
            "workspace": str(self.workspace), "state_dir": str(self.root / "state"),
            "env_file": str(self.workspace / ".env"), "requirement": requirement,
            "model": {"base_url": server.base_url, "name": "fixture-model",
                      "api_key_env": "AGENT_TEST_API_KEY", "stream": False},
            "limits": {"max_implementation_attempts": 3, "max_model_requests": 40,
                       "command_timeout_seconds": 2, "output_limit_bytes": 4096},
            "checks": check_specs,
            "acceptance_paths": [ACCEPTANCE],
        }

    def invoke(self, server, request=None):
        self.invocations += 1
        request = request or self.default_request(server)
        request_path = self.root / f"request-{self.invocations}.json"
        report_path = self.root / f"report-{self.invocations}.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        (self.workspace / ".env").write_text(
            f"DEEPSEEK_API_KEY={self.secret}\nDEEPSEEK_BASE_URL={server.base_url}\n",
            encoding="utf-8",
        )
        allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
                   "USERPROFILE", "APPDATA", "LOCALAPPDATA", "COMSPEC", "HOME", "LANG"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment.update({
            "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8", "AGENT_TEST_API_KEY": self.secret,
            "DEEPSEEK_API_KEY": self.secret, "DEEPSEEK_BASE_URL": server.base_url,
            "AGENT_ENV_FILE": str(self.workspace / ".env"), "NO_PROXY": "127.0.0.1,localhost",
        })
        configured = os.environ.get("AGENT_ACCEPTANCE_COMMAND")
        driver = json.loads(configured) if configured else [sys.executable, "-B", "-m", "app"]
        self.assertTrue(
            isinstance(driver, list) and driver and all(isinstance(arg, str) for arg in driver),
            "AGENT_ACCEPTANCE_COMMAND must be a nonempty JSON string array",
        )
        result = command(
            [*driver, "--request", str(request_path), "--report", str(report_path)],
            self.root, timeout=35, environment=environment,
        )
        self.assertTrue(
            report_path.is_file(),
            "Agent CLI did not produce a report. Implement the CLI contract or set "
            f"AGENT_ACCEPTANCE_COMMAND. Exit={result.returncode}; stderr={result.stderr[-2000:]}",
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for field in ["schema_version", "task_id", "status", "implementation_attempts",
                      "events", "checks", "artifacts", "commit", "summary"]:
            self.assertIn(field, report, f"Missing report field: {field}")
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["task_id"], self.task_id)
        exit_codes = {"succeeded": 0, "no_changes": 0, "failed": 1,
                      "needs_clarification": 2, "paused": 3}
        self.assertIn(report["status"], exit_codes)
        self.assertEqual(result.returncode, exit_codes[report["status"]], result.stderr)
        self.assertGreaterEqual(report["implementation_attempts"], 0)
        self.assertLessEqual(report["implementation_attempts"], 3)
        self.assertEqual(server.errors, [], f"Unexpected model requests: {server.errors}")
        return report

    def assert_real_behavior(self):
        for directory, pattern in [
            (FIXTURES / "checks", "average_acceptance.py"), ("tests", "test_*.py"),
        ]:
            result = command([
                sys.executable, "-B", "-m", "unittest", "discover",
                "-s", str(directory), "-p", pattern, "-v",
            ], self.workspace)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / "sample_stats/text.py").read_bytes(), self.unrelated)

    def assert_success(self, report, attempts):
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(report["implementation_attempts"], attempts)
        self.assert_real_behavior()
        self.assertIsInstance(report["commit"], str)
        self.assertNotEqual(report["commit"], self.baseline)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), report["commit"])
        self.assertEqual(self.git("status", "--porcelain"), "")
        changed = self.git("diff", "--name-only", self.baseline, report["commit"]).splitlines()
        self.assertIn(TARGET, changed)
        self.assertIn(ACCEPTANCE, changed)
        for name, expected in [("regression", REGRESSION_IDS), ("acceptance", ACCEPTANCE_IDS)]:
            checks = [item for item in report["checks"]
                      if item["name"] == name and item["attempt"] == attempts]
            self.assertTrue(checks, f"Missing final {name} execution")
            check = checks[-1]
            self.assertEqual(check["returncode"], 0)
            path = Path(check["report_path"]).resolve()
            self.assertTrue(path.is_relative_to(self.root))
            root = ET.parse(path).getroot()
            cases = list(root.iter("testcase"))
            identifiers = {f"{case.get('classname')}.{case.get('name')}" for case in cases}
            self.assertTrue(set(expected).issubset(identifiers))
            self.assertFalse(any(case.find(tag) is not None for case in cases
                                 for tag in ["failure", "error", "skipped"]))

    def assert_error_event(self, report, codes):
        self.assertEqual(report["status"], "failed", report)
        errors = [event for event in report["events"] if event.get("code") in codes]
        self.assertTrue(errors, f"Missing error evidence for {codes}: {report['events']}")
        self.assertTrue(report["summary"].strip())
        self.assertIsNone(report["commit"])

    def fault_command(self, request, check_name, mode):
        specification = next(item for item in request["checks"] if item["name"] == check_name)
        specification["argv"] = [
            sys.executable, "-B", str(FIXTURES / "commands/report_fault.py"),
            mode, "--report", "{report}", "--suite", check_name,
        ]

    def cleanup_probe(self, directory):
        pid_file = directory / "pids.json"
        if not pid_file.exists():
            return
        for pid in reversed(json.loads(pid_file.read_text(encoding="utf-8"))):
            if os.name == "nt":
                command(["taskkill", "/PID", str(pid), "/T", "/F"], self.root)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def assert_probe_stopped(self, directory):
        heartbeat = directory / "heartbeat"
        self.assertTrue(heartbeat.exists(), "Probe never started its child process")
        size = heartbeat.stat().st_size
        time.sleep(0.2)
        self.assertEqual(heartbeat.stat().st_size, size, "Timed-out child is still running")
