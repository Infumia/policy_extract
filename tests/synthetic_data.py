"""Sentetik test verisi üreticileri (PDF + metin + tablo katalogları).

Test dosyaları bu modülden beslenir; testler gerçek poliçe PDF'ine muhtaç
değildir. Modül bilerek `policy_extract`'ten bağımsız: yalnızca veri üretir.

İçerik:
- `build_pdf_bytes` / `write_pdf`: stdlib ile üretilen, pypdf'in gerçekten
  okuyabildiği minimal PDF'ler (1..N sayfa, satır satır metin).
- `synthetic_policy_texts`: elle kurulmuş 15 sentetik poliçe metni.
- `synthetic_pdf_pages`: PDF'e yazılabilecek ASCII sayfa metinleri
  (WinAnsiEncoding sınırı nedeniyle yalnızca Latin-1 karakterler).
- `synthetic_tables`: farklı düzenlerde tablo örnekleri.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal PDF üretici
# ---------------------------------------------------------------------------
# Gerçek PDF yazıcı (reportlab vb.) bağımlılığı yok: catalog/pages/font/page/
# content nesneleri elle kurulur, xref tablosu byte offset'lerle yazılır.
# pypdf bu dosyaları normal PDF gibi okur ve layout modunda metni çıkarır.


def _escape_pdf_text(value: str) -> bytes:
    """PDF literal string kaçışları; karakterler WinAnsi (cp1252) ile yazılır."""
    raw = value.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _content_stream(lines: list[str], *, font_size: int, leading: int) -> bytes:
    parts = [
        b"BT\n",
        f"/F1 {font_size} Tf\n".encode("ascii"),
        f"{leading} TL\n".encode("ascii"),
        b"72 780 Td\n",
    ]
    for index, line in enumerate(lines):
        if index:
            parts.append(b"T*\n")  # bir sonraki satıra geç (leading = TL)
        parts.append(b"(" + _escape_pdf_text(line) + b") Tj\n")
    parts.append(b"ET\n")
    body = b"".join(parts)
    return f"<< /Length {len(body)} >>\nstream\n".encode("ascii") + body + b"endstream"


def build_pdf_bytes(pages: list[str], *, font_size: int = 12, leading: int = 18) -> bytes:
    """Sayfa başına bir metin bloğu yazan geçerli bir PDF üretir.

    `pages=[""]` -> tek sayfalık, içinde metin olmayan PDF.
    """
    if not pages:
        pages = [""]
    page_nums = [4 + 2 * index for index in range(len(pages))]
    content_nums = [5 + 2 * index for index in range(len(pages))]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{num} 0 R" for num in page_nums)
            + f"] /Count {len(pages)} >>"
        ).encode("ascii"),
        (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"
        ),
    ]
    for index, text in enumerate(pages):
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_nums[index]} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            _content_stream(text.splitlines(), font_size=font_size, leading=leading)
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_pos = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def write_pdf(path: str | Path, pages: list[str]) -> Path:
    """`build_pdf_bytes` çıktısını diske yazar (klasörü gerekirse açar)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_pdf_bytes(pages))
    return target


# ---------------------------------------------------------------------------
# Sentetik poliçe metinleri (etiket, metin)
# ---------------------------------------------------------------------------


def synthetic_policy_texts() -> list[tuple[str, str]]:
    """(etiket, metin) listesi. Her metin farklı bir düzen/şirket taklit eder."""
    return [
        (
            "allianz_yuvam",
            "## YUVAM SİGORTA POLİÇESİ\n"
            "Sayın Örnek Müşteri, Allianz ailesine hoş geldiniz.\n"
            "0001-0110-00000001 no'lu Allianz Yuvam poliçeniz.\n"
            "ALLIANZ SİGORTA A.Ş. info@allianz.com.tr allianz.com.tr",
        ),
        (
            "anadolu_police_vadesi",
            "Acente Kodu      Poli ç e No     Yenileme No     Poli ç e Vadesi\n"
            "100001           1000000001       0               10/12/2025 - 10/12/2026\n"
            "SBM Poli ç e No  SBM Poli ç e No 700000001      700000001",
        ),
        (
            "turkiye_sigorta_slash",
            "Müşteri No 10000000001 Acente No 1000001 "
            "Poliçe No 100000002 / 2 Ek Zeyil No 0\n"
            "Sigorta Başlangıç-Bitiş Tarihi 21.09.2025 - 21.09.2026\n"
            "Türkiye Sigorta A.Ş. turkiyesigorta.com.tr",
        ),
        (
            "mapfre_isveren",
            "İŞVEREN MALİ MESULİYET SİGORTA POLİÇESİ\n"
            "Acente No    TOBB Levha No      Poliçe No      Ek Belge No    Yenileme No\n"
            "100001       T000000-AAAA        2000000000004       0              0",
        ),
        (
            "ingilizce_marine",
            "MARINE CARGO INSURANCE POLICY\n"
            "Policy No: 2000000000001 Sales Channel ID: 100001\n"
            "Issued by AXA Sigorta A.Ş. www.axa.com.tr",
        ),
        (
            "hdi_birlesik",
            "ZORUNLU DEPREM SİGORTASI ANA POLİÇE\n"
            "Sigorta Şirketi Unvanı :HDISİGORTA A.Ş.\n"
            "Sigorta Şirketi Poliçe No :2000000000005 - T3",
        ),
        (
            "magdeburger_domain",
            "www.magdeburger.com.tr\nMAGDEBURGER SİGORTA A.Ş.\n"
            "GENİŞLETİLMİŞ KASKO SİGORTA POLİÇESİ\nPoliçe No 9000000017",
        ),
        (
            "zeyilli_kasko",
            "Poliçe No 123456789 Ek Zeyil No 3\nAXA Sigorta A.Ş.",
        ),
        (
            "gunes_unvan",
            "GÜNEŞ SİGORTA A.Ş. poliçesi gunessigorta.com.tr\n"
            "Poliçe No 555000111 Ek Zeyil No 0",
        ),
        (
            "sompo_kisa",
            "Sompo Sigorta A.Ş. somposigorta.com.tr\nPOLİÇE NO: 456789123",
        ),
        (
            "neova_ters_yazim",
            "NEVASİGORTA A.Ş. neova.com.tr\n853123456 no'lu poliçeniz yürürlüktedir.",
        ),
        (
            "groupama_bozuk_glif",
            "M\ufffd\ufffdteri No 10000000001 Acente No 1000001 "
            "Poli\ufffde No 100000002 / 2 Ek Zeyil No 0\n"
            "GROUPAMA SİGORTA A.Ş. groupama.com.tr",
        ),
        (
            "onceki_sirket",
            "Sigorta Şirketi Unvanı TÜRKİYE SİGORTA AŞ\n"
            "Önceki Şirket Adı ALLIANZ SİGORTA A.Ş. Ruhsat No 0000011111\n"
            "Poliçe No 3500000012",
        ),
        (
            "sadece_sehir",
            "Güneşli bir günde Türkiye'nin İstanbul'unda eviniz için hazırlanan "
            "bilgi notu. Poliçe No 0001-0110-00000001.",
        ),
        (
            "bos_anlamsiz",
            "Bu bir deneme dokümanıdır. İçinde sigorta bilgisi yoktur.\n"
            "Sadece düz metin, tablo ve tarih yok.",
        ),
    ]


