"""Extractor uç durum testleri: etiket varyantları, sınır değerler, tablo geometrisi.

Çalıştırma:  python tests/test_extractor_edge_cases.py
(pytest varsa `pytest tests/test_extractor_edge_cases.py` ile de çalışır.)
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "src"))
sys.path.insert(0, str(_HERE.parent))

from policy_extract.cli import is_not_found_record, record_from_extraction
from policy_extract.extractor import (
    POLICE_HEADERS,
    ZEYIL_HEADERS,
    PolicyExtraction,
    _clean_identifier,
    _find_layout_police_no,
    _is_header,
    _looks_like_police_no,
    _looks_like_zeyil_no,
    _normalize,
    extract_policy,
    find_inline_police_no,
    find_inline_zeyil_no,
    find_table_police_no,
    find_table_zeyil_no,
)
from synthetic_data import synthetic_policy_texts, synthetic_tables


# ---------------------------------------------------------------------------
# Normalizasyon ve değer doğrulama
# ---------------------------------------------------------------------------


def test_normalize_turkce_harfleri_katlar() -> None:
    for raw, expected in [
        ("İSTANBUL", "istanbul"),
        ("IŞIK", "isik"),
        ("İ", "i"),
        ("ı", "i"),
        ("ĞÜŞİÖÇ", "gusioc"),
        ("ŞİŞLİ/İSTANBUL", "sisli/istanbul"),
        ("  Çok   Boşluk  ", "cok bosluk"),
        ("Poliçe:", "police"),
        # İki nokta önce, sonra boşluk sıyrılır: sondaki boşluk kalmaz.
        ("Poliçe No :", "police no"),
        # Bozuk glif korunur; "önceki" önek kontrolü bunu regex ile tolere eder.
        ("\ufffdnceki", "\ufffdnceki"),
    ]:
        assert _normalize(raw) == expected, (raw, _normalize(raw), expected)


def test_clean_identifier_bosluk_ve_tire_duzeltir() -> None:
    for raw, expected in [
        ("  100000002 / 2 ", "100000002/2"),
        ("100000002/2,", "100000002/2"),
        ("0001\u20130110\u201400000001", "0001-0110-00000001"),
        ("100000002 - 2", "100000002-2"),
        # Noktalama ve ardından gelen boşluk birlikte sıyrılır.
        ("123456 :", "123456"),
        ("A B", "A B"),
    ]:
        assert _clean_identifier(raw) == expected, (raw, _clean_identifier(raw))


def test_police_no_dogrulama_sinirlari() -> None:
    gecerli = [
        "123456",  # tam sınır: 6 karakter / 6 rakam
        "A12345",  # 5 rakam + harf
        "AB-12345",
        "12/34/5678",
        "21092025",  # nokta yok -> tarih sayılmaz
        "0001-0110-06857993",
    ]
    gecersiz = [
        "12345",  # 5 karakter: uzunluk sınırının altında
        "ABCDEF",  # rakam yok
        "A1B2C3",  # sadece 3 rakam
        "AB1234",  # 4 rakam
        "0/0",
        "123 456",  # boşluk kabul edilmez
        "21/09/2025",
        "1/1/2020",
        "13.11.2025",  # nokta ayraç değil
        "",
    ]
    for value in gecerli:
        assert _looks_like_police_no(value) is True, value
    for value in gecersiz:
        assert _looks_like_police_no(value) is False, value


def test_zeyil_no_dogrulama_sinirlari() -> None:
    for value in ["0", "00", "0/0", "12", "1A", "0007", "7-8"]:
        assert _looks_like_zeyil_no(value) is True, value
    for value in ["", "ABC", " ", "0 0", "-"]:
        assert _looks_like_zeyil_no(value) is False, value


# ---------------------------------------------------------------------------
# Inline (metin içi) çıkarım
# ---------------------------------------------------------------------------


def test_inline_etiket_varyantlari() -> None:
    for text in [
        "Poliçe No: 123456789",
        "poliçe no : 123456789",
        "POLICE NO 123456789",
        "PoliçeNo: 123456789",  # boşluksuz birleşik etiket
        "Police No: 123456789",
        "Policy No: 123456789",
        "Policy Number: 123456789",
        "Poliçe No 123456789",
    ]:
        assert find_inline_police_no(text) == "123456789", text


def test_inline_birlesik_yenileme_etiketi() -> None:
    assert find_inline_police_no("Poliçe No / Yenileme No : 1000000031 / 0") == "1000000031"
    assert find_inline_police_no("Poliçe / Yenileme No : 1000000032 / 0") == "1000000032"
    assert find_inline_police_no("POLİÇE NO / YENİLEME NO : 1000000033 / 7") == "1000000033"


def test_inline_deger_alt_satirda_olabilir() -> None:
    assert find_inline_police_no("Poliçe No\n123456789") == "123456789"
    assert find_inline_police_no("Poliçe No:\n\t100000002 / 2") == "100000002/2"


def test_inline_numara_etiketi_desteklenir() -> None:
    # "Poliçe Numarası" hem metin içinde hem tabloda geçerli bir etikettir.
    for text in [
        "Poliçe Numarası: 123456789",
        "POLİÇE NUMARASI : 123456789",
        "Poliçe Numarası 100000002 / 2",
    ]:
        assert find_inline_police_no(text) is not None, text
    assert find_inline_police_no("Poliçe Numarası: 123456789") == "123456789"
    assert find_inline_police_no("Poliçe Numarası 100000002 / 2") == "100000002/2"
    # Birleşik yenileme etiketinde yine sol taraf alınır.
    assert find_inline_police_no("Poliçe Numarası / Yenileme No : 1000000042 / 0") == "1000000042"


def test_inline_onceki_ve_kurum_onekleri_reddedilir() -> None:
    # Şirket tespitindeki sözcük ailesiyle aynı: onceki (bozuk glif dahil),
    # previous, prior, old, eski + sbm, dask, open cover.
    for prefix in [
        "Önceki",
        "Onceki",
        "\ufffdnceki",
        "Previous",
        "Prior",
        "Old",
        "Eski",
    ]:
        text = f"{prefix} Poliçe No 123456789"
        assert find_inline_police_no(text) is None, text
    for text in [
        "SBM Poliçe No 123456789",
        "DASK Poliçe No 123456789",
        "Open Cover Poliçe No 123456789",
        "Previous Policy No 123456789",
        "Eski Policy No 123456789",
        "OPEN COVER POLICY NO 123456789",
    ]:
        assert find_inline_police_no(text) is None, text


def test_inline_ilk_eslesme_kazanir() -> None:
    text = "Poliçe No 111111 Ek Zeyil No 2 Poliçe No 222222"
    assert find_inline_police_no(text) == "111111"


def test_inline_genel_kalic_yedekleri() -> None:
    assert find_inline_police_no("0001-0110-06857993") == "0001-0110-06857993"
    assert find_inline_police_no("0001-0110-06857993 no'lu poliçeniz") == "0001-0110-06857993"
    assert find_inline_police_no("123456789 nolu poliçeniz") == "123456789"
    assert find_inline_police_no("123456789 no\ufffdlu poliçeniz") == "123456789"


def test_inline_gecersiz_girdilerde_none() -> None:
    for text in [
        "",
        "Poliçe No",
        "Poliçe No:",
        "Poliçe No: 12345",
        "Poliçe No 21/09/2025",
        "Bu belge poliçe içermez.",
        "Poliçe No** 123456789",
    ]:
        assert find_inline_police_no(text) is None, text


def test_inline_kucuk_harfli_deger_kabul_edilir() -> None:
    assert find_inline_police_no("poliçe no: ABC123456") == "ABC123456"
    assert find_inline_police_no("policy no: abc123456") == "abc123456"


def test_inline_zeyil_varyantlari() -> None:
    assert find_inline_zeyil_no("Poliçe No 123456789 Ek Zeyil No 3") == "3"
    assert find_inline_zeyil_no("Poliçe No 123456789 Ek Belge No 2") == "2"
    assert find_inline_zeyil_no("Policy No 2000000000003 Endorsement No 2") == "2"
    assert find_inline_zeyil_no("Endorsement Number: 3") == "3"
    assert find_inline_zeyil_no("Zeyil No: 0007") == "0007"
    assert find_inline_zeyil_no("Ek Zeyil No 12/3") == "12"


def test_inline_zeyil_temel_police_sifirdir() -> None:
    for text in ["Ek Zeyil No 0", "Ek Zeyil No 00", "Ek Zeyil No 0/0", "Ek Zeyil No 0-0"]:
        assert find_inline_zeyil_no(text) is None, text
    assert find_inline_zeyil_no("Poliçe No 123456789") is None


# ---------------------------------------------------------------------------
# Layout (sütun konumuna göre) çıkarımı
# ---------------------------------------------------------------------------


def test_layout_baslik_altindaki_degeri_bulur() -> None:
    assert _find_layout_police_no("Police No            Start Date\n1000000001           21.09.2025\n") == "1000000001"
    assert _find_layout_police_no("            Police No\n              0\n         1000000001\n") == "1000000001"


def test_layout_uzak_tokeni_reddeder() -> None:
    # Değer başlığın altında değil, satırın öbür ucunda: yakınlık sınırı dışı.
    assert _find_layout_police_no("Police No\n" + " " * 60 + "123456789\n") is None


def test_layout_yakin_tokeni_kabul_eder() -> None:
    assert _find_layout_police_no("Police No\n    123456789\n") == "123456789"


def test_layout_previous_basligini_atlar() -> None:
    text = "Police No        Previous Policy No\n1000000001              1000000002\n"
    assert find_inline_police_no(text) == "1000000001"


def test_layout_eslesmeyen_baslik_none() -> None:
    assert _find_layout_police_no("Yenileme No\n0\n") is None
    assert _find_layout_police_no("") is None


# ---------------------------------------------------------------------------
# Tablo çıkarımı
# ---------------------------------------------------------------------------


def test_sentetik_tablo_katalogu() -> None:
    for label, tables, expected_police, expected_zeyil in synthetic_tables():
        assert find_table_police_no(tables) == expected_police, label
        assert find_table_zeyil_no(tables) == expected_zeyil, label


def test_tablo_bos_girdiler() -> None:
    assert find_table_police_no([]) is None
    assert find_table_police_no([[]]) is None
    assert find_table_police_no([[[]]]) is None
    assert find_table_zeyil_no([]) is None
    assert find_table_zeyil_no([[]]) is None


def test_tablo_gecersiz_degeri_atlayip_sonraki_satira_bakar() -> None:
    table = [
        ["Police No", "Bitiş Tarihi"],
        ["21.09.2025", "13.11.2026"],  # tarih: geçersiz -> atlanır
        ["1000000001", "13.11.2026"],
    ]
    assert find_table_police_no([table]) == "1000000001"


def test_tablo_komsu_hucre_basliksa_alt_satira_bakar() -> None:
    table = [["Police No", "Ek Zeyil No", "Yenileme No"], ["123456789", "0", "1"]]
    assert find_table_police_no([table]) == "123456789"
    assert find_table_zeyil_no([table]) is None  # Ek Zeyil No = 0 -> temel poliçe


def test_tablo_sayisal_ve_bos_hucreler() -> None:
    assert find_table_police_no([[["Police No"], [1234567]]]) == "1234567"
    assert find_table_police_no([[["Police No"], [None]]]) is None
    assert find_table_police_no([[["Police No"], [""]]]) is None
    # Hücre sonundaki noktalama + boşluk birlikte sıyrılır, değer geçerli olur.
    assert find_table_police_no([[["Poliçe No"], ["123456 :"]]]) == "123456"


def test_tablo_yenileme_etiketi_slash_sol_tarafi() -> None:
    assert find_table_police_no([[["Poliçe No / Yenileme No"], ["123456789 / 4"]]]) == "123456789"


def test_tablo_basligi_ikinci_satirda_olabilir() -> None:
    table = [["X"], ["Poliçe No", "Ek Zeyil No"], ["1000000001", "0"]]
    assert find_table_police_no([table]) == "1000000001"


def test_tablo_previous_policy_no_ana_police_degil() -> None:
    table = [["Previous Policy No", "Poliçe No"], ["0001-0110-00000002", "0001-0110-00000001"]]
    assert find_table_police_no([table]) == "0001-0110-00000001"
    assert find_table_police_no([[["Previous Policy No"], ["0001-0110-00000002"]]]) is None
    assert find_table_police_no([[["SBM Poliçe No"], ["700000001"]]]) is None


def test_tablo_numara_basligi_desteklenir() -> None:
    assert find_table_police_no([[["Poliçe Numarası"], ["1000000001"]]]) == "1000000001"
    assert find_table_police_no([[["Poliçe Numarası", "1000000001"]]]) == "1000000001"
    assert find_table_police_no([[["Poli ç e Numarası"], ["1000000001"]]]) == "1000000001"


def test_tablo_zeyil_varyantlari() -> None:
    assert find_table_zeyil_no([[["Ek Zeyil No"], ["9"]]]) == "9"
    assert find_table_zeyil_no([[["Ek Zeyil No", "12"]]]) == "12"
    assert find_table_zeyil_no([[["Endorsement No"], ["0007"]]]) == "0007"
    assert find_table_zeyil_no([[["Ek Belge No", "Poliçe No"], ["5", "1000000002"]]]) == "5"
    assert find_table_zeyil_no([[["Ek Zeyil No"], ["0"], ["9"]]]) is None
    assert find_table_zeyil_no([[["Ek Zeyil No"], ["0/0"]]]) is None
    assert find_table_zeyil_no([[["Poliçe No"], ["1000000001"]]]) is None


def test_header_kumesi_kapsami() -> None:
    for header in [
        "Poliçe No",
        "POLİÇE NO",
        "Poliçe Numarası",
        "Poliçe No / Yenileme No",
        "Policy Number",
    ]:
        assert _is_header(header, POLICE_HEADERS), header
    for not_header in ["Previous Policy No", "SBM Poliçe No", "Müşteri No", "Yenileme No"]:
        assert not _is_header(not_header, POLICE_HEADERS), not_header
    for header in ["Ek Zeyil No", "Zeyil No", "Ek Belge No", "Endorsement Number"]:
        assert _is_header(header, ZEYIL_HEADERS), header


# ---------------------------------------------------------------------------
# extract_policy birleştirme mantığı
# ---------------------------------------------------------------------------


def test_extract_policy_inline_tablodan_once_gelir() -> None:
    result = extract_policy(
        "x.pdf",
        full_text="Poliçe No 100000002 / 2\nAXA Sigorta A.Ş. axa.com.tr",
        tables=[[["Police No"], ["999999999"]]],
    )
    assert result.police_no == "100000002/2"
    assert result.police_no_source == "inline"
    assert result.company == "axa"
    assert result.company_confidence in ("high", "medium")


def test_extract_policy_tablo_yedegi_ve_kaynak_alani() -> None:
    result = extract_policy("x.pdf", tables=[[["Police No"], ["123456789"]]])
    assert result.police_no == "123456789"
    assert result.police_no_source == "table"
    assert result.zeyil_no is None and result.zeyil_no_source is None
    assert result.company is None and result.company_confidence == "unknown"
    assert result.company_scores == {}


def test_extract_policy_bos_girdi_varsayilanlari() -> None:
    result = extract_policy("y.pdf")
    assert result.source_file == "y.pdf"
    assert result.full_text == ""
    assert result.tables == []
    assert result.police_no is None and result.police_no_source is None
    assert result.zeyil_no is None and result.zeyil_no_source is None
    assert result.company is None and result.company_confidence == "unknown"


def test_extract_policy_zeyil_kaynagi_inline_ve_tablo() -> None:
    inline = extract_policy("a.pdf", full_text="Poliçe No 123456789 Ek Zeyil No 4")
    assert (inline.zeyil_no, inline.zeyil_no_source) == ("4", "inline")
    tabled = extract_policy("b.pdf", tables=[[["Ek Zeyil No"], ["7"]]])
    assert (tabled.zeyil_no, tabled.zeyil_no_source) == ("7", "table")
    # Tablo zeyil alanı boş ("0") olsa da inline'daki gerçek zeyil korunur.
    both = extract_policy(
        "c.pdf",
        full_text="Poliçe No 123456789 Ek Zeyil No 9",
        tables=[[["Ek Zeyil No"], ["0"]]],
    )
    assert (both.zeyil_no, both.zeyil_no_source) == ("9", "inline")


def test_to_dict_derin_kopya_dondurur() -> None:
    result = PolicyExtraction(
        source_file="a.pdf",
        police_no="123456789",
        police_no_source="inline",
        tables=[[["Police No"], ["123456789"]]],
        company_scores={"axa": 12},
    )
    payload = result.to_dict()
    payload["tables"].append([["ek"]])
    payload["company_scores"]["axa"] = 999
    assert len(result.tables) == 1
    assert result.company_scores == {"axa": 12}
    assert payload["source_file"] == "a.pdf"
    assert payload["police_no_source"] == "inline"


# ---------------------------------------------------------------------------
# Kayıt (record) kuralları
# ---------------------------------------------------------------------------


def test_record_from_extraction_ve_not_found_kurallari() -> None:
    result = PolicyExtraction(
        source_file="klasör/örnek poliçe.pdf",
        police_no="123456789",
        police_no_source="table",
        zeyil_no="3",
        zeyil_no_source="table",
        company="axa",
        company_confidence="high",
        company_scores={"axa": 12},
        full_text="çok uzun metin",
        tables=[[["x"]]],
    )
    record = record_from_extraction(result, sha256="abc")
    assert record["file"] == "örnek poliçe.pdf"
    assert record["police_no_source"] == "table"
    assert record["zeyil_no_source"] == "table"
    assert "full_text" not in record and "tables" not in record
    assert not is_not_found_record(record)

    for missing in ("police_no", "company"):
        assert is_not_found_record(record | {missing: None}), missing
        assert is_not_found_record(record | {missing: ""}), missing
    assert is_not_found_record(
        {"file": "x.pdf", "error": "okunamadı", "police_no": "1", "company": "axa"}
    )
    # "0" dolu bir değerdir: kayıt eksik sayılmaz.
    assert not is_not_found_record({"police_no": "0", "company": "axa"})


# ---------------------------------------------------------------------------
# Sentetik metin kataloğu (uçtan uca tutarlılık)
# ---------------------------------------------------------------------------


def test_sentetik_metinlerden_beklenen_sonuclar() -> None:
    beklenen = {
        "allianz_yuvam": "0001-0110-00000001",
        "turkiye_sigorta_slash": "100000002/2",
        "mapfre_isveren": "2000000000004",
        "ingilizce_marine": "2000000000001",
        # Yenileme eki değerin parçası olarak kalır (mevcut davranış).
        "hdi_birlesik": "2000000000005-T3",
        "magdeburger_domain": "9000000017",
        "zeyilli_kasko": "123456789",
        "gunes_unvan": "555000111",
        "sompo_kisa": "456789123",
        "neova_ters_yazim": "853123456",
        "groupama_bozuk_glif": "100000002/2",
        "onceki_sirket": "3500000012",
        "sadece_sehir": "0001-0110-00000001",
        "bos_anlamsiz": None,
    }
    texts = dict(synthetic_policy_texts())
    for label, expected in beklenen.items():
        result = extract_policy(f"{label}.pdf", full_text=texts[label])
        assert result.police_no == expected, (label, result.police_no, expected)


def test_sentetik_metinler_tutarli_ve_deterministik() -> None:
    for label, text in synthetic_policy_texts():
        first = extract_policy(f"{label}.pdf", full_text=text)
        second = extract_policy(f"{label}.pdf", full_text=text)
        assert (first.police_no, first.zeyil_no, first.company) == (
            second.police_no,
            second.zeyil_no,
            second.company,
        ), label
        if first.police_no is None:
            assert first.police_no_source is None, label
        else:
            assert first.police_no_source in ("inline", "table"), label
            assert _looks_like_police_no(first.police_no), (label, first.police_no)
        if first.zeyil_no is not None:
            assert first.zeyil_no_source in ("inline", "table"), label
        if first.company is None:
            assert first.company_confidence == "unknown", label
        else:
            assert first.company_confidence in ("high", "medium", "low"), label
            assert first.company_scores.get(first.company), label
        # Kayıt şekli her zaman dolu olmalı (Flutter sözleşmesi).
        record = record_from_extraction(first, sha256="h" * 64)
        assert record["file"] == f"{label}.pdf"
        assert set(record) == {
            "file",
            "sha256",
            "police_no",
            "police_no_source",
            "zeyil_no",
            "zeyil_no_source",
            "company",
            "company_confidence",
            "company_scores",
        }, label


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    raise SystemExit(1 if failed else 0)




