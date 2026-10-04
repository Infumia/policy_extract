"""Regression coverage for versioned metadata in batch and serve modes."""
from pathlib import Path

import pytest

from policy_extract import cli, service
from policy_extract.models import PolicyExtraction
from policy_extract.pdf_io import hash_file
from policy_extract.records import (
    is_reusable_cache_entry, load_metadata_cache, record_from_extraction,
)
from policy_extract.version import SERVICE_VERSION


@pytest.mark.parametrize("version", [None, "older-version", SERVICE_VERSION])
@pytest.mark.parametrize("mode", ["batch", "serve"])
def test_version_change_refreshes_same_pdf(tmp_path, monkeypatch, version, mode):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"same PDF contents")
    digest = hash_file(str(pdf))
    old = {
        "file": pdf.name, "sha256": digest, "extractor_version": version,
        "police_no": "123", "company": "anadolu", "zeyil_no": None,
    }
    calls = []

    def extract(path, **kwargs):
        calls.append(path)
        return PolicyExtraction(
            source_file=path, police_no="123", company="anadolu", zeyil_no="2",
        )

    if mode == "batch":
        monkeypatch.setattr(cli, "extract_policy_fast", extract)
        record, from_cache, failed = cli._process_batch_file(pdf, {pdf.name: old}, 7)
        assert not failed
    else:
        monkeypatch.setattr(service, "extract_policy_fast", extract)
        cfg = service.ServiceConfig(
            folder=tmp_path, meta_path=tmp_path / ".metadata",
            not_found_path=tmp_path / ".not-found-metadata", handle_stdin=False,
        )
        watcher = service.WatchService(cfg)
        watcher.cache[pdf.name] = old
        outcome = watcher.process_one(pdf)
        record, from_cache = outcome["record"], outcome["from_cache"]
        watcher._persist(record)
        restored = load_metadata_cache(cfg.meta_path)
        watcher.cache = restored
        assert watcher.process_one(pdf)["from_cache"] is True

    current = version == SERVICE_VERSION
    assert from_cache is current
    assert len(calls) == (0 if current else 1)
    assert record["sha256"] == digest
    assert record["extractor_version"] == SERVICE_VERSION
    assert record["zeyil_no"] == (None if current else "2")


def test_current_version_still_requires_matching_pdf_and_success():
    record = {"sha256": "pdf", "extractor_version": SERVICE_VERSION,
              "police_no": "123", "company": "anadolu"}
    assert is_reusable_cache_entry(record, "pdf")
    assert not is_reusable_cache_entry(record, "changed")
    assert not is_reusable_cache_entry(record, None)
    assert not is_reusable_cache_entry({**record, "error": "failure"}, "pdf")
    assert not is_reusable_cache_entry({**record, "company": None}, "pdf")


def test_record_uses_the_version_reported_by_serve():
    result = PolicyExtraction(source_file="a.pdf", police_no="123", company="anadolu")
    record = record_from_extraction(result, sha256="pdf")
    assert record["extractor_version"] == service.SERVICE_VERSION == SERVICE_VERSION
