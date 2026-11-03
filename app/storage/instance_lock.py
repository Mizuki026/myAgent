"""Cross-platform single-instance lock for one application data directory."""

from __future__ import annotations

import os
from pathlib import Path


class InstanceLockError(Exception):
    code = "INSTANCE_ALREADY_RUNNING"


class InstanceLock:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir.resolve() / "instance.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        self._stream.seek(0)
        if self._stream.read(1) == b"":
            self._stream.seek(0)
            self._stream.write(b"0")
            self._stream.flush()
        self._stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._stream.close()
            raise InstanceLockError(
                "Another application instance owns this data directory."
            ) from error

    def close(self) -> None:
        if self._stream.closed:
            return
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()
