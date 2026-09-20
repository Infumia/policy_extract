"""Saf fonksiyon testleri.

Çalıştırma:  python tests/test_extractor.py
(pytest varsa `pytest` ile de çalışır.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from policy_extract.extractor import (
    PolicyExtraction,
    detect_company,
    extract_policy,
    extract_policy_fast,
    file_sha256,
    find_inline_police_no,
    find_inline_zeyil_no,
    find_table_police_no,
    find_table_zeyil_no,
)

SENTETIK_TABLE = [
    ["0", "1", "2", "3"],
    ["Poliçe No", "Önceki Poliçe No", "Başlangıç Tarihi", "Bitiş Tarihi"],
    ["0001-0110-00000001", "0001-0110-00000002", "13/11/2025 12:00", "13/11/2026 12:00"],
]


def test_tireli_format_table_police_no() -> None:
    assert find_table_police_no([SENTETIK_TABLE]) == "0001-0110-00000001"


def test_bosluklu_anadolu_police_basligi() -> None:
    table = [
        ["0", "1", "2", "3"],
        ["Acente Kodu", "Poli ç e No", "Yenileme No", "Poli ç e Vadesi"],
        ["100001", "1000000001", "0", "10/12/2025 - 10/12/2026"],
        ["SBM Poli ç e No", "SBM Poli ç e No", "700000001", "700000001"],
    ]
    assert find_table_police_no([table]) == "1000000001"


def test_turkiye_sigorta_ayni_satir_key_value_tablosu() -> None:
    table = [
        [
            "Müşteri No",
            "10000000001",
            "Acente No",
            "1000001",
            "Poliçe No",
            "100000002 / 2",
            "Ek Zeyil No",
            "0",
        ],
        ["Sigorta Başlangıç-Bitiş Tarihi", "21.09.2025", "", "", "", "", "Süre (Gün)", "365"],
    ]
    assert find_table_police_no([table]) == "100000002/2"
    assert find_table_zeyil_no([table]) is None


def test_turkiye_sigorta_layout_metni() -> None:
    text = (
        "Müşteri No 10000000001 Acente No 1000001 "
        "Poliçe No 100000002 / 2 Ek Zeyil No 0\n"
        "Sigorta Başlangıç-Bitiş Tarihi 21.09.2025 - 21.09.2026"
    )
    assert find_inline_police_no(text) == "100000002/2"
    assert find_inline_zeyil_no(text) is None


def test_bozuk_turkce_glifli_pypdf_layout_metni() -> None:
    text = "M��teri No 10000000001 Acente No 1000001 Poli�e No 100000002 / 2 Ek Zeyil No 0"
    assert find_inline_police_no(text) == "100000002/2"


def test_kisa_sayi_ve_tarih_police_no_degil() -> None:
    assert find_inline_police_no("Poliçe No 21") is None
    assert find_inline_police_no("Poliçe No 40") is None
    assert find_inline_police_no("Poliçe No 21/09/2025") is None


def test_ingilizce_policy_no_bulunur() -> None:
    text = (
        "MARINE CARGO INSURANCE POLICY\n"
        "Policy No: 2000000000001 Sales Channel ID: 100001\n"
        "OPEN COVER POLICY NO 2000000000002"
    )
    assert find_inline_police_no(text) == "2000000000001"


def test_open_cover_ve_previous_policy_no_ana_police_degil() -> None:
    assert find_inline_police_no("OPEN COVER POLICY NO 2000000000002") is None
    assert find_inline_police_no("Previous Policy No 2000000000002") is None


def test_ingilizce_tabloda_policy_no_bulunur() -> None:
    table = [
        ["Policy No", "Sales Channel ID"],
        ["2000000000003", "100001"],
    ]
    assert find_table_police_no([table]) == "2000000000003"


def test_ingilizce_endorsement_no_bulunur() -> None:
    assert find_inline_zeyil_no("Policy No 2000000000003 Endorsement No 2") == "2"


def test_birlesik_police_yenileme_alaninda_sadece_police_no() -> None:
    assert find_inline_police_no("POLİÇE NO / YENİLEME NO : 1000000031 / 0") == "1000000031"
    assert find_inline_police_no("POLİÇE / YENİLEME NO : 1000000032 / 0") == "1000000032"


def test_gercek_zeyil_no_bulunur() -> None:
    assert find_inline_zeyil_no("Poliçe No 123456789 Ek Zeyil No 3") == "3"
    assert find_inline_zeyil_no("Poliçe No 123456789 Ek Belge No 2") == "2"


def test_mapfre_isveren_alt_satirdaki_police_no() -> None:
    text = (
        "                       ��VEREN MAL� MESUL�YET S�GORTA POL��ES�\n"
        "Acente No    TOBB Levha No      Poli�e No      Ek Belge No    Yenileme No\n"
        "100001      T000000-AAAA        2000000000004       0              0\n"
    )
    result = extract_policy("ornek.pdf", full_text=text)
    assert result.police_no == "2000000000004"
    assert result.zeyil_no is None


def test_allianz_yuvam() -> None:
    text = (
        "## YUVAM SİGORTA POLİÇESİ\n"
        "Sayın Örnek Müşteri, Allianz ailesine hoş geldiniz.\n"
        "0001-0110-00000001 no'lu Allianz Yuvam poliçeniz.\n"
        "ALLIANZ SİGORTA A.Ş. info@allianz.com.tr allianz.com.tr\n"
    )
    company, conf, scores = detect_company(text)
    assert company == "allianz", scores
    assert conf in ("high", "medium")
    assert scores.get("allianz", 0) >= 8


def test_gunes_kelimesi_false_positive_degil() -> None:
    # "güneşli bir günde Türkiye'nin İstanbul'unda" -> şirket bulunmamalı.
    text = (
        "Güneşli bir günde Türkiye'nin İstanbul'unda eviniz için "
        "hazırlanan bilgi notu. Poliçe No 0001-0110-00000001."
    )
    company, conf, scores = detect_company(text)
    assert company is None, scores
    assert conf == "unknown"
    assert "gunes" not in scores


def test_gunes_sigorta_unvanla_bulunur() -> None:
    text = (
        "GÜNEŞ SİGORTA A.Ş. poliçesi gunessigorta.com.tr "
        "Güneş Sigorta güvencesiyle konut poliçesi."
    )
    company, conf, scores = detect_company(text)
    assert company == "gunes", scores
    assert conf in ("high", "medium")


def test_allianz_yuvam_zeyilnamesi_sirketi_bulunur() -> None:
    text = "ALLIANZ YUVAM SİGORTA ZEYİLNAMESİ\n" * 3
    company, confidence, scores = detect_company(text)
    assert company == "allianz", scores
    assert confidence == "high"


def test_magdeburger_domain_ve_unvanla_bulunur() -> None:
    text = (
        "www.magdeburger.com.tr\n"
        "MAGDEBURGER SİGORTA A.Ş.\n"
        "GENİŞLETİLMİŞ KASKO SİGORTA POLİÇESİ"
    )
    company, confidence, scores = detect_company(text)
    assert company == "magdeburger", scores
    assert confidence == "high"


def test_turkiye_sigorta_unvanla_bulunur() -> None:
    text = (
        "TÜRKİYE SİGORTA A.Ş. turkiyesigorta.com.tr "
        "Türkiye Sigorta güvencesiyle."
    )
    company, conf, scores = detect_company(text)
    assert company == "turkiye", scores


def test_turkiye_kelimesi_tek_basina_yetmez() -> None:
    text = "Türkiye genelinde geçerli kampanya. Allianz Sigorta A.Ş. allianz.com.tr"
    company, _, scores = detect_company(text)
    # Allianz sinyalleri Türkiye'den ağır basmalı
    assert company == "allianz", scores
    assert scores.get("allianz", 0) > scores.get("turkiye", 0)


def test_cok_sayida_turkiye_adresi_sirket_sinyali_degil() -> None:
    text = (
        "Allianz ailesine hoş geldiniz. Allianz İŞYERİM. "
        "Türkiye, İstanbul; Türkiye, Kadıköy; Türkiye, Ataşehir."
    )
    company, confidence, scores = detect_company(text)
    assert company == "allianz", scores
    assert confidence in ("high", "medium")
    assert "turkiye" not in scores


def test_beraberlikte_karar_verilmez() -> None:
    text = "ALLIANZ SİGORTA A.Ş. MAPFRE SİGORTA A.Ş."
    company, conf, scores = detect_company(text)
    # İkisi de eşikte ve başabaş -> unknown
    assert company is None, scores
    assert conf == "unknown"


def test_onceki_sirket_adi_skorlanmaz() -> None:
    text = (
        "Sigorta Şirketi Unvanı TÜRKİYE SİGORTA AŞ\n"
        "Önceki Şirket Adı ALLIANZ SİGORTA A.Ş. Ruhsat No 0000011111\n"
        "Sigorta Başlangıç-Bitiş Tarihi 14.11.2025 - 14.11.2026"
    )
    company, _, scores = detect_company(text)
    assert company == "turkiye", scores
    assert "allianz" not in scores


def test_anadolu_tam_hukuki_unvan_tek_gecisla_bulunur() -> None:
    # Siber poliçe: ön yüzde okunabilir şirket adı yok, tek sinyal
    # genel şartlardaki tam unvan (header penceresi dışında bile).
    filler = "lorem ipsum dolor sit amet. " * 100
    text = (
        "TİCARİ SİBER GÜVENLİK PAKET SİGORTA POLİÇESİ\n"
        "Poliçe No 1000000041 Poliçe Vadesi 13/12/2025 - 13/12/2026\n"
        + filler
        + "Anadolu Anonim Türk Sigorta Şirketi, sigorta ettirenin "
        "beyanlarına dayanarak düzenlemiştir."
    )
    company, _, scores = detect_company(text)
    assert company == "anadolu", scores


def test_bosluksuz_birlesik_unvan_hdi() -> None:
    # pypdf layout çıkarımı boşluğu yutabilir: "HDISİGORTA A.Ş."
    text = (
        "ZORUNLU DEPREM SİGORTASI ANA POLİÇE\n"
        "Sigorta Şirketi Unvanı :HDISİGORTA A.Ş.\n"
        "Sigorta Şirketi Poliçe No :2000000000005 - T3"
    )
    company, _, scores = detect_company(text)
    assert company == "hdi", scores


def test_bosluksuz_birlesik_unvan_allianz() -> None:
    text = "Sigorta Şirketi Unvanı :ALLIANZSİGORTA A.Ş."
    company, _, scores = detect_company(text)
    assert company == "allianz", scores


def test_bozuk_glifli_onceki_sirket_adi_skorlanmaz() -> None:
    text = (
        "Sigorta Sirketi Unvani TURKIYE SIGORTA AS\n"
        "\ufffdnceki \ufffdirket Ad\ufffd ALLIANZ SIGORTA A.S. Ruhsat No 1"
    )
    company, _, scores = detect_company(text)
    assert company == "turkiye", scores
    assert "allianz" not in scores


def test_extract_policy_birlesik() -> None:
    text = (
        "## YUVAM SİGORTA POLİÇESİ\nAllianz Sigorta A.Ş. allianz.com.tr "
        "0001-0110-00000001 no'lu poliçeniz."
    )
    result = extract_policy("x.pdf", full_text=text, tables=[SENTETIK_TABLE])
    assert result.police_no == "0001-0110-00000001"
    assert result.company == "allianz"


def test_record_from_extraction() -> None:
    from policy_extract.cli import is_not_found_record, record_from_extraction

    result = PolicyExtraction(
        source_file="/tmp/klasor/sentetik.pdf",
        police_no="0001-0110-00000001",
        police_no_source="inline",
        company="allianz",
        company_confidence="high",
        company_scores={"allianz": 28},
        full_text="çok uzun metin...",
        tables=[[["a"]]],
    )
    record = record_from_extraction(result, sha256="abc123")
    assert record["file"] == "sentetik.pdf"
    assert record["sha256"] == "abc123"
    assert record["police_no"] == "0001-0110-00000001"
    assert record["company"] == "allianz"
    assert "policy_type" not in record
    assert "full_text" not in record
    assert "tables" not in record
    assert not is_not_found_record(record)


def test_not_found_metadata_kurali() -> None:
    from policy_extract.cli import is_not_found_record

    complete = {
        "police_no": "123456789",
        "company": "allianz",
    }
    assert not is_not_found_record(complete)
    for missing_field in ("police_no", "company"):
        record = complete | {missing_field: None}
        assert is_not_found_record(record), missing_field
    assert is_not_found_record({"file": "bozuk.pdf", "error": "okunamadı"})


def test_cli_file_parametresi() -> None:
    from policy_extract.cli import _input_path, build_parser

    args = build_parser().parse_args(["--file", "ornek.pdf"])
    assert _input_path(args) == Path("ornek.pdf")
    assert args.input is None


def test_fast_path_sentetik_pdf() -> None:
    pdf = Path(__file__).resolve().parents[1] / "sentetik.pdf"
    if not pdf.is_file():
        return  # örnek PDF yoksa atla (gitignore'lu)
    result = extract_policy_fast(str(pdf))
    # Gerçek veriye gömülü beklenti yok: crash'siz okuma ve kayıt şekli yeterli.
    assert result.police_no is None or isinstance(result.police_no, str)


def test_file_sha256() -> None:
    import hashlib
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(b"abc123")
        name = fh.name
    try:
        assert file_sha256(name) == hashlib.sha256(b"abc123").hexdigest()
    finally:
        Path(name).unlink(missing_ok=True)


def test_load_metadata_cache() -> None:
    import tempfile

    from policy_extract.cli import load_metadata_cache

    with tempfile.TemporaryDirectory() as tmp:
        meta = Path(tmp) / ".metadata"
        assert load_metadata_cache(meta) == {}
        meta.write_text(
            json.dumps({"file": "a.pdf", "sha256": "h1", "police_no": "1"}) + "\n"
            "bozuk satır\n"
            + json.dumps({"file": "b.pdf", "sha256": "h2"}) + "\n",
            encoding="utf-8",
        )
        cache = load_metadata_cache(meta)
        assert set(cache) == {"a.pdf", "b.pdf"}
        assert cache["a.pdf"]["sha256"] == "h1"


def test_batch_not_found_kayitlari_yeniden_hesaplanir() -> None:
    import tempfile

    import policy_extract.cli as cli_module

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        pdf_a = folder / "a.pdf"
        pdf_b = folder / "b.pdf"
        pdf_a.write_bytes(b"aaa")
        pdf_b.write_bytes(b"bbb")
        sha_a = file_sha256(str(pdf_a))
        sha_b = file_sha256(str(pdf_b))
        (folder / ".metadata").write_text(
            json.dumps(
                {"file": "a.pdf", "sha256": sha_a, "police_no": "111", "company": "allianz"}
            )
            + "\n"
            + json.dumps(
                {"file": "b.pdf", "sha256": sha_b, "police_no": None, "company": None}
            )
            + "\n",
            encoding="utf-8",
        )

        calls: list[str] = []
        orig = cli_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            calls.append(Path(pdf_path).name)
            return PolicyExtraction(
                source_file=pdf_path,
                police_no="222",
                police_no_source="inline",
                company="turkiye",
                company_confidence="medium",
                company_scores={"turkiye": 10},
            )

        cli_module.extract_policy_fast = fake  # type: ignore[method-assign]
        try:
            args = cli_module.build_parser().parse_args([str(folder)])
            assert cli_module.run_batch(args) == 0
        finally:
            cli_module.extract_policy_fast = orig  # type: ignore[method-assign]

        # Tam kayıt önbellekten geldi, not-found olan yeniden hesaplandı.
        assert calls == ["b.pdf"], calls
        lines = (folder / ".metadata").read_text(encoding="utf-8").splitlines()
        records = {json.loads(line)["file"]: json.loads(line) for line in lines}
        assert records["a.pdf"]["police_no"] == "111"
        assert records["a.pdf"]["company"] == "allianz"
        assert records["b.pdf"]["police_no"] == "222"
        assert records["b.pdf"]["company"] == "turkiye"


def test_serve_modu_argumanlari() -> None:
    from policy_extract.cli import build_parser

    args = build_parser().parse_args(["klasor", "--serve", "--poll-interval", "1.5"])
    assert args.serve is True
    assert args.poll_interval == 1.5
    args2 = build_parser().parse_args(["klasor", "--watch"])
    assert args2.serve is True


def test_servis_kuyrugu_yeni_dosyayi_alir() -> None:
    import tempfile

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        events: list[dict] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            poll_interval=10.0,  # otomatik poll kapalı gibi davransın
            stable_checks=1,
            emit=events.append,
            handle_stdin=False,
            stream_events=True,
        )
        service = WatchService(cfg)
        assert service.poll_once(reason_initial=True) == 1
        # Aynı dosya tekrar kuyruklanmaz (dedupe).
        assert service.poll_once() == 0
        (folder / "b.pdf").write_bytes(b"bbb")
        assert service.poll_once() == 1
        assert [e["file"] for e in events if e["type"] == "file_queued"] == ["a.pdf", "b.pdf"]

        orig = svc_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            return PolicyExtraction(
                source_file=pdf_path,
                police_no="P-" + Path(pdf_path).name,
                police_no_source="inline",
                company="allianz",
                company_confidence="high",
                company_scores={"allianz": 20},
            )

        svc_module.extract_policy_fast = fake  # type: ignore[method-assign]
        try:
            first = service._dequeue()
            assert first == "a.pdf"
            outcome = service.process_one(folder / first)
            assert outcome["record"]["police_no"] == "P-a.pdf"
            assert not outcome["from_cache"]
            service._persist(outcome["record"])
            # Önbellekteki tam kayıt yeniden hesaplanmaz.
            outcome2 = service.process_one(folder / first)
            assert outcome2["from_cache"]
        finally:
            svc_module.extract_policy_fast = orig  # type: ignore[method-assign]


def test_servis_sessiz_modda_stderr_yazmaz() -> None:
    import io
    import tempfile
    from contextlib import redirect_stderr

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        events: list[dict] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            poll_interval=10.0,
            stable_checks=1,
            emit=events.append,
            handle_stdin=False,
            verbose=False,
            stream_events=True,
        )
        service = WatchService(cfg)
        service.poll_once(reason_initial=True)
        buf = io.StringIO()
        orig = svc_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            return PolicyExtraction(
                source_file=pdf_path,
                police_no="P-" + Path(pdf_path).name,
                police_no_source="inline",
                company="allianz",
                company_confidence="high",
                company_scores={"allianz": 20},
            )

        svc_module.extract_policy_fast = fake  # type: ignore[method-assign]
        try:
            with redirect_stderr(buf):
                name = service._dequeue()
                assert name == "a.pdf"
                outcome = service.process_one(folder / name)
                service._persist(outcome["record"])
                service.handle_command({"cmd": "status"})
                service.handle_command({"cmd": "shutdown"})
        finally:
            svc_module.extract_policy_fast = orig  # type: ignore[method-assign]
        assert buf.getvalue() == "", repr(buf.getvalue())
        assert any(e["type"] == "file_queued" for e in events)
        assert any(e["type"] == "status" for e in events)


def test_servis_beklenmedik_hatada_yasamaya_devam_eder() -> None:
    """_persist patlasa bile (disk dolu vb.) servis çökmez, bye ile kapanır."""
    import tempfile
    import threading
    import time

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        events: list[dict] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            poll_interval=10.0,
            stable_checks=1,
            emit=events.append,
            handle_stdin=False,
            stream_events=True,
        )
        service = WatchService(cfg)

        def boom(record: dict) -> None:
            raise OSError("disk dolu (test)")

        service._persist = boom  # type: ignore[method-assign]
        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        time.sleep(2.0)
        service.stop_event.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), "servis takılı kalmamalı"
        kinds = [e["type"] for e in events]
        assert "file_error" in kinds, kinds
        assert kinds[-1] == "bye", kinds
        assert service.stats.failures >= 1


def test_servis_varsayilan_sessiz_stdout() -> None:
    """Varsayılan: ara olaylar yutulur, yalnızca komut yanıtları akar."""
    import tempfile

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        events: list[dict] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            poll_interval=10.0,
            stable_checks=1,
            emit=events.append,
            handle_stdin=False,
        )
        service = WatchService(cfg)
        service.poll_once(reason_initial=True)
        orig = svc_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            return PolicyExtraction(
                source_file=pdf_path,
                police_no="P-" + Path(pdf_path).name,
                police_no_source="inline",
                company="allianz",
                company_confidence="high",
                company_scores={"allianz": 20},
            )

        svc_module.extract_policy_fast = fake  # type: ignore[method-assign]
        try:
            name = service._dequeue()
            outcome = service.process_one(folder / name)
            service._persist(outcome["record"])
            service.handle_command({"cmd": "status"})
        finally:
            svc_module.extract_policy_fast = orig  # type: ignore[method-assign]
        kinds = [e["type"] for e in events]
        assert kinds == ["status"], kinds
        # Kayıt yine de .metadata'ya yazılır (Flutter oradan okur).
        assert (folder / ".metadata").is_file()


def test_servis_status_komutu() -> None:
    import tempfile

    from policy_extract.service import SERVICE_VERSION, ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        out: list[dict] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            emit=out.append,
            handle_stdin=False,
        )
        WatchService(cfg).handle_command({"cmd": "status"})
        assert out and out[0]["type"] == "status"
        assert out[0]["version"] == SERVICE_VERSION


def test_update_surum_karsilastirma() -> None:
    from policy_extract.updater import compare_versions

    assert compare_versions("0.3.0", "0.4.0") == -1
    assert compare_versions("0.4.0", "0.4.0") == 0
    assert compare_versions("0.4.1", "0.4.0") == 1
    assert compare_versions("0.4", "0.4.0") == 0
    assert compare_versions("1.10.0", "1.9.9") == 1


def test_update_check_fetch_install_yerel_sunucu() -> None:
    import hashlib
    import http.server
    import threading

    import policy_extract.updater as updater

    payload = b"fake-exe-icerik" * 1000
    digest = hashlib.sha256(payload).hexdigest()
    state: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/latest":
                body = json.dumps(
                    {
                        "version": "9.9.9",
                        "download_url": f"http://127.0.0.1:{state['port']}/exe",
                        "sha256": digest,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/exe":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    state["port"] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import tempfile

        info = updater.check_for_update(
            f"http://127.0.0.1:{state['port']}/latest",
            current_version="0.4.0",
            timeout=10.0,
        )
        assert info["update_available"] is True
        assert info["latest_version"] == "9.9.9"
        assert info["sha256"] == digest

        same = updater.check_for_update(
            f"http://127.0.0.1:{state['port']}/latest",
            current_version="9.9.9",
            timeout=10.0,
        )
        assert same["update_available"] is False

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "staged.exe"
            seen: list = []
            result = updater.download_update(
                info["download_url"], dest,
                expected_sha256=digest, timeout=10.0,
                progress_cb=lambda r, t: seen.append((r, t)),
            )
            assert result["verified"] is True
            assert dest.read_bytes() == payload
            assert seen, "progress callback çağrılmalı"

            # Bozuk sha -> indirilen silinir, eski sürüm korunur.
            try:
                updater.download_update(
                    info["download_url"], Path(tmp) / "bozuk.exe",
                    expected_sha256="0" * 64, timeout=10.0,
                )
            except updater.UpdateError:
                pass
            else:
                raise AssertionError("sha uyuşmazlığı hata vermeli")
            assert not (Path(tmp) / "bozuk.exe").exists()

            # Kurulum: hedef değişir, eski .bak'ta durur.
            target = Path(tmp) / "policy-extract.exe"
            target.write_bytes(b"eski-exe")
            installed = updater.install_update(dest, target)
            assert target.read_bytes() == payload
            assert installed["backup"] and Path(installed["backup"]).read_bytes() == b"eski-exe"
            assert not dest.exists(), "staging taşınmış olmalı"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_servis_ikinci_calismada_tekrar_yazmaz() -> None:
    """Önbellek isabeti dosyaya append etmez; açılıştaki çift satırlar temizlenir."""
    import tempfile
    import threading
    import time

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        digest = file_sha256(str(folder / "a.pdf"))
        record = {
            "file": "a.pdf",
            "sha256": digest,
            "police_no": "111",
            "company": "allianz",
        }
        meta = folder / ".metadata"
        meta.write_text(
            (json.dumps(record, ensure_ascii=False) + "\n") * 3, encoding="utf-8"
        )
        events: list[dict] = []
        calls: list[str] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=meta,
            not_found_path=folder / ".not-found-metadata",
            poll_interval=10.0,
            stable_checks=1,
            emit=events.append,
            handle_stdin=False,
            stream_events=True,
        )
        orig = svc_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            calls.append(pdf_path)
            return PolicyExtraction(
                source_file=pdf_path,
                police_no="999",
                police_no_source="inline",
                company="axa",
                company_confidence="high",
                company_scores={"axa": 20},
            )

        svc_module.extract_policy_fast = fake  # type: ignore[method-assign]
        try:
            service = WatchService(cfg)
            thread = threading.Thread(target=service.run, daemon=True)
            thread.start()
            deadline = time.time() + 10
            while service.stats.done < 1 and time.time() < deadline:
                time.sleep(0.05)
            service.stop_event.set()
            thread.join(timeout=10)
        finally:
            svc_module.extract_policy_fast = orig  # type: ignore[method-assign]
        assert not thread.is_alive(), "servis takılı kalmamalı"
        assert calls == [], calls  # yeniden hesaplanmadı
        assert service.stats.cached == 1
        lines = meta.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, lines  # çiftler temizlendi, yenisi eklenmedi
        assert json.loads(lines[0])["police_no"] == "111"
        assert any(e["type"] == "file_cached" for e in events)


def test_servis_kararlilik_beklemesi_sadece_taze_dosyada() -> None:
    import os
    import tempfile
    import time

    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        pdf = folder / "a.pdf"
        pdf.write_bytes(b"aaa")
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            emit=lambda e: None,
            handle_stdin=False,
        )
        service = WatchService(cfg)
        assert service._needs_stability_wait(pdf) is True  # taze dosya bekler
        old = time.time() - 3600
        os.utime(pdf, (old, old))
        assert service._needs_stability_wait(pdf) is False  # eski dosya beklemez


def test_persist_ayni_kaydi_tekrar_yazmaz() -> None:
    import tempfile

    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            emit=lambda e: None,
            handle_stdin=False,
        )
        service = WatchService(cfg)
        service.stats.done = 1  # done=0 iken her persist compact tetikler, onu ezme
        record = {"file": "a.pdf", "sha256": "h", "police_no": "111", "company": "x"}
        assert service._persist(record) is True
        assert service._persist(dict(record)) is False  # aynı içerik: yazma yok
        assert service._persist(record | {"police_no": "222"}) is True
        lines = (folder / ".metadata").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2, lines


def test_klasor_kilidi() -> None:
    import tempfile

    import policy_extract.service as svc_module
    from policy_extract.service import (
        acquire_folder_lock,
        release_folder_lock,
    )

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        lock = acquire_folder_lock(folder)
        assert lock is not None and lock.is_file()
        # Bayat kilit (ölü pid) devralınır.
        lock.write_text("999999999\n", encoding="utf-8")
        lock2 = acquire_folder_lock(folder)
        assert lock2 is not None
        release_folder_lock(lock2)
        assert not lock2.exists()
        # Canlı pid devralınamaz (başka prosesin kilidi).
        lock3 = acquire_folder_lock(folder)
        assert lock3 is not None
        lock3.write_text("424242\n", encoding="utf-8")
        orig = svc_module.parent_alive
        svc_module.parent_alive = lambda pid: True  # type: ignore[assignment]
        try:
            assert acquire_folder_lock(folder) is None
        finally:
            svc_module.parent_alive = orig  # type: ignore[method-assign]
        release_folder_lock(lock3)


def test_not_found_ikinci_turda_tekrar_eklenmez() -> None:
    """Kullanıcı senaryosu: eksik kayıt her serve'da .not-found'a çiftlenmemeli."""
    import tempfile
    import threading
    import time

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        calls: list[str] = []
        orig = svc_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            calls.append(pdf_path)
            return PolicyExtraction(
                source_file=pdf_path,
                police_no=None,  # eksik -> not-found, her tur yeniden hesaplanır
                police_no_source=None,
                company="allianz",
                company_confidence="high",
                company_scores={"allianz": 20},
            )

        svc_module.extract_policy_fast = fake  # type: ignore[method-assign]
        try:
            for _ in (1, 2):
                events: list[dict] = []
                cfg = ServiceConfig(
                    folder=folder,
                    meta_path=folder / ".metadata",
                    not_found_path=folder / ".not-found-metadata",
                    poll_interval=10.0,
                    stable_checks=1,
                    emit=events.append,
                    handle_stdin=False,
                )
                service = WatchService(cfg)
                thread = threading.Thread(target=service.run, daemon=True)
                thread.start()
                deadline = time.time() + 10
                while service.stats.done < 1 and time.time() < deadline:
                    time.sleep(0.05)
                service.stop_event.set()
                thread.join(timeout=10)
                assert not thread.is_alive()
        finally:
            svc_module.extract_policy_fast = orig  # type: ignore[method-assign]
        assert len(calls) == 2, calls  # retry çalıştı (yeniden hesaplandı)...
        meta_lines = (folder / ".metadata").read_text(encoding="utf-8").splitlines()
        nf_lines = (folder / ".not-found-metadata").read_text(encoding="utf-8").splitlines()
        assert len(meta_lines) == 1, meta_lines  # ...ama dosyalara eklenmedi
        assert len(nf_lines) == 1, nf_lines


def test_sinyal_kurulumu_ana_thread_disinda_sessiz() -> None:
    import tempfile
    import threading

    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            emit=lambda e: None,
            handle_stdin=False,
        )
        errors: list[BaseException] = []
        thread = threading.Thread(target=lambda: _run(errors, cfg))
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert errors == []


def _run(errors: list[BaseException], cfg) -> None:  # type: ignore[no-untyped-def]
    from policy_extract.service import WatchService

    try:
        WatchService(cfg)._install_signal_handlers()  # ValueError vermemeli
    except BaseException as exc:  # noqa: BLE001
        errors.append(exc)


def test_servis_calisirken_gelen_dosyayi_isler() -> None:
    """70bin dosya senaryosunun kalbi: tarama sürerken düşen dosya kaybolmaz."""
    import tempfile
    import threading
    import time

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        calls: list[str] = []
        orig = svc_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            name = Path(pdf_path).name
            if name == "a.pdf":
                # a bitmeden b düşsün diye bekle (10 sn tavan, takılma yok).
                deadline = time.time() + 10
                while not (folder / "b.pdf").exists() and time.time() < deadline:
                    time.sleep(0.05)
            calls.append(name)
            return PolicyExtraction(
                source_file=pdf_path,
                police_no="P-" + name,
                police_no_source="inline",
                company="allianz",
                company_confidence="high",
                company_scores={"allianz": 20},
            )

        events: list[dict] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            poll_interval=0.2,
            stable_checks=1,
            stable_interval_ms=10,
            emit=events.append,
            handle_stdin=False,
            stream_events=True,
        )
        svc_module.extract_policy_fast = fake  # type: ignore[method-assign]
        try:
            service = WatchService(cfg)
            thread = threading.Thread(target=service.run, daemon=True)
            thread.start()
            time.sleep(0.5)  # a işlemdeyken...
            (folder / "b.pdf").write_bytes(b"bbb")  # ...b klasöre düşer
            deadline = time.time() + 20
            while service.stats.done < 2 and time.time() < deadline:
                time.sleep(0.05)
            service.stop_event.set()
            thread.join(timeout=10)
        finally:
            svc_module.extract_policy_fast = orig  # type: ignore[method-assign]
        assert not thread.is_alive()
        assert calls[0] == "a.pdf" and sorted(calls) == ["a.pdf", "b.pdf"], calls
        created = [e for e in events if e["type"] == "file_queued" and e["file"] == "b.pdf"]
        assert created and created[0]["reason"] == "created", events
        kinds = [e["type"] for e in events]
        assert kinds[-1] == "bye" and kinds[-1] is not None
        bye = events[-1]
        assert bye["persist_error"] is None
        assert not (folder / ".policy-extract.lock").exists(), "kilit kapanışta kalkmalı"


def test_servis_kuyruktayken_silinen_dosyayi_atlar() -> None:
    """Kuyruktaki dosya okunmadan silinirse: kayıt yok, sayaç yok, çökme yok."""
    import tempfile
    import threading
    import time

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        events: list[dict] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            poll_interval=10.0,
            stable_checks=1,
            emit=events.append,
            handle_stdin=False,
            stream_events=True,
        )
        orig = svc_module.extract_policy_fast
        calls: list[str] = []
        svc_module.extract_policy_fast = lambda p, **k: calls.append(p) or None  # type: ignore[assignment]
        try:
            service = WatchService(cfg)
            service.paused.set()  # kuyruk dolsun ama kimse okumasın
            thread = threading.Thread(target=service.run, daemon=True)
            thread.start()
            time.sleep(0.5)
            (folder / "a.pdf").unlink()  # okunmadan silindi
            service.paused.clear()
            deadline = time.time() + 10
            while "idle" not in [e["type"] for e in events] and time.time() < deadline:
                time.sleep(0.05)
            service.stop_event.set()
            thread.join(timeout=10)
        finally:
            svc_module.extract_policy_fast = orig  # type: ignore[method-assign]
        assert not thread.is_alive()
        assert calls == [], calls
        assert service.stats.done == 0
        assert not (folder / ".metadata").exists(), ".metadata yazılmamalı"


def test_servis_degisen_dosyayi_yeniden_hesaplar() -> None:
    """sha değişen dosya yeniden okunur, .metadata güncellenir, satır tek kalır."""
    import tempfile
    import threading
    import time

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        calls: list[str] = []
        orig = svc_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            calls.append(Path(pdf_path).name)
            tag = Path(pdf_path).read_bytes().hex()[:8]
            return PolicyExtraction(
                source_file=pdf_path,
                police_no=tag,
                police_no_source="inline",
                company="allianz",
                company_confidence="high",
                company_scores={"allianz": 20},
            )

        svc_module.extract_policy_fast = fake  # type: ignore[method-assign]
        try:
            for _ in (1, 2):
                cfg = ServiceConfig(
                    folder=folder,
                    meta_path=folder / ".metadata",
                    not_found_path=folder / ".not-found-metadata",
                    poll_interval=10.0,
                    stable_checks=1,
                    emit=lambda e: None,
                    handle_stdin=False,
                )
                service = WatchService(cfg)
                thread = threading.Thread(target=service.run, daemon=True)
                thread.start()
                deadline = time.time() + 10
                while service.stats.done < 1 and time.time() < deadline:
                    time.sleep(0.05)
                service.stop_event.set()
                thread.join(timeout=10)
                assert not thread.is_alive()
                if len(calls) == 1:
                    (folder / "a.pdf").write_bytes(b"bbbb")  # turlar arası değişti
        finally:
            svc_module.extract_policy_fast = orig  # type: ignore[method-assign]
        assert calls == ["a.pdf", "a.pdf"], calls
        lines = (folder / ".metadata").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, lines
        assert json.loads(lines[0])["police_no"] == "62626262"


def test_servis_pause_resume_ve_status() -> None:
    """Duraklatınca sayaç kıpırdamaz, devam edince işler; status bayrağı doğru."""
    import tempfile
    import threading
    import time

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        events: list[dict] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            poll_interval=10.0,
            stable_checks=1,
            emit=events.append,
            handle_stdin=False,
        )
        orig = svc_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            return PolicyExtraction(
                source_file=pdf_path,
                police_no="P-a",
                police_no_source="inline",
                company="allianz",
                company_confidence="high",
                company_scores={"allianz": 20},
            )

        svc_module.extract_policy_fast = fake  # type: ignore[method-assign]
        try:
            service = WatchService(cfg)
            service.paused.set()
            thread = threading.Thread(target=service.run, daemon=True)
            thread.start()
            time.sleep(0.6)
            assert service.stats.done == 0, "duraklatılmışken işlememeli"
            service.handle_command({"cmd": "status"})
            assert events[-1]["type"] == "status" and events[-1]["paused"] is True
            service.handle_command({"cmd": "resume"})
            deadline = time.time() + 10
            while service.stats.done < 1 and time.time() < deadline:
                time.sleep(0.05)
            service.handle_command({"cmd": "status"})
            assert events[-1]["paused"] is False
            service.handle_command({"cmd": "rescan"})
            assert events[-1]["type"] == "rescanned"
            service.stop_event.set()
            thread.join(timeout=10)
        finally:
            svc_module.extract_policy_fast = orig  # type: ignore[method-assign]
        assert not thread.is_alive()
        assert service.stats.done == 1


def test_servis_bozuk_pdf_cokertmez() -> None:
    """Gerçek extractor çöp baytta patlamaz: null kayıt, hatasız dönüş."""
    import tempfile

    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "bozuk.pdf").write_bytes(b"bu bir pdf degil %%%%")
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            emit=lambda e: None,
            handle_stdin=False,
        )
        outcome = WatchService(cfg).process_one(folder / "bozuk.pdf")
        assert outcome["from_cache"] is False
        assert "error" not in outcome["record"]
        assert outcome["record"]["police_no"] is None
        assert outcome["record"]["company"] is None


def test_servis_dosya_turleri_ve_turkce_isim() -> None:
    """txt görmezden gelinir, .PDF (büyük) ve Türkçe isim kuyruğa girer."""
    import tempfile

    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "not.txt").write_bytes(b"metin")
        (folder / "BUYUK.PDF").write_bytes(b"aaa")
        tr_name = "Ğüşiöç_Test_Dosyası_(örnek).pdf"
        (folder / tr_name).write_bytes(b"bbb")
        events: list[dict] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            emit=events.append,
            handle_stdin=False,
            stream_events=True,
        )
        service = WatchService(cfg)
        assert service.poll_once(reason_initial=True) == 2
        queued = sorted(e["file"] for e in events if e["type"] == "file_queued")
        assert queued == ["BUYUK.PDF", tr_name], queued
        # Türkçe isim metadata'ya bozulmadan yazar ve geri okunur.
        service._persist(
            {"file": tr_name, "sha256": "h", "police_no": "111", "company": "x"}
        )
        from policy_extract.cli import load_metadata_cache

        assert load_metadata_cache(folder / ".metadata")[tr_name]["police_no"] == "111"


def test_metadata_cache_cift_satirda_son_kazanir() -> None:
    import tempfile

    from policy_extract.cli import load_metadata_cache

    with tempfile.TemporaryDirectory() as tmp:
        meta = Path(tmp) / ".metadata"
        meta.write_text(
            json.dumps({"file": "a.pdf", "police_no": "111"}) + "\n"
            + json.dumps({"file": "a.pdf", "police_no": "222"}) + "\n",
            encoding="utf-8",
        )
        assert load_metadata_cache(meta)["a.pdf"]["police_no"] == "222"


def test_compact_if_needed_ciftleri_temizler() -> None:
    import tempfile

    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        meta = folder / ".metadata"
        record = {"file": "a.pdf", "sha256": "h", "police_no": "111", "company": "x"}
        meta.write_text(
            (json.dumps(record, ensure_ascii=False) + "\n") * 3, encoding="utf-8"
        )
        cfg = ServiceConfig(
            folder=folder,
            meta_path=meta,
            not_found_path=folder / ".not-found-metadata",
            emit=lambda e: None,
            handle_stdin=False,
        )
        service = WatchService(cfg)
        service.cache = {"a.pdf": record}
        service._compact_if_needed()
        assert len(meta.read_text(encoding="utf-8").splitlines()) == 1


def test_buyuyen_dosya_bitmeden_islenmez() -> None:
    """Kopyalanmakta olan dosya: boyut durmadan extractor çağrılmaz."""
    import tempfile
    import threading
    import time

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        pdf = folder / "a.pdf"
        pdf.write_bytes(b"0" * 1024)
        stop_writing = threading.Event()
        calls: list[float] = []
        t0 = time.perf_counter()

        def writer() -> None:
            for _ in range(8):  # ~640ms boyunca büyüt
                if stop_writing.is_set():
                    return
                with pdf.open("ab") as fh:
                    fh.write(b"1" * 1024)
                time.sleep(0.08)

        orig = svc_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
            calls.append(time.perf_counter() - t0)
            return PolicyExtraction(
                source_file=pdf_path,
                police_no="P-a",
                police_no_source="inline",
                company="allianz",
                company_confidence="high",
                company_scores={"allianz": 20},
            )

        events: list[dict] = []
        cfg = ServiceConfig(
            folder=folder,
            meta_path=folder / ".metadata",
            not_found_path=folder / ".not-found-metadata",
            poll_interval=10.0,
            stable_checks=3,
            stable_interval_ms=120,
            stable_grace_secs=3600.0,  # taze dosya: bekleme aktif olsun
            emit=events.append,
            handle_stdin=False,
        )
        svc_module.extract_policy_fast = fake  # type: ignore[method-assign]
        w = threading.Thread(target=writer, daemon=True)
        try:
            service = WatchService(cfg)
            thread = threading.Thread(target=service.run, daemon=True)
            thread.start()
            w.start()
            deadline = time.time() + 15
            while service.stats.done < 1 and time.time() < deadline:
                time.sleep(0.05)
            service.stop_event.set()
            thread.join(timeout=10)
        finally:
            stop_writing.set()
            svc_module.extract_policy_fast = orig  # type: ignore[method-assign]
        w.join(timeout=5)
        assert not thread.is_alive()
        assert len(calls) == 1, calls
        assert calls[0] >= 0.6, calls  # büyüme durmadan (~0.64sn) okunmadı


def test_update_hata_yollari() -> None:
    import http.server
    import tempfile
    import threading

    import policy_extract.updater as updater

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/bad":
                body = b"json degil bu"
            elif self.path == "/noval":
                body = b"{}"
            elif self.path == "/empty":
                body = b""
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        for path in ("/bad", "/noval"):
            try:
                updater.check_for_update(base + path, timeout=5.0)
            except updater.UpdateError:
                pass
            else:
                raise AssertionError(f"{path} hata vermeli")
        try:
            updater.download_update(base + "/empty", Path(tempfile.gettempdir()) / "bos.exe", timeout=5.0)
        except updater.UpdateError:
            pass
        else:
            raise AssertionError("boş indirme hata vermeli")
        try:
            updater.check_for_update("http://127.0.0.1:1/kapali", timeout=3.0)
        except updater.UpdateError:
            pass
        else:
            raise AssertionError("ulaşılamaz API hata vermeli")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                updater.install_update(Path(tmp) / "yok.exe", Path(tmp) / "hedef.exe")
            except updater.UpdateError:
                pass
            else:
                raise AssertionError("kayıp staging hata vermeli")
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _realdata_dir() -> Path | None:
    """Gerçek veri klasörü (env) yoksa None döner; test SKIP eder."""
    import os

    raw = os.environ.get("POLICY_EXTRACT_TESTDATA_DIR", "").strip().strip('"')
    if not raw:
        return None
    folder = Path(raw)
    return folder if folder.is_dir() else None


def _realdata_sample(folder: Path, max_n: int = 20) -> list[Path]:
    """Klasöre dokunmadan, temsili stride örneklemesi (sıralı, deterministik)."""
    import os

    try:
        max_n = int(os.environ.get("POLICY_EXTRACT_TESTDATA_MAX", str(max_n)))
    except ValueError:
        pass
    pdfs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    if max_n > 0 and len(pdfs) > max_n:
        step = max(1, len(pdfs) // max_n)
        pdfs = pdfs[::step][:max_n]
    return pdfs


def _assert_record_wellformed(pdf: Path, record: dict) -> None:
    assert record["file"] == pdf.name
    assert record["sha256"] == file_sha256(str(pdf)), "sha256 kayıttakiyle tutmalı"
    if record.get("police_no") is not None:
        assert record.get("police_no_source") in ("inline", "table"), record
    else:
        assert record.get("police_no_source") is None, record
    if record.get("company") is not None:
        assert record.get("company_confidence") in ("high", "medium", "low"), record
        scores = record.get("company_scores") or {}
        assert scores.get(record["company"]), record
    else:
        assert record.get("company_confidence") == "unknown", record


def test_realdata_records_wellformed_and_deterministic() -> None:
    """Gerçek PDF'ler: crash yok, kayıt şekli tutarlı, iki okuma aynı sonuç."""
    from collections import Counter

    from policy_extract.cli import record_from_extraction
    from policy_extract.extractor import extract_policy_fast

    folder = _realdata_dir()
    if folder is None:
        print("SKIP real-data (POLICY_EXTRACT_TESTDATA_DIR tanımsız)")
        return
    pdfs = _realdata_sample(folder)
    if not pdfs:
        print(f"SKIP real-data ({folder} içinde PDF yok)")
        return
    failures: list[str] = []
    stats = Counter()
    for pdf in pdfs:
        try:
            first = extract_policy_fast(str(pdf))
            second = extract_policy_fast(str(pdf))
        except Exception as exc:  # noqa: BLE001 — hangisi patlarsa raporla
            failures.append(f"{pdf.name}: {exc!r}")
            continue
        assert (first.police_no, first.company, first.zeyil_no) == (
            second.police_no, second.company, second.zeyil_no,
        ), f"deterministik değil: {pdf.name}"
        record = record_from_extraction(first, sha256=file_sha256(str(pdf)))
        _assert_record_wellformed(pdf, record)
        stats["police_no" if record["police_no"] else "no_police_no"] += 1
        stats["company" if record["company"] else "no_company"] += 1
    assert not failures, f"{len(failures)} dosya patladı: {failures[:5]}"
    total = sum(1 for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    print(
        f"real-data: {len(pdfs)} dosya (klasörde {total} PDF) | "
        f"police_no={stats['police_no']} company={stats['company']}"
    )


def test_realdata_serve_twice_no_dupes() -> None:
    """Gerçek dosyalar temp kopyada: iki serve sonrası satır sayısı sabit."""
    import shutil
    import tempfile
    import threading
    import time

    import policy_extract.service as svc_module
    from policy_extract.service import ServiceConfig, WatchService

    folder = _realdata_dir()
    if folder is None:
        print("SKIP real-data (POLICY_EXTRACT_TESTDATA_DIR tanımsız)")
        return
    pdfs = _realdata_sample(folder)
    if not pdfs:
        print(f"SKIP real-data ({folder} içinde PDF yok)")
        return
    orig = svc_module.extract_policy_fast
    calls: list[str] = []
    real_extract = orig

    def counting(pdf_path: str, *, max_pages: int = 7):  # type: ignore[no-untyped-def]
        calls.append(Path(pdf_path).name)
        return real_extract(pdf_path, max_pages=max_pages)

    svc_module.extract_policy_fast = counting  # type: ignore[method-assign]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            for pdf in pdfs:
                shutil.copy2(pdf, work / pdf.name)  # kaynak klasöre yazılmaz
            counts: list[tuple[int, int]] = []
            for _ in (1, 2):
                cfg = ServiceConfig(
                    folder=work,
                    meta_path=work / ".metadata",
                    not_found_path=work / ".not-found-metadata",
                    poll_interval=0.3,
                    stable_checks=1,
                    stable_interval_ms=10,
                    emit=lambda e: None,
                    handle_stdin=False,
                )
                service = WatchService(cfg)
                thread = threading.Thread(target=service.run, daemon=True)
                thread.start()
                deadline = time.time() + max(180.0, len(pdfs) * 15.0)
                while service.stats.done < len(pdfs) and time.time() < deadline:
                    time.sleep(0.1)
                service.stop_event.set()
                thread.join(timeout=30)
                assert not thread.is_alive()
                assert service.stats.done == len(pdfs), service.stats
                meta_lines = (work / ".metadata").read_text(encoding="utf-8").splitlines()
                nf_path = work / ".not-found-metadata"
                nf_lines = nf_path.read_text(encoding="utf-8").splitlines() if nf_path.is_file() else []
                counts.append((len(meta_lines), len(nf_lines)))
            assert counts[0][0] == len(pdfs), counts
            assert counts[1] == counts[0], f"ikinci tur yazmamalı: {counts}"
            assert len(calls) >= len(pdfs), calls  # ilk tur hepsini okudu
            second_run_calls = len(calls) - len(pdfs)
            print(
                f"real-data serve: {len(pdfs)} dosya | 2. tur ek okuma={second_run_calls} "
                f"(not-found retry'ler) | meta={counts[1][0]} notfound={counts[1][1]}"
            )
    finally:
        svc_module.extract_policy_fast = orig  # type: ignore[method-assign]


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
