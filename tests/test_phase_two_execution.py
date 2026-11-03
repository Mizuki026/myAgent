"""Real-process tests for bounded commands and managed services."""

import sys
import tempfile
import unittest
from pathlib import Path

from app.execution import ProcessManager
from app.execution.process_manager import CommandSpec, ExecutionFailure, ServiceSpec
from app.tooling import ExecutionLimits, ToolContext


class ProcessManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase-two-process-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.manager = ProcessManager()
        self.addCleanup(self.manager.shutdown)
        self.context = ToolContext(
            task_id="task-1",
            project_id="project-1",
            workspace=self.workspace,
            artifact_dir=self.workspace / "run-artifacts",
            limits=ExecutionLimits(command_timeout_seconds=2, output_limit_bytes=512),
        )

    def test_exec_runs_real_program_and_registers_bounded_output(self) -> None:
        result = self.manager.run_command(
            CommandSpec(mode="exec", program=sys.executable, args=["-c", "print('hello')"]),
            self.context,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout_tail.replace("\r\n", "\n"), "hello\n")
        self.assertFalse(result.output_truncated)
        self.assertTrue((self.context.artifact_dir / f"{result.stdout_artifact.id}.txt").exists())

    def test_timeout_and_output_limit_terminate_the_command(self) -> None:
        timed = self.manager.run_command(
            CommandSpec(
                mode="exec",
                program=sys.executable,
                args=["-c", "import time; print('started', flush=True); time.sleep(5)"],
                timeout_seconds=1,
            ),
            self.context,
        )
        self.assertTrue(timed.timed_out)
        self.assertIn("started", timed.stdout_tail)
        large = self.manager.run_command(
            CommandSpec(mode="exec", program=sys.executable, args=["-c", "print('x' * 5000)"]),
            self.context,
        )
        self.assertTrue(large.output_truncated)
        self.assertLessEqual(large.stdout_artifact.size_bytes, 512)

    def test_workspace_scope_and_project_timeout_are_enforced(self) -> None:
        with self.assertRaises(ExecutionFailure) as error:
            self.manager.run_command(
                CommandSpec(mode="exec", program=sys.executable, cwd=".."), self.context
            )
        self.assertEqual(error.exception.code, "OUTSIDE_WORKSPACE")
        with self.assertRaises(ExecutionFailure) as error:
            self.manager.run_command(
                CommandSpec(mode="exec", program=sys.executable, timeout_seconds=3), self.context
            )
        self.assertEqual(error.exception.code, "INVALID_ARGUMENTS")

    def test_managed_service_can_be_stopped_without_pid_access(self) -> None:
        service = self.manager.start_service(
            ServiceSpec(
                name="preview",
                command=CommandSpec(
                    mode="exec", program=sys.executable, args=["-c", "import time; time.sleep(30)"]
                ),
                url="http://127.0.0.1:3000",
            ),
            self.context,
        )
        self.assertEqual(service.status, "running")
        stopped = self.manager.stop_service(service.id, self.context)
        self.assertEqual(stopped.status, "stopped")
        self.assertEqual(self.manager.get_service(service.id, self.context).status, "stopped")


if __name__ == "__main__":
    unittest.main()
