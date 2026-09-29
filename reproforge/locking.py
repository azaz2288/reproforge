"""Cross-platform process lock for a project's mutable ReproForge state."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .storage import StorageError


@contextmanager
def project_lock(root: Path, timeout: float = 10.0) -> Iterator[None]:
    """Serialize writers; the OS releases this lock if a process crashes."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "run.lock"
    if path.is_symlink():
        raise StorageError(f"Lock path is a symbolic link: {path}")
    try:
        with path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise StorageError(f"Timed out waiting for project lock {path}: {exc}") from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise StorageError(f"Cannot use project lock {path}: {exc}") from exc
