"""Git status, snapshots, and bounded diffs without mutating the index."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import StrictStr

from app.models_v2 import DiffFile
from app.tooling.types import StrictModel, ToolContext


class GitFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GetDiffArgs(StrictModel):
    scope: Literal["task", "workspace"] = "task"


class GitSnapshot(StrictModel):
    head: StrictStr
    index_fingerprint: StrictStr
    staged_paths: list[StrictStr]
    unstaged_paths: list[StrictStr]
    untracked_paths: list[StrictStr]


class GitService:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def snapshot(self) -> GitSnapshot:
        head = self._git(["rev-parse", "HEAD"])
        index_path = Path(self._git(["rev-parse", "--git-path", "index"]))
        if not index_path.is_absolute():
            index_path = (self.workspace / index_path).resolve()
        index_fingerprint = (
            hashlib.sha256(index_path.read_bytes()).hexdigest()
            if index_path.is_file()
            else "missing"
        )
        staged = self._paths(["diff", "--cached", "--name-only", "-z"])
        unstaged = self._paths(["diff", "--name-only", "-z"])
        untracked = self._paths(["ls-files", "--others", "--exclude-standard", "-z"])
        return GitSnapshot(
            head=head,
            index_fingerprint=index_fingerprint,
            staged_paths=staged,
            unstaged_paths=unstaged,
            untracked_paths=untracked,
        )

    def status(self, ctx: ToolContext) -> dict[str, object]:
        baseline = ctx.services.get("git_snapshot")
        if not isinstance(baseline, GitSnapshot):
            baseline = self.snapshot()
        current = self.snapshot()
        task_changes = ctx.services.get("task_changes", [])
        task_paths = sorted(
            {str(item[key]) for item in task_changes for key in ("path", "target") if item.get(key)}
        )
        original_paths = sorted(
            set(baseline.staged_paths)
            | set(baseline.unstaged_paths)
            | set(baseline.untracked_paths)
        )
        conflict = (
            current.head != baseline.head or current.index_fingerprint != baseline.index_fingerprint
        )
        return {
            "head": current.head,
            "user_changes": original_paths,
            "task_changes": task_paths,
            "staged_paths": current.staged_paths,
            "unstaged_paths": current.unstaged_paths,
            "untracked_paths": current.untracked_paths,
            "conflict": conflict,
        }

    def diff(self, scope: str, ctx: ToolContext) -> dict[str, object]:
        if scope not in {"task", "workspace"}:
            raise GitFailure("INVALID_ARGUMENTS", "Diff scope must be task or workspace.")
        changes = ctx.services.get("task_changes", [])
        paths = sorted(
            {str(item[key]) for item in changes for key in ("path", "target") if item.get(key)}
        )
        baseline = ctx.services.get("git_snapshot")
        baseline_head = (
            baseline.head if scope == "task" and isinstance(baseline, GitSnapshot) else "HEAD"
        )
        args = ["diff", "--no-ext-diff", "--no-color", baseline_head, "--"]
        if scope == "task":
            args.extend(paths)
        text = self._git(args, allow_nonzero=True)
        status_args = ["diff", "--name-status", baseline_head, "--"]
        if scope == "task":
            status_args.extend(paths)
        files = self._files_from_status(self._git(status_args, allow_nonzero=True))
        if scope == "workspace":
            untracked = self._paths(["ls-files", "--others", "--exclude-standard", "-z"])
        else:
            untracked = [
                path
                for path in paths
                if (self.workspace / path).is_file() and path not in self._tracked_paths()
            ]
        for relative in untracked:
            target = (self.workspace / relative).resolve()
            if not target.is_relative_to(self.workspace) or not target.is_file():
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                content = "[binary or undecodable file omitted]\n"
            text += (
                f"\n--- /dev/null\n+++ b/{relative}\n"
                + "\n".join(f"+{line}" for line in content.splitlines())
                + "\n"
            )
            files.append(DiffFile(path=relative, change="added"))
        encoded = text.encode("utf-8")
        truncated = len(encoded) > 40_000
        clipped = encoded[:40_000].decode("utf-8", errors="ignore")
        return {
            "scope": scope,
            "text": ctx.redact(clipped),
            "files": files,
            "truncated": truncated,
            "artifact": None,
        }

    def validate_write(self, relative: str, operation: str, snapshot: GitSnapshot) -> None:
        path = relative.replace("\\", "/")
        staged = {item.casefold() for item in snapshot.staged_paths}
        untracked = {item.casefold() for item in snapshot.untracked_paths}
        unstaged = {item.casefold() for item in snapshot.unstaged_paths}
        if path.casefold() in staged:
            raise GitFailure("USER_CHANGE_CONFLICT", "The path has user-staged changes.")
        if path.casefold() in untracked:
            raise GitFailure(
                "USER_CHANGE_CONFLICT", "An initial untracked file is protected from task writes."
            )
        if path.casefold() in unstaged and operation in {"move", "delete"}:
            raise GitFailure(
                "USER_CHANGE_CONFLICT", "Moving or deleting a file with user changes is unsafe."
            )
        if path.casefold() in untracked and operation in {"move", "delete"}:
            raise GitFailure(
                "USER_CHANGE_CONFLICT", "Moving or deleting an initial untracked file is unsafe."
            )

    def registrations(self):
        from app.tooling.registry import ToolRegistration

        class EmptyArgs(StrictModel):
            pass

        return [
            ToolRegistration(
                "git_status",
                "Show the current Git state and distinguish task changes from existing work.",
                EmptyArgs,
                lambda _args, ctx: self.status(ctx),
            ),
            ToolRegistration(
                "get_diff",
                "Read a bounded task or workspace diff.",
                GetDiffArgs,
                lambda args, ctx: self.diff(args.scope, ctx),
            ),
        ]

    def ensure_safe_delivery(self, baseline: GitSnapshot, task_paths: set[str]) -> None:
        current = self.snapshot()
        if current.head != baseline.head:
            raise GitFailure("GIT_BASE_CHANGED", "Git HEAD changed during the task.")
        if current.index_fingerprint != baseline.index_fingerprint:
            raise GitFailure("GIT_INDEX_CHANGED", "Git index changed during the task.")
        initial = (
            set(baseline.staged_paths)
            | set(baseline.unstaged_paths)
            | set(baseline.untracked_paths)
        )
        present = (
            set(current.staged_paths) | set(current.unstaged_paths) | set(current.untracked_paths)
        )
        known = {path.casefold() for path in initial | task_paths}
        unexpected = sorted(path for path in present if path.casefold() not in known)
        if unexpected:
            raise GitFailure(
                "USER_CHANGE_CONFLICT",
                "Commands created or changed files outside the registered task changes.",
            )

    def _git(self, args: list[str], allow_nonzero: bool = False) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.workspace,
            capture_output=True,
            timeout=10,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        if result.returncode and not allow_nonzero:
            raise GitFailure("GIT_ERROR", "Git query failed.")
        return result.stdout.decode("utf-8", errors="replace")

    def _paths(self, args: list[str]) -> list[str]:
        return [value for value in self._git(args).split("\0") if value]

    def _tracked_paths(self) -> set[str]:
        return set(self._paths(["ls-files", "-z"]))

    @staticmethod
    def _files_from_status(output: str) -> list[DiffFile]:
        files: list[DiffFile] = []
        for line in output.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            code, path = parts
            kind = {"A": "added", "M": "modified", "D": "deleted"}.get(code[:1], "unknown")
            source = None
            if code.startswith("R") and "\t" in path:
                source, path = path.split("\t", 1)
                kind = "renamed"
            files.append(DiffFile(path=path, source=source, change=kind))
        return files
