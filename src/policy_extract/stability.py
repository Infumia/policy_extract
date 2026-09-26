"""File-stability wait: don't process half-written (copying) PDFs."""

from __future__ import annotations

import threading
import time
from pathlib import Path


def wait_for_stable(
    pdf: Path,
    *,
    checks: int = 3,
    interval_ms: int = 500,
    stop: threading.Event | None = None,
) -> bool:
    """True when size is steady `checks` times in a row; False if deleted."""
    if checks <= 1:
        return pdf.is_file()
    last_size: int | None = None
    steady = 0
    for _ in range(max(checks * 5, checks)):
        if stop is not None and stop.is_set():
            return False
        try:
            size = pdf.stat().st_size
        except OSError:
            return False
        if last_size is not None and size == last_size:
            steady += 1
            if steady >= checks - 1:
                return True
        else:
            steady = 0
        last_size = size
        time.sleep(interval_ms / 1000.0)
    return pdf.is_file()
