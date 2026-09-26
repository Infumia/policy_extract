"""Policy / endorsement number search in text and tables.

Two sources, in priority order:
1. Inline text: ``Policy No: 0000`` (plus reversed and generic fallbacks).
2. Tables: value next to / below a ``Policy No`` header cell.
"""

from __future__ import annotations

import re

from policy_extract.text_utils import (
    clean_cell,
    clean_identifier,
    is_base_endorsement,
    is_header_label,
    looks_like_police_no,
    looks_like_zeyil_no,
    normalize_text,
    strip_renewal_suffix,
    value_after_label_in_row,
)

POLICE_HEADERS = {
    "police no",
    "police nosu",
    "police numarasi",
    "police numarası",
    "police no / yenileme no",
    "police / yenileme no",
    "policy no",
    "policy number",
}

ZEYIL_HEADERS = {
    "ek zeyil no",
    "zeyil no",
    "ek belge no",
    "ek/yenileme no",
    "ek / yenileme no",
    "endorsement no",
    "endorsement number",
}

_POLICE_WORD = r"poli[çc\ufffd]e"
_POLICE_LABEL = (
    rf"(?:{_POLICE_WORD}\s*(?:"
    r"(?:no(?:su|maras[iı])?|numaras[iı])(?:\s*/\s*yeni(?:leme)?\s*no)?"
    r"|/\s*yeni(?:leme)?\s*no"
    r")|policy\s*(?:no|number))"
)
_PREVIOUS_POLICE_RE = re.compile(
    r"(?:onceki|\ufffdnceki|previous|prior|\bold\b|eski|sbm|dask|open cover)\s*$",
)

_ZEYIL_LABEL = (
    r"(?:ek\s+)?zeyil\s*no|ek\s+belge\s*no|ek\s*/\s*yenileme\s*no"
    r"|endorsement\s*(?:no|number)"
)

_NEIGHBOR_LABELS = re.compile(
    r"(?:m[uü][şs]teri|musteri)\s*no"
    r"|acente\s*(?:no|kodu|levha(?:\s*no)?)"
    r"|zeyil\s*no"
    r"|ek\s*belge\s*no"
    r"|poli[çc]e\s*s[uü]resi"
    r"|ba[şs]lama\s*tarihi"
    r"|biti[şs]\s*tarihi",
    re.IGNORECASE,
)

_INLINE_RE = re.compile(
    rf"(?P<label>{_POLICE_LABEL})\s*[:\-]?\s*"
    r"(?P<value>[A-Z0-9][A-Z0-9]*(?:\s*[-/]\s*[A-Z0-9]+)*)",
    re.IGNORECASE,
)

_ZEYIL_INLINE_RE = re.compile(
    rf"(?P<label>{_ZEYIL_LABEL})\s*[:\-]?\s*"
    r"(?P<value>[A-Z0-9]+(?:\s*[-/]\s*[A-Z0-9]+)*)",
    re.IGNORECASE,
)

_REVERSED_RE = re.compile(
    r"([A-Z0-9][A-Z0-9\-/]*)\s*no(?:['’\ufffd]?lu)?",
    re.IGNORECASE,
)

_GENERIC_NO_RE = re.compile(r"\b\d{4}-\d{3,4}-\d{7,8}\b")

_TOKEN_RE = re.compile(r"[A-Z0-9][A-Z0-9/-]*", re.IGNORECASE)
_LABEL_WINDOW = 12
_LAYOUT_SEARCH_ROWS = 5
_LAYOUT_MAX_DISTANCE = 24

Table = list[list[str]]


def _has_previous_prefix(text: str, match_start: int) -> bool:
    prefix = normalize_text(text[max(0, match_start - _LABEL_WINDOW) : match_start])
    return bool(_PREVIOUS_POLICE_RE.search(prefix))


def _resolve_inline_police_value(label: str, raw_value: str) -> str | None:
    candidate = clean_identifier(raw_value)
    if "yeni" in normalize_text(label):
        candidate = candidate.split("/", 1)[0]
    return candidate if looks_like_police_no(candidate) else None


def find_inline_police_no(text: str) -> str | None:
    for match in _INLINE_RE.finditer(text):
        if _has_previous_prefix(text, match.start()):
            continue
        resolved = _resolve_inline_police_value(
            match.group("label"), match.group("value")
        )
        if resolved:
            return resolved
    neighbor = find_neighbor_label_police_no(text)
    if neighbor:
        return neighbor
    layout = find_layout_police_no(text)
    if layout:
        return layout
    reversed_match = _REVERSED_RE.search(text)
    if reversed_match:
        candidate = clean_identifier(reversed_match.group(1))
        if looks_like_police_no(candidate):
            return candidate
    generic = _GENERIC_NO_RE.search(text)
    if generic:
        return generic.group(0)
    return None


