"""Basit CLI: python -m policy_extract.cli <dosya.pdf | klasör>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from policy_extract.extractor import (
    PolicyExtraction,
    extract_policy_fast,
    file_sha256,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PDF poliçeden structured JSON çıkarır (hızlı pypdf motoru).",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Girdi PDF dosyası veya PDF klasörü",
    )
    parser.add_argument(
        "--file",
        dest="single_file",
        help="Test için yalnızca belirtilen PDF dosyasını tara.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="Tek dosya modunda çıktı JSON dosyası (varsayılan: stdout için '-'). "
        "Klasör modunda .metadata yolu (varsayılan: <klasör>/.metadata).",
    )
    parser.add_argument(
        "--with-text",
        action="store_true",
        help="full_text alanını çıktıya ekler (varsayılan: kapalı).",
    )
    parser.add_argument(
        "--no-text",
        action="store_true",
        help="(Eski bayrak, artık etkisiz: full_text zaten varsayılan kapalı.)",
    )
    parser.add_argument(
        "--no-tables",
        action="store_true",
        help="tables alanını çıktıdan çıkarır.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=7,
        help="PDF'in ilk N sayfasını işler (varsayılan: 7; tümü için 0).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=".metadata önbelleğini yok sayar, tüm dosyaları yeniden hesaplar.",
    )
    parser.add_argument(
        "--serve",
        "--watch",
        dest="serve",
        action="store_true",
        help="Servis modu: klasörü izler, kuyrukla işler, stdout'a JSONL olay yazar. "
        "Flutter entegrasyonu içindir (tek seferlik tarama yerine).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Servis modunda klasör tarama aralığı, saniye (varsayılan: 2.0).",
    )
    parser.add_argument(
        "--stable-checks",
        type=int,
        default=3,
        help="Dosya boyutu kaç üst üste kontrolde aynıysa kararlı sayılır (varsayılan: 3).",
    )
    parser.add_argument(
        "--stable-interval-ms",
        type=int,
        default=500,
        help="Kararlılık kontrolleri arası bekleme, ms (varsayılan: 500).",
    )
    parser.add_argument(
        "--stable-grace-secs",
        type=float,
        default=10.0,
        help="mtime'ı bundan eski dosya yazılmıyor sayılır, kararlılık "
        "beklemez (varsayılan: 10). Yalnızca taze dosyalar bekler.",
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=0,
        help="Ebeveyn süreç PID'i; ölürse servis kapanır. "
        "Flutter Process.start sonrası pid değerini verir (varsayılan: 0 = kapalı).",
    )
    parser.add_argument(
        "--compact-every",
        type=int,
        default=200,
        help=".metadata'nın kaç dosyada bir tekilleştirileceği (varsayılan: 200).",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Sürümü yazıp çıkar (Flutter update kontrolü için).",
    )
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="Update API'daki sürümü sorar, sonucu tek satır JSON yazar. "
        "--update-info-url ile kullanılır.",
    )
    parser.add_argument(
        "--fetch-update",
        action="store_true",
        help="Yeni exe'yi indirip sha256 doğrular. Olaylar stdout'a JSONL, "
        "sonuç {type: fetch_done}. --dest ile birlikte kullanılır.",
    )
    parser.add_argument(
        "--install-update",
        action="store_true",
        help="Doğrulanmış staging exe'yi hedefle değiştirir (yedekli). "
        "Önce serve süreci durdurulmalı. --staged + --target ile kullanılır.",
    )
    parser.add_argument(
        "--update-info-url",
        default="",
        help="Update JSON API endpoint "
        '(e.g. {"version": "0.4.0", "download_url": "...", "sha256": "..."}) '
        "or a GitHub Releases API URL (.../repos/OWNER/REPO/releases/latest).",
    )
    parser.add_argument(
        "--github-repo",
        default="",
        help="GitHub source as OWNER/REPO (uses the latest release's .exe asset).",
    )
    parser.add_argument(
        "--url",
        default="",
        help="--fetch-update için doğrudan indirme URL'i (API yerine).",
    )
    parser.add_argument(
        "--sha256",
        default="",
        help="--fetch-update için beklenen sha256 (API vermiyorsa elle).",
    )
    parser.add_argument(
        "--dest",
        default="",
        help="--fetch-update indirme hedefi (staging dosya yolu).",
    )
    parser.add_argument(
        "--staged",
        default="",
        help="--install-update için indirilmiş doğrulanmış exe yolu.",
    )
    parser.add_argument(
        "--target",
        default="",
        help="--install-update için değiştirilecek mevcut exe yolu.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="--install-update sırasında .bak yedeği alma.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Update ağı işlemleri zaman aşımı, saniye (varsayılan: 60).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Servis modunda detay loglarını stderr'e yazar "
        "(varsayılan: stderr tam sessiz).",
    )
    parser.add_argument(
        "--stream-events",
        action="store_true",
        help="Servis modunda dosya başı ara olayları da stdout'a yazar "
        "(progress/file_done/...). Varsayılan kapalı: yalnızca hello, "
        "komut yanıtları ve bye akar; durum `status` + .metadata'dan okunur.",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Klasör tek-instance kilidini atlar (aynı klasöre iki serve "
        "açmak metadata'yı bozar; yalnızca özel kurulumda kullan).",
    )
    return parser


def record_from_extraction(
    result: PolicyExtraction, *, sha256: str | None = None
) -> dict:
    """Batch kaydı: .metadata'ya yazılan kompakt satır (metin/tablo yok)."""
    return {
        "file": Path(result.source_file).name,
        "sha256": sha256,
        "police_no": result.police_no,
        "police_no_source": result.police_no_source,
        "zeyil_no": result.zeyil_no,
        "zeyil_no_source": result.zeyil_no_source,
        "company": result.company,
        "company_confidence": result.company_confidence,
        "company_scores": result.company_scores,
    }


