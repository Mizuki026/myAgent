"""Phase-one behavior acceptance through the real agent CLI."""

import json
import os
import sys
from pathlib import Path

from tests.acceptance.fake_model import final_message, tool_message
from tests.acceptance.support import ACCEPTANCE, FIXTURES, TARGET, AgentAcceptanceCase, digest, step


class CodingAgentAcceptanceTests(AgentAcceptanceCase):
    def failed_tool_steps(self, name, arguments, before_send=None):
        steps = self.preparation()
        for index in range(1, 4):
            fields = {"before_send": before_send} if index == 1 and before_send else {}
            steps.append(step(tool_message(name, arguments, f"rejected-{index}"), **fields))
            steps.append(step(final_message("implementation_ready", summary="已完成，请验证。")))
        return steps

    def test_A01_first_implementation_passes_real_tests_and_is_committed(self):
        server = self.model(self.implementation_steps(["correct"]))
        report = self.invoke(server)
        self.assert_success(report, 1)
        self.assertEqual(server.implementation_replies, 1)
        events = report["events"]
        frozen = next(item for item in events if item["type"] == "acceptance_frozen")
        modified = next(item for item in events if item["type"] == "implementation_changed")
        self.assertLess(frozen["seq"], modified["seq"])
        self.assertEqual(frozen["sha256"], digest((self.workspace / ACCEPTANCE).read_bytes()))
        self.assertTrue(any(item.get("tool") == "search_code" for item in events))
        reads = {item.get("path") for item in events if item.get("tool") == "read_file"}
        self.assertTrue({TARGET, "sample_stats/summary.py", "tests/test_statistics.py"}.issubset(reads))
        baseline = [item for item in report["checks"]
                    if item["name"] == "acceptance" and item["attempt"] == 0]
        self.assertTrue(baseline, "Acceptance was not run against the original implementation")
        self.assertNotEqual(baseline[-1]["returncode"], 0)

    def test_A02_failed_tests_are_given_to_model_before_second_attempt(self):
        server = self.model(self.implementation_steps(["empty_zero", "correct"]))
        report = self.invoke(server)
        self.assert_success(report, 2)
        self.assertEqual(server.implementation_replies, 2)
        indices = [index for index, item in enumerate(server.delivered) if item.get("implementation")]
        feedback = json.dumps(server.requests[indices[1]], ensure_ascii=False)
        self.assertIn("test_empty_input_returns_none", feedback)
        self.assertTrue(any(item["attempt"] == 1 and item["returncode"] != 0
                            for item in report["checks"]))

    def test_A03_third_attempt_is_verified_before_success(self):
        server = self.model(self.implementation_steps(["empty_zero", "wrong_mean", "correct"]))
        report = self.invoke(server)
        self.assert_success(report, 3)
        self.assertEqual(server.implementation_replies, 3)

    def test_A04_three_failures_stop_without_a_fourth_implementation(self):
        server = self.model(self.implementation_steps(["empty_zero", "all_none", "wrong_mean"]))
        report = self.invoke(server)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["implementation_attempts"], 3)
        self.assertEqual(server.implementation_replies, 3)
        self.assertIsNone(report["commit"])
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.baseline)
        self.assertTrue(report["summary"].strip())
        self.assertTrue(any(item["attempt"] == 3 and item["returncode"] != 0
                            for item in report["checks"]))
        self.assertTrue({"diff", "events", "verification"}.issubset(
            {artifact["kind"] for artifact in report["artifacts"]}
        ))

    def test_A05_already_satisfied_requirement_needs_no_implementation(self):
        self.write_source(TARGET, self.implementations["correct"])
        self.git("add", TARGET)
        self.git("-c", "user.name=Acceptance Test", "-c", "user.email=test@example.invalid",
                 "commit", "--quiet", "-m", "already satisfies requirement")
        self.baseline = self.git("rev-parse", "HEAD").strip()
        server = self.model(self.preparation())
        report = self.invoke(server)
        self.assertEqual(report["status"], "no_changes")
        self.assertEqual(report["implementation_attempts"], 0)
        self.assertEqual(server.implementation_replies, 0)
        self.assert_real_behavior()
        self.assertIsNone(report["commit"])
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.baseline)
        self.assertTrue({"regression", "acceptance"}.issubset(
            {check["name"] for check in report["checks"] if check["returncode"] == 0}
        ))

    def test_A06_ambiguous_requirement_is_clarified_before_changes(self):
        server = self.model([step(final_message(
            "needs_clarification", questions=["空列表应返回 None、返回 0，还是抛出异常？"],
        ))])
        request = self.default_request(server)
        request["requirement"] = json.loads(
            (FIXTURES / "requirements/ambiguous_average.json").read_text(encoding="utf-8")
        )["requirement"]
        report = self.invoke(server, request)
        self.assertEqual(report["status"], "needs_clarification")
        self.assertEqual(report["implementation_attempts"], 0)
        self.assertTrue(report["questions"])
        self.assertEqual((self.workspace / TARGET).read_bytes(), self.original)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_A07_invalid_tool_parameters_do_not_modify_files(self):
        server = self.model(self.failed_tool_steps("edit_file", {"path": 123}))
        report = self.invoke(server)
        self.assert_error_event(report, {"INVALID_ARGUMENTS"})
        self.assertEqual((self.workspace / TARGET).read_bytes(), self.original)

    def test_A07_stale_hash_preserves_an_external_change(self):
        external = self.original + b"\n# externally added after read\n"
        arguments = {
            "path": TARGET, "old_text": self.original.decode("utf-8"),
            "new_text": self.implementations["correct"], "expected_hash": digest(self.original),
        }
        server = self.model(self.failed_tool_steps(
            "edit_file", arguments,
            before_send=lambda _: (self.workspace / TARGET).write_bytes(external),
        ))
        report = self.invoke(server)
        self.assert_error_event(report, {"STALE_FILE"})
        self.assertEqual((self.workspace / TARGET).read_bytes(), external)

    def test_A07_nonunique_old_text_is_rejected(self):
        server = self.model(self.failed_tool_steps("edit_file", {
            "path": TARGET, "old_text": '"""', "new_text": "#",
            "expected_hash": digest(self.original),
        }))
        report = self.invoke(server)
        self.assert_error_event(report, {"NON_UNIQUE_MATCH"})
        self.assertEqual((self.workspace / TARGET).read_bytes(), self.original)

    def test_A07_missing_old_text_is_rejected(self):
        server = self.model(self.failed_tool_steps("edit_file", {
            "path": TARGET, "old_text": "this text does not exist", "new_text": "replacement",
            "expected_hash": digest(self.original),
        }))
        report = self.invoke(server)
        self.assert_error_event(report, {"NON_UNIQUE_MATCH"})
        self.assertEqual((self.workspace / TARGET).read_bytes(), self.original)

    def test_A08_parent_directory_read_is_denied(self):
        outside = self.root / "outside.txt"
        outside.write_text("outside-private-text", encoding="utf-8")
        server = self.model(self.failed_tool_steps("read_file", {
            "path": "../outside.txt", "start_line": 1, "end_line": 10,
        }))
        report = self.invoke(server)
        self.assert_error_event(report, {"OUTSIDE_WORKSPACE"})
        self.assertNotIn("outside-private-text", json.dumps(server.requests))
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside-private-text")

    def test_A08_absolute_outside_write_is_denied(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"private")
        server = self.model(self.failed_tool_steps("edit_file", {
            "path": str(outside), "old_text": "private", "new_text": "overwritten",
            "expected_hash": digest(b"private"),
        }))
        report = self.invoke(server)
        self.assert_error_event(report, {"OUTSIDE_WORKSPACE"})
        self.assertEqual(outside.read_bytes(), b"private")

    def test_A08_link_to_outside_directory_is_denied(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.txt").write_bytes(b"private")
        link = self.workspace / "shortcut"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            from tests.acceptance.support import command
            created = command(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)], self.root)
            self.assertEqual(created.returncode, 0, "Cannot create link fixture: " + created.stderr)
        server = self.model(self.failed_tool_steps("edit_file", {
            "path": "shortcut/private.txt", "old_text": "private", "new_text": "overwritten",
            "expected_hash": digest(b"private"),
        }))
        report = self.invoke(server)
        self.assert_error_event(report, {"OUTSIDE_WORKSPACE"})
        self.assertEqual((outside / "private.txt").read_bytes(), b"private")

    def test_A09_timeout_stops_the_test_process_and_its_child(self):
        server = self.model([])
        request = self.default_request(server)
        probe = self.root / "probe"
        self.addCleanup(self.cleanup_probe, probe)
        request["checks"][0]["argv"] = [
            sys.executable, "-B", str(FIXTURES / "commands/process_probe.py"), "parent", str(probe),
        ]
        report = self.invoke(server, request)
        self.assert_error_event(report, {"COMMAND_TIMEOUT"})
        self.assertTrue(any(check["timed_out"] for check in report["checks"]))
        self.assert_probe_stopped(probe)

    def test_A09_large_output_is_bounded_and_marked(self):
        server = self.model([])
        request = self.default_request(server)
        request["checks"][0]["argv"] = [
            sys.executable, "-B", str(FIXTURES / "commands/process_probe.py"),
            "output", str(self.root / "probe"),
        ]
        report = self.invoke(server, request)
        self.assertEqual(report["status"], "failed")
        truncated = [check for check in report["checks"] if check["output_truncated"]]
        self.assertTrue(truncated)
        for check in truncated:
            path = Path(check["stdout_path"]).resolve()
            self.assertTrue(path.is_relative_to(self.root))
            self.assertLessEqual(path.stat().st_size, request["limits"]["output_limit_bytes"])

    def protected_acceptance_change(self, replacement):
        steps = self.implementation_steps(["empty_zero"])
        for index in [2, 3]:
            steps.append(self.edit(ACCEPTANCE, self.acceptance_source, replacement, f"tamper-{index}"))
            steps.append(step(final_message("implementation_ready", summary="所有测试已经通过。")))
        report = self.invoke(self.model(steps))
        self.assert_error_event(report, {"PROTECTED_ACCEPTANCE"})
        self.assertEqual((self.workspace / ACCEPTANCE).read_text(encoding="utf-8"), self.acceptance_source)

    def test_A10_weakened_acceptance_assertion_is_rejected(self):
        self.protected_acceptance_change(self.acceptance_source.replace(
            "self.assertIsNone(average([]))", "self.assertEqual(average([]), 0)"
        ))

    def test_A10_deleted_acceptance_tests_are_rejected(self):
        self.protected_acceptance_change("")

    def test_A10_skipped_acceptance_tests_are_rejected(self):
        self.protected_acceptance_change(self.acceptance_source.replace(
            "class AverageAcceptanceTests", '@unittest.skip("bypass")\nclass AverageAcceptanceTests'
        ))

    def test_A11_passing_acceptance_does_not_hide_a_regression(self):
        steps = self.preparation()
        previous = self.original.decode("utf-8")
        decoy = self.unrelated.decode("utf-8")
        broken_decoy = "def average(words):\n    return -1\n"
        for index in range(1, 4):
            candidate = self.implementations["correct"] + f"\n# repair {index}\n"
            message = self.edit(TARGET, previous, candidate, f"impl-{index}")["message"]
            if index == 1:
                message["tool_calls"].extend(self.edit(
                    "sample_stats/text.py", decoy, broken_decoy, "break-unrelated",
                )["message"]["tool_calls"])
            steps.extend([step(message, implementation=True), step(final_message("implementation_ready"))])
            previous = candidate
        report = self.invoke(self.model(steps))
        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["commit"])
        self.assertTrue(any(check["name"] == "acceptance" and check["returncode"] == 0
                            for check in report["checks"]))
        self.assertTrue(any(check["name"] == "regression" and check["returncode"] != 0
                            and check["attempt"] > 0 for check in report["checks"]))

    def assert_bad_report(self, mode, codes):
        server = self.model([])
        request = self.default_request(server)
        self.fault_command(request, "regression", mode)
        report = self.invoke(server, request)
        self.assert_error_event(report, codes)

    def test_A11_zero_tests_with_exit_zero_is_not_success(self):
        self.assert_bad_report("zero", {"EXPECTED_TESTS_MISSING"})

    def test_A11_skipped_required_test_is_not_success(self):
        self.assert_bad_report("skipped", {"REQUIRED_TEST_SKIPPED"})

    def test_A11_skipped_acceptance_test_is_not_success(self):
        server = self.model(self.preparation())
        request = self.default_request(server)
        self.fault_command(request, "acceptance", "skipped")
        report = self.invoke(server, request)
        self.assert_error_event(report, {"REQUIRED_TEST_SKIPPED"})

    def test_A11_missing_report_is_not_success(self):
        self.assert_bad_report("missing", {"REPORT_MISSING"})

    def test_A11_corrupt_report_is_not_success(self):
        self.assert_bad_report("corrupt", {"REPORT_INVALID"})

    def test_A11_existing_baseline_failure_is_reported(self):
        self.assert_bad_report("failing", {"BASELINE_FAILED"})

    def test_A12_resume_keeps_the_original_three_attempt_budget(self):
        server = self.model(self.implementation_steps(["empty_zero", "wrong_mean"]))
        request = self.default_request(server)
        request["pause_after_attempt"] = 2
        paused = self.invoke(server, request)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["implementation_attempts"], 2)
        self.assertIsNone(paused["commit"])
        server.steps.extend([
            self.edit(TARGET, self.implementations["wrong_mean"], self.implementations["correct"],
                      "resumed-implementation", implementation=True),
            step(final_message("implementation_ready")),
        ])
        request.pop("pause_after_attempt")
        request["operation"] = "resume"
        report = self.invoke(server, request)
        self.assert_success(report, 3)
        self.assertEqual(server.implementation_replies, 3)
        self.assertTrue({1, 2, 3}.issubset({check["attempt"] for check in report["checks"]}))

    def test_A13_legal_parameters_do_not_turn_a_missing_file_into_success(self):
        server = self.model(self.failed_tool_steps("read_file", {
            "path": "does-not-exist.py", "start_line": 1, "end_line": 20,
        }))
        report = self.invoke(server)
        self.assert_error_event(report, {"FILE_NOT_FOUND", "IO_ERROR"})
        self.assertEqual((self.workspace / TARGET).read_bytes(), self.original)

    def test_A14_evidence_exists_and_contains_no_credentials(self):
        server = self.model(self.implementation_steps(["correct"]))
        report = self.invoke(server)
        self.assert_success(report, 1)
        self.assertTrue({"diff", "events", "verification"}.issubset(
            {artifact["kind"] for artifact in report["artifacts"]}
        ))
        for artifact in report["artifacts"]:
            path = Path(artifact["path"]).resolve()
            self.assertTrue(path.is_relative_to(self.root))
            self.assertTrue(path.is_file())
            self.assertNotIn(self.secret.encode(), path.read_bytes())
        self.assertNotIn(self.secret, json.dumps(report))
        self.assertNotIn(self.secret, json.dumps(server.requests))
        self.assertNotIn(self.secret, self.git("show", "--format=full", "HEAD"))
        for path in (self.root / "state").rglob("*"):
            if path.is_file():
                self.assertNotIn(self.secret.encode(), path.read_bytes(), str(path))

    def test_A14_user_changes_are_preserved_and_excluded_from_agent_commit(self):
        note = "\n# user-owned uncommitted note\n"
        self.write_source(TARGET, self.original.decode("utf-8") + note)
        (self.workspace / "personal-notes.txt").write_text("user notes", encoding="utf-8")
        arguments = {
            "path": TARGET, "old_text": self.original.decode("utf-8"),
            "new_text": self.implementations["correct"],
            "expected_hash": digest((self.workspace / TARGET).read_bytes()),
        }
        steps = self.preparation() + [
            step(tool_message("edit_file", arguments, "implementation-1"), implementation=True),
            step(final_message("implementation_ready")),
        ]
        report = self.invoke(self.model(steps))
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["implementation_attempts"], 1)
        self.assert_real_behavior()
        self.assertIn(note, (self.workspace / TARGET).read_text(encoding="utf-8"))
        self.assertEqual((self.workspace / "personal-notes.txt").read_text(encoding="utf-8"), "user notes")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), report["commit"])
        self.assertNotIn("user-owned uncommitted note", self.git("show", f"HEAD:{TARGET}"))
        self.assertNotIn("personal-notes.txt", self.git("show", "--format=", "--name-only", "HEAD"))
