"""Sentetik PDF testleri: üretilen gerçek PDF'lerle uçtan uca çıkarım.

Testler reportlab/gerçek poliçe gerektirmez; `synthetic_data.build_pdf_bytes`
stdlib ile geçerli bir PDF yazar, pypdf onu normal PDF gibi okur.

Çalıştırma:  python tests/test_synthetic_pdfs.py
(pytest varsa `pytest tests/test_synthetic_pdfs.py` ile de çalışır.)
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "src"))
sys.path.insert(0, str(_HERE.parent))

import policy_extract.cli as cli_module
import policy_extract.service as svc_module
from policy_extract.cli import is_not_found_record, main, record_from_extraction
from policy_extract.extractor import (
    extract_policy_fast,
    extract_text_fast,
    file_sha256,
    find_inline_police_no,
)
from policy_extract.service import ServiceConfig, WatchService
from synthetic_data import build_pdf_bytes, synthetic_pdf_pages, write_pdf


def _run_main(argv: list[str]) -> tuple[int, str]:
    """cli.main'i çağırır, (çıkış kodu, stdout) döner. stderr yutulur."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue()


# ---------------------------------------------------------------------------
# PDF üreticisinin kendisi
# ---------------------------------------------------------------------------


def test_pdf_uretici_gecerli_dosya_uretiyor() -> None:
    raw = build_pdf_bytes(["birinci satir", "ikinci satir"])
    assert raw.startswith(b"%PDF-1.4")
    assert raw.rstrip().endswith(b"%%EOF")
    assert b"/Type /Page" in raw
    assert b"/Count 2" in raw
    assert b"/Subtype /Type1" in raw

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    assert len(reader.pages) == 2
    text = reader.pages[0].extract_text(extraction_mode="layout")
    assert "birinci satir" in text


def test_pdf_uretici_bos_liste_tek_bos_sayfa() -> None:
    from pypdf import PdfReader

    raw = build_pdf_bytes([])
    reader = PdfReader(io.BytesIO(raw))
    assert len(reader.pages) == 1
    assert raw.count(b"/Type /Page ") == 1


def test_write_pdf_klasoru_olusturur() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "alt" / "klasor" / "x.pdf"
        path = write_pdf(target, ["Policy No: 123456789"])
        assert path == target and target.is_file()
        assert target.read_bytes().startswith(b"%PDF")


def test_file_sha256_pdf_dosyalari() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        first = write_pdf(Path(tmp) / "a.pdf", ["Policy No: 123456789"])
        second = write_pdf(Path(tmp) / "b.pdf", ["Policy No: 987654321"])
        assert file_sha256(first) == hashlib.sha256(first.read_bytes()).hexdigest()
        assert file_sha256(first) == file_sha256(first)  # deterministik
        assert file_sha256(first) != file_sha256(second)


# ---------------------------------------------------------------------------
# Sentetik PDF kataloğu: uçtan uca çıkarım
# ---------------------------------------------------------------------------


def test_sentetik_pdf_katalogu_uctan_uca() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name, pages, expected_police in synthetic_pdf_pages():
            path = write_pdf(folder / name, pages)
            text = extract_text_fast(str(path))
            assert text.strip(), f"{name}: metin çıkmadı"

            first = extract_policy_fast(str(path))
            second = extract_policy_fast(str(path))
            assert (first.police_no, first.company, first.zeyil_no) == (
                second.police_no,
                second.company,
                second.zeyil_no,
            ), f"{name}: deterministik değil"
            assert first.police_no == expected_police, (name, first.police_no, expected_police)
            if expected_police is None:
                assert first.police_no_source is None, name
                assert first.company is None, (name, first.company)
            else:
                assert first.police_no_source in ("inline", "table"), name
            assert first.company_confidence in ("high", "medium", "low", "unknown"), name