# ---------------------------------------------------------------------------
# PDF'e yazılabilen sayfa metinleri (WinAnsi uyumlu) + tablo katalogları
# ---------------------------------------------------------------------------


def synthetic_pdf_pages() -> list[tuple[str, list[str], str | None]]:
    """(dosya adı, sayfa metinleri, beklenen poliçe no) — ASCII/PDF'e uygun."""
    return [
        (
            "allianz_policy.pdf",
            [
                "YUVAM SIGORTA POLICESI\n"
                "Sayin Ornek Musteri, Allianz ailesine hos geldiniz.\n"
                "Policy No: 1111222333444\n"
                "ALLIANZ SIGORTA A.S. allianz.com.tr",
            ],
            "1111222333444",
        ),
        (
            "axa_policy.pdf",
            [
                "GENISLETILMIS KASKO POLICESI\n"
                "Policy No: 2222333444555 Endorsement No 2\n"
                "AXA SIGORTA A.S. axa.com.tr",
            ],
            "2222333444555",
        ),
        (
            "multipage_late_hit.pdf",
            [
                "COVER PAGE\nBu sayfada police numarasi yoktur.",
                "GENERAL CONDITIONS\n" + "lorem ipsum dolor sit amet " * 20,
                "ISSUING DETAILS\nPolicy No: 3333444555666\nHDI SIGORTA A.S.",
            ],
            "3333444555666",
        ),
        (
            "no_policy_no.pdf",
            [
                "BILGI NOTU\nBu PDF'te police numarasi yoktur.\n"
                "Yalnizca tanitim metni ve adres bilgisi bulunur.",
            ],
            None,
        ),
        (
            "empty_first_page.pdf",
            [
                "",
                "Policy No: 4444555666777\nSompo Sigorta A.S. somposigorta.com.tr",
            ],
            "4444555666777",
        ),
    ]


def synthetic_tables() -> list[tuple[str, list[list[list[str]]], str | None, str | None]]:
    """(etiket, tablolar, beklenen polis_no, beklenen zeyil_no)."""
    return [
        (
            "yatay_baslikli",
            [
                [
                    ["0", "1", "2", "3"],
                    ["Police No", "Previous Policy No", "Start Date", "End Date"],
                    [
                        "0001-0110-00000001",
                        "0001-0110-00000002",
                        "13.11.2025",
                        "13.11.2026",
                    ],
                ]
            ],
            "0001-0110-00000001",
            None,
        ),
        (
            "ayni_satir_key_value",
            [
                [
                    [
                        "Musteri No", "10000000001", "Acente No", "1000001",
                        "Police No", "100000002 / 2", "Ek Zeyil No", "0",
                    ],
                ]
            ],
            "100000002/2",
            None,
        ),
        (
            "dikey_key_value",
            [
                [
                    ["Police No", "581858217"],
                    ["Start Date", "21.09.2025"],
                ]
            ],
            "581858217",
            None,
        ),
        (
            "zeyilli_tablo",
            [
                [
                    ["Police No", "Ek Zeyil No", "Company"],
                    ["1000000333", "7", "Gunes Sigorta"],
                ]
            ],
            "1000000333",
            "7",
        ),
        (
            "iki_tablo_ilki_eslesmez",
            [
                [["Header A", "Header B"], ["-", "-"]],
                [["Policy No", "Endorsement No"], ["123456789", "0"]],
            ],
            "123456789",
            None,
        ),
    ]