def find_neighbor_label_police_no(text: str) -> str | None:
    """Skip a neighbor field's value and take the trailing policy number.

    Some forms draw the box value before its label, so the layout line ends
    with ``<neighbor value> <policy no>``. With a single token the line holds
    only the neighbor value -> no policy number here.
    """
    for line in text.splitlines():
        for label in re.finditer(_POLICE_LABEL, line, re.IGNORECASE):
            if _has_previous_prefix(line, label.start()):
                continue
            tail = line[label.end() :]
            neighbor = _NEIGHBOR_LABELS.search(tail)
            if not neighbor:
                continue
            tokens = [
                clean_identifier(token.group(0))
                for token in _TOKEN_RE.finditer(tail[neighbor.end() :])
            ]
            if len(tokens) < 2:
                continue
            for candidate in reversed(tokens):
                if looks_like_police_no(candidate):
                    return candidate
    return None


def find_layout_police_no(text: str) -> str | None:
    """Find the policy number in rows below a header label column."""
    lines = text.splitlines()
    for line_idx, line in enumerate(lines):
        for label in re.finditer(_POLICE_LABEL, line, re.IGNORECASE):
            if _has_previous_prefix(line, label.start()):
                continue
            label_center = (label.start() + label.end()) / 2
            for value_line in lines[line_idx + 1 : line_idx + 1 + _LAYOUT_SEARCH_ROWS]:
                candidates = _aligned_candidates(value_line, label_center)
                if not candidates:
                    continue
                distance, candidate = min(candidates)
                if distance <= _LAYOUT_MAX_DISTANCE:
                    return candidate
                break
    return None


def _aligned_candidates(value_line: str, label_center: float) -> list[tuple[float, str]]:
    candidates: list[tuple[float, str]] = []
    for token in _TOKEN_RE.finditer(value_line):
        candidate = clean_identifier(token.group(0))
        if looks_like_police_no(candidate):
            token_center = (token.start() + token.end()) / 2
            candidates.append((abs(token_center - label_center), candidate))
    return candidates


def _resolve_table_police_value(header_name: str, raw_value: str) -> str | None:
    candidate = clean_identifier(raw_value)
    if not candidate or not looks_like_police_no(candidate):
        return None
    if "yeni" in header_name:
        candidate = candidate.split("/", 1)[0]
    return candidate


def find_table_police_no(tables: list[Table]) -> str | None:
    for table in tables:
        found = _search_horizontal_police_table(table) or _search_vertical_police_table(
            table
        )
        if found:
            return found
    return None


def _search_horizontal_police_table(table: Table) -> str | None:
    for header_idx, header_row in enumerate(table):
        header = [normalize_text(clean_cell(cell)) for cell in header_row]
        for col, name in enumerate(header):
            if not is_header_label(name, POLICE_HEADERS):
                continue
            adjacent = value_after_label_in_row(
                header_row, col, validator=looks_like_police_no
            )
            if adjacent:
                resolved = _resolve_table_police_value(name, adjacent)
                if resolved:
                    return resolved
            for row in table[header_idx + 1 :]:
                if col < len(row):
                    resolved = _resolve_table_police_value(name, row[col])
                    if resolved:
                        return resolved
    return None


def _search_vertical_police_table(table: Table) -> str | None:
    for row in table:
        if len(row) >= 2 and is_header_label(row[0], POLICE_HEADERS):
            candidate = clean_cell(row[1])
            if candidate and looks_like_police_no(candidate):
                return candidate
    return None


def find_inline_zeyil_no(text: str) -> str | None:
    for match in _ZEYIL_INLINE_RE.finditer(text):
        candidate = clean_identifier(match.group("value"))
        if not looks_like_zeyil_no(candidate):
            continue
        if is_base_endorsement(candidate):
            return None
        return strip_renewal_suffix(candidate)
    return None


def find_table_zeyil_no(tables: list[Table]) -> str | None:
    for table in tables:
        for header_idx, header_row in enumerate(table):
            header = [normalize_text(clean_cell(cell)) for cell in header_row]
            for col, name in enumerate(header):
                if not is_header_label(name, ZEYIL_HEADERS):
                    continue
                adjacent = value_after_label_in_row(
                    header_row, col, validator=looks_like_zeyil_no
                )
                if adjacent:
                    if is_base_endorsement(adjacent):
                        return None
                    return strip_renewal_suffix(adjacent)
                for row in table[header_idx + 1 :]:
                    if col < len(row):
                        candidate = clean_identifier(row[col])
                        if looks_like_zeyil_no(candidate):
                            if is_base_endorsement(candidate):
                                return None
                            return strip_renewal_suffix(candidate)
    return None
