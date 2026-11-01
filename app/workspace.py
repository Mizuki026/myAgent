"""Workspace tools with path, version and fixed-acceptance safeguards."""

import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import tokenize
from collections.abc import Callable
from pathlib import Path
from typing import Any


class WorkspaceTools:
    def __init__(
        self,
        workspace: Path,
        state: dict[str, Any],
        emit: Callable[..., None],
        redact: Callable[[str], str],
    ) -> None:
        self.workspace = workspace.resolve()
        self.state = state
        self.emit = emit
        self.redact = redact

    def call(self, name: str, arguments: Any, phase: str, attempt: int) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return self._error(name, "INVALID_ARGUMENTS", "Tool arguments must be an object.")
        try:
            if name == "search_code":
                return self.search_code(arguments)
            if name == "read_file":
                return self.read_file(arguments)
            if name == "edit_file":
                return self.edit_file(arguments, phase, attempt)
            if name == "get_diff":
                if arguments:
                    raise ToolFailure("INVALID_ARGUMENTS", "get_diff takes no arguments.")
                return self.get_diff()
            return self._error(name, "INVALID_ARGUMENTS", "Unknown tool.")
        except ToolFailure as error:
            return self._error(name, error.code, str(error), arguments.get("path"))
        except (OSError, UnicodeError) as error:
            return self._error(
                name, "IO_ERROR", f"File operation failed: {error}", arguments.get("path")
            )

    def search_code(self, arguments: dict[str, Any]) -> dict[str, Any]:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolFailure("INVALID_ARGUMENTS", "pattern must be a nonempty string.")
        rg = shutil.which("rg")
        if rg:
            result = subprocess.run(
                [
                    rg,
                    "--line-number",
                    "--hidden",
                    "--glob",
                    "!.git/**",
                    "--glob",
                    "!.venv/**",
                    "--glob",
                    "!.env*",
                    "--glob",
                    "!**/__pycache__/**",
                    "--",
                    pattern,
                    ".",
                ],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            if result.returncode not in (0, 1):
                raise ToolFailure("IO_ERROR", self.redact(result.stderr[-1000:]))
            matches = result.stdout.splitlines()
        else:
            matches = self._python_search(pattern)
        output = "\n".join(self.redact(line) for line in matches[:200])
        truncated = len(matches) > 200 or len(output) > 20_000
        output = output[:20_000]
        self.emit(type="tool_call", tool="search_code")
        return {"matches": output, "match_count": len(matches), "truncated": truncated}

    def read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path_value = arguments.get("path")
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line", start + 199 if isinstance(start, int) else 200)
        if not isinstance(path_value, str) or not path_value:
            raise ToolFailure("INVALID_ARGUMENTS", "path must be a nonempty string.")
        if type(start) is not int or type(end) is not int or start < 1 or end < start:
            raise ToolFailure("INVALID_ARGUMENTS", "line range must use positive integers.")
        if end - start > 1999:
            raise ToolFailure("INVALID_ARGUMENTS", "A read may include at most 2000 lines.")
        path = self._resolve(path_value)
        if self._sensitive_path(path):
            raise ToolFailure(
                "CREDENTIAL_PROTECTION",
                "Credential configuration files cannot be read by model tools.",
            )
        if not path.is_file():
            raise ToolFailure("FILE_NOT_FOUND", f"File does not exist: {self._relative(path)}")
        try:
            raw = path.read_bytes()
            text = _decode(raw)
        except (OSError, UnicodeError) as error:
            raise ToolFailure("IO_ERROR", f"Unable to read file: {error}") from error
        lines = text.splitlines()
        selection = "\n".join(lines[start - 1 : end])
        encoded = selection.encode("utf-8")
        truncated = len(encoded) > 20_000 or end < len(lines)
        selection = encoded[:20_000].decode("utf-8", errors="ignore")
        self.emit(tool="read_file", path=self._relative(path), type="tool_call")
        return {
            "path": self._relative(path),
            "start_line": start,
            "end_line": min(end, len(lines)),
            "content": self.redact(selection),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "truncated": truncated,
        }

    def edit_file(self, arguments: dict[str, Any], phase: str, attempt: int) -> dict[str, Any]:
        path_value = arguments.get("path")
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        expected_hash = arguments.get("expected_hash")
        if (
            not isinstance(path_value, str)
            or not path_value
            or not isinstance(old_text, str)
            or not isinstance(new_text, str)
            or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash)
        ):
            raise ToolFailure(
                "INVALID_ARGUMENTS",
                "edit_file requires path, old_text, new_text and a SHA-256 expected_hash.",
            )
        path = self._resolve(path_value)
        relative = self._relative(path)
        frozen = self.state.get("frozen_acceptance", {})
        if relative in frozen:
            raise ToolFailure("PROTECTED_ACCEPTANCE", "Frozen acceptance files cannot be changed.")
        if self._sensitive_path(path):
            raise ToolFailure(
                "CREDENTIAL_PROTECTION", "Credential configuration files cannot be edited."
            )
        if not path.is_file():
            raise ToolFailure("FILE_NOT_FOUND", f"File does not exist: {relative}")
        raw = path.read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash.casefold() != expected_hash.casefold():
            raise ToolFailure("STALE_FILE", f"File changed after it was read: {relative}")
        if self.redact(old_text) != old_text or self.redact(new_text) != new_text:
            raise ToolFailure(
                "CREDENTIAL_PROTECTION",
                "Credential values cannot be written to agent state or commits.",
            )
        text = _decode(raw)
        old_normalized = _normalize_newlines(old_text)
        updated = _replace_preserving_newlines(text, old_text, new_text)
        if not old_normalized or updated is None:
            raise ToolFailure(
                "NON_UNIQUE_MATCH", "old_text must match exactly once; no change was made."
            )
        new_raw = _encode(updated, raw)
        if new_raw == raw:
            raise ToolFailure(
                "NON_UNIQUE_MATCH", "The requested replacement does not change the file."
            )
        self._atomic_write(path, new_raw)
        new_hash = hashlib.sha256(new_raw).hexdigest()
        self.state.setdefault("modified_paths", [])
        if relative not in self.state["modified_paths"]:
            self.state["modified_paths"].append(relative)
        self.state.setdefault("latest_hashes", {})[relative] = new_hash
        self.state.setdefault("edits", []).append(
            {"path": relative, "old_text": old_text, "new_text": new_text}
        )
        if phase == "implementation" and relative not in self.state.get("acceptance_paths", []):
            self.state.setdefault("implementation_paths", [])
            if relative not in self.state["implementation_paths"]:
                self.state["implementation_paths"].append(relative)
            self.emit(
                type="implementation_changed",
                tool="edit_file",
                path=relative,
                sha256=new_hash,
                attempt=attempt,
            )
        else:
            self.emit(type="tool_call", tool="edit_file", path=relative, attempt=attempt)
        return {"path": relative, "sha256": new_hash, "changed": True}

    def get_diff(self) -> dict[str, Any]:
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--", "."],
            cwd=self.workspace,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise ToolFailure(
                "IO_ERROR", self.redact(result.stderr.decode("utf-8", "replace")[-1000:])
            )
        diff = self.redact(result.stdout.decode("utf-8", "replace"))[:40_000]
        self.emit(type="tool_call", tool="get_diff")
        return {"diff": diff, "truncated": len(result.stdout) > 40_000}

    def resolve_acceptance_path(self, path_value: str) -> Path:
        path = self._resolve(path_value)
        if self._sensitive_path(path):
            raise ToolFailure("CREDENTIAL_PROTECTION", "Acceptance cannot target credential files.")
        return path

    def _resolve(self, path_value: str) -> Path:
        supplied = Path(path_value)
        candidate = supplied if supplied.is_absolute() else self.workspace / supplied
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.workspace):
            raise ToolFailure(
                "OUTSIDE_WORKSPACE", "Resolved path is outside the authorized workspace."
            )
        return resolved

    @staticmethod
    def _sensitive_path(path: Path) -> bool:
        return path.name == ".env" or path.name.startswith(".env.")

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    def _error(self, tool: str, code: str, reason: str, path: Any = None) -> dict[str, Any]:
        safe_path = path if isinstance(path, str) else None
        self.emit(
            type="tool_error", tool=tool, path=safe_path, code=code, reason=self.redact(reason)
        )
        return {"error": {"code": code, "reason": self.redact(reason)}}

    def _python_search(self, pattern: str) -> list[str]:
        try:
            matcher = re.compile(pattern)
        except re.error:
            matcher = re.compile(re.escape(pattern))
        found: list[str] = []
        for file in self.workspace.rglob("*"):
            if (
                not file.is_file()
                or ".git" in file.parts
                or ".venv" in file.parts
                or self._sensitive_path(file)
                or "__pycache__" in file.parts
            ):
                continue
            try:
                for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                    if matcher.search(line):
                        found.append(f"{self._relative(file)}:{number}:{line}")
            except (OSError, UnicodeError):
                continue
        return found

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        mode = path.stat().st_mode
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary_path = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, mode)
            os.replace(temporary_path, path)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()


class ToolFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _decode(raw: bytes) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    except (SyntaxError, UnicodeDecodeError) as error:
        raise UnicodeError(f"Unable to detect source encoding: {error}") from error
    return raw.decode(encoding)


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _restore_newlines(text: str, original: str) -> str:
    if "\r\n" in original:
        return text.replace("\n", "\r\n")
    if "\r" in original and "\n" not in original:
        return text.replace("\n", "\r")
    return text


def _replace_preserving_newlines(original: str, old_text: str, new_text: str) -> str | None:
    normalized_characters = []
    source_starts = []
    source_ends = []
    index = 0
    while index < len(original):
        source_starts.append(index)
        if original.startswith("\r\n", index):
            normalized_characters.append("\n")
            index += 2
        elif original[index] in "\r\n":
            normalized_characters.append("\n")
            index += 1
        else:
            normalized_characters.append(original[index])
            index += 1
        source_ends.append(index)
    normalized = "".join(normalized_characters)
    old_normalized = _normalize_newlines(old_text)
    if not old_normalized or normalized.count(old_normalized) != 1:
        return None
    start = normalized.index(old_normalized)
    end = start + len(old_normalized)
    source_start = source_starts[start]
    source_end = source_ends[end - 1]
    span_newlines = re.findall(r"\r\n|\r|\n", original[source_start:source_end])
    all_newlines = re.findall(r"\r\n|\r|\n", original)
    candidates = span_newlines or all_newlines
    newline = max(candidates, key=candidates.count) if candidates else "\n"
    replacement = _normalize_newlines(new_text).replace("\n", newline)
    return original[:source_start] + replacement + original[source_end:]


def _encode(text: str, original: bytes) -> bytes:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(original).readline)
    except (SyntaxError, UnicodeDecodeError) as error:
        raise UnicodeError(f"Unable to detect source encoding: {error}") from error
    return text.encode(encoding)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search project source for a keyword or regular expression.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a line range from a workspace file with its SHA-256 version.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one exact text span after verifying the whole-file SHA-256.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "expected_hash": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text", "expected_hash"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_checks",
            "description": "Run the configured regression and acceptance checks.",
            "parameters": {
                "type": "object",
                "properties": {"scope": {"type": "string", "enum": ["all"]}},
                "required": ["scope"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diff",
            "description": "Return the current workspace diff.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]
