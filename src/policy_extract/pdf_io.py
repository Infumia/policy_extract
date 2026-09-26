"""PDF reading (pypdf fast path) and pure merge logic."""

from __future__ import annotations

import hashlib
from pathlib import Path

from policy_extract.company_detection import detect_company
from policy_extract.models import PolicyExtraction
from policy_extract.policy_numbers import (
    find_inline_police_no,
    find_inline_zeyil_no,
    find_table_police_no,
    find_table_zeyil_no,
)

Table = list[list[str]]
_HASH_CHUNK_SIZE = 1024 * 1024


def hash_file(path: str | Path, *, chunk_size: int = _HASH_CHUNK_SIZE) -> str:
    """SHA-256 hex digest of a file (batch cache key)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_text_fast(pdf_path: str, *, max_pages: int = 7) -> str:
    """Layout-preserving text of the first N pages; '' on failure."""
    import logging

    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(pdf_path)
    except Exception:
        return ""
    chunks: list[str] = []
    pages = reader.pages if max_pages <= 0 else reader.pages[:max_pages]
    for page in pages:
        chunks.append(_extract_page_text(page))
    return "\n".join(chunks)


def _extract_page_text(page) -> str:
    try:
        text = page.extract_text(extraction_mode="layout") or ""
        if not text.strip():
            text = page.extract_text() or ""
        return text
    except Exception:
        try:
            return page.extract_text() or ""
        except Exception:
            return ""


def extract_policy(
    pdf_path: str,
    *,
    full_text: str | None = None,
    tables: list[list[Table]] | list[Table] | None = None,
) -> PolicyExtraction:
    """Pure merge: text + table rows -> PolicyExtraction."""
    text = full_text or ""
    rows = tables or []

    police_no = find_inline_police_no(text)
    police_source = "inline" if police_no else None
    if police_no is None:
        police_no = find_table_police_no(rows)  # type: ignore[arg-type]
        police_source = "table" if police_no else None

    zeyil_no = find_inline_zeyil_no(text)
    zeyil_source = "inline" if zeyil_no else None
    if zeyil_no is None:
        zeyil_no = find_table_zeyil_no(rows)  # type: ignore[arg-type]
        zeyil_source = "table" if zeyil_no else None

    company, confidence, scores = detect_company(text)
    return PolicyExtraction(
        source_file=pdf_path,
        police_no=police_no,
        police_no_source=police_source,
        zeyil_no=zeyil_no,
        zeyil_no_source=zeyil_source,
        company=company,
        company_confidence=confidence,
        company_scores=scores,
        full_text=text,
        tables=rows,  # type: ignore[arg-type]
    )


def extract_policy_fast(pdf_path: str, *, max_pages: int = 7) -> PolicyExtraction:
    """Fast path: pypdf text only, no tables."""
    return extract_policy(
        pdf_path, full_text=extract_text_fast(pdf_path, max_pages=max_pages), tables=[]
    )
