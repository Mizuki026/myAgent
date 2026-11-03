"""Project inspection and revisioned project configuration."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.models_v2 import ProjectInput, ProjectInspection, ProjectProfile
from app.storage.state_store import StateStore, StoreError
from app.tooling.types import ToolError


class ProjectServiceError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ProjectService:
    def __init__(
        self,
        store: StateStore,
        is_project_active: Callable[[str], bool] | None = None,
    ) -> None:
        self.store = store
        self.is_project_active = is_project_active or (lambda _project_id: False)

    def inspect_project(self, workspace: str) -> ProjectInspection:
        path = Path(workspace).expanduser()
        if not path.exists() or not path.is_dir():
            raise ProjectServiceError(
                "INVALID_ARGUMENTS", "workspace must be an existing directory."
            )
        normalized = path.resolve()
        issues: list[ToolError] = []
        git_root = self._git("rev-parse", "--show-toplevel", cwd=normalized)
        head = self._git("rev-parse", "HEAD", cwd=normalized) if git_root else None
        is_repository = git_root is not None and head is not None
        if not is_repository:
            issues.append(
                ToolError(
                    code="ENVIRONMENT_NOT_READY",
                    message="Workspace must be a Git repository with a commit.",
                )
            )

        candidates = self._python_candidates(normalized)
        selected = None
        pytest_available = False
        for candidate in candidates:
            try:
                result = subprocess.run(
                    [candidate, "-m", "pytest", "--version"],
                    cwd=normalized,
                    capture_output=True,
                    timeout=10,
                    check=False,
                    env=self._safe_environment(),
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0:
                selected = candidate
                pytest_available = True
                break
        if selected is None:
            issues.append(
                ToolError(
                    code="ENVIRONMENT_NOT_READY",
                    message="No available Python interpreter can run pytest.",
                )
            )
        return ProjectInspection(
            workspace=str(normalized),
            is_git_repository=is_repository,
            head=head,
            candidate_interpreters=candidates,
            selected_interpreter=selected,
            pytest_available=pytest_available,
            suggested_pytest_args=[],
            ready=not issues,
            issues=issues,
        )

    def save_project(
        self,
        profile: ProjectInput,
        project_id: str | None = None,
        expected_revision: int | None = None,
    ) -> ProjectProfile:
        normalized = Path(profile.workspace).expanduser().resolve()
        inspection = self.inspect_project(str(normalized))
        if not inspection.ready:
            message = "; ".join(issue.message for issue in inspection.issues)
            raise ProjectServiceError("ENVIRONMENT_NOT_READY", message)
        interpreter = self._resolve_interpreter(profile.python_executable, inspection)
        self._validate_pytest_args(profile.pytest_args)
        existing = None
        if project_id is not None:
            if expected_revision is None:
                raise ProjectServiceError(
                    "INVALID_ARGUMENTS", "expected_revision is required for updates."
                )
            existing = self.store.get_project(project_id)
            if self.is_project_active(project_id):
                raise ProjectServiceError("PROJECT_IN_USE", "Project is used by an active task.")
            if existing.revision != expected_revision:
                raise ProjectServiceError("REVISION_CONFLICT", "Project configuration changed.")
        elif expected_revision is not None:
            raise ProjectServiceError(
                "INVALID_ARGUMENTS", "expected_revision is only valid for updates."
            )

        now = datetime.now(UTC).isoformat()
        profile_id = project_id or str(uuid.uuid4())
        data = profile.model_dump()
        data["workspace"] = str(normalized)
        data["python_executable"] = interpreter
        if existing:
            before = existing.model_dump(exclude={"revision", "created_at", "updated_at"})
            after = {
                **data,
                "id": existing.id,
            }
            changed = before != after
            revision = existing.revision + int(changed)
            created_at = existing.created_at
        else:
            revision = 1
            created_at = now
        saved = ProjectProfile(
            **data,
            id=profile_id,
            revision=revision,
            created_at=created_at,
            updated_at=now,
        )
        try:
            self.store.save_project(saved, expected_revision)
        except StoreError as error:
            raise ProjectServiceError(error.code, str(error)) from error
        return saved

    def get_project(self, project_id: str) -> ProjectProfile:
        try:
            return self.store.get_project(project_id)
        except StoreError as error:
            raise ProjectServiceError(error.code, str(error)) from error

    def list_projects(self, offset: int = 0, limit: int = 20) -> tuple[list[ProjectProfile], int]:
        self._validate_pagination(offset, limit)
        return self.store.list_projects(offset, limit)

    def patch_project(
        self, project_id: str, fields: dict[str, object], expected_revision: int
    ) -> ProjectProfile:
        existing = self.get_project(project_id)
        allowed = set(ProjectInput.model_fields)
        if set(fields) - (allowed | {"expected_revision"}):
            raise ProjectServiceError("INVALID_ARGUMENTS", "Unknown project configuration field.")
        if fields.pop("expected_revision", expected_revision) != expected_revision:
            raise ProjectServiceError("INVALID_ARGUMENTS", "Conflicting expected_revision values.")
        merged = existing.model_dump(include=allowed)
        merged.update(fields)
        try:
            profile = ProjectInput.model_validate(merged, strict=True)
        except Exception as error:
            raise ProjectServiceError(
                "INVALID_ARGUMENTS", "Invalid project configuration."
            ) from error
        return self.save_project(profile, project_id, expected_revision)

    @staticmethod
    def _python_candidates(workspace: Path) -> list[str]:
        candidates = [
            workspace / ".venv" / "Scripts" / "python.exe",
            workspace / ".venv" / "bin" / "python",
            Path(sys.executable),
        ]
        result: list[str] = []
        for candidate in candidates:
            value = str(candidate.resolve()) if candidate.exists() else shutil.which(str(candidate))
            if value and value not in result:
                result.append(value)
        return result

    @staticmethod
    def _resolve_interpreter(value: str, inspection: ProjectInspection) -> str:
        supplied = Path(value).expanduser()
        candidate = str(supplied.resolve()) if supplied.exists() else shutil.which(value)
        if not candidate or candidate not in inspection.candidate_interpreters:
            raise ProjectServiceError(
                "ENVIRONMENT_NOT_READY", "Selected interpreter is not a verified candidate."
            )
        return candidate

    @staticmethod
    def _validate_pytest_args(args: list[str]) -> None:
        forbidden = ("--junitxml", "--collect-only", "--ignore", "--deselect")
        if any(
            any(argument == flag or argument.startswith(flag + "=") for flag in forbidden)
            for argument in args
        ):
            raise ProjectServiceError(
                "INVALID_ARGUMENTS",
                "pytest_args cannot override system report or discovery behavior.",
            )
        if any(argument in {"-p", "--pyargs"} for argument in args):
            raise ProjectServiceError(
                "INVALID_ARGUMENTS", "pytest_args contains a reserved pytest option."
            )

    @staticmethod
    def _validate_pagination(offset: int, limit: int) -> None:
        if offset < 0 or not 1 <= limit <= 100:
            raise ProjectServiceError("INVALID_ARGUMENTS", "Invalid pagination range.")

    @staticmethod
    def _git(*arguments: str, cwd: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                env={**ProjectService._safe_environment(), "GIT_OPTIONAL_LOCKS": "0"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        sensitive = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        return {
            key: value
            for key, value in os.environ.items()
            if not any(word in key.upper() for word in sensitive)
        }
