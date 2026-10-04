"""Stdout event / stderr log helpers (Flutter protocol)."""

from __future__ import annotations

import json
import sys
from typing import Any

# Quiet by default: only lifecycle + replies flow. Per-file interim events
# (progress/file_done/...) require --stream-events; normally Flutter polls
# `status` and reads finished records from .metadata.
STREAMED_TYPES = frozenset(
    {"progress", "file_done", "file_cached", "file_error", "file_queued", "idle"}
)


def configure_standard_streams() -> None:
    """Use UTF-8 for desktop pipes, independent of the Windows code page.

    Configure before parsing arguments or starting service threads. Captured
    text streams such as StringIO have no encoder and need no configuration.
    """
    for stream, errors in (
        (sys.stdin, "strict"),
        (sys.stdout, "strict"),
        (sys.stderr, "backslashreplace"),
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)


def emit_event(payload: dict[str, Any], *, stream=None) -> None:
    """Write one JSON line to stdout (listened by Flutter)."""
    out = stream or sys.stdout
    out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    out.flush()


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
