"""Shared domain model for extraction results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class PolicyExtraction:
    """Structured result of extracting a single PDF."""

    source_file: str
    police_no: str | None = None
    police_no_source: str | None = None  # "inline" | "table"
    zeyil_no: str | None = None
    zeyil_no_source: str | None = None  # "inline" | "table"
    company: str | None = None
    company_confidence: str = "unknown"  # high | medium | low | unknown
    company_scores: dict[str, int] = field(default_factory=dict)
    full_text: str = ""
    tables: list[list[list[str]]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
