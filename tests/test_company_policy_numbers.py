"""Insurer-specific policy numbers across text, tables, PDFs and cache refresh."""

import pytest

from policy_extract import cli, service
from policy_extract.pdf_io import extract_policy, extract_policy_fast, hash_file
from policy_extract.records import record_from_extraction
from synthetic_data import write_pdf


@pytest.mark.parametrize("company,raw,expected", [
    ("allianz", "0001-0310-05746303", "05746303"),
    ("allianz", "0001 - 0310 - 00000001", "00000001"),
    ("allianz", "05746303", "05746303"),
    ("hdi", "175101008353 - T3", "175101008353"),
    ("hdi", "175101008353-T3", "175101008353"),
    ("hdi", "175101008353 - t12", "175101008353"),
    ("hdi", "175101008353", "175101008353"),
    ("axa", "0001-0310-05746303", "0001-0310-05746303"),
    ("axa", "175101008353-T3", "175101008353-T3"),
    (None, "0001-0310-05746303", "0001-0310-05746303"),
    (None, "175101008353-T3", "175101008353-T3"),
])
@pytest.mark.parametrize("source", ["inline", "table"])
def test_company_number_normalization(company, raw, expected, source):
    text = f"{company} Sigorta" if company else ""
    tables = []
    if source == "inline":
        text += f"\nPoliçe No: {raw}\nZeyil No: 2"
    else:
        tables = [[["Poliçe No", "Zeyil No"], [raw, "2"]]]
    result = extract_policy("policy.pdf", full_text=text, tables=tables)
    assert result.company == company
    assert result.police_no == expected
    assert result.police_no_source == source
    assert result.zeyil_no == "2"
    assert record_from_extraction(result)["police_no"] == expected


def test_allianz_number_without_label():
    result = extract_policy(
        "policy.pdf", full_text="Allianz Sigorta\n0001-0310-05746303"
    )
    assert result.police_no == "05746303"


@pytest.mark.parametrize("company", ["allianz", "hdi"])
def test_missing_policy_number(company):
    assert extract_policy("policy.pdf", full_text=f"{company} Sigorta").police_no is None


@pytest.mark.parametrize("company,raw,expected", [
    ("allianz", "0001-0310-05746303", "05746303"),
    ("hdi", "175101008353 - T3", "175101008353"),
    ("hdi", "175101008353", "175101008353"),
])
@pytest.mark.parametrize("mode", ["pdf", "batch", "serve"])
def test_pdf_and_old_cache_refresh(tmp_path, company, raw, expected, mode):
    pdf = write_pdf(tmp_path / "policy.pdf", [
        f"{company.upper()} SIGORTA\nPolicy No: {raw}"
    ])
    old = {
        "file": pdf.name, "sha256": hash_file(pdf),
        "extractor_version": "0.5.5", "police_no": raw, "company": company,
    }
    if mode == "pdf":
        result = extract_policy_fast(str(pdf))
        assert result.company == company
        assert result.police_no == expected
        return
    if mode == "batch":
        record, from_cache, failed = cli._process_batch_file(pdf, {pdf.name: old}, 7)
        assert not failed
    else:
        watcher = service.WatchService(service.ServiceConfig(
            folder=tmp_path, meta_path=tmp_path / ".metadata",
            not_found_path=tmp_path / ".not-found-metadata", handle_stdin=False,
        ))
        watcher.cache[pdf.name] = old
        outcome = watcher.process_one(pdf)
        record, from_cache = outcome["record"], outcome["from_cache"]
    assert not from_cache
    assert record["company"] == company
    assert record["police_no"] == expected
