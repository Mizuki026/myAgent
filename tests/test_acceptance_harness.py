"""Self-tests for test infrastructure; these are not product acceptance."""

import json
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from unittest.mock import patch

from tests.acceptance.fake_model import ScriptedModelServer, final_message, tool_message
from tests.acceptance.support import FIXTURES, REGRESSION_IDS, AgentAcceptanceCase, command, step


def post_completion(server, messages, api_key=None):
    headers = {"Content-Type": "application/json"}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        server.base_url + "/chat/completions",
        data=json.dumps({"model": "fixture-model", "messages": messages, "stream": False}).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


class ModelServerTests(unittest.TestCase):
    def test_external_server_returns_scripted_tools_and_records_feedback(self):
        server = ScriptedModelServer([
            step(tool_message("read_file", {"path": "sample.py"}, "read-1")),
            step(final_message("implementation_ready"), implementation=True),
        ]).start()
        self.addCleanup(server.close)
        response = post_completion(server, [{"role": "user", "content": "change sample"}])
        message = response["choices"][0]["message"]
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "read_file")
        response = post_completion(server, [{"role": "tool", "tool_call_id": "read-1", "content": "file missing"}])
        self.assertEqual(response["choices"][0]["finish_reason"], "stop")
        self.assertEqual(server.requests[1]["messages"][0]["content"], "file missing")
        self.assertEqual(server.implementation_replies, 1)

    def test_unexpected_extra_model_request_is_rejected(self):
        server = ScriptedModelServer([]).start()
        self.addCleanup(server.close)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            post_completion(server, [{"role": "user", "content": "unexpected"}])
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("script exhausted", server.errors[0])

    def test_test_api_key_is_required_but_not_recorded_in_messages(self):
        server = ScriptedModelServer(
            [step(final_message("acceptance_ready"))], expected_api_key="fixture-key"
        ).start()
        self.addCleanup(server.close)
        with self.assertRaises(urllib.error.HTTPError):
            post_completion(server, [{"role": "user", "content": "request"}])
        post_completion(server, [{"role": "user", "content": "request"}], api_key="fixture-key")
        self.assertEqual(len(server.delivered), 1)
        self.assertNotIn("fixture-key", json.dumps(server.requests))


class HarnessInfrastructureTests(AgentAcceptanceCase):
    def test_missing_agent_report_is_a_failure_with_actionable_message(self):
        driver = [sys.executable, "-B", "-c", "raise SystemExit(1)"]
        with patch.dict("os.environ", {"AGENT_ACCEPTANCE_COMMAND": json.dumps(driver)}):
            with self.assertRaisesRegex(AssertionError, "Agent CLI did not produce a report"):
                self.invoke(self.model([]))

    def test_empty_driver_configuration_is_rejected(self):
        with patch.dict("os.environ", {"AGENT_ACCEPTANCE_COMMAND": "[]"}):
            with self.assertRaisesRegex(AssertionError, "nonempty JSON string array"):
                self.invoke(self.model([]))

    def test_suite_runner_reports_actual_regression_test_identifiers(self):
        report = self.root / "regression.xml"
        result = command([
            sys.executable, "-B", str(FIXTURES / "commands/run_suite.py"),
            "--start", "tests", "--report", str(report),
        ], self.workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        root = ET.parse(report).getroot()
        self.assertEqual(root.get("tests"), "6")
        identifiers = {f"{case.get('classname')}.{case.get('name')}" for case in root}
        self.assertEqual(identifiers, set(REGRESSION_IDS))

    def test_suite_runner_records_the_unsatisfied_requirement(self):
        report = self.root / "acceptance.xml"
        result = command([
            sys.executable, "-B", str(FIXTURES / "commands/run_suite.py"),
            "--start", str(FIXTURES / "checks"), "--pattern", "average_acceptance.py",
            "--report", str(report),
        ], self.workspace)
        self.assertEqual(result.returncode, 1, result.stderr)
        root = ET.parse(report).getroot()
        self.assertEqual(root.get("errors"), "1")
        errors = [case for case in root if case.find("error") is not None]
        self.assertEqual(errors[0].get("name"), "test_empty_input_returns_none")

    def test_fault_reports_are_distinguishable(self):
        for mode in ["zero", "skipped", "missing", "corrupt", "failing"]:
            with self.subTest(mode=mode):
                report = self.root / f"{mode}.xml"
                result = command([
                    sys.executable, "-B", str(FIXTURES / "commands/report_fault.py"),
                    mode, "--report", str(report),
                ], self.workspace)
                self.assertEqual(result.returncode, 1 if mode == "failing" else 0)
                if mode == "missing":
                    self.assertFalse(report.exists())
                elif mode == "corrupt":
                    with self.assertRaises(ET.ParseError):
                        ET.parse(report)
                else:
                    root = ET.parse(report).getroot()
                    self.assertEqual(root.get("tests"), "0" if mode == "zero" else "6")
                    if mode == "skipped":
                        self.assertEqual(root.get("skipped"), "1")

    def test_skipped_acceptance_report_contains_the_expected_case_ids(self):
        from tests.acceptance.support import ACCEPTANCE_IDS
        report = self.root / "skipped-acceptance.xml"
        result = command([
            sys.executable, "-B", str(FIXTURES / "commands/report_fault.py"), "skipped",
            "--report", str(report), "--suite", "acceptance",
        ], self.workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        root = ET.parse(report).getroot()
        self.assertEqual(root.get("skipped"), "1")
        self.assertEqual(
            {f"{case.get('classname')}.{case.get('name')}" for case in root},
            set(ACCEPTANCE_IDS),
        )

    def test_timeout_probe_starts_a_child_and_emergency_cleanup_stops_it(self):
        probe = self.root / "probe"
        process = subprocess.Popen(
            [sys.executable, "-B", str(FIXTURES / "commands/process_probe.py"), "parent", str(probe)],
            cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait, timeout=5)
        self.addCleanup(self.cleanup_probe, probe)
        deadline = time.monotonic() + 5
        while not (probe / "heartbeat").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((probe / "heartbeat").exists())
        self.cleanup_probe(probe)
        process.wait(timeout=5)
        self.assert_probe_stopped(probe)


if __name__ == "__main__":
    unittest.main()
