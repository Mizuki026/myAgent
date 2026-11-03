"""Guarded text workspace operations with version-aware changes."""

from __future__ import annotations

import fnmatch
import hashlib
import io
import os
import re
import tempfile
import tokenize
from pathlib import Path

from pydantic import Field, StrictInt, StrictStr

from app.tooling.types import StrictModel, ToolContext

MAX_FILE_BYTES = 1_048_576
MAX_TEXT_BYTES = 20_000
MAX_LINES = 2_000
IGNORED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}


class WorkspaceFailure(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ListFilesArgs(StrictModel):
    path: StrictStr = "."
    glob: StrictStr | None = None
    limit: StrictInt = Field(default=200, ge=1, le=2000)


class SearchCodeArgs(StrictModel):
    pattern: StrictStr = Field(min_length=1, max_length=2000)
    path: StrictStr = "."
    literal: bool = False
    limit: StrictInt = Field(default=200, ge=1, le=2000)


class ReadFileArgs(StrictModel):
    path: StrictStr = Field(min_length=1)
    start_line: StrictInt = Field(default=1, ge=1)
    end_line: StrictInt | None = Field(default=None, ge=1)


class CreateFileArgs(StrictModel):
    path: StrictStr = Field(min_length=1)
    content: StrictStr


class EditFileArgs(StrictModel):
    path: StrictStr = Field(min_length=1)
    old_text: StrictStr
    new_text: StrictStr
    expected_hash: StrictStr = Field(pattern=r"^[0-9a-fA-F]{64}$")


class MoveFileArgs(StrictModel):
    source: StrictStr = Field(min_length=1)
    target: StrictStr = Field(min_length=1)
    expected_hash: StrictStr = Field(pattern=r"^[0-9a-fA-F]{64}$")


class DeleteFileArgs(StrictModel):
    path: StrictStr = Field(min_length=1)
    expected_hash: StrictStr = Field(pattern=r"^[0-9a-fA-F]{64}$")


class DirectoryArgs(StrictModel):
    path: StrictStr = Field(min_length=1)


class WorkspaceService:
    def list_files(self, args: ListFilesArgs, ctx: ToolContext) -> dict[str, object]:
        root = self._resolve(ctx, args.path)
        if not root.is_dir():
            raise WorkspaceFailure("FILE_NOT_FOUND", "Directory does not exist.", True)
        entries: list[dict[str, object]] = []
        for item in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if self._excluded(item, ctx):
                continue
            relative = item.relative_to(ctx.workspace).as_posix()
            if args.glob and not fnmatch.fnmatch(relative, args.glob):
                continue
            entries.append(
                {
                    "path": relative,
                    "kind": "directory" if item.is_dir() else "file",
                    "size_bytes": None if item.is_dir() else item.stat().st_size,
                }
            )
        return {"entries": entries[: args.limit], "truncated": len(entries) > args.limit}

    def search_code(self, args: SearchCodeArgs, ctx: ToolContext) -> dict[str, object]:
        root = self._resolve(ctx, args.path)
        if not root.is_dir():
            raise WorkspaceFailure("FILE_NOT_FOUND", "Search directory does not exist.", True)
        try:
            matcher = re.compile(re.escape(args.pattern) if args.literal else args.pattern)
        except re.error as error:
            raise WorkspaceFailure(
                "INVALID_ARGUMENTS", f"Invalid search expression: {error}"
            ) from error
        matches: list[dict[str, object]] = []
        observed = 0
        byte_count = 0
        truncated = False
        for file in sorted(root.rglob("*"), key=lambda path: path.as_posix().casefold()):
            if not file.is_file() or self._excluded(file, ctx):
                continue
            try:
                text = _decode(file.read_bytes())
            except (OSError, UnicodeError):
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if not matcher.search(line):
                    continue
                relative = file.relative_to(ctx.workspace).as_posix()
                safe_line = ctx.redact(line)
                size = len(f"{relative}:{line_no}:{safe_line}".encode())
                if observed >= args.limit or byte_count + size > MAX_TEXT_BYTES:
                    truncated = True
                    break
                matches.append({"path": relative, "line": line_no, "text": safe_line})
                observed += 1
                byte_count += size
            if truncated:
                break
        return {"matches": matches, "match_count": len(matches), "truncated": truncated}

    def read_file(self, args: ReadFileArgs, ctx: ToolContext) -> dict[str, object]:
        path = self._resolve(ctx, args.path)
        self._check_file(path)
        raw = path.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            raise WorkspaceFailure("FILE_TOO_LARGE", "Editable text files are limited to 1 MiB.")
        text = _decode(raw)
        lines = text.splitlines(keepends=True)
        end_line = args.end_line or min(len(lines), args.start_line + 199)
        if end_line < args.start_line or end_line - args.start_line >= MAX_LINES:
            raise WorkspaceFailure("INVALID_ARGUMENTS", "A read may include at most 2000 lines.")
        excerpt = "".join(lines[args.start_line - 1 : end_line])
        encoded = excerpt.encode("utf-8")
        truncated = len(encoded) > MAX_TEXT_BYTES or end_line < len(lines)
        excerpt = encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
        return {
            "path": path.relative_to(ctx.workspace).as_posix(),
            "start_line": args.start_line,
            "end_line": min(end_line, len(lines)),
            "content": ctx.redact(excerpt),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "encoding": _encoding(raw),
            "truncated": truncated,
        }

    def create_file(self, args: CreateFileArgs, ctx: ToolContext) -> dict[str, object]:
        path = self._resolve(ctx, args.path)
        self._validate_write(ctx, path, "create")
        if not path.parent.is_dir():
            raise WorkspaceFailure("FILE_NOT_FOUND", "Parent directory must already exist.")
        content = args.content.replace("\r\n", "\n").replace("\r", "\n")
        self._check_writable_text(content, ctx)
        raw = content.encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise WorkspaceFailure("FILE_TOO_LARGE", "Text files are limited to 1 MiB.")
        try:
            with path.open("xb") as stream:
                stream.write(raw)
        except FileExistsError as error:
            raise WorkspaceFailure("PROJECT_CONFLICT", "Target already exists.") from error
        digest = hashlib.sha256(raw).hexdigest()
        relative = path.relative_to(ctx.workspace).as_posix()
        self._record(ctx, "create", relative, None, digest)
        return self._change("create", relative, None, digest, True)

    def edit_file(self, args: EditFileArgs, ctx: ToolContext) -> dict[str, object]:
        path = self._resolve(ctx, args.path)
        self._validate_write(ctx, path, "edit")
        self._check_file(path)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceFailure("FILE_TOO_LARGE", "Editable text files are limited to 1 MiB.")
        raw = path.read_bytes()
        before = hashlib.sha256(raw).hexdigest()
        if before.casefold() != args.expected_hash.casefold():
            raise WorkspaceFailure("STALE_FILE", "File changed after it was read.", True)
        text = _decode(raw)
        if not args.old_text:
            raise WorkspaceFailure("NON_UNIQUE_MATCH", "old_text must not be empty.")
        updated = _replace_preserving_newlines(text, args.old_text, args.new_text)
        if updated is None:
            raise WorkspaceFailure("NON_UNIQUE_MATCH", "old_text must match exactly once.", True)
        self._check_writable_text(args.old_text, ctx)
        self._check_writable_text(args.new_text, ctx)
        output = _encode(updated, raw)
        if len(output) > MAX_FILE_BYTES:
            raise WorkspaceFailure("FILE_TOO_LARGE", "Editable text files are limited to 1 MiB.")
        if output == raw:
            raise WorkspaceFailure("NON_UNIQUE_MATCH", "The replacement makes no change.")
        _atomic_write(path, output)
        after = hashlib.sha256(output).hexdigest()
        relative = path.relative_to(ctx.workspace).as_posix()
        self._record(
            ctx,
            "edit",
            relative,
            before,
            after,
            details={"old_text": args.old_text, "new_text": args.new_text},
        )
        return self._change("edit", relative, before, after, True)

    def move_file(self, args: MoveFileArgs, ctx: ToolContext) -> dict[str, object]:
        source = self._resolve(ctx, args.source)
        target = self._resolve(ctx, args.target)
        self._validate_write(ctx, source, "move")
        self._validate_write(ctx, target, "create")
        self._check_file(source)
        if not target.parent.is_dir():
            raise WorkspaceFailure("FILE_NOT_FOUND", "Target parent directory must exist.")
        raw = source.read_bytes()
        before = hashlib.sha256(raw).hexdigest()
        if before.casefold() != args.expected_hash.casefold():
            raise WorkspaceFailure("STALE_FILE", "File changed after it was read.", True)
        if target.exists():
            raise WorkspaceFailure("PROJECT_CONFLICT", "Target already exists.")
        os.rename(source, target)
        old = source.relative_to(ctx.workspace).as_posix()
        new = target.relative_to(ctx.workspace).as_posix()
        self._record(ctx, "move", old, before, before, target=new)
        return {
            "operation": "move",
            "path": old,
            "target": new,
            "before_sha256": before,
            "after_sha256": before,
            "changed": True,
        }

    def delete_file(self, args: DeleteFileArgs, ctx: ToolContext) -> dict[str, object]:
        path = self._resolve(ctx, args.path)
        self._validate_write(ctx, path, "delete")
        self._check_file(path)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        if before.casefold() != args.expected_hash.casefold():
            raise WorkspaceFailure("STALE_FILE", "File changed after it was read.", True)
        relative = path.relative_to(ctx.workspace).as_posix()
        path.unlink()
        self._record(ctx, "delete", relative, before, None)
        return self._change("delete", relative, before, None, True)

    def create_directory(self, args: DirectoryArgs, ctx: ToolContext) -> dict[str, object]:
        path = self._resolve(ctx, args.path)
        if path.exists() and not path.is_dir():
            raise WorkspaceFailure("PROJECT_CONFLICT", "A file already exists at this path.")
        changed = not path.is_dir()
        path.mkdir(parents=True, exist_ok=True)
        return {
            "path": path.relative_to(ctx.workspace).as_posix(),
            "operation": "create",
            "changed": changed,
        }

    def remove_empty_directory(self, args: DirectoryArgs, ctx: ToolContext) -> dict[str, object]:
        path = self._resolve(ctx, args.path)
        if path == ctx.workspace:
            raise WorkspaceFailure("PROTECTED_PATH", "The workspace root cannot be removed.")
        if not path.is_dir():
            raise WorkspaceFailure("FILE_NOT_FOUND", "Directory does not exist.")
        try:
            path.rmdir()
        except OSError as error:
            raise WorkspaceFailure(
                "DIRECTORY_NOT_EMPTY", "Only an empty directory can be removed."
            ) from error
        return {
            "path": path.relative_to(ctx.workspace).as_posix(),
            "operation": "remove",
            "changed": True,
        }

    def registrations(self):
        from app.tooling.registry import ToolRegistration

        specs = [
            ("list_files", "List workspace files.", ListFilesArgs, self.list_files, False),
            (
                "search_code",
                "Search text in project files.",
                SearchCodeArgs,
                self.search_code,
                False,
            ),
            ("read_file", "Read a versioned text file range.", ReadFileArgs, self.read_file, False),
            (
                "create_file",
                "Create a new UTF-8 text file.",
                CreateFileArgs,
                self.create_file,
                True,
            ),
            (
                "edit_file",
                "Replace one unique text span after checking the file hash.",
                EditFileArgs,
                self.edit_file,
                True,
            ),
            (
                "move_file",
                "Move a file without overwriting an existing target.",
                MoveFileArgs,
                self.move_file,
                True,
            ),
            (
                "delete_file",
                "Delete one file after checking its hash.",
                DeleteFileArgs,
                self.delete_file,
                True,
            ),
            (
                "create_directory",
                "Create a directory and missing parents.",
                DirectoryArgs,
                self.create_directory,
                True,
            ),
            (
                "remove_empty_directory",
                "Remove an empty directory.",
                DirectoryArgs,
                self.remove_empty_directory,
                True,
            ),
        ]
        return [
            ToolRegistration(
                name,
                description,
                args,
                lambda value, ctx, fn=fn: fn(value, ctx),
                modifies_project=modifies,
            )
            for name, description, args, fn, modifies in specs
        ]

    def _resolve(self, ctx: ToolContext, value: str) -> Path:
        supplied = Path(value)
        if supplied.is_absolute():
            raise WorkspaceFailure(
                "OUTSIDE_WORKSPACE", "Tool paths must be relative to the workspace."
            )
        if ".." in supplied.parts:
            raise WorkspaceFailure("OUTSIDE_WORKSPACE", "Parent traversal is not permitted.")
        candidate = (ctx.workspace / supplied).resolve(strict=False)
        try:
            candidate.relative_to(ctx.workspace)
        except ValueError as error:
            raise WorkspaceFailure(
                "OUTSIDE_WORKSPACE", "Resolved path is outside the workspace."
            ) from error
        parts = {part.casefold() for part in candidate.relative_to(ctx.workspace).parts}
        if parts & {".git", ".env"} or any(part.startswith(".env.") for part in parts):
            raise WorkspaceFailure(
                "PROTECTED_PATH", "Credential files and Git metadata are protected."
            )
        if str(candidate).casefold() in ctx.protected_paths:
            raise WorkspaceFailure("PROTECTED_ACCEPTANCE", "This task path is protected.")
        return candidate

    def _check_file(self, path: Path) -> None:
        if not path.is_file():
            raise WorkspaceFailure("FILE_NOT_FOUND", "File does not exist.", True)

    def _excluded(self, path: Path, ctx: ToolContext) -> bool:
        try:
            relative = path.relative_to(ctx.workspace)
        except ValueError:
            return True
        parts = {part.casefold() for part in relative.parts}
        if parts & {part.casefold() for part in IGNORED_PARTS}:
            return True
        if any(part == ".env" or part.startswith(".env.") for part in parts):
            return True
        return str(path.resolve()).casefold() in ctx.protected_paths

    def _check_writable_text(self, content: str, ctx: ToolContext) -> None:
        if ctx.redact(content) != content:
            raise WorkspaceFailure("CREDENTIAL_PROTECTION", "Credential values cannot be written.")

    @staticmethod
    def _validate_write(ctx: ToolContext, path: Path, operation: str) -> None:
        validate = ctx.services.get("validate_write")
        if validate:
            validate(path.relative_to(ctx.workspace).as_posix(), operation)

    @staticmethod
    def _record(
        ctx: ToolContext,
        operation: str,
        path: str,
        before: str | None,
        after: str | None,
        target: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        change: dict[str, object] = {
            "operation": operation,
            "path": path,
            "before_sha256": before,
            "after_sha256": after,
        }
        if target is not None:
            change["target"] = target
        ctx.record_change({**change, **(details or {})})
        ctx.emit(
            type="file_changed",
            **{key: value for key, value in change.items() if key != "operation"},
            operation=operation,
        )

    @staticmethod
    def _change(
        operation: str, path: str, before: str | None, after: str | None, changed: bool
    ) -> dict[str, object]:
        return {
            "operation": operation,
            "path": path,
            "target": None,
            "before_sha256": before,
            "after_sha256": after,
            "changed": changed,
        }


def _decode(raw: bytes) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        return raw.decode(encoding)
    except (SyntaxError, UnicodeDecodeError) as error:
        raise WorkspaceFailure(
            "INVALID_ENCODING", f"File is not decodable text: {error}"
        ) from error


def _encoding(raw: bytes) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        return encoding
    except (SyntaxError, UnicodeDecodeError) as error:
        raise WorkspaceFailure("INVALID_ENCODING", "File encoding cannot be detected.") from error


def _encode(text: str, original: bytes) -> bytes:
    return text.encode(_encoding(original))


def _replace_preserving_newlines(original: str, old_text: str, new_text: str) -> str | None:
    normalized_characters: list[str] = []
    source_starts: list[int] = []
    source_ends: list[int] = []
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
    old_normalized = old_text.replace("\r\n", "\n").replace("\r", "\n")
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
    replacement = new_text.replace("\r\n", "\n").replace("\r", "\n")
    replacement = replacement.replace("\n", newline)
    return original[:source_start] + replacement + original[source_end:]


def _atomic_write(path: Path, content: bytes) -> None:
    mode = path.stat().st_mode
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
