"""Small SQLite-backed task and event store."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TaskStore:
    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "state.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                task_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (task_id, seq)
            );
            """
        )
        self.connection.commit()

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def create(self, task_id: str, payload: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT INTO tasks(task_id, payload, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (task_id, self._dump(payload), now, now),
        )
        self.connection.commit()

    def save(self, task_id: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE tasks SET payload = ?, updated_at = ? WHERE task_id = ?",
            (self._dump(payload), datetime.now(UTC).isoformat(), task_id),
        )
        self.connection.commit()

    def add_event(self, task_id: str, event: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO events(task_id, seq, payload) VALUES (?, ?, ?)",
            (task_id, event["seq"], self._dump(event)),
        )
        self.connection.commit()

    def events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM events WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
