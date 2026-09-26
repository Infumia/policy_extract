"""Fast (pypdf-based) policy extractor facade.

Clean-code layout: implementation lives in focused modules
(models, text_utils, company_detection, policy_numbers, pdf_io).
This module re-exports the public + legacy private names so existing
imports (including tests) keep working.
"""

from __future__ import annotations

from policy_extract.company_detection import (
    COMPANY_SPECS,
    COMPANY_THRESHOLD,
    GENERIC_BRANDS,
    HEADER_WINDOW,
    CompanySpec,
    detect_company,
)
from policy_extract.company_detection import (
    score_company as _score_company,
)
from policy_extract.models import PolicyExtraction
from policy_extract.pdf_io import (
    extract_policy,
    extract_policy_fast,
    extract_text_fast,
    hash_file as file_sha256,
)
from policy_extract.policy_numbers import (
    POLICE_HEADERS,
    ZEYIL_HEADERS,
    find_inline_police_no,
    find_inline_zeyil_no,
    find_layout_police_no,
    find_neighbor_label_police_no,
    find_table_police_no,
    find_table_zeyil_no,
)
from policy_extract.text_utils import (
    clean_cell as _clean_cell,
)
from policy_extract.text_utils import (
    clean_identifier as _clean_identifier,
)
from policy_extract.text_utils import (
    count_phrase_occurrences as _count_phrase,
)
from policy_extract.text_utils import (
    is_header_label as _is_header,
)
from policy_extract.text_utils import (
    looks_like_police_no as _looks_like_police_no,
)
from policy_extract.text_utils import (
    looks_like_zeyil_no as _looks_like_zeyil_no,
)
from policy_extract.text_utils import (
    normalize_text as _normalize,
)
from policy_extract.text_utils import (
    value_after_label_in_row as _value_after_label_in_row,
)
from policy_extract.text_utils import (
    MIN_POLICE_DIGITS as _MIN_POLICE_DIGITS,
)
from policy_extract.text_utils import (
    MIN_POLICE_LENGTH as _MIN_POLICE_LENGTH,
)

# Legacy private aliases kept for backward compatibility.
_COMPANY_THRESHOLD = COMPANY_THRESHOLD
_GENERIC_BRANDS = GENERIC_BRANDS
_HEADER_WINDOW = HEADER_WINDOW
_find_layout_police_no = find_layout_police_no
_find_neighbor_label_police_no = find_neighbor_label_police_no

__all__ = [
    "POLICE_HEADERS",
    "ZEYIL_HEADERS",
    "COMPANY_SPECS",
    "CompanySpec",
    "PolicyExtraction",
    "detect_company",
    "extract_policy",
    "extract_policy_fast",
    "extract_text_fast",
    "file_sha256",
    "find_inline_police_no",
    "find_inline_zeyil_no",
    "find_table_police_no",
    "find_table_zeyil_no",
]
