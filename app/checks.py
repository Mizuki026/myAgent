"""Bounded subprocess execution and independent JUnit report validation."""

import os
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import CheckResult, CheckSpec, Limits


@dataclass
class CheckOutcome:
    record: CheckResult
    passed: bool
    codes: list[tuple[str, str]]
    test_ids: set[str]
    test_failures: int


class CheckRunner:
    def __init__(
        self,
        workspace: Path,
        state_dir: Path,
        limits: Limits,
        redact: Callable[[str], str],
    ) -> None:
        self.workspace = workspace
        self.state_dir = state_dir
        self.limits = limits
        self.redact = redact

    def run(self, spec: CheckSpec, attempt: int) -> CheckOutcome:
        check_dir = self.state_dir / "checks"
        check_dir.mkdir(parents=True, exist_ok=True)
        name = f"attempt-{attempt:03d}-{spec.name}"
        report_path = (check_dir / f"{name}.xml").resolve()
        stdout_path = (check_dir / f"{name}.stdout.txt").resolve()
        stderr_path = (check_dir / f"{name}.stderr.txt").resolve()
        report_path.unlink(missing_ok=True)
        argv = [argument.replace("{report}", str(report_path)) for argument in spec.argv]
        stdout = bytearray()
        stderr = bytearray()
        truncated = {"stdout": False, "stderr": False}
        readers: list[threading.Thread] = []
        timed_out = False
        output_exceeded = False
        returncode = -1
        process: subprocess.Popen[bytes] | None = None
        codes: list[tuple[str, str]] = []
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.workspace,
                env=self._command_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            assert process.stdout is not None and process.stderr is not None
            readers = [
                threading.Thread(
                    target=self._read_stream,
                    args=(process.stdout, stdout, truncated, "stdout"),
                    daemon=True,
                ),
                threading.Thread(
                    target=self._read_stream,
                    args=(process.stderr, stderr, truncated, "stderr"),
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + self.limits.command_timeout_seconds
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._terminate_tree(process)
                    break
                if any(truncated.values()):
                    output_exceeded = True
                    self._terminate_tree(process)
                    break
                time.sleep(0.01)
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._terminate_tree(process)
                returncode = process.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=5)
            if timed_out:
                codes.append(("COMMAND_TIMEOUT", "The configured command time limit was reached."))
            if output_exceeded:
                codes.append(
                    (
                        "OUTPUT_LIMIT_EXCEEDED",
                        "Command output exceeded the configured limit and was stopped.",
                    )
                )
        except (OSError, ValueError) as error:
            codes.append(("IO_ERROR", f"Unable to execute configured command: {error}"))
            if process and process.poll() is None:
                self._terminate_tree(process)
        finally:
            self._write_bounded_output(stdout_path, stdout)
            self._write_bounded_output(stderr_path, stderr)

        output_truncated = any(truncated.values())
        test_ids: set[str] = set()
        report_valid = False
        report_failures = 0
        report_errors = 0
        skipped_ids: set[str] = set()
        if not report_path.exists():
            codes.append(("REPORT_MISSING", "The command did not create its JUnit XML report."))
        else:
            try:
                raw_report = self.redact(report_path.read_text(encoding="utf-8", errors="replace"))
                report_path.write_text(raw_report, encoding="utf-8")
                root = ET.fromstring(raw_report)
                cases = list(root.iter("testcase"))
                report_valid = True
                for case in cases:
                    identifier = f"{case.get('classname', '')}.{case.get('name', '')}"
                    test_ids.add(identifier)
                    if case.find("skipped") is not None:
                        skipped_ids.add(identifier)
                    report_failures += case.find("failure") is not None
                    report_errors += case.find("error") is not None
                if not cases:
                    codes.append(
                        ("EXPECTED_TESTS_MISSING", "JUnit report contains no executed test cases.")
                    )
                missing = sorted(set(spec.expected_test_ids) - test_ids)
                if missing:
                    codes.append(
                        (
                            "EXPECTED_TESTS_MISSING",
                            "Required tests were not reported: " + ", ".join(missing),
                        )
                    )
                required_skips = sorted(set(spec.expected_test_ids) & skipped_ids)
                if required_skips:
                    codes.append(
                        (
                            "REQUIRED_TEST_SKIPPED",
                            "Required tests were skipped: " + ", ".join(required_skips),
                        )
                    )
            except (ET.ParseError, OSError, UnicodeError, ValueError) as error:
                codes.append(
                    (
                        "REPORT_INVALID",
                        f"JUnit XML report is missing required structure or invalid: {error}",
                    )
                )

        if not codes and report_valid and (report_failures or report_errors or returncode != 0):
            codes.append(
                ("CHECK_FAILED", f"{spec.name} checks failed with exit code {returncode}.")
            )
        passed = (
            not codes
            and report_valid
            and returncode == 0
            and report_failures == 0
            and report_errors == 0
            and not skipped_ids
            and set(spec.expected_test_ids).issubset(test_ids)
        )
        record = CheckResult(
            name=spec.name,
            attempt=attempt,
            argv=argv,
            cwd=str(self.workspace),
            returncode=returncode,
            timed_out=timed_out,
            output_truncated=output_truncated,
            report_path=str(report_path),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        return CheckOutcome(record, passed, codes, test_ids, report_failures + report_errors)

    def validate_acceptance_baseline(
        self, outcome: CheckOutcome
    ) -> tuple[bool, list[tuple[str, str]]]:
        """Return whether the baseline report is valid and its structural problems."""
        non_test_errors = [code for code in outcome.codes if code[0] != "CHECK_FAILED"]
        if non_test_errors:
            return False, non_test_errors
        if outcome.passed or outcome.test_failures > 0:
            return True, []
        return False, [
            ("REPORT_INVALID", "Acceptance baseline returned success despite failed test results.")
        ]

    def _read_stream(
        self,
        stream,
        target: bytearray,
        truncated: dict[str, bool],
        key: str,
    ) -> None:
        limit = self.limits.output_limit_bytes
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            remaining = limit - len(target)
            if remaining > 0:
                target.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[key] = True

    def _write_bounded_output(self, path: Path, content: bytearray) -> None:
        safe = self.redact(content.decode("utf-8", "replace")).encode("utf-8")
        safe = safe[: self.limits.output_limit_bytes]
        path.write_bytes(safe)

    @staticmethod
    def _command_environment() -> dict[str, str]:
        environment = os.environ.copy()
        for key in list(environment):
            upper = key.upper()
            if upper.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")):
                environment.pop(key, None)
        return environment

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, 9)
            except (ProcessLookupError, PermissionError):
                process.kill()
