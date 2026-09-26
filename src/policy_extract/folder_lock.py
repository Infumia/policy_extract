"""Single-instance folder lock (one serve per folder)."""

from __future__ import annotations

import os
from pathlib import Path

LOCK_FILENAME = ".policy-extract.lock"


def acquire_folder_lock(folder: Path, *, alive_fn=None) -> Path | None:
    """Return lock path on success; None when a live serve owns the folder.

    A stale lock left by a dead process is taken over automatically.
    `alive_fn` is injectable for tests (defaults to process_utils.parent_alive).
    """
    if alive_fn is None:
        from policy_extract.process_utils import parent_alive as alive_fn

    lock = folder / LOCK_FILENAME
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _is_live_lock(lock, alive_fn=alive_fn):
                return None
            try:
                lock.unlink()
            except OSError:
                return None
            continue
        except OSError:
            return None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
        except OSError:
            return None
        return lock
    return None


def _is_live_lock(lock: Path, *, alive_fn) -> bool:

    try:
        raw = lock.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    try:
        pid = int(raw.split()[0])
    except (ValueError, IndexError):
        return False
    return pid > 0 and pid != os.getpid() and bool(alive_fn(pid))


def release_folder_lock(lock: Path) -> None:
    """Release the lock, but only if owned by this process."""
    try:
        raw = lock.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if raw.split()[0:1] == [str(os.getpid())]:
        lock.unlink(missing_ok=True)
