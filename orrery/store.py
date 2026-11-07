"""iCloud-safe storage operations: unavailable reads, backed-up atomic writes, file locks."""

import contextlib
import fcntl
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orrery import paths


@dataclass(frozen=True)
class Unavailable:
    """Returned when a file cannot be read."""

    reason: str


class RefusedWrite(RuntimeError):
    """Raised when refusing to overwrite an unreadable file."""

    pass


def read_text(path: Path) -> str | Unavailable:
    """Read a text file, returning Unavailable if the file is missing or unreadable.

    Args:
        path: The file to read.

    Returns:
        The file's text content, or Unavailable("missing") if the file doesn't exist,
        or Unavailable(str(e)) if the file can't be read.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Unavailable("missing")
    except (OSError, UnicodeDecodeError) as e:
        return Unavailable(str(e))


def atomic_write(path: Path, text: str, *, backup: bool = True) -> None:
    """Write text to a file atomically with optional backup.

    If backup=True and the file exists and is readable, it is backed up to
    backup_dir()/<name>.<YYYYmmddTHHMMSS> before being overwritten.
    If the file exists but can't be read, raises RefusedWrite and does nothing.

    The write is atomic: creates a unique temp file in the same directory,
    writes the content, fsyncs, and renames over the target.

    Args:
        path: The file to write.
        text: The text content to write.
        backup: Whether to back up existing file (default True).

    Raises:
        RefusedWrite: If the file exists but can't be read.
    """
    # Check if the file exists and back it up or refuse
    existing = read_text(path)
    if isinstance(existing, Unavailable):
        if existing.reason != "missing":
            raise RefusedWrite(f"Refusing to overwrite unreadable file: {path}")
    else:
        if backup:
            # Back up the existing file (byte copy to preserve encoding)
            timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            backup_path = paths.backup_dir() / f"{path.name}.{timestamp}"
            shutil.copy2(path, backup_path)

    # Write to a unique temp file in the same directory
    parent = path.parent
    fd, temp_path = tempfile.mkstemp(dir=parent, prefix=path.name + ".tmp")
    temp_path = Path(temp_path)

    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # Atomically rename to the target
        os.replace(temp_path, path)
        replaced = True
    finally:
        if not replaced:
            try:
                temp_path.unlink()
            except OSError:
                pass


class _Held(threading.local):
    """The lock names this thread holds."""

    def __init__(self) -> None:
        self.names: set[str] = set()


_held = _Held()


@contextlib.contextmanager
def file_lock(name: str, *, blocking: bool = True):
    """Acquire a file lock for the given name.

    The lock is stored in cache_dir()/<name>.lock and uses fcntl.flock.
    It is non-reentrant: a thread taking a name it already holds raises RuntimeError at once
    instead of deadlocking. Other threads and processes block until it is released.

    Args:
        name: The lock name (without .lock extension).
        blocking: If False, fail at once instead of waiting for another holder.

    Yields:
        None after acquiring the lock. The lock is released on exit.

    Raises:
        RuntimeError: If this thread already holds the lock.
        BlockingIOError: If blocking is False and another holder has the lock.
    """
    if name in _held.names:
        raise RuntimeError(f"file lock '{name}' is already held by this thread")
    lock_path = paths.cache_dir() / f"{name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        _held.names.add(name)
        try:
            yield
        finally:
            _held.names.discard(name)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
