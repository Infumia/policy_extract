"""Hızlı (pypdf tabanlı) minimal policy extractor.

Poliçe no iki yoldan bulunur:
1. Düz metin: "Poliçe No: 0000" (ters yazım ve genel kalıp dahil).
2. Tablo: "Poliçe No" hücresinin yanındaki veya kolonun altındaki değer.

Şirket skorlamalı sözlük eşleşmesiyle bulunur
(tek kelime araması değil; domain/unvan/başlık bölgesi ağırlıklı).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _normalize(value: str) -> str:
    # Türkçe İ/ı: Python lower() "İ"yi "i\u0307" yapar, o yüzden önce maple.
    text = value.replace("İ", "i").replace("I", "ı")
    # Onceki iki nokta, sonra bosluk: "No :" -> "no".
    text = text.lower().replace(":", "").strip()
    text = text.replace("\u0307", "")  # birlesen nokta kalintisi
    text = text.replace("ç", "c").replace("ğ", "g").replace("ö", "o")
    text = text.replace("ş", "s").replace("ü", "u").replace("ı", "i")
    return re.sub(r"\s+", " ", text)


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

# Bazı PDF fontlarında Türkçe glif eşlemesi bozuk ve ç yerine U+FFFD gelir.
# pypdf yine de tablonun geometrisini doğru koruduğu için etikette bunu tolere
# ediyoruz. Değer tarafında ise yalnızca kimlik numarası karakterlerine izin var.
_POLICE_WORD = r"poli[çc\ufffd]e"
_POLICE_LABEL = (
    rf"(?:{_POLICE_WORD}\s*(?:"
    r"(?:no(?:su|maras[iı])?|numaras[iı])(?:\s*/\s*yeni(?:leme)?\s*no)?"
    r"|/\s*yeni(?:leme)?\s*no"
    r")|policy\s*(?:no|number))"
)
# Ana poliçe numarası değildir: "Önceki Poliçe No", "Eski/Previous/Prior/Old Policy No",
# "SBM/DASK Poliçe No", "Open Cover Policy No". Önek metni normalize edilmiş
# olduktan sonra eşleştirilir.
_PREVIOUS_POLICE_RE = re.compile(
    r"(?:onceki|\ufffdnceki|previous|prior|\bold\b|eski|sbm|dask|open cover)\s*$",
)

_ZEYIL_LABEL = (
    r"(?:ek\s+)?zeyil\s*no|ek\s+belge\s*no|ek\s*/\s*yenileme\s*no"
    r"|endorsement\s*(?:no|number)"
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

# "0001-0110-06857993 no'lu / nolu" gibi ters yazımlar için.
_REVERSED_RE = re.compile(
    r"([A-Z0-9][A-Z0-9\-\/]*)\s*no(?:['’\ufffd]?lu)?",
    re.IGNORECASE,
)

# Çıpa görevi gören genel poliçe-no kalıbı (örn. 0001-0110-06857993).
_GENERIC_NO_RE = re.compile(r"\b\d{4}-\d{3,4}-\d{7,8}\b")

_VALUE_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-\/]*$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(?:[0-3]?\d)[./-](?:[01]?\d)[./-](?:19|20)\d{2}$")
_MIN_POLICE_DIGITS = 5
_MIN_POLICE_LENGTH = 6


@dataclass
class PolicyExtraction:
    source_file: str
    police_no: str | None = None
    police_no_source: str | None = None  # "inline" | "table"
    zeyil_no: str | None = None
    zeyil_no_source: str | None = None  # "inline" | "table"
    company: str | None = None  # örn. "allianz", yoksa None
    company_confidence: str = "unknown"  # high | medium | low | unknown
    company_scores: dict[str, int] = field(default_factory=dict)
    full_text: str = ""
    tables: list[list[list[str]]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Şirket tespiti (skorlamalı)
# ---------------------------------------------------------------------------

# Tek başına genel kelime olan markalar: marka eşleşmesi düşük puan verir,
# karar için domain/unvan şart olur ("güneşli bir gün" vs "Güneş Sigorta").
_GENERIC_BRANDS = frozenset({"gunes", "turkiye", "anadolu", "ankara", "quick", "ray"})

_DOMAIN_HIT = 10
_DOMAIN_EXTRA_CAP = 3
_TITLE_HIT = 5
_TITLE_EXTRA_CAP = 2
_BRAND_HIT = 2
_GENERIC_BRAND_HIT = 1
_BRAND_EXTRA_CAP = 2
_HEADER_BONUS_DOMAIN = 4
_HEADER_BONUS_TITLE = 3
_HEADER_BONUS_BRAND = 2
_HEADER_WINDOW = 1500  # normalize metnin ilk N karakteri ≈ antet/1. sayfa
_COMPANY_THRESHOLD = 8
_TIE_MARGIN = 3


@dataclass(frozen=True)
class CompanySpec:
    key: str
    label: str
    domains: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()  # normalize edilmiş unvan kalıpları
    brands: tuple[str, ...] = ()  # normalize edilmiş marka tokenları


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
    # "Anadolu Anonim Türk Sigorta Şirketi" tam hukuki unvanı: bazı
    # poliçelerde (örn. siber) ön yüzde okunabilir şirket adı yoktur, tek
    # sinyal genel şartlardaki bu unvandır. Eşiği tek başına geçebilsin
    # diye tam hali ayrı başlık olarak yer alır.
    CompanySpec("anadolu", "Anadolu Sigorta", ("anadolusigorta.com.tr",), ("anadolu sigorta", "anadolu anonim turk sigorta", "anadolu anonim turk sigorta sirketi"), ("anadolusigorta", "anadolu")),
    CompanySpec("ak", "Ak Sigorta", ("aksigorta.com.tr",), ("ak sigorta",), ("aksigorta",)),
    CompanySpec("quick", "Quick Sigorta", ("quicksigorta.com.tr",), ("quick sigorta",), ("quicksigorta", "quick")),
    # Tek başına "güneş" günlük metinlerde sık geçtiği için şirket sinyali
    # değildir. Unvan, birleşik marka veya domain gerekir.
    CompanySpec("gunes", "Güneş Sigorta", ("gunessigorta.com.tr",), ("gunes sigorta",), ("gunessigorta",)),
    # Tek başına "Türkiye" ülke/adres metinlerinde çok sık geçtiği için şirket
    # sinyali değildir. Unvan, birleşik marka veya domain gerekir.
    CompanySpec("turkiye", "Türkiye Sigorta", ("turkiyesigorta.com.tr",), ("turkiye sigorta",), ("turkiyesigorta",)),
    CompanySpec("sompo", "Sompo Sigorta", ("somposigorta.com.tr",), ("sompo sigorta",), ("sompo",)),
    CompanySpec("neova", "Neova Sigorta", ("neova.com.tr", "neovasigorta.com.tr"), ("neova sigorta",), ("neova",)),
    CompanySpec("ray", "Ray Sigorta", ("raysigorta.com.tr",), ("ray sigorta",), ("raysigorta", "ray")),
    CompanySpec("hdi", "HDI Sigorta", ("hdisigorta.com.tr",), ("hdi sigorta",), ("hdi",)),
    CompanySpec("groupama", "Groupama Sigorta", ("groupama.com.tr",), ("groupama sigorta",), ("groupama",)),
    CompanySpec("zurich", "Zurich Sigorta", ("zurich.com.tr",), ("zurich sigorta",), ("zurich",)),
    CompanySpec(
        "magdeburger",
        "Magdeburger Sigorta",
        ("magdeburger.com.tr",),
        ("magdeburger sigorta",),
        ("magdeburger",),
    ),
)


def _count_phrase(text: str, phrase: str) -> int:
    # pypdf layout çıkarımı bazen kelime arası boşluğu yutar
    # ("HDISİGORTA A.Ş."). Çok kelimeli başlıklarda boşlukları opsiyonel
    # say ki "hdi sigorta" hem "hdi sigorta"yı hem "hdisigorta"yı yakalasın.
    # Tek kelimelik kalıplarda davranış değişmez.
    pattern = r"\b" + r"\s*".join(re.escape(tok) for tok in phrase.split()) + r"\b"
    return len(re.findall(pattern, text))


# "Önceki Şirket Adı: ALLIANZ ..." mevcut sigortacı değildir; skorlanmamalı.
# (Poliçe no'daki "Önceki Poliçe No" kuralının şirket karşılığı.)
# İlk harf bozuk glif de olabilir (Önceki -> �nceki), o yüzden "nceki"/"irket"
# gövdesine göre eşleşir.
_PREVIOUS_COMPANY_RE = re.compile(
    r"(?:\S*nceki|previous|prior|\bold\b|eski)\s+"
    r"(?:sigorta\s+)?(?:\S*irket|company|insurer|firma)[^\n]{0,150}",
    re.IGNORECASE,
)


def _score_company(spec: CompanySpec, raw_lower: str, norm: str, header: str) -> tuple[int, dict[str, int]]:
    score = 0
    parts: dict[str, int] = {}
    for domain in spec.domains:
        hits = raw_lower.count(domain)
        if hits:
            gained = _DOMAIN_HIT + min(hits - 1, _DOMAIN_EXTRA_CAP)
            score += gained
            parts[f"domain:{domain}"] = gained
    # Domain başlık bonusu (normalize metin noktaları korur).
    for domain in spec.domains:
        if domain in header:
            score += _HEADER_BONUS_DOMAIN
            parts["header:domain"] = parts.get("header:domain", 0) + _HEADER_BONUS_DOMAIN
    for title in spec.titles:
        hits = _count_phrase(norm, title)
        if hits:
            gained = _TITLE_HIT + min(hits - 1, _TITLE_EXTRA_CAP)
            score += gained
            parts[f"title:{title}"] = gained
            if _count_phrase(header, title):
                score += _HEADER_BONUS_TITLE
                parts["header:title"] = parts.get("header:title", 0) + _HEADER_BONUS_TITLE
    for brand in spec.brands:
        hits = _count_phrase(norm, brand)
        if hits:
            unit = _GENERIC_BRAND_HIT if brand in _GENERIC_BRANDS else _BRAND_HIT
            gained = unit + min(hits - 1, _BRAND_EXTRA_CAP)
            score += gained
            parts[f"brand:{brand}"] = gained
            if _count_phrase(header, brand):
                score += _HEADER_BONUS_BRAND
                parts["header:brand"] = parts.get("header:brand", 0) + _HEADER_BONUS_BRAND
    return score, parts


def detect_company(full_text: str) -> tuple[str | None, str, dict[str, int]]:
    """Skorlamalı şirket tespiti -> (key | None, confidence, {key: skor})."""
    cleaned = _PREVIOUS_COMPANY_RE.sub(" ", full_text)
    raw_lower = cleaned.lower()
    norm = _normalize(cleaned)
    header = norm[:_HEADER_WINDOW]
    scores: dict[str, int] = {}
    for spec in COMPANY_SPECS:
        score, _ = _score_company(spec, raw_lower, norm, header)
        if score:
            scores[spec.key] = score
    if not scores:
        return None, "unknown", {}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if best_score < _COMPANY_THRESHOLD:
        return None, "unknown", scores
    if runner_up >= _COMPANY_THRESHOLD and best_score - runner_up <= _TIE_MARGIN:
        return None, "unknown", scores  # başabaş: karar verme
    confidence = "high" if best_score >= 15 and best_score - runner_up >= 5 else "medium"
    return best, confidence, scores


def find_inline_police_no(text: str) -> str | None:
    for match in _INLINE_RE.finditer(text):
        # "Önceki Poliçe No" ve "SBM Poliçe No" asıl poliçe numarası değildir.
        prefix = _normalize(text[max(0, match.start() - 12) : match.start()])
        if _PREVIOUS_POLICE_RE.search(prefix):
            continue
        candidate = _clean_identifier(match.group("value"))
        # Birleşik "Poliçe No / Yenileme No" alanında slash'ın sağ tarafı
        # yenileme numarasıdır; tek başına Poliçe No alanındaki slash ise değere
        # aittir (örn. Türkiye Sigorta: 581858217 / 2).
        if "yeni" in _normalize(match.group("label")):
            candidate = candidate.split("/", 1)[0]
        if _looks_like_police_no(candidate):
            return candidate
    layout_candidate = _find_layout_police_no(text)
    if layout_candidate:
        return layout_candidate
    # Ters yazım: "<NO> no'lu" (örn. özet sayfadaki "0001-... no'lu Allianz").
    rev = _REVERSED_RE.search(text)
    if rev:
        candidate = _clean_identifier(rev.group(1))
        if _looks_like_police_no(candidate):
            return candidate
    # Son çare: metinde geçen klasik 4-4-8 poliçe-no kalıbı.
    generic = _GENERIC_NO_RE.search(text)
    if generic:
        return generic.group(0)
    return None


def _clean_cell(value: object) -> str:
    return str(value).strip().strip(",;:").strip()


def _clean_identifier(value: object) -> str:
    cleaned = _clean_cell(value).replace("–", "-").replace("—", "-")
    return re.sub(r"\s*([/-])\s*", r"\1", cleaned)


def _looks_like_police_no(value: str) -> bool:
    candidate = _clean_identifier(value)
    digits = sum(ch.isdigit() for ch in candidate)
    return (
        bool(_VALUE_RE.fullmatch(candidate))
        and len(candidate) >= _MIN_POLICE_LENGTH
        and digits >= _MIN_POLICE_DIGITS
        and not _DATE_RE.fullmatch(candidate)
    )


def _looks_like_zeyil_no(value: str) -> bool:
    candidate = _clean_identifier(value)
    return bool(_VALUE_RE.fullmatch(candidate)) and any(ch.isdigit() for ch in candidate)


def _find_layout_police_no(text: str) -> str | None:
    """Layout metninde başlık sütununun altındaki poliçe numarasını bulur."""
    lines = text.splitlines()
    token_re = re.compile(r"[A-Z0-9][A-Z0-9/-]*", re.IGNORECASE)
    for line_idx, line in enumerate(lines):
        for label in re.finditer(_POLICE_LABEL, line, re.IGNORECASE):
            prefix = _normalize(line[max(0, label.start() - 12) : label.start()])
            if _PREVIOUS_POLICE_RE.search(prefix):
                continue
            label_center = (label.start() + label.end()) / 2
            for value_line in lines[line_idx + 1 : line_idx + 5]:
                candidates: list[tuple[float, str]] = []
                for token in token_re.finditer(value_line):
                    candidate = _clean_identifier(token.group(0))
                    if _looks_like_police_no(candidate):
                        token_center = (token.start() + token.end()) / 2
                        candidates.append((abs(token_center - label_center), candidate))
                if candidates:
                    distance, candidate = min(candidates)
                    # Yakınlık sınırı, ilgisiz bir sonraki paragraftaki uzun
                    # müşteri/vergi numarasının seçilmesini engeller.
                    if distance <= 24:
                        return candidate
                    break
    return None


def _is_header(value: str, headers: set[str]) -> bool:
    # OCR bazen Türkçe karakterleri ayrı tokenlara böler:
    # "Poliçe No" -> "Poli ç e No". Başlık eşleşmesinde boşlukları yok
    # saymak bu varyantı kabul eder; değer doğrulaması ayrıca yapılır.
    compact = re.sub(r"\s+", "", _normalize(_clean_cell(value)))
    return compact in {
        re.sub(r"\s+", "", _normalize(item)) for item in headers
    }


def _value_after_label_in_row(
    row: list[str], col: int, *, validator
) -> str | None:
    if col + 1 >= len(row):
        return None
    candidate = _clean_identifier(row[col + 1])
    if _is_header(candidate, POLICE_HEADERS | ZEYIL_HEADERS):
        return None
    return candidate if validator(candidate) else None


def find_table_police_no(tables: list[list[list[str]]]) -> str | None:
    for table in tables:
        if not table:
            continue
        # 1) Yatay tablolar: başlık satırı her zaman 0. satırda olmayabilir.
        for header_idx, header_row in enumerate(table):
            header = [_normalize(_clean_cell(cell)) for cell in header_row]
            for col, name in enumerate(header):
                if _is_header(name, POLICE_HEADERS):
                    # Key-value tablolarında başlık ve değer aynı satırdadır:
                    # | Poliçe No | 581858217 / 2 | Ek Zeyil No | 0 |
                    adjacent = _value_after_label_in_row(
                        header_row, col, validator=_looks_like_police_no
                    )
                    if adjacent:
                        if "yeni" in name:
                            adjacent = adjacent.split("/", 1)[0]
                        return adjacent
                    for row in table[header_idx + 1 :]:
                        if col < len(row):
                            candidate = _clean_identifier(row[col])
                            if candidate and _looks_like_police_no(candidate):
                                if "yeni" in name:
                                    candidate = candidate.split("/", 1)[0]
                                return candidate
        # 2) Dikey key-value tablolar: "Poliçe No : | <değer>".
        for row in table:
            if len(row) >= 2 and _is_header(row[0], POLICE_HEADERS):
                candidate = _clean_cell(row[1])
                if candidate and _looks_like_police_no(candidate):
                    return candidate
    return None


def find_inline_zeyil_no(text: str) -> str | None:
    for match in _ZEYIL_INLINE_RE.finditer(text):
        candidate = _clean_identifier(match.group("value"))
        if _looks_like_zeyil_no(candidate):
            # 0, 00 ve 0/0 temel poliçeyi ifade eder; zeyil yoktur.
            if set(candidate.replace("/", "").replace("-", "")) == {"0"}:
                return None
            return candidate.split("/", 1)[0]
    return None


def find_table_zeyil_no(tables: list[list[list[str]]]) -> str | None:
    for table in tables:
        for header_idx, header_row in enumerate(table):
            header = [_normalize(_clean_cell(cell)) for cell in header_row]
            for col, name in enumerate(header):
                if not _is_header(name, ZEYIL_HEADERS):
                    continue
                candidates: list[str] = []
                adjacent = _value_after_label_in_row(
                    header_row, col, validator=_looks_like_zeyil_no
                )
                if adjacent:
                    if set(adjacent.replace("/", "").replace("-", "")) == {"0"}:
                        return None
                    return adjacent.split("/", 1)[0]
                for row in table[header_idx + 1 :]:
                    if col < len(row):
                        candidate = _clean_identifier(row[col])
                        if _looks_like_zeyil_no(candidate):
                            candidates.append(candidate)
                            break
                for candidate in candidates:
                    if set(candidate.replace("/", "").replace("-", "")) != {"0"}:
                        return candidate.split("/", 1)[0]
                return None
    return None


def extract_policy(
    pdf_path: str,
    *,
    full_text: str | None = None,
    tables: list[list[list[str]]] | None = None,
) -> PolicyExtraction:
    """Saf birleştirme mantığı (metin + tablo satırları)."""
    text = full_text or ""
    rows = tables or []

    police_no = find_inline_police_no(text)
    source = "inline" if police_no else None
    if police_no is None:
        police_no = find_table_police_no(rows)
        source = "table" if police_no else None

    zeyil_no = find_inline_zeyil_no(text)
    zeyil_source = "inline" if zeyil_no else None
    if zeyil_no is None:
        zeyil_no = find_table_zeyil_no(rows)
        zeyil_source = "table" if zeyil_no else None

    company, company_confidence, company_scores = detect_company(text)
    return PolicyExtraction(
        source_file=pdf_path,
        police_no=police_no,
        police_no_source=source,
        zeyil_no=zeyil_no,
        zeyil_no_source=zeyil_source,
        company=company,
        company_confidence=company_confidence,
        company_scores=company_scores,
        full_text=text,
        tables=rows,
    )


# ---------------------------------------------------------------------------
# Hızlı yol: pypdf metni (saniyeler sürer). Tek motor budur.
# ---------------------------------------------------------------------------


def file_sha256(pdf_path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Dosyanın sha256 hex digest'i. Batch önbellek anahtarı olarak kullanılır."""
    digest = hashlib.sha256()
    with open(pdf_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_text_fast(pdf_path: str, *, max_pages: int = 7) -> str:
    """pypdf ile ilk N sayfanın düzen-korumalı metni. Hata durumunda '' döner."""
    import logging

    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font uyarı dökümlerini sustur
    try:
        reader = PdfReader(pdf_path)
    except Exception:
        return ""
    chunks: list[str] = []
    pages = reader.pages if max_pages <= 0 else reader.pages[:max_pages]
    for page in pages:
        try:
            # Normal mod çok kolonlu tablolarda başlık ve değerleri karıştırır.
            # Layout modu görsel okuma sırasını korur ve Poliçe No alanını aynı
            # satırda doğru değerle eşleştirmemizi sağlar.
            text = page.extract_text(extraction_mode="layout") or ""
            if not text.strip():
                text = page.extract_text() or ""
            chunks.append(text)
        except Exception:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
    return "\n".join(chunks)


def extract_policy_fast(pdf_path: str, *, max_pages: int = 7) -> PolicyExtraction:
    """Hızlı çıkarım (tablo yok, sadece pypdf metni)."""
    return extract_policy(
        pdf_path, full_text=extract_text_fast(pdf_path, max_pages=max_pages), tables=[]
    )