def test_pdf_metni_layout_satirlarini_korur() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = write_pdf(
            Path(tmp) / "kapsam.pdf",
            ["YUVAM SIGORTA POLICESI\nPolicy No: 1111222333444\nALLIANZ SIGORTA A.S."],
        )
        text = extract_text_fast(str(path))
        assert "YUVAM SIGORTA POLICESI" in text
        assert text.index("YUVAM") < text.index("Policy No")
        assert "ALLIANZ SIGORTA A.S." in text


def test_pdf_tireli_police_no_korunur() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = write_pdf(Path(tmp) / "tireli.pdf", ["Policy No: 0001-0110-06857993"])
        text = extract_text_fast(str(path))
        assert find_inline_police_no(text) == "0001-0110-06857993"
        assert extract_policy_fast(str(path)).police_no == "0001-0110-06857993"


def test_pdf_turkce_karakterler_ve_bozuk_glif() -> None:
    # WinAnsi kodlaması ç/ö/ü'yü taşır; ş/ğ/İ taşınmaz ve "?" olur.
    # Etiket ve domain hâlâ okunabildiği için çıkarım bozulmaz.
    with tempfile.TemporaryDirectory() as tmp:
        path = write_pdf(
            Path(tmp) / "turkce.pdf",
            ["Poliçe No: 123456789\nGÜNEŞ SİGORTA A.Ş. gunessigorta.com.tr"],
        )
        text = extract_text_fast(str(path))
        assert "gunessigorta.com.tr" in text
        assert "Poliçe" in text  # ç taşınabilir
        assert "GÜNE? S?GORTA" in text  # ş/İ taşınamaz -> "?" olur
        result = extract_policy_fast(str(path))
        assert result.police_no == "123456789"
        assert result.company == "gunes", result.company_scores


def test_pdf_zeyil_no_inline() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = write_pdf(
            Path(tmp) / "zeyil.pdf",
            ["Policy No: 2222333444555 Endorsement No 2\nAXA SIGORTA A.S. axa.com.tr"],
        )
        result = extract_policy_fast(str(path))
        assert (result.zeyil_no, result.zeyil_no_source) == ("2", "inline")


def test_pdf_operator_kacislari_metni_bozmaz() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = write_pdf(
            Path(tmp) / "kacis.pdf",
            ["POLICE (PARANTEZ) \\EGIK\\ Policy No: 123456789"],
        )
        text = extract_text_fast(str(path))
        assert "(PARANTEZ)" in text and "\\EGIK\\" in text
        assert extract_policy_fast(str(path)).police_no == "123456789"


# ---------------------------------------------------------------------------
# Sayfa limiti
# ---------------------------------------------------------------------------


def test_cok_sayfali_pdf_varsayilan_limit_icinde_taranir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = write_pdf(
            Path(tmp) / "cok_sayfa.pdf",
            [f"SAYFA {i}\nlorem ipsum dolor" for i in range(1, 6)],
        )
        text = extract_text_fast(str(path))  # varsayılan: ilk 7 sayfa
        assert all(f"SAYFA {i}" in text for i in range(1, 6))
        limited = extract_text_fast(str(path), max_pages=2)
        assert "SAYFA 1" in limited and "SAYFA 2" in limited
        assert "SAYFA 3" not in limited


def test_gec_sayfadaki_police_no_limitle_kacar() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        pages = [
            "COVER PAGE\nBu sayfada police numarasi yoktur.",
            "GENERAL CONDITIONS\nlorem ipsum",
            "ISSUING DETAILS\nPolicy No: 3333444555666\nHDI SIGORTA A.S.",
        ]
        path = write_pdf(folder / "gec.pdf", pages)
        assert extract_policy_fast(str(path), max_pages=5).police_no == "3333444555666"
        assert extract_policy_fast(str(path), max_pages=2).police_no is None
        # max_pages<=0 -> sınırsız (CLI ile aynı anlam: --max-pages 0).
        assert extract_policy_fast(str(path), max_pages=0).police_no == "3333444555666"
        assert extract_policy_fast(str(path), max_pages=-3).police_no == "3333444555666"


