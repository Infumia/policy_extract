"""Batch record helpers shared by CLI batch mode and serve mode.

A record is the compact JSONL line stored in ``.metadata`` (no text/tables).
A record is "not found" when extraction failed or a key field is missing, in
which case it is retried on every scan and mirrored to ``.not-found-metadata``.
"""

from __future__ import annotations

import json
from pathlib import Path

from policy_extract.models import PolicyExtraction

REQUIRED_FIELDS = ("police_no", "company")


def record_from_extraction(
    result: PolicyExtraction, *, sha256: str | None = None
) -> dict:
    """Compact batch record (no full text / tables)."""
    return {
        "file": Path(result.source_file).name,
        "sha256": sha256,
        "police_no": result.police_no,
        "police_no_source": result.police_no_source,
        "zeyil_no": result.zeyil_no,
        "zeyil_no_source": result.zeyil_no_source,
        "company": result.company,
        "company_confidence": result.company_confidence,
        "company_scores": result.company_scores,
    }


def is_not_found_record(record: dict) -> bool:
    """True when at least one key field is missing or an error is present."""
    if "error" in record:
        return True
    return any(not record.get(field) for field in REQUIRED_FIELDS)


def is_reusable_cache_entry(cached: dict, digest: str | None) -> bool:
    """Cache hit rule: same sha, no error, and complete (not not-found)."""
    return (
        "error" not in cached
        and cached.get("sha256") == digest
        and not is_not_found_record(cached)
    )


def load_metadata_cache(meta_path: Path) -> dict[str, dict]:
    """Read ``.metadata`` JSONL into ``{filename: record}`` (last wins)."""
    cache: dict[str, dict] = {}
    if not meta_path.is_file():
        return cache
    try:
        text = meta_path.read_text(encoding="utf-8")
    except OSError:
        return cache
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = record.get("file")
        if isinstance(name, str):
            cache[name] = record
    return cache


def append_record(meta_path: Path, record: dict) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with meta_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def compact_metadata(meta_path: Path, cache: dict[str, dict]) -> None:
    """Deduplicate records and rewrite sorted (atomic replace)."""
    import os

    if not cache and not meta_path.is_file():
        return
    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as handle:
        for name in sorted(cache):
            handle.write(json.dumps(cache[name], ensure_ascii=False) + "\n")
    os.replace(tmp, meta_path)


def rewrite_not_found(not_found_path: Path, cache: dict[str, dict]) -> None:
    if not cache and not not_found_path.is_file():
        return
    not_found_path.parent.mkdir(parents=True, exist_ok=True)
    with not_found_path.open("w", encoding="utf-8") as handle:
        for name in sorted(cache):
            record = cache[name]
            if is_not_found_record(record):
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
