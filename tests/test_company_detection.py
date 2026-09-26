"""Şirket (sigortacı) skorlaması uç durum testleri.

Çalıştırma:  python tests/test_company_detection.py
(pytest varsa `pytest tests/test_company_detection.py` ile de çalışır.)
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "src"))
sys.path.insert(0, str(_HERE.parent))

from policy_extract.extractor import (  # noqa: E402
    _COMPANY_THRESHOLD,
    _GENERIC_BRANDS,
    _HEADER_WINDOW,
    COMPANY_SPECS,
    detect_company,
)

_HEADER_FILLER = "x " * 800  # 1600 karakter: header penceresini (1500) taşar


def _company(text: str) -> tuple[str | None, str, dict[str, int]]:
    return detect_company(text)


# ---------------------------------------------------------------------------
# Boş / anlamsız metinler
# ---------------------------------------------------------------------------


def test_bos_ve_anlamsiz_metin_sirket_uretmez() -> None:
    for text in [
        "",
        "   ",
        "Sigorta poliçesi genel şartlar ve teminatlar listesi.",
        "Beyaz Sigorta A.Ş. adına düzenlenmiştir.",
        "Ankara ilinde ikamet eden sigortalı.",
    ]:
        company, confidence, scores = _company(text)
        assert company is None, (text, scores)
        assert confidence == "unknown", (text, confidence)


def test_genel_marka_kelimeleri_tek_basina_yetmez() -> None:
    # "gunes", "turkiye", "ray" gibi günlük kelimeler marka olarak düşük puan
    # alır; unvan/domain olmadan eşiği geçemez.
    for text in ["güneş", "türkiye", "ray", "Sompo", "axa", "allianz", "neova"]:
        company, confidence, scores = _company(text)
        assert company is None, (text, scores)
        assert confidence == "unknown"
        for key, value in scores.items():
            assert value < _COMPANY_THRESHOLD, (key, value)


def test_genel_marka_listesi_kapsami() -> None:
    for key in ("gunes", "turkiye", "anadolu", "ankara", "quick", "ray"):
        assert key in _GENERIC_BRANDS


# ---------------------------------------------------------------------------
# Eşik ve güven seviyeleri
# ---------------------------------------------------------------------------


def test_esik_sinirinda_orta_guven() -> None:
    # Unvan + header bonusu tam 8 puan: eşiği geçer ama "medium" kalır.
    for text, key in [("Güneş Sigorta", "gunes"), ("Türkiye Sigorta", "turkiye")]:
        company, confidence, scores = _company(text)
        assert company == key, (text, scores)
        assert scores[key] == _COMPANY_THRESHOLD, (text, scores)
        assert confidence == "medium", (text, confidence)


def test_yuksek_guven_domain_ve_unvan_birlikte() -> None:
    for text, key in [
        ("Allianz Sigorta A.Ş. allianz.com.tr", "allianz"),
        ("Türkiye Sigorta A.Ş. turkiyesigorta.com.tr", "turkiye"),
        ("Neova Sigorta neova.com.tr", "neova"),
        ("Zurich Sigorta zurich.com.tr", "zurich"),
        ("Groupama Sigorta groupama.com.tr", "groupama"),
    ]:
        company, confidence, scores = _company(text)
        assert company == key, (text, scores)
        assert confidence == "high", (text, confidence, scores)


def test_domain_ve_unvan_tek_basina_yetmez_durumlar() -> None:
    # Domain + marka + header bonusu 18 puan: "high" için yeter.
    company, confidence, scores = _company("allianz.com.tr")
    assert company == "allianz"
    assert scores["allianz"] == 18  # domain 10 + header 4 + marka 2 + header 2
    assert confidence == "high"

    # Unvan + marka + header bonusu 12 puan: "medium"da kalır.
    company, confidence, scores = _company("ALLIANZ SİGORTA A.Ş.")
    assert (company, confidence) == ("allianz", "medium")
    assert scores["allianz"] == 12


def test_unvan_boşluksuz_yazilabilir() -> None:
    for text, key in [
        ("Sigorta Şirketi Unvanı :HDISİGORTA A.Ş.", "hdi"),
        ("Sigorta Şirketi Unvanı :ALLIANZSİGORTA A.Ş.", "allianz"),
        ("Sigorta Şirketi Unvanı :ANADOLUSİGORTA A.Ş.", "anadolu"),
        ("Sigorta Şirketi Unvanı :QUICKSİGORTA A.Ş.", "quick"),
    ]:
        company, _, scores = _company(text)
        assert company == key, (text, scores)


def test_anadolu_tam_hukuki_unvan_dusuk_guvenle_bulunur() -> None:
    # Tek sinyal: genel şartlardaki tam hukuki unvan (header penceresi dışında).
    company, confidence, scores = _company(_HEADER_FILLER + "Anadolu Anonim Türk Sigorta Şirketi")
    assert company == "anadolu", scores
    assert confidence == "medium"  # header bonusu yok: sadece unvan + marka
    assert scores["anadolu"] >= _COMPANY_THRESHOLD


# ---------------------------------------------------------------------------
# Beraberlik / marj kuralları
# ---------------------------------------------------------------------------


def test_beraberlikte_karar_verilmez() -> None:
    for text in [
        "ALLIANZ SİGORTA A.Ş. MAPFRE SİGORTA A.Ş.",
        "AXA SİGORTA A.Ş. HDI SİGORTA A.Ş.",
        "allianz sigorta mapfre sigorta",
    ]:
        company, confidence, scores = _company(text)
        assert company is None, (text, scores)
        assert confidence == "unknown"
        assert len(set(scores.values())) == 1 and len(scores) >= 2, (text, scores)


def test_marj_yeterliyse_kazanan_secilir() -> None:
    company, confidence, scores = _company("mapfre.com.tr ALLIANZ SİGORTA A.Ş.")
    assert company == "mapfre", scores
    assert scores["mapfre"] - scores["allianz"] > 3
    assert confidence == "high"


def test_esik_alti_rakip_engellemez() -> None:
    # allianz sadece marka olarak (4 puan) geçer; ak unvanıyla 8 puan alır.
    company, confidence, scores = _company("AK SİGORTA A.Ş. allianz")
    assert company == "ak", scores
    assert scores["allianz"] < _COMPANY_THRESHOLD
    assert confidence == "medium"


# ---------------------------------------------------------------------------
# Header penceresi ve puan tavanları
# ---------------------------------------------------------------------------


def test_header_penceresi_disinda_bonus_yok() -> None:
    # Aynı metin iki kez: biri pencere içinde, diğeri 1600+ karakter sonra.
    in_window = _company("Allianz Sigorta A.Ş. allianz.com.tr")[2]["allianz"]
    out_window = _company(_HEADER_FILLER + "Allianz Sigorta A.Ş. allianz.com.tr")[2]["allianz"]
    assert in_window - out_window > 0, (in_window, out_window)
    assert out_window == 18  # domain 10 + unvan 5 + marka 3 (bonuslar yok)


def test_header_penceresi_esik_altinda_kalirsa_sirket_yok() -> None:
    company, confidence, scores = _company(_HEADER_FILLER + "ANADOLU SİGORTA A.Ş.")
    assert company is None, scores
    assert confidence == "unknown"
    assert scores["anadolu"] < _COMPANY_THRESHOLD


def test_puan_tekrar_tavanlari() -> None:
    # Domain tekrarı en fazla +3, marka tekrarı en fazla +2 ekler.
    _, _, scores = _company("allianz.com.tr " * 5)
    assert scores["allianz"] == 23, scores  # 10+3 domain, 4 marka, 4+2 header
    _, _, scores = _company("allianz sigorta " * 5)
    assert scores["allianz"] == 16, scores  # unvan 5+2, marka 2+2, header 3+2
    _, _, scores = _company("allianz " * 5)
    assert scores["allianz"] == 6, scores  # marka 2+2, header 2 -> eşik altı


def test_domain_katalogu_tutarliligi() -> None:
    keys = [spec.key for spec in COMPANY_SPECS]
    assert len(keys) == len(set(keys)), keys
    for spec in COMPANY_SPECS:
        assert spec.domains, spec.key
        for domain in spec.domains:
            assert domain == domain.lower() and "." in domain, (spec.key, domain)
        for phrase in spec.titles + spec.brands:
            assert phrase == phrase.strip().lower(), (spec.key, phrase)
            assert phrase, spec.key


# ---------------------------------------------------------------------------
# Önceki şirket bastırma kuralı
# ---------------------------------------------------------------------------


def test_onceki_sirket_satirlari_skorlanmaz() -> None:
    for text in [
        "Önceki Sigorta Şirketi: ALLIANZ SİGORTA A.Ş.",
        "\ufffdnceki \ufffdirket Ad\ufffd ALLIANZ SIGORTA A.S.",
        "Previous Insurer: ALLIANZ SIGORTA A.S.",
        "Prior Insurer: mapfre.com.tr",
        "Old Company: axa.com.tr",
        "Eski Sigorta Şirketi: allianz.com.tr",
    ]:
        company, confidence, scores = _company(text)
        assert company is None, (text, scores)
        assert confidence == "unknown"


def test_bastirma_satir_sonunda_biter() -> None:
    # Bir sonraki satırdaki gerçek sigortacı bastırmadan etkilenmez.
    text = "Önceki Sigorta Şirketi: ALLIANZ SİGORTA A.Ş.\nAXA Sigorta A.Ş. axa.com.tr"
    company, confidence, scores = _company(text)
    assert company == "axa", scores
    assert confidence == "high"
    assert "allianz" not in scores, scores


def test_mevcut_sigortaci_bastirilmaz() -> None:
    for text in [
        "Sigorta Şirketi Unvanı AXA Sigorta A.Ş.",
        "Sigortacı: Neova Sigorta A.Ş. neova.com.tr",
        "İşveren sigorta şirketi MAPFRE SİGORTA A.Ş. mapfre.com.tr",
    ]:
        company, _, scores = _company(text)
        assert company is not None, (text, scores)


# ---------------------------------------------------------------------------
# Determinizm ve kapsam
# ---------------------------------------------------------------------------


def test_ayni_metin_ayni_karari_verir() -> None:
    texts = [
        "Allianz Sigorta A.Ş. allianz.com.tr",
        "AXA SİGORTA A.Ş. HDI SİGORTA A.Ş.",
        _HEADER_FILLER + "Anadolu Anonim Türk Sigorta Şirketi",
    ]
    for text in texts:
        first = _company(text)
        second = _company(text)
        assert first == second, text


def test_her_sirket_kendi_domainiyle_bulunur() -> None:
    for spec in COMPANY_SPECS:
        for domain in spec.domains:
            company, confidence, scores = _company(f"{domain}")
            assert company == spec.key, (domain, scores)
            assert confidence in ("high", "medium"), (domain, confidence)


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

