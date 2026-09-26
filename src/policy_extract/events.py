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


def emit_event(payload: dict[str, Any], *, stream=None) -> None:
    """Write one JSON line to stdout (listened by Flutter)."""
    out = stream or sys.stdout
    out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    out.flush()


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
