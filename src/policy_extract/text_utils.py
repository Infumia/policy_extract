"""Turkish-aware text normalization and identifier validation.

Single place for all string cleaning rules so extractor, company
detection and table search share the same definitions (DRY).
"""

from __future__ import annotations

import re
from collections.abc import Callable

# -- normalization -----------------------------------------------------------

_TURKISH_FOLD = (
    ("İ", "i"),
    ("I", "ı"),
    ("ç", "c"),
    ("ğ", "g"),
    ("ö", "o"),
    ("ş", "s"),
    ("ü", "u"),
    ("ı", "i"),
)

_WHITESPACE_RE = re.compile(r"\s+")
_IDENTIFIER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-/]*$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(?:[0-3]?\d)[./-](?:[01]?\d)[./-](?:19|20)\d{2}$")

MIN_POLICE_DIGITS = 5
MIN_POLICE_LENGTH = 6


def normalize_text(value: str) -> str:
    """Lowercase, fold Turkish chars, drop colons, collapse whitespace."""
    text = value.replace("İ", "i").replace("I", "ı")
    text = text.lower().replace(":", "").strip()
    text = text.replace("̇", "")  # combining dot left by İ.lower()
    for src, dst in _TURKISH_FOLD[2:]:
        text = text.replace(src, dst)
    return _WHITESPACE_RE.sub(" ", text)


def compact_normalized(value: str) -> str:
    """Normalized text without any whitespace (tolerates split glyphs)."""
    return re.sub(r"\s+", "", normalize_text(clean_cell(value)))


def clean_cell(value: object) -> str:
    return str(value).strip().strip(",;:").strip()


def clean_identifier(value: object) -> str:
    cleaned = clean_cell(value).replace("–", "-").replace("—", "-")
    return re.sub(r"\s*([/-])\s*", r"\1", cleaned)


def looks_like_police_no(value: str) -> bool:
    candidate = clean_identifier(value)
    digits = sum(ch.isdigit() for ch in candidate)
    return (
        bool(_IDENTIFIER_RE.fullmatch(candidate))
        and len(candidate) >= MIN_POLICE_LENGTH
        and digits >= MIN_POLICE_DIGITS
        and not _DATE_RE.fullmatch(candidate)
    )


def looks_like_zeyil_no(value: str) -> bool:
    candidate = clean_identifier(value)
    return bool(_IDENTIFIER_RE.fullmatch(candidate)) and any(
        ch.isdigit() for ch in candidate
    )


def is_base_endorsement(value: str) -> bool:
    """'0', '00', '0/0' mean base policy (no endorsement)."""
    stripped = clean_identifier(value).replace("/", "").replace("-", "")
    return bool(stripped) and set(stripped) == {"0"}


def strip_renewal_suffix(value: str) -> str:
    """Keep the policy part of 'POLICY / RENEWAL' composite fields."""
    return clean_identifier(value).split("/", 1)[0]


def is_header_label(value: str, headers: set[str]) -> bool:
    compact = compact_normalized(value)
    normalized_headers = {compact_normalized(item) for item in headers}
    return compact in normalized_headers


def value_after_label_in_row(
    row: list[str], col: int, *, validator: Callable[[str], bool]
) -> str | None:
    if col + 1 >= len(row):
        return None
    candidate = clean_identifier(row[col + 1])
    return candidate if validator(candidate) else None


def count_phrase_occurrences(text: str, phrase: str) -> int:
    """Count phrase with optional whitespace (layout may swallow spaces)."""
    pattern = r"\b" + r"\s*".join(re.escape(tok) for tok in phrase.split()) + r"\b"
    return len(re.findall(pattern, text))
