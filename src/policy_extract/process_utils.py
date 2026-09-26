"""OS process helpers (parent tracking, no extra dependencies)."""

from __future__ import annotations

import ctypes
import os


def parent_alive(pid: int) -> bool:
    """True when pid is alive. False when it cannot be verified (safe side)."""
    if pid <= 0:
        return True
    try:
        if os.name == "nt":
            return _windows_pid_alive(pid)
        os.kill(pid, 0)
        return True
    except (OSError, AttributeError):
        return False


def _windows_pid_alive(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
