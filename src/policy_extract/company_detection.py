"""Scored insurer detection (dictionary match, never single-word search)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from policy_extract.text_utils import count_phrase_occurrences, normalize_text

# Brands that are common Turkish words: a brand hit alone is not a signal,
# a domain or legal title is required.
GENERIC_BRANDS = frozenset({"gunes", "turkiye", "anadolu", "ankara", "quick", "ray"})

DOMAIN_HIT = 10
DOMAIN_EXTRA_CAP = 3
TITLE_HIT = 5
TITLE_EXTRA_CAP = 2
BRAND_HIT = 2
GENERIC_BRAND_HIT = 1
BRAND_EXTRA_CAP = 2
HEADER_BONUS_DOMAIN = 4
HEADER_BONUS_TITLE = 3
HEADER_BONUS_BRAND = 2
HEADER_WINDOW = 1500
COMPANY_THRESHOLD = 8
TIE_MARGIN = 3
HIGH_CONFIDENCE_MIN = 15
HIGH_CONFIDENCE_MARGIN = 5


@dataclass(frozen=True)
class CompanySpec:
    key: str
    label: str
    domains: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()


COMPANY_SPECS: tuple[CompanySpec, ...] = (
    CompanySpec(
        "allianz",
        "Allianz",
        ("allianz.com.tr",),
        ("allianz sigorta", "allianz ailesine", "allianz yuvam"),
        ("allianz",),
    ),
    CompanySpec("mapfre", "Mapfre", ("mapfre.com.tr",), ("mapfre sigorta",), ("mapfre",)),
    CompanySpec("axa", "Axa", ("axa.com.tr",), ("axa sigorta",), ("axa",)),
    CompanySpec(
        "anadolu",
        "Anadolu Sigorta",
        ("anadolusigorta.com.tr",),
        (
            "anadolu sigorta",
            "anadolu anonim turk sigorta",
            "anadolu anonim turk sigorta sirketi",
        ),
        ("anadolusigorta", "anadolu"),
    ),
    CompanySpec("ak", "Ak Sigorta", ("aksigorta.com.tr",), ("ak sigorta",), ("aksigorta",)),
    CompanySpec(
        "quick",
        "Quick Sigorta",
        ("quicksigorta.com.tr",),
        ("quick sigorta",),
        ("quicksigorta", "quick"),
    ),
    CompanySpec(
        "gunes",
        "Güneş Sigorta",
        ("gunessigorta.com.tr",),
        ("gunes sigorta",),
        ("gunessigorta",),
    ),
    CompanySpec(
        "turkiye",
        "Türkiye Sigorta",
        ("turkiyesigorta.com.tr",),
        ("turkiye sigorta",),
        ("turkiyesigorta",),
    ),
    CompanySpec(
        "sompo", "Sompo Sigorta", ("somposigorta.com.tr",), ("sompo sigorta",), ("sompo",)
    ),
    CompanySpec(
        "neova",
        "Neova Sigorta",
        ("neova.com.tr", "neovasigorta.com.tr"),
        ("neova sigorta",),
        ("neova",),
    ),
    CompanySpec(
        "ray", "Ray Sigorta", ("raysigorta.com.tr",), ("ray sigorta",), ("raysigorta", "ray")
    ),
    CompanySpec("hdi", "HDI Sigorta", ("hdisigorta.com.tr",), ("hdi sigorta",), ("hdi",)),
    CompanySpec(
        "groupama",
        "Groupama Sigorta",
        ("groupama.com.tr",),
        ("groupama sigorta",),
        ("groupama",),
    ),
    CompanySpec(
        "zurich",
        "Zurich Sigorta",
        ("zurich.com.tr",),
        ("zurich sigorta",),
        ("zurich",),
    ),
    CompanySpec(
        "magdeburger",
        "Magdeburger Sigorta",
        ("magdeburger.com.tr",),
        ("magdeburger sigorta",),
        ("magdeburger",),
    ),
)

# "Previous Company Name: ALLIANZ ..." is not the current insurer.
_PREVIOUS_COMPANY_RE = re.compile(
    r"(?:\S*nceki|previous|prior|\bold\b|eski)\s+"
    r"(?:sigorta\s+)?(?:\S*irket|company|insurer|firma)[^\n]{0,150}",
    re.IGNORECASE,
)


def _score_domains(spec: CompanySpec, raw_lower: str, header: str) -> tuple[int, dict[str, int]]:
    score = 0
    parts: dict[str, int] = {}
    for domain in spec.domains:
        hits = raw_lower.count(domain)
        if hits:
            gained = DOMAIN_HIT + min(hits - 1, DOMAIN_EXTRA_CAP)
            score += gained
            parts[f"domain:{domain}"] = gained
    for domain in spec.domains:
        if domain in header:
            score += HEADER_BONUS_DOMAIN
            parts["header:domain"] = parts.get("header:domain", 0) + HEADER_BONUS_DOMAIN
    return score, parts


def _score_titles(spec: CompanySpec, norm: str, header: str) -> tuple[int, dict[str, int]]:
    score = 0
    parts: dict[str, int] = {}
    for title in spec.titles:
        hits = count_phrase_occurrences(norm, title)
        if not hits:
            continue
        gained = TITLE_HIT + min(hits - 1, TITLE_EXTRA_CAP)
        score += gained
        parts[f"title:{title}"] = gained
        if count_phrase_occurrences(header, title):
            score += HEADER_BONUS_TITLE
            parts["header:title"] = parts.get("header:title", 0) + HEADER_BONUS_TITLE
    return score, parts


def _score_brands(spec: CompanySpec, norm: str, header: str) -> tuple[int, dict[str, int]]:
    score = 0
    parts: dict[str, int] = {}
    for brand in spec.brands:
        hits = count_phrase_occurrences(norm, brand)
        if not hits:
            continue
        unit = GENERIC_BRAND_HIT if brand in GENERIC_BRANDS else BRAND_HIT
        gained = unit + min(hits - 1, BRAND_EXTRA_CAP)
        score += gained
        parts[f"brand:{brand}"] = gained
        if count_phrase_occurrences(header, brand):
            score += HEADER_BONUS_BRAND
            parts["header:brand"] = parts.get("header:brand", 0) + HEADER_BONUS_BRAND
    return score, parts


def score_company(
    spec: CompanySpec, raw_lower: str, norm: str, header: str
) -> tuple[int, dict[str, int]]:
    """Score one insurer spec; returns (total, explainable parts)."""
    total = 0
    parts: dict[str, int] = {}
    for scorer in (_score_domains, _score_titles, _score_brands):
        if scorer is _score_domains:
            gained, detail = scorer(spec, raw_lower, header)
        else:
            gained, detail = scorer(spec, norm, header)
        total += gained
        parts.update(detail)
    return total, parts


def detect_company(full_text: str) -> tuple[str | None, str, dict[str, int]]:
    """Scored insurer detection -> (key | None, confidence, {key: score})."""
    cleaned = _PREVIOUS_COMPANY_RE.sub(" ", full_text)
    raw_lower = cleaned.lower()
    norm = normalize_text(cleaned)
    header = norm[:HEADER_WINDOW]
    scores: dict[str, int] = {}
    for spec in COMPANY_SPECS:
        score, _ = score_company(spec, raw_lower, norm, header)
        if score:
            scores[spec.key] = score
    if not scores:
        return None, "unknown", {}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if best_score < COMPANY_THRESHOLD:
        return None, "unknown", scores
    if runner_up >= COMPANY_THRESHOLD and best_score - runner_up <= TIE_MARGIN:
        return None, "unknown", scores
    confidence = (
        "high"
        if best_score >= HIGH_CONFIDENCE_MIN
        and best_score - runner_up >= HIGH_CONFIDENCE_MARGIN
        else "medium"
    )
    return best, confidence, scores