def test_cli_max_pages_siniri_ve_sinirsiz_mod() -> None:
    """CLI'nin --max-pages sözleşmesi uçtan uca: 0/eksi = tüm sayfalar, N = ilk N."""
    pages = [
        "COVER PAGE\nBu sayfada police numarasi yoktur.",
        "GENERAL CONDITIONS\n" + "lorem ipsum dolor sit amet " * 10,
        "ISSUING DETAILS\nPolicy No: 3333444555666\nHDI SIGORTA A.S. hdisigorta.com.tr",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        pdf = write_pdf(folder / "gec.pdf", pages)

        for limit in ("0", "-1"):
            out = folder / f"tum_{limit}.json"
            code, _ = _run_main(["--file", str(pdf), "-o", str(out), "--max-pages", limit])
            assert code == 0
            payload = json.loads(out.read_text(encoding="utf-8"))
            assert payload["police_no"] == "3333444555666", limit
            assert payload["company"] == "hdi", limit

        out = folder / "iki.json"
        code, _ = _run_main(["--file", str(pdf), "-o", str(out), "--max-pages", "2"])
        assert code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["police_no"] is None
        assert payload["company"] is None  # şirket de 3. sayfada


def test_bos_pdf_metni_bos_doner() -> None:
    from synthetic_data import build_pdf_bytes

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bos_sayfa.pdf"
        path.write_bytes(build_pdf_bytes([""]))
        assert extract_text_fast(str(path)) == ""
        result = extract_policy_fast(str(path))
        assert result.police_no is None and result.company is None


# ---------------------------------------------------------------------------
# Bozuk / eksik dosyalar
# ---------------------------------------------------------------------------


def test_bozuk_pdf_cokertmez() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        bozuk = folder / "bozuk.pdf"
        bozuk.write_bytes(b"bu bir pdf degil %%%%")
        assert extract_text_fast(str(bozuk)) == ""
        result = extract_policy_fast(str(bozuk))
        assert result.police_no is None and result.company is None
        record = record_from_extraction(result, sha256=file_sha256(bozuk))
        assert record["police_no"] is None
        assert is_not_found_record(record)


def test_bos_ve_kirpik_pdf_metni_bos_doner() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        bos = folder / "bos.pdf"
        bos.write_bytes(b"")
        kirpik = folder / "kirpik.pdf"
        kirpik.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\n")
        header_only = folder / "sadece_header.pdf"
        header_only.write_bytes(b"%PDF-1.4\n")
        for path in (bos, kirpik, header_only):
            assert extract_text_fast(str(path)) == "", path.name
            assert extract_policy_fast(str(path)).police_no is None, path.name


def test_olmayan_dosya_ve_klasor_yolu_cokertmez() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        missing = folder / "yok.pdf"
        assert extract_text_fast(str(missing)) == ""
        assert extract_policy_fast(str(missing)).police_no is None
        assert extract_text_fast(str(folder)) == ""  # klasör: PDF değil


# ---------------------------------------------------------------------------
# CLI: tek dosya modu
# ---------------------------------------------------------------------------


def test_cli_tek_dosya_cikisi() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        pdf = write_pdf(
            folder / "tek.pdf", ["Policy No: 7777888999000\nAXA SIGORTA A.S. axa.com.tr"]
        )
        out = folder / "out.json"

        code, _ = _run_main(["--file", str(pdf), "-o", str(out)])
        assert code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["police_no"] == "7777888999000"
        assert payload["company"] == "axa"
        assert payload["sha256"] == file_sha256(pdf)
        assert payload["source_file"] == str(pdf)
        assert "full_text" not in payload  # varsayılan kapalı
        assert payload["tables"] == []

        code, _ = _run_main(["--file", str(pdf), "-o", str(out), "--with-text", "--no-tables"])
        assert code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert "tables" not in payload
        assert "Policy No" in payload["full_text"]

        # stdout modu (-o verilmezse): tek JSON bloğu basılır.
        code, stdout = _run_main(["--file", str(pdf)])
        assert code == 0
        assert json.loads(stdout)["police_no"] == "7777888999000"


def test_cli_hata_cikislari() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        assert _run_main(["--file", str(folder / "yok.pdf")])[0] == 2

        bos_klasor = folder / "bos_klasor"
        bos_klasor.mkdir()
        assert _run_main([str(bos_klasor)])[0] == 2

        txt_klasor = folder / "txt_klasor"
        txt_klasor.mkdir()
        (txt_klasor / "notlar.txt").write_text("pdf degil", encoding="utf-8")
        assert _run_main([str(txt_klasor)])[0] == 2

        # Girdi yolu olmayan klasör gibi davranır: tek dosya modu -> bulunamadı.
        assert _run_main([str(folder / "hic_olmayan")])[0] == 2

        # --serve yalnızca klasör modunda çalışır.
        pdf = write_pdf(folder / "tek.pdf", ["Policy No: 123456789"])
        assert _run_main(["--file", str(pdf), "--serve"])[0] == 2


def test_cli_arguman_dogrulamalari() -> None:
    for argv in ([], ["--file", "a.pdf", "b.pdf"]):
        try:
            with redirect_stderr(io.StringIO()):  # argparse usage çıktısı sussun
                main(argv)
        except SystemExit as exc:
            assert exc.code == 2, argv
        else:
            raise AssertionError(f"{argv} SystemExit beklenirdi")

    from policy_extract.cli import build_parser

    # --max-pages artık olduğu gibi extract_policy_fast'e geçer (<=0 sınırsız).
    assert build_parser().parse_args(["x"]).max_pages == 7
    assert build_parser().parse_args(["x", "--max-pages", "3"]).max_pages == 3
    assert build_parser().parse_args(["x", "--max-pages", "0"]).max_pages == 0
    assert build_parser().parse_args(["x", "--max-pages", "-5"]).max_pages == -5
    assert build_parser().parse_args(["--watch", "x"]).serve is True
    assert not hasattr(cli_module, "_max_pages"), "CLI'de max_pages çeviricisi kalmamalı"


# ---------------------------------------------------------------------------
# CLI: klasör taraması (.metadata / .not-found-metadata sözleşmesi)
# ---------------------------------------------------------------------------


def _write_catalogue(folder: Path) -> dict[str, str | None]:
    """Sentetik PDF kataloğunu klasöre yazar; (ad -> beklenen poliçe no) döner."""
    expected: dict[str, str | None] = {}
    for name, pages, police in synthetic_pdf_pages():
        write_pdf(folder / name, pages)
        expected[name] = police
    (folder / "notlar.txt").write_text("pdf degil", encoding="utf-8")
    (folder / "isim_pdf_ama_klasor.pdf").mkdir()  # dosya değil: atlanmalı
    return expected


def test_cli_klasor_taramasi_sentetik_pdf() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        expected = _write_catalogue(folder)

        code, _ = _run_main([str(folder)])
        assert code == 0
        meta = folder / ".metadata"
        records = {
            json.loads(line)["file"]: json.loads(line)
            for line in meta.read_text(encoding="utf-8").splitlines()
        }
        assert set(records) == set(expected), sorted(records)
        for name, police in expected.items():
            assert records[name]["police_no"] == police, name
            assert records[name]["sha256"] == file_sha256(folder / name), name
            assert "full_text" not in records[name] and "tables" not in records[name], name

        nf_path = folder / ".not-found-metadata"
        nf = {
            json.loads(line)["file"]
            for line in nf_path.read_text(encoding="utf-8").splitlines()
        }
        eksik = {
            name
            for name, record in records.items()
            if not record.get("police_no") or not record.get("company")
        }
        assert nf == eksik, (nf, eksik)
        assert eksik, "katalogda eşleşmeyen dosya bulunmalı"

        # İkinci tur: tam kayıtlar önbellekten gelir, yalnızca eksikler yeniden okunur.
        calls: list[str] = []
        original = cli_module.extract_policy_fast

        def counting(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            calls.append(Path(pdf_path).name)
            return original(pdf_path, max_pages=max_pages)

        cli_module.extract_policy_fast = counting  # type: ignore[method-assign]
        try:
            code, _ = _run_main([str(folder)])
        finally:
            cli_module.extract_policy_fast = original  # type: ignore[method-assign]
        assert code == 0
        assert sorted(calls) == sorted(eksik), calls
        assert len(meta.read_text(encoding="utf-8").splitlines()) == len(expected)


def test_cli_no_cache_yeniden_hesaplar() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        expected = _write_catalogue(folder)
        assert _run_main([str(folder)])[0] == 0

        calls: list[str] = []
        original = cli_module.extract_policy_fast

        def counting(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            calls.append(Path(pdf_path).name)
            return original(pdf_path, max_pages=max_pages)

        cli_module.extract_policy_fast = counting  # type: ignore[method-assign]
        try:
            code, _ = _run_main([str(folder), "--no-cache"])
        finally:
            cli_module.extract_policy_fast = original  # type: ignore[method-assign]
        assert code == 0
        assert sorted(calls) == sorted(expected), calls
        lines = (folder / ".metadata").read_text(encoding="utf-8").splitlines()
        assert len(lines) == len(expected)


# ---------------------------------------------------------------------------
# Serve modu: sentetik klasör üzerinde iki tur
# ---------------------------------------------------------------------------


def test_serve_sentetik_klasorde_cift_kayit_uretmez() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        expected = _write_catalogue(folder)
        names = list(expected)

        calls: list[str] = []
        original = svc_module.extract_policy_fast

        def counting(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            calls.append(Path(pdf_path).name)
            return original(pdf_path, max_pages=max_pages)

        svc_module.extract_policy_fast = counting  # type: ignore[method-assign]
        try:
            counts: list[tuple[int, int, int, int]] = []
            for _ in (1, 2):
                cfg = ServiceConfig(
                    folder=folder,
                    meta_path=folder / ".metadata",
                    not_found_path=folder / ".not-found-metadata",
                    poll_interval=0.2,
                    stable_checks=1,
                    stable_interval_ms=10,
                    emit=lambda e: None,
                    handle_stdin=False,
                )
                service = WatchService(cfg)
                thread = threading.Thread(target=service.run, daemon=True)
                thread.start()
                deadline = time.time() + 60
                while service.stats.done < len(names) and time.time() < deadline:
                    time.sleep(0.05)
                service.stop_event.set()
                thread.join(timeout=15)
                assert not thread.is_alive(), "servis takılı kalmamalı"
                assert service.stats.done == len(names), service.stats
                meta_lines = (folder / ".metadata").read_text(encoding="utf-8").splitlines()
                nf_path = folder / ".not-found-metadata"
                nf_lines = (
                    nf_path.read_text(encoding="utf-8").splitlines() if nf_path.is_file() else []
                )
                counts.append(
                    (len(meta_lines), len(nf_lines), service.stats.computed, service.stats.cached)
                )
        finally:
            svc_module.extract_policy_fast = original  # type: ignore[method-assign]

        assert counts[0][0] == len(names), counts
        assert counts[1][0] == counts[0][0], counts
        assert counts[1][1] == counts[0][1], counts

        records = {
            json.loads(line)["file"]: json.loads(line)
            for line in (folder / ".metadata").read_text(encoding="utf-8").splitlines()
        }
        eksik = {
            name
            for name, record in records.items()
            if not record.get("police_no") or not record.get("company")
        }
        assert counts[0][1] == len(eksik), (counts, eksik)

        # Serve, sha değişmediği sürece not-found kayıtlarını da önbellekten verir
        # (bkz. test_not_found_ikinci_turda_tekrar_eklenmez). Batch modu ise
        # not-found kayıtlarını her turda yeniden hesaplar.
        assert counts[0][2] == len(names) and counts[0][3] == 0, counts
        assert counts[1][2] == 0 and counts[1][3] == len(names), counts
        assert len(calls) == len(names), calls
        assert not (folder / ".policy-extract.lock").exists(), "kilit kapanışta kalkmalı"


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




