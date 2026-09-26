"""Command line interface: single PDF, folder batch, serve, and updates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from policy_extract.models import PolicyExtraction
from policy_extract.pdf_io import extract_policy_fast, hash_file
from policy_extract.records import (
    is_not_found_record,
    is_reusable_cache_entry,
    load_metadata_cache,
    record_from_extraction,
)

__all__ = [
    "build_parser",
    "is_not_found_record",
    "load_metadata_cache",
    "record_from_extraction",
    "run_single",
    "run_batch",
    "run_check_update",
    "run_fetch_update",
    "run_install_update",
    "run_serve",
    "main",
]


# ---------------------------------------------------------------------------
# Argument parser (one helper per option group keeps build_parser small)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PDF poliçeden structured JSON çıkarır (hızlı pypdf motoru).",
    )
    _add_input_options(parser)
    _add_output_options(parser)
    _add_serve_options(parser)
    _add_update_options(parser)
    return parser


def _add_input_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", nargs="?", help="Girdi PDF dosyası veya PDF klasörü")
    parser.add_argument(
        "--file", dest="single_file", help="Test için yalnızca belirtilen PDF dosyasını tara."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=7,
        help="PDF'in ilk N sayfasını işler (varsayılan: 7; 0 veya eksi = tüm sayfalar).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=".metadata önbelleğini yok sayar, tüm dosyaları yeniden hesaplar.",
    )


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="Tek dosya modunda çıktı JSON dosyası (varsayılan: stdout için '-'). "
        "Klasör modunda .metadata yolu (varsayılan: <klasör>/.metadata).",
    )
    parser.add_argument(
        "--with-text", action="store_true", help="full_text alanını çıktıya ekler (varsayılan: kapalı)."
    )
    parser.add_argument(
        "--no-text",
        action="store_true",
        help="(Eski bayrak, artık etkisiz: full_text zaten varsayılan kapalı.)",
    )
    parser.add_argument("--no-tables", action="store_true", help="tables alanını çıktıdan çıkarır.")


def _add_serve_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--serve", "--watch", dest="serve", action="store_true",
        help="Servis modu: klasörü izler, kuyrukla işler, stdout'a JSONL olay yazar.",
    )
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--stable-checks", type=int, default=3)
    parser.add_argument("--stable-interval-ms", type=int, default=500)
    parser.add_argument("--stable-grace-secs", type=float, default=10.0)
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--compact-every", type=int, default=200)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--stream-events", action="store_true")
    parser.add_argument("--no-lock", action="store_true")


def _add_update_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--check-update", action="store_true")
    parser.add_argument("--fetch-update", action="store_true")
    parser.add_argument("--install-update", action="store_true")
    parser.add_argument("--update-info-url", default="")
    parser.add_argument("--github-repo", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--sha256", default="")
    parser.add_argument("--dest", default="")
    parser.add_argument("--staged", default="")
    parser.add_argument("--target", default="")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--timeout", type=float, default=60.0)


# ---------------------------------------------------------------------------
# Shared path / hashing helpers
# ---------------------------------------------------------------------------


def _input_path(args: argparse.Namespace) -> Path:
    return Path(args.single_file or args.input)


def _safe_sha256(pdf: Path) -> str | None:
    try:
        return hash_file(str(pdf))
    except OSError:
        return None


def _resolve_batch_paths(args: argparse.Namespace, folder: Path) -> tuple[Path, Path]:
    meta_path = Path(args.output) if args.output != "-" else folder / ".metadata"
    return meta_path, folder / ".not-found-metadata"


# ---------------------------------------------------------------------------
# Single file mode
# ---------------------------------------------------------------------------


def run_single(args: argparse.Namespace) -> int:
    pdf = _input_path(args)
    if not pdf.is_file():
        print(f"dosya bulunamadı: {pdf}", file=sys.stderr)
        return 2
    result = extract_policy_fast(str(pdf), max_pages=args.max_pages)
    payload = _single_payload(result, pdf, with_text=args.with_text, no_tables=args.no_tables)
    if args.output == "-":
        print(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")
    return 0


def _single_payload(
    result: PolicyExtraction, pdf: Path, *, with_text: bool, no_tables: bool
) -> str:
    payload_dict = result.to_dict()
    payload_dict["sha256"] = _safe_sha256(pdf)
    if not with_text:
        payload_dict.pop("full_text", None)
    if no_tables:
        payload_dict.pop("tables", None)
    return json.dumps(payload_dict, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Batch folder mode
# ---------------------------------------------------------------------------


def run_batch(args: argparse.Namespace) -> int:
    folder = _input_path(args)
    pdfs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    if not pdfs:
        print(f"klasörde PDF yok: {folder}", file=sys.stderr)
        return 2
    meta_path, not_found_path = _resolve_batch_paths(args, folder)
    cache = {} if args.no_cache else load_metadata_cache(meta_path)
    computed = cached = failures = not_found_count = 0
    with (
        meta_path.open("w", encoding="utf-8") as handle,
        not_found_path.open("w", encoding="utf-8") as not_found_handle,
    ):
        for index, pdf in enumerate(pdfs, 1):
            record, from_cache, failed = _process_batch_file(
                pdf, cache, args.max_pages, index=index, total=len(pdfs)
            )
            computed += 0 if (from_cache or failed) else 1
            cached += 1 if from_cache else 0
            failures += 1 if failed else 0
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            if is_not_found_record(record):
                not_found_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                not_found_handle.flush()
                not_found_count += 1
            _log_batch_progress(index, len(pdfs), pdf.name, record, from_cache)
    _log_batch_summary(len(pdfs), computed, cached, failures, meta_path, not_found_path, not_found_count)
    return 1 if failures else 0


def _process_batch_file(
    pdf: Path,
    cache: dict[str, dict],
    max_pages: int,
    *,
    index: int = 0,
    total: int = 0,
) -> tuple[dict, bool, bool]:
    if total:
        print(f"[{index}/{total}] {pdf.name}...", file=sys.stderr, flush=True)
    try:
        digest = _safe_sha256(pdf)
        cached = cache.get(pdf.name)
        if cached is not None and digest is not None and is_reusable_cache_entry(cached, digest):
            print("  -> önbellekten (sha256 eşleşti)", file=sys.stderr, flush=True)
            return cached, True, False
        if cached is not None and is_not_found_record(cached):
            print("  -> not-found kaydı, yeniden hesaplanıyor", file=sys.stderr, flush=True)
        result = extract_policy_fast(str(pdf), max_pages=max_pages)
        return record_from_extraction(result, sha256=digest), False, False
    except Exception as exc:
        return {"file": pdf.name, "sha256": _safe_sha256(pdf), "error": str(exc)}, False, True


def _log_batch_progress(
    index: int, total: int, name: str, record: dict, from_cache: bool
) -> None:
    _ = (index, total, name, from_cache)  # kept for structured logging parity
    print(
        f"  -> police_no={record.get('police_no')} company={record.get('company')}",
        file=sys.stderr,
        flush=True,
    )


def _log_batch_summary(
    total: int,
    computed: int,
    cached: int,
    failures: int,
    meta_path: Path,
    not_found_path: Path,
    not_found_count: int,
) -> None:
    print(
        f"bitti: {total} dosya ({computed} hesaplandı, {cached} önbellekten), "
        f"{failures} hata -> {meta_path}; {not_found_count} eksik kayıt -> {not_found_path}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Update commands (thin wrappers over update_service)
# ---------------------------------------------------------------------------


def run_check_update(args: argparse.Namespace) -> int:
    import policy_extract.update_service as update_service

    if not args.update_info_url and not getattr(args, "github_repo", ""):
        print("--check-update needs --update-info-url or --github-repo", file=sys.stderr)
        return 2
    try:
        result = update_service.check_for_update(
            args.update_info_url,
            github_repo=getattr(args, "github_repo", ""),
            timeout=max(float(args.timeout), 1.0),
        )
    except update_service.UpdateError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def run_fetch_update(args: argparse.Namespace) -> int:
    import policy_extract.update_service as update_service

    if args.update_info_url or getattr(args, "github_repo", ""):
        return _run_fetch_via_api(args, update_service)
    return _run_fetch_direct(args, update_service)


def _run_fetch_via_api(args: argparse.Namespace, update_service) -> int:
    try:
        info = update_service.check_for_update(
            args.update_info_url,
            github_repo=getattr(args, "github_repo", ""),
            timeout=max(float(args.timeout), 1.0),
        )
    except update_service.UpdateError as exc:
        print(json.dumps({"type": "fatal", "error": str(exc)}, ensure_ascii=False))
        return 1
    if not info["update_available"]:
        print(
            json.dumps(
                {
                    "type": "fetch_done",
                    "skipped": True,
                    "reason": "already_current",
                    "current_version": info["current_version"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    return _download_and_report(
        args, update_service, url=info["download_url"], expected=info["sha256"]
    )


def _run_fetch_direct(args: argparse.Namespace, update_service) -> int:
    if not args.url:
        print(
            "--fetch-update needs --update-info-url, --github-repo or --url",
            file=sys.stderr,
        )
        return 2
    return _download_and_report(
        args, update_service, url=args.url, expected=args.sha256 or None
    )


def _download_and_report(
    args: argparse.Namespace, update_service, *, url: str, expected: str | None
) -> int:
    if not args.dest:
        print("--fetch-update needs --dest", file=sys.stderr)
        return 2
    try:
        result = update_service.download_update(
            url,
            args.dest,
            expected_sha256=expected,
            timeout=max(float(args.timeout), 1.0),
            progress_cb=_stdout_progress,
        )
    except update_service.UpdateError as exc:
        print(json.dumps({"type": "fatal", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"type": "fetch_done", **result}, ensure_ascii=False))
    return 0


def _stdout_progress(received: int, total: int | None) -> None:
    print(
        json.dumps(
            {"type": "fetch_progress", "received": received, "total": total},
            ensure_ascii=False,
        ),
        flush=True,
    )


def run_install_update(args: argparse.Namespace) -> int:
    import policy_extract.update_service as update_service

    if not args.staged or not args.target:
        print("--install-update --staged ve --target gerektirir", file=sys.stderr)
        return 2
    try:
        result = update_service.install_update(
            args.staged, args.target, keep_backup=not args.no_backup
        )
    except update_service.UpdateError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"type": "install_done", **result}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# Serve mode
# ---------------------------------------------------------------------------


def run_serve(args: argparse.Namespace) -> int:
    import logging
    import warnings

    from policy_extract.service import ServiceConfig, WatchService

    if not bool(getattr(args, "verbose", False)):
        warnings.filterwarnings("ignore")
        logging.disable(logging.CRITICAL)

    folder = _input_path(args)
    meta_path = Path(args.output) if args.output != "-" else folder / ".metadata"
    config = ServiceConfig(
        folder=folder,
        meta_path=meta_path,
        not_found_path=folder / ".not-found-metadata",
        max_pages=args.max_pages,
        no_cache=args.no_cache,
        poll_interval=max(float(args.poll_interval), 0.2),
        stable_checks=max(int(args.stable_checks), 1),
        stable_interval_ms=max(int(args.stable_interval_ms), 50),
        stable_grace_secs=max(float(args.stable_grace_secs), 0.0),
        parent_pid=int(args.parent_pid or 0),
        compact_every=max(int(args.compact_every), 1),
        verbose=bool(getattr(args, "verbose", False)),
        stream_events=bool(getattr(args, "stream_events", False)),
        no_lock=bool(getattr(args, "no_lock", False)),
    )
    return WatchService(config).run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        from policy_extract.version import SERVICE_VERSION

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
