"""Bounded command execution and application-owned child processes."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, model_validator

from app.tooling.types import StrictModel, ToolContext


class ExecutionFailure(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CommandSpec(StrictModel):
    mode: Literal["exec", "powershell"] = "exec"
    program: StrictStr | None = None
    args: list[StrictStr] = Field(default_factory=list)
    script: StrictStr | None = None
    cwd: StrictStr = "."
    timeout_seconds: StrictInt | None = Field(default=None, ge=1, le=3600)
    encoding: Literal["utf-8", "gb18030", "utf-16-le"] = "utf-8"

    @model_validator(mode="after")
    def validate_mode(self) -> CommandSpec:
        if self.mode == "exec" and (not self.program or self.script is not None):
            raise ValueError("exec mode requires program and does not accept script")
        if self.mode == "powershell" and (not self.script or self.program is not None or self.args):
            raise ValueError("powershell mode requires script and does not accept program or args")
        return self


class ServiceSpec(StrictModel):
    name: StrictStr = Field(min_length=1, max_length=100)
    command: CommandSpec
    url: StrictStr | None = None

    @model_validator(mode="after")
    def validate_url(self) -> ServiceSpec:
        if self.url and not self.url.startswith(
            ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")
        ):
            raise ValueError("service URL must point to localhost")
        return self


class ProcessArtifact(StrictModel):
    id: StrictStr
    task_id: StrictStr
    kind: StrictStr
    media_type: StrictStr = "text/plain; charset=utf-8"
    size_bytes: StrictInt = Field(ge=0)
    mutable: bool = False
    sha256: StrictStr | None = None
    created_at: StrictStr


class CommandResult(StrictModel):
    returncode: StrictInt
    timed_out: bool
    output_truncated: bool
    stdout_tail: StrictStr
    stderr_tail: StrictStr
    duration_ms: StrictInt
    stdout_artifact: ProcessArtifact | None = None
    stderr_artifact: ProcessArtifact | None = None


class ServiceSnapshot(StrictModel):
    id: StrictStr
    task_id: StrictStr
    project_id: StrictStr
    name: StrictStr
    status: Literal["starting", "running", "stopping", "stopped", "exited", "failed", "unknown"]
    command_summary: StrictStr
    url: StrictStr | None = None
    stdout_artifact: ProcessArtifact | None = None
    stderr_artifact: ProcessArtifact | None = None
    created_at: StrictStr
    ended_at: StrictStr | None = None
    exit_code: StrictInt | None = None


class _Job:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.handle = None
        if os.name == "nt":
            import ctypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.restype = ctypes.c_void_p
            handle = kernel.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            self.handle = handle
            limits = ctypes.create_string_buffer(144)
            ctypes.c_uint32.from_buffer(limits, 16).value = 0x2000
            if not kernel.SetInformationJobObject(handle, 9, limits, len(limits)):
                kernel.CloseHandle(handle)
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            if not kernel.AssignProcessToJobObject(handle, int(process._handle)):
                kernel.CloseHandle(handle)
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
            resume = ctypes.WinDLL("ntdll").NtResumeProcess
            resume.argtypes = [ctypes.c_void_p]
            resume.restype = ctypes.c_long
            status = resume(int(process._handle))
            if status != 0:
                kernel.TerminateJobObject(handle, 1)
                kernel.CloseHandle(handle)
                raise OSError(f"NtResumeProcess failed with NTSTATUS {status}")

    def terminate(self) -> None:
        if self.handle is not None:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject(self.handle, 1)
        elif self.process.poll() is None:
            if os.name == "nt":
                self.process.kill()
            else:
                os.killpg(self.process.pid, signal.SIGKILL)

    def close(self) -> None:
        if self.handle is not None:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
            self.handle = None


class _ManagedService:
    def __init__(
        self,
        snapshot: ServiceSnapshot,
        process: subprocess.Popen[bytes],
        job: _Job,
        stdout_path: Path,
        stderr_path: Path,
        limit: int,
        encoding: str,
        redact: Callable[[str], str],
    ) -> None:
        self.snapshot = snapshot
        self.process = process
        self.job = job
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.limit = limit
        self.encoding = encoding
        self.redact = redact
        self.lock = threading.Lock()
        self.threads = [
            threading.Thread(target=self._copy, args=(process.stdout, stdout_path), daemon=True),
            threading.Thread(target=self._copy, args=(process.stderr, stderr_path), daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def _copy(self, stream, path: Path) -> None:
        if stream is None:
            return
        pending = bytearray()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as output:
            while chunk := stream.read(4096):
                with self.lock:
                    pending.extend(chunk)
                    if len(pending) > 8192:
                        safe = self.redact(pending.decode(self.encoding, errors="replace")[:-512])
                        output.write(safe.encode("utf-8"))
                        pending = bytearray(pending[-512:])
                    if output.tell() >= self.limit:
                        output.seek(0)
                        output.truncate(0)
            if pending:
                output.write(
                    self.redact(pending.decode(self.encoding, errors="replace")).encode("utf-8")
                )


class ProcessManager:
    def __init__(self) -> None:
        self._services: dict[str, _ManagedService] = {}
        self._lock = threading.Lock()

    def run_command(self, spec: CommandSpec, ctx: ToolContext) -> CommandResult:
        cwd = self._cwd(spec.cwd, ctx)
        timeout = spec.timeout_seconds or ctx.limits.command_timeout_seconds
        if timeout > ctx.limits.command_timeout_seconds:
            raise ExecutionFailure("INVALID_ARGUMENTS", "Command timeout exceeds project limits.")
        argv = self._argv(spec)
        ctx.record_change(
            {
                "operation": "command",
                "argv": ctx.redact(" ".join(argv)),
                "cwd": cwd.relative_to(ctx.workspace).as_posix(),
            }
        )
        started = time.monotonic()
        process, job = self._spawn(argv, cwd, self._environment(ctx))
        stdout = bytearray()
        stderr = bytearray()
        overflow = threading.Event()
        limit = ctx.limits.output_limit_bytes
        readers = [
            threading.Thread(
                target=self._read_limited,
                args=(process.stdout, stdout, limit, overflow),
                daemon=True,
            ),
            threading.Thread(
                target=self._read_limited,
                args=(process.stderr, stderr, limit, overflow),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        deadline = started + timeout
        while process.poll() is None:
            if overflow.is_set():
                job.terminate()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                job.terminate()
                break
            time.sleep(0.02)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            job.terminate()
            process.wait(timeout=2)
        for reader in readers:
            reader.join(timeout=2)
        job.close()
        duration = max(0, int((time.monotonic() - started) * 1000))
        out_text = ctx.redact(bytes(stdout).decode(spec.encoding, errors="replace"))
        err_text = ctx.redact(bytes(stderr).decode(spec.encoding, errors="replace"))
        out_ref = self._artifact(ctx, "command_stdout", out_text.encode("utf-8"))
        err_ref = self._artifact(ctx, "command_stderr", err_text.encode("utf-8"))
        return CommandResult(
            returncode=process.returncode if process.returncode is not None else -1,
            timed_out=timed_out,
            output_truncated=overflow.is_set(),
            stdout_tail=out_text.encode("utf-8")[-8192:].decode("utf-8", errors="ignore"),
            stderr_tail=err_text.encode("utf-8")[-8192:].decode("utf-8", errors="ignore"),
            duration_ms=duration,
            stdout_artifact=out_ref,
            stderr_artifact=err_ref,
        )

    def start_service(self, spec: ServiceSpec, ctx: ToolContext) -> ServiceSnapshot:
        cwd = self._cwd(spec.command.cwd, ctx)
        argv = self._argv(spec.command)
        service_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        service_dir = ctx.artifact_dir / "services" / service_id
        service_dir.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path = service_dir / "stdout.log", service_dir / "stderr.log"
        process, job = self._spawn(argv, cwd, self._environment(ctx))
        summary = ctx.redact(" ".join(argv))[:500]
        stdout_ref = self._mutable_artifact(ctx, service_id, "service_stdout", stdout_path)
        stderr_ref = self._mutable_artifact(ctx, service_id, "service_stderr", stderr_path)
        snapshot = ServiceSnapshot(
            id=service_id,
            task_id=ctx.task_id,
            project_id=ctx.project_id,
            name=spec.name,
            status="starting",
            command_summary=summary,
            url=spec.url,
            stdout_artifact=stdout_ref,
            stderr_artifact=stderr_ref,
            created_at=now,
        )
        service = _ManagedService(
            snapshot,
            process,
            job,
            stdout_path,
            stderr_path,
            ctx.limits.output_limit_bytes,
            spec.command.encoding,
            ctx.redact,
        )
        with self._lock:
            self._services[service_id] = service
        time.sleep(1.0)
        updated = self._observe(service)
        self._save_service(ctx, updated)
        return updated

    def get_service(self, service_id: str, ctx: ToolContext) -> ServiceSnapshot:
        service = self._find(service_id)
        result = self._observe(service)
        self._save_service(ctx, result)
        return result

    def list_services(self, ctx: ToolContext) -> list[ServiceSnapshot]:
        with self._lock:
            services = list(self._services.values())
        result = [
            self._observe(service)
            for service in services
            if service.snapshot.project_id == ctx.project_id
        ]
        return sorted(result, key=lambda item: item.created_at)

    def stop_service(self, service_id: str, ctx: ToolContext) -> ServiceSnapshot:
        service = self._find(service_id)
        if service.snapshot.project_id != ctx.project_id:
            raise ExecutionFailure("SERVICE_NOT_FOUND", "Service does not belong to this project.")
        was_running = service.process.poll() is None
        if was_running:
            service.snapshot = service.snapshot.model_copy(update={"status": "stopping"})
            service.job.terminate()
            try:
                service.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                service.job.terminate()
                service.process.wait(timeout=2)
            service.job.close()
        result = self._observe(service)
        if was_running:
            result = result.model_copy(
                update={"status": "stopped", "ended_at": datetime.now(UTC).isoformat()}
            )
        service.snapshot = result
        self._save_service(ctx, result)
        return result

    def shutdown(self) -> None:
        with self._lock:
            services = list(self._services.values())
        for service in services:
            if service.process.poll() is None:
                service.job.terminate()
                try:
                    service.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    service.job.terminate()
            service.job.close()

    def registrations(self):
        from app.tooling.registry import ToolRegistration

        class StartServiceArgs(StrictModel):
            name: StrictStr = Field(min_length=1, max_length=100)
            command: CommandSpec
            url: StrictStr | None = None

        class ServiceIdArgs(StrictModel):
            service_id: StrictStr = Field(min_length=1)

        class EmptyArgs(StrictModel):
            pass

        return [
            ToolRegistration(
                "run_command",
                "Run a bounded project command.",
                CommandSpec,
                lambda value, ctx: self.run_command(value, ctx),
            ),
            ToolRegistration(
                "start_service",
                "Start an app-managed local service.",
                StartServiceArgs,
                lambda value, ctx: self.start_service(ServiceSpec(**value.model_dump()), ctx),
            ),
            ToolRegistration(
                "get_service",
                "Get a managed service status.",
                ServiceIdArgs,
                lambda value, ctx: self.get_service(value.service_id, ctx),
            ),
            ToolRegistration(
                "stop_service",
                "Stop a managed service.",
                ServiceIdArgs,
                lambda value, ctx: self.stop_service(value.service_id, ctx),
            ),
            ToolRegistration(
                "list_services",
                "List project services.",
                EmptyArgs,
                lambda _value, ctx: [
                    item.model_dump(mode="json") for item in self.list_services(ctx)
                ],
            ),
        ]

    def _spawn(
        self, argv: list[str], cwd: Path, env: dict[str, str]
    ) -> tuple[subprocess.Popen[bytes], _Job]:
        flags = 0
        startupinfo = None
        if os.name == "nt":
            flags |= 0x00000004 | subprocess.CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=flags,
                startupinfo=startupinfo,
            )
            try:
                return process, _Job(process)
            except OSError as error:
                process.kill()
                process.wait()
                raise ExecutionFailure("PROCESS_CONTROL_UNAVAILABLE", str(error)) from error
        except FileNotFoundError as error:
            raise ExecutionFailure(
                "PROGRAM_NOT_FOUND", "Command program was not found.", True
            ) from error
        except PermissionError as error:
            raise ExecutionFailure("EXECUTION_FAILED", "Command could not be started.") from error

    def _argv(self, spec: CommandSpec) -> list[str]:
        if spec.mode == "powershell":
            program = shutil_which("pwsh") or shutil_which("powershell")
            if not program:
                raise ExecutionFailure("PROGRAM_NOT_FOUND", "PowerShell is not installed.")
            return [
                program,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                spec.script or "",
            ]
        assert spec.program is not None
        if os.name == "nt" and spec.program.lower().endswith((".cmd", ".bat")):
            raise ExecutionFailure(
                "INVALID_ARGUMENTS", "Use explicit powershell mode for script wrappers."
            )
        return [spec.program, *spec.args]

    def _cwd(self, value: str, ctx: ToolContext) -> Path:
        supplied = Path(value)
        if supplied.is_absolute() or ".." in supplied.parts:
            raise ExecutionFailure(
                "OUTSIDE_WORKSPACE", "Command cwd must stay inside the workspace."
            )
        path = (ctx.workspace / supplied).resolve()
        try:
            path.relative_to(ctx.workspace)
        except ValueError as error:
            raise ExecutionFailure(
                "OUTSIDE_WORKSPACE", "Command cwd leaves the workspace."
            ) from error
        if not path.is_dir():
            raise ExecutionFailure("FILE_NOT_FOUND", "Command cwd does not exist.")
        return path

    @staticmethod
    def _environment(ctx: ToolContext) -> dict[str, str]:
        blocked = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        return {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in blocked)
            and key.upper() not in {"OPENAI_API_BASE", "OPENAI_BASE_URL"}
        }

    @staticmethod
    def _read_limited(stream, buffer: bytearray, limit: int, overflow: threading.Event) -> None:
        if stream is None:
            return
        while chunk := stream.read(65536):
            remaining = limit - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()

    def _artifact(self, ctx: ToolContext, kind: str, content: bytes) -> ProcessArtifact:
        artifact_id = str(uuid.uuid4())
        path = ctx.artifact_dir / f"{artifact_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        ref = ProcessArtifact(
            id=artifact_id,
            task_id=ctx.task_id,
            kind=kind,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=datetime.now(UTC).isoformat(),
        )
        callback = ctx.services.get("register_artifact")
        if callback:
            callback(ref, path)
        return ref

    def _mutable_artifact(
        self, ctx: ToolContext, service_id: str, kind: str, path: Path
    ) -> ProcessArtifact:
        ref = ProcessArtifact(
            id=str(uuid.uuid4()),
            task_id=ctx.task_id,
            kind=kind,
            size_bytes=0,
            mutable=True,
            created_at=datetime.now(UTC).isoformat(),
        )
        callback = ctx.services.get("register_artifact")
        if callback:
            callback(ref, path, service_id=service_id)
        return ref

    def _observe(self, service: _ManagedService) -> ServiceSnapshot:
        code = service.process.poll()
        if service.snapshot.status == "stopped":
            status = "stopped"
            ended = service.snapshot.ended_at
        elif code is None:
            status = "running"
            ended = None
        else:
            status = "exited" if code == 0 else "failed"
            ended = service.snapshot.ended_at or datetime.now(UTC).isoformat()
            for thread in service.threads:
                thread.join(timeout=1)
        updated = service.snapshot.model_copy(
            update={"status": status, "exit_code": code, "ended_at": ended}
        )
        service.snapshot = updated
        return updated

    def _find(self, service_id: str) -> _ManagedService:
        with self._lock:
            service = self._services.get(service_id)
        if service is None:
            raise ExecutionFailure("SERVICE_NOT_FOUND", "Managed service does not exist.")
        return service

    @staticmethod
    def _save_service(ctx: ToolContext, service: ServiceSnapshot) -> None:
        callback = ctx.services.get("save_service")
        if callback:
            callback(service)


def shutil_which(program: str) -> str | None:
    import shutil

    return shutil.which(program)
