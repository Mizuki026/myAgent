"""SQLite state store with per-operation connections and atomic event sequences."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.execution.process_manager import ProcessArtifact, ServiceSnapshot
from app.models_v2 import ArtifactContent, ProjectProfile, TaskEvent, TaskSnapshot
from app.tooling.types import ArtifactRef


class StoreError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StateStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "runs").mkdir(exist_ok=True)
        self.path = self.data_dir / "state.sqlite3"
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    workspace_key TEXT NOT NULL UNIQUE,
                    profile_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    snapshot_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    runtime_json TEXT NOT NULL DEFAULT '{}',
                    next_event_seq INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS web_tasks_project_created
                    ON web_tasks(project_id, id);
                CREATE TABLE IF NOT EXISTS web_events (
                    task_id TEXT NOT NULL REFERENCES web_tasks(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(task_id, seq)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES web_tasks(id),
                    service_id TEXT,
                    kind TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mutable INTEGER NOT NULL,
                    sha256 TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS services (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES web_tasks(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    snapshot_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS services_project_created
                    ON services(project_id, updated_at);
                """
            )

    def get_project(self, project_id: str) -> ProjectProfile:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT profile_json FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if row is None:
            raise StoreError("PROJECT_NOT_FOUND", "Project does not exist.")
        return ProjectProfile.model_validate_json(row[0])

    def list_projects(self, offset: int = 0, limit: int = 20) -> tuple[list[ProjectProfile], int]:
        with self._connection() as connection:
            total = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            rows = connection.execute(
                "SELECT profile_json FROM projects ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [ProjectProfile.model_validate_json(row[0]) for row in rows], total

    def save_project(self, profile: ProjectProfile, expected_revision: int | None = None) -> None:
        now = profile.updated_at
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT revision FROM projects WHERE id=?", (profile.id,)
            ).fetchone()
            if expected_revision is None and existing is not None:
                raise StoreError("PROJECT_CONFLICT", "Project already exists.")
            if expected_revision is not None:
                if existing is None:
                    raise StoreError("PROJECT_NOT_FOUND", "Project does not exist.")
                if existing[0] != expected_revision:
                    raise StoreError("REVISION_CONFLICT", "Project configuration changed.")
            try:
                connection.execute(
                    """INSERT INTO projects(
                         id, workspace_key, profile_json, revision, created_at, updated_at
                       )
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET workspace_key=excluded.workspace_key,
                         profile_json=excluded.profile_json, revision=excluded.revision,
                         updated_at=excluded.updated_at""",
                    (
                        profile.id,
                        _workspace_key(profile.workspace),
                        profile.model_dump_json(),
                        profile.revision,
                        profile.created_at,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreError(
                    "PROJECT_CONFLICT", "Another project uses this workspace."
                ) from error

    def create_task(self, task: TaskSnapshot, config: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT id FROM web_tasks WHERE json_extract(snapshot_json, '$.status') IN "
                "('running', 'needs_clarification') LIMIT 1"
            ).fetchone()
            if active:
                raise StoreError("TASK_BUSY", "Another task is active.")
            connection.execute(
                "INSERT INTO web_tasks(id, project_id, snapshot_json, config_json) "
                "VALUES (?, ?, ?, ?)",
                (task.id, task.project_id, task.model_dump_json(), _dump(config)),
            )
            self._append_event(connection, task.id, "task_status_changed", {"status": "running"})

    def get_task(self, task_id: str) -> TaskSnapshot:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM web_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise StoreError("TASK_NOT_FOUND", "Task does not exist.")
        return TaskSnapshot.model_validate_json(row[0])

    def list_tasks(
        self, project_id: str | None = None, offset: int = 0, limit: int = 20
    ) -> tuple[list[TaskSnapshot], int]:
        where = "WHERE project_id=?" if project_id else ""
        params = (project_id, limit, offset) if project_id else (limit, offset)
        with self._connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM web_tasks {where}", (project_id,) if project_id else ()
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT snapshot_json FROM web_tasks {where} "
                "ORDER BY json_extract(snapshot_json, '$.created_at') DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [TaskSnapshot.model_validate_json(row[0]) for row in rows], total

    def save_task(self, task: TaskSnapshot, runtime: dict[str, Any] | None = None) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT runtime_json FROM web_tasks WHERE id=?", (task.id,)
            ).fetchone()
            if row is None:
                raise StoreError("TASK_NOT_FOUND", "Task does not exist.")
            runtime_json = _dump(runtime) if runtime is not None else row[0]
            connection.execute(
                "UPDATE web_tasks SET snapshot_json=?, runtime_json=? WHERE id=?",
                (task.model_dump_json(), runtime_json, task.id),
            )

    def transition_task(
        self,
        task: TaskSnapshot,
        event_kind: str,
        payload: dict[str, Any],
        expected_statuses: set[str] | None = None,
    ) -> TaskSnapshot:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT snapshot_json FROM web_tasks WHERE id=?", (task.id,)
            ).fetchone()
            if row is None:
                raise StoreError("TASK_NOT_FOUND", "Task does not exist.")
            current = TaskSnapshot.model_validate_json(row[0])
            if expected_statuses and current.status not in expected_statuses:
                raise StoreError("INVALID_TASK_STATE", "Task status changed before transition.")
            connection.execute(
                "UPDATE web_tasks SET snapshot_json=? WHERE id=?",
                (task.model_dump_json(), task.id),
            )
            self._append_event(connection, task.id, event_kind, payload)
            updated = connection.execute(
                "SELECT snapshot_json FROM web_tasks WHERE id=?", (task.id,)
            ).fetchone()[0]
        return TaskSnapshot.model_validate_json(updated)

    def load_runtime_state(self, task_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT runtime_json FROM web_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise StoreError("TASK_NOT_FOUND", "Task does not exist.")
        return json.loads(row[0])

    def save_runtime_state(self, task_id: str, runtime: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE web_tasks SET runtime_json=? WHERE id=?", (_dump(runtime), task_id)
            )

    def append_event(self, task_id: str, kind: str, payload: dict[str, Any]) -> TaskEvent:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._append_event(connection, task_id, kind, payload)

    def events_after(self, task_id: str, seq: int, limit: int = 100) -> list[TaskEvent]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT seq, kind, timestamp, payload_json FROM web_events "
                "WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                (task_id, seq, limit),
            ).fetchall()
        return [
            TaskEvent(
                task_id=task_id,
                seq=row[0],
                kind=row[1],
                timestamp=row[2],
                payload=json.loads(row[3]),
            )
            for row in rows
        ]

    def register_artifact(
        self,
        artifact: ArtifactRef | ProcessArtifact,
        path: Path,
        *,
        service_id: str | None = None,
    ) -> ArtifactRef:
        path = path.resolve()
        task_dir = (self.data_dir / "runs" / artifact.task_id).resolve()
        try:
            relative = path.relative_to(task_dir).as_posix()
        except ValueError as error:
            raise StoreError(
                "OUTSIDE_WORKSPACE", "Artifact must be inside its task directory."
            ) from error
        ref = ArtifactRef(
            id=artifact.id,
            task_id=artifact.task_id,
            service_id=service_id or getattr(artifact, "service_id", None),
            kind=artifact.kind,
            media_type=getattr(artifact, "media_type", "text/plain; charset=utf-8"),
            size_bytes=path.stat().st_size if path.exists() else artifact.size_bytes,
            mutable=getattr(artifact, "mutable", False),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest()
            if path.exists() and not getattr(artifact, "mutable", False)
            else None,
            created_at=getattr(artifact, "created_at", datetime.now(UTC).isoformat()),
        )
        with self._connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO artifacts
                   (id, task_id, service_id, kind, media_type, relative_path, size_bytes,
                    mutable, sha256, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ref.id,
                    ref.task_id,
                    ref.service_id,
                    ref.kind,
                    ref.media_type,
                    relative,
                    ref.size_bytes,
                    int(ref.mutable),
                    ref.sha256,
                    ref.created_at,
                ),
            )
        return ref

    def list_artifacts(
        self, task_id: str, offset: int = 0, limit: int = 20
    ) -> tuple[list[ArtifactRef], int]:
        with self._connection() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT id, task_id, service_id, kind, media_type, size_bytes, mutable, "
                "sha256, created_at "
                "FROM artifacts WHERE task_id=? ORDER BY created_at LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            ).fetchall()
        items = [
            ArtifactRef(
                id=r[0],
                task_id=r[1],
                service_id=r[2],
                kind=r[3],
                media_type=r[4],
                size_bytes=r[5],
                mutable=bool(r[6]),
                sha256=r[7],
                created_at=r[8],
            )
            for r in rows
        ]
        return items, total

    def read_artifact(
        self, artifact_id: str, offset: int = 0, limit: int = 65536
    ) -> ArtifactContent:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, task_id, service_id, kind, media_type, relative_path, size_bytes, "
                "mutable, sha256, created_at "
                "FROM artifacts WHERE id=?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise StoreError("ARTIFACT_NOT_FOUND", "Artifact does not exist.")
        artifact = ArtifactRef(
            id=row[0],
            task_id=row[1],
            service_id=row[2],
            kind=row[3],
            media_type=row[4],
            size_bytes=row[6],
            mutable=bool(row[7]),
            sha256=row[8],
            created_at=row[9],
        )
        task_dir = (self.data_dir / "runs" / artifact.task_id).resolve()
        path = (task_dir / row[5]).resolve()
        if not path.is_relative_to(task_dir):
            raise StoreError(
                "OUTSIDE_WORKSPACE", "Artifact path escaped its registered task directory."
            )
        raw = path.read_bytes()
        if offset < 0 or offset > len(raw):
            raise StoreError("INVALID_OFFSET", "Artifact offset is invalid.")
        if offset < len(raw) and offset > 0:
            while offset < len(raw) and raw[offset] & 0xC0 == 0x80:
                offset += 1
        chunk = raw[offset : offset + limit]
        end = offset + len(chunk)
        while end > offset and end < len(raw) and raw[end] & 0xC0 == 0x80:
            end -= 1
        chunk = raw[offset:end]
        return ArtifactContent(
            artifact=artifact,
            offset=offset,
            next_offset=end,
            size_bytes=len(raw),
            text=chunk.decode("utf-8", errors="replace"),
            eof=end >= len(raw),
            truncated=artifact.mutable or end < len(raw),
        )

    def save_service(self, service: ServiceSnapshot) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO services("
                "id, task_id, project_id, snapshot_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    service.id,
                    service.task_id,
                    service.project_id,
                    service.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_service(self, service_id: str) -> ServiceSnapshot:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM services WHERE id=?", (service_id,)
            ).fetchone()
        if row is None:
            raise StoreError("SERVICE_NOT_FOUND", "Service does not exist.")
        return ServiceSnapshot.model_validate_json(row[0])

    def list_services(
        self,
        project_id: str | None = None,
        task_id: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[ServiceSnapshot], int]:
        filters: list[str] = []
        params: list[Any] = []
        if project_id:
            filters.append("project_id=?")
            params.append(project_id)
        if task_id:
            filters.append("task_id=?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self._connection() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM services {where}", params).fetchone()[
                0
            ]
            rows = connection.execute(
                f"SELECT snapshot_json FROM services {where} "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [ServiceSnapshot.model_validate_json(row[0]) for row in rows], total

    def mark_unfinished_interrupted(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, snapshot_json FROM web_tasks "
                "WHERE json_extract(snapshot_json, '$.status') IN "
                "('running', 'needs_clarification')"
            ).fetchall()
        task_ids: list[str] = []
        for task_id, raw in rows:
            task = TaskSnapshot.model_validate_json(raw).model_copy(
                update={"status": "interrupted", "phase": "finished", "updated_at": _now()}
            )
            self.save_task(task)
            self.append_event(task_id, "task_status_changed", {"status": "interrupted"})
            task_ids.append(task_id)
        with self._connection() as connection:
            connection.execute(
                "UPDATE services SET snapshot_json=json_set(snapshot_json, '$.status', 'unknown')"
            )
        return task_ids

    def close(self) -> None:
        return None

    def _append_event(
        self, connection: sqlite3.Connection, task_id: str, kind: str, payload: dict[str, Any]
    ) -> TaskEvent:
        row = connection.execute(
            "SELECT next_event_seq, snapshot_json FROM web_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise StoreError("TASK_NOT_FOUND", "Task does not exist.")
        seq, snapshot_json = row
        timestamp = _now()
        connection.execute(
            "INSERT INTO web_events(task_id, seq, kind, timestamp, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, seq, kind, timestamp, _dump(payload)),
        )
        snapshot = TaskSnapshot.model_validate_json(snapshot_json).model_copy(
            update={"last_event_seq": seq, "updated_at": timestamp}
        )
        connection.execute(
            "UPDATE web_tasks SET next_event_seq=?, snapshot_json=? WHERE id=?",
            (seq + 1, snapshot.model_dump_json(), task_id),
        )
        return TaskEvent(task_id=task_id, seq=seq, kind=kind, timestamp=timestamp, payload=payload)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _workspace_key(value: str) -> str:
    return str(Path(value).resolve()).casefold()