def is_not_found_record(record: dict) -> bool:
    """Temel alanlardan en az biri bulunamadıysa True döner."""
    if "error" in record:
        return True
    return any(not record.get(field) for field in ("police_no", "company"))


def _max_pages(args: argparse.Namespace) -> int:
    return 9223372036854775807 if args.max_pages <= 0 else args.max_pages


def _input_path(args: argparse.Namespace) -> Path:
    return Path(args.single_file or args.input)


def _safe_sha256(pdf: Path) -> str | None:
    try:
        return file_sha256(str(pdf))
    except OSError:
        return None


def load_metadata_cache(meta_path: Path) -> dict[str, dict]:
    """Mevcut .metadata dosyasını okur: {dosya_adı: kayıt}."""
    cache: dict[str, dict] = {}
    if not meta_path.is_file():
        return cache
    try:
        for line in meta_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = record.get("file")
            if isinstance(name, str):
                cache[name] = record
    except OSError:
        pass
    return cache


def run_single(args: argparse.Namespace) -> int:
    pdf = _input_path(args)
    if not pdf.is_file():
        print(f"dosya bulunamadı: {pdf}", file=sys.stderr)
        return 2

    result = extract_policy_fast(str(pdf), max_pages=_max_pages(args))

    payload_dict = result.to_dict()
    payload_dict["sha256"] = _safe_sha256(pdf)
    if not args.with_text:
        payload_dict.pop("full_text", None)
    if args.no_tables:
        payload_dict.pop("tables", None)

    payload = json.dumps(payload_dict, ensure_ascii=False, indent=2)
    if args.output == "-":
        print(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")
    return 0


def run_batch(args: argparse.Namespace) -> int:
    folder = _input_path(args)
    pdfs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    if not pdfs:
        print(f"klasörde PDF yok: {folder}", file=sys.stderr)
        return 2

    meta_path = Path(args.output) if args.output != "-" else folder / ".metadata"
    not_found_path = folder / ".not-found-metadata"
    cache = {} if args.no_cache else load_metadata_cache(meta_path)
    stats = {"computed": 0, "cached": 0}
    failures = 0
    not_found_count = 0
    with (
        meta_path.open("w", encoding="utf-8") as fh,
        not_found_path.open("w", encoding="utf-8") as not_found_fh,
    ):
        for i, pdf in enumerate(pdfs, 1):
            print(f"[{i}/{len(pdfs)}] {pdf.name}...", file=sys.stderr, flush=True)
            try:
                digest = _safe_sha256(pdf)
                cached = cache.get(pdf.name)
                if (
                    cached is not None
                    and "error" not in cached
                    and cached.get("sha256") == digest
                    and not is_not_found_record(cached)
                ):
                    record = cached
                    stats["cached"] += 1
                    print("  -> önbellekten (sha256 eşleşti)", file=sys.stderr, flush=True)
                else:
                    if cached is not None and is_not_found_record(cached):
                        print(
                            "  -> not-found kaydı, yeniden hesaplanıyor",
                            file=sys.stderr,
                            flush=True,
                        )
                    result = extract_policy_fast(str(pdf), max_pages=_max_pages(args))
                    record = record_from_extraction(result, sha256=digest)
                    stats["computed"] += 1
            except Exception as exc:  # bir dosya bozarsa diğerlerine devam et
                failures += 1
                record = {"file": pdf.name, "sha256": _safe_sha256(pdf), "error": str(exc)}
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            if is_not_found_record(record):
                not_found_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                not_found_fh.flush()
                not_found_count += 1
            print(
                f"  -> police_no={record.get('police_no')} "
                f"company={record.get('company')}",
                file=sys.stderr,
                flush=True,
            )
    print(
        f"bitti: {len(pdfs)} dosya ({stats['computed']} hesaplandı, "
        f"{stats['cached']} önbellekten), "
        f"{failures} hata -> {meta_path}; "
        f"{not_found_count} eksik kayıt -> {not_found_path}",
        file=sys.stderr,
    )
    return 1 if failures else 0


def run_check_update(args: argparse.Namespace) -> int:
    """Ask the update source for its version, print a single JSON line (Flutter parses it)."""
    import policy_extract.updater as updater

    if not args.update_info_url and not getattr(args, "github_repo", ""):
        print("--check-update needs --update-info-url or --github-repo", file=sys.stderr)
        return 2
    try:
        result = updater.check_for_update(
            args.update_info_url,
            github_repo=getattr(args, "github_repo", ""),
            timeout=max(float(args.timeout), 1.0),
        )
    except updater.UpdateError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def run_fetch_update(args: argparse.Namespace) -> int:
    """Download the new exe + verify sha256; progress goes to stdout as JSONL."""
    import policy_extract.updater as updater

    url = args.url
    expected: str | None = args.sha256 or None
    if args.update_info_url or getattr(args, "github_repo", ""):
        try:
            info = updater.check_for_update(
                args.update_info_url,
                github_repo=getattr(args, "github_repo", ""),
                timeout=max(float(args.timeout), 1.0),
            )
        except updater.UpdateError as exc:
            print(json.dumps({"type": "fatal", "error": str(exc)}, ensure_ascii=False))
            return 1
        if not info["update_available"]:
            print(
                json.dumps(
                    {"type": "fetch_done", "skipped": True, "reason": "already_current",
                     "current_version": info["current_version"]},
                    ensure_ascii=False,
                )
            )
            return 0
        url = info["download_url"]
        expected = info["sha256"]
    if not url:
        print("--fetch-update needs --update-info-url, --github-repo or --url", file=sys.stderr)
        return 2
    if not args.dest:
        print("--fetch-update needs --dest", file=sys.stderr)
        return 2

    def on_progress(received: int, total: int | None) -> None:
        print(
            json.dumps(
                {"type": "fetch_progress", "received": received, "total": total},
                ensure_ascii=False,
            ),
            flush=True,
        )

    try:
        result = updater.download_update(
            url, args.dest,
            expected_sha256=expected,
            timeout=max(float(args.timeout), 1.0),
            progress_cb=on_progress,
        )
    except updater.UpdateError as exc:
        print(json.dumps({"type": "fatal", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"type": "fetch_done", **result}, ensure_ascii=False))
    return 0


def run_install_update(args: argparse.Namespace) -> int:
    """Staging exe'yi hedefle değiştir, sonucu tek satır JSON yaz."""
    import policy_extract.updater as updater

    if not args.staged or not args.target:
        print("--install-update --staged ve --target gerektirir", file=sys.stderr)
        return 2
    try:
        result = updater.install_update(
            args.staged, args.target, keep_backup=not args.no_backup
        )
    except updater.UpdateError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"type": "install_done", **result}, ensure_ascii=False))
    return 0


