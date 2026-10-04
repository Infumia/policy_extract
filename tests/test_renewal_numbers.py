"""Renewal-number regressions using the reported Anadolu header layout."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "src"))
sys.path.insert(0, str(_HERE.parent))

from policy_extract.cli import record_from_extraction
from policy_extract.extractor import (
    extract_policy,
    extract_policy_fast,
    find_inline_zeyil_no,
    find_table_zeyil_no,
)
from synthetic_data import write_pdf


ANADOLU_HEADER = (
    "Poliçe No                     Poliçe Vadesi                              "
    "Yenileme No       Düzenleme Tarihi - Yeri ve Saati            Sigorta Süresi"
)
ANADOLU_VALUES = (
    "1031263724            18/01/2026-18/01/2027                    "
    "2                         12/01/2026 - 14:53                       365 gün"
)


def test_reported_anadolu_layout() -> None:
    result = extract_policy(
        "anadolu.pdf",
        full_text=f"{ANADOLU_HEADER}\n{ANADOLU_VALUES}\nwww.anadolusigorta.com.tr",
    )
    assert result.police_no == "1031263724"
    assert (result.zeyil_no, result.zeyil_no_source) == ("2", "inline")
    assert result.company == "anadolu"


def test_inline_renewal_labels() -> None:
    for label in ("Yenileme No", "YENİLEME NO", "Ek / Yenileme No"):
        assert find_inline_zeyil_no(f"{label}: 2") == "2", label
        assert find_inline_zeyil_no(f"{label}: 0007") == "0007", label
        assert find_inline_zeyil_no(f"{label}: 0") is None, label


def test_renewal_table_layouts() -> None:
    for table in (
        [["Poliçe No", "Yenileme No", "Düzenleme Tarihi"], ["1031263724", "2", "12/01/2026"]],
        [["YENİLEME NO", "2"]],
    ):
        assert find_table_zeyil_no([table]) == "2"
        result = extract_policy("table.pdf", tables=[table])
        assert (result.zeyil_no, result.zeyil_no_source) == ("2", "table")
    assert find_table_zeyil_no([[["Yenileme No"], ["0"]]]) is None


def test_combined_policy_renewal_is_not_endorsement() -> None:
    for label in ("Poliçe No / Yenileme No", "POLİÇE / YENİLEME NO"):
        assert find_inline_zeyil_no(f"{label}: 1031263724 / 2") is None
        assert find_inline_zeyil_no(f"{label}\n1031263724 / 2") is None
        assert find_table_zeyil_no([[[label], ["1031263724 / 2"]]]) is None


def test_explicit_endorsement_takes_priority_over_renewal() -> None:
    for endorsement, expected in (("3", "3"), ("0", None)):
        assert find_inline_zeyil_no(
            f"Yenileme No: 2 Ek Zeyil No: {endorsement}"
        ) == expected
        assert find_table_zeyil_no([
            [["Yenileme No", "Ek Zeyil No"], ["2", endorsement]]
        ]) == expected


def test_empty_renewal_column_does_not_take_neighbor_values() -> None:
    missing_value = ANADOLU_VALUES.replace("2                         12/01", "                          12/01")
    assert find_inline_zeyil_no(f"{ANADOLU_HEADER}\n{missing_value}") is None
    assert find_inline_zeyil_no(f"{ANADOLU_HEADER}\n\nSBM Poliçe No 809417754") is None


def test_layout_zero_and_explicit_endorsement() -> None:
    assert find_inline_zeyil_no(
        f"{ANADOLU_HEADER}\n{ANADOLU_VALUES.replace('2                         12/01', '0                         12/01')}"
    ) is None
    header = "Poliçe No           Ek Zeyil No       Yenileme No"
    for value, expected in (("3", "3"), ("0", None)):
        assert find_inline_zeyil_no(
            f"{header}\n1031263724          {value}                 2"
        ) == expected


def test_renewal_value_below_single_header() -> None:
    assert find_inline_zeyil_no("Yenileme No\n\n    0007") == "0007"
    for zero in ("0", "00", "0/0", "0-0"):
        assert find_inline_zeyil_no(f"Yenileme No\n    {zero}") is None


def test_layout_date_or_time_is_not_renewal_number() -> None:
    for value in (
        "12/01/2026", "12-01-2026", "12.01.2026",
        "18/01/2026-18/01/2027", "14:53",
    ):
        assert find_inline_zeyil_no(f"Yenileme No\n    {value}") is None, value


def test_pdf_renewal_layout_to_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pdf = write_pdf(
            Path(tmp) / "anadolu.pdf",
            [f"{ANADOLU_HEADER}\n{ANADOLU_VALUES}\nwww.anadolusigorta.com.tr"],
        )
        result = extract_policy_fast(str(pdf))
        record = record_from_extraction(result, sha256="test")
        assert record["police_no"] == "1031263724"
        assert record["zeyil_no"] == "2"
        assert record["zeyil_no_source"] == "inline"
        assert record["company"] == "anadolu"