def run_serve(args: argparse.Namespace) -> int:
    """Uzun süreli servis: klasörü izler, kuyrukla işler, olayları stdout'a yazar."""
    import logging
    import warnings

    from policy_extract.service import ServiceConfig, WatchService

    verbose = bool(getattr(args, "verbose", False))
    if not verbose:
        # Tam sessiz daemon: üçüncü parti uyarı/log kırıntıları da stderr'e düşmesin.
        # Tüm durum bilgisi stdout olaylarındadır.
        warnings.filterwarnings("ignore")
        logging.disable(logging.CRITICAL)

    folder = _input_path(args)
    meta_path = Path(args.output) if args.output != "-" else folder / ".metadata"
    not_found_path = folder / ".not-found-metadata"
    cfg = ServiceConfig(
        folder=folder,
        meta_path=meta_path,
        not_found_path=not_found_path,
        max_pages=_max_pages(args),
        no_cache=args.no_cache,
        poll_interval=max(float(args.poll_interval), 0.2),
        stable_checks=max(int(args.stable_checks), 1),
        stable_interval_ms=max(int(args.stable_interval_ms), 50),
        stable_grace_secs=max(float(args.stable_grace_secs), 0.0),
        parent_pid=int(args.parent_pid or 0),
        compact_every=max(int(args.compact_every), 1),
        verbose=verbose,
        stream_events=bool(getattr(args, "stream_events", False)),
        no_lock=bool(getattr(args, "no_lock", False)),
    )
    return WatchService(cfg).run()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        from policy_extract.service import SERVICE_VERSION

        print(SERVICE_VERSION)
        return 0
    if getattr(args, "check_update", False):
        return run_check_update(args)
    if getattr(args, "fetch_update", False):
        return run_fetch_update(args)
    if getattr(args, "install_update", False):
        return run_install_update(args)
    if args.input and args.single_file:
        parser.error("girdi yolu ve --file aynı anda kullanılamaz")
    if not args.input and not args.single_file:
        parser.error("bir PDF/klasör yolu veya --file belirtilmeli")
    if _input_path(args).is_dir():
        if getattr(args, "serve", False):
            return run_serve(args)
        return run_batch(args)
    if getattr(args, "serve", False):
        print("--serve yalnızca klasör modunda kullanılır", file=sys.stderr)
        return 2
    return run_single(args)


if __name__ == "__main__":
    raise SystemExit(main())
