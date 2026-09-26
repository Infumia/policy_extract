"""Serve (uzun süreli servis) uç durum testleri: kuyruk, komutlar, kilit, metadata.

Çalıştırma:  python tests/test_service_edge_cases.py
(pytest varsa `pytest tests/test_service_edge_cases.py` ile de çalışır.)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "src"))
sys.path.insert(0, str(_HERE.parent))

import policy_extract.service as svc_module
from policy_extract.extractor import PolicyExtraction, file_sha256
from policy_extract.service import (
    SERVICE_VERSION,
    ServiceConfig,
    WatchService,
    append_record,
    acquire_folder_lock,
    compact_metadata,
    parent_alive,
    release_folder_lock,
    rewrite_not_found,
    wait_for_stable,
)


def _service(folder: Path, **overrides: object) -> tuple[WatchService, list[dict]]:
    """Sessiz test servisi + olay listesi."""
    events: list[dict] = []
    kwargs: dict[str, object] = {
        "folder": folder,
        "meta_path": folder / ".metadata",
        "not_found_path": folder / ".not-found-metadata",
        "poll_interval": 10.0,
        "stable_checks": 1,
        "emit": events.append,
        "handle_stdin": False,
        "stream_events": True,
    }
    kwargs.update(overrides)
    return WatchService(ServiceConfig(**kwargs)), events  # type: ignore[arg-type]


def _fake_extraction(pdf_path: str, **_: object) -> PolicyExtraction:
    name = Path(pdf_path).name
    return PolicyExtraction(
        source_file=pdf_path,
        police_no="P-" + name,
        police_no_source="inline",
        company="allianz",
        company_confidence="high",
        company_scores={"allianz": 20},
    )


# ---------------------------------------------------------------------------
# Kuyruk ve olay filtresi
# ---------------------------------------------------------------------------


def test_enqueue_dedupe_ve_fifo_sirasi() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, events = _service(Path(tmp))
        assert service.enqueue("a.pdf") is True
        assert service.enqueue("a.pdf") is False  # kuyrukta: tekrar eklenmez
        assert service.enqueue("b.pdf", reason="created") is True
        assert service._queued_count() == 2
        queued = [e for e in events if e["type"] == "file_queued"]
        assert [e["file"] for e in queued] == ["a.pdf", "b.pdf"]
        assert [e["queued"] for e in queued] == [1, 2]
        assert queued[0]["reason"] == "created"
        assert service._dequeue() == "a.pdf"
        assert service._dequeue() == "b.pdf"
        assert service._dequeue() is None
        assert service._queued_count() == 0
        # Kuyruktan çıkan dosya yeniden kuyruklanabilir.
        assert service.enqueue("a.pdf") is True


def test_emit_ara_olaylari_stream_events_ile_suzer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        silent, silent_events = _service(Path(tmp), stream_events=False)
        silent._emit({"type": "progress", "file": "a.pdf"})
        silent._emit({"type": "file_done", "file": "a.pdf"})
        silent._emit({"type": "paused"})
        silent._emit({"type": "status"})
        assert [e["type"] for e in silent_events] == ["paused", "status"]

        loud, loud_events = _service(Path(tmp))
        loud._emit({"type": "progress", "file": "a.pdf"})
        assert [e["type"] for e in loud_events] == ["progress"]


# ---------------------------------------------------------------------------
# stdin komut protokolü
# ---------------------------------------------------------------------------


def test_bilinmeyen_ve_bos_komutlar() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, events = _service(Path(tmp))
        service.handle_command({})
        service.handle_command({"cmd": "zzz"})
        service.handle_command({"cmd": ""})
        kinds = [e["type"] for e in events]
        assert kinds == ["unknown_command"] * 3, kinds
        assert events[1]["command"] == {"cmd": "zzz"}


def test_komut_buyuk_kucuk_harf_ve_bosluk_toleransi() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, events = _service(Path(tmp))
        service.handle_command({"cmd": "  Pause  "})
        assert service.paused.is_set()
        service.handle_command({"cmd": "RESUME"})
        assert not service.paused.is_set()
        assert [e["type"] for e in events] == ["paused", "resumed"]
        service.handle_command({"cmd": "Shutdown"})
        assert service.stop_event.is_set()


def test_status_komutu_alanlari() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, events = _service(Path(tmp))
        service.stats.done = 3
        service.stats.computed = 2
        service.stats.cached = 1
        service.stats.failures = 1
        service.enqueue("a.pdf", reason="retry")
        service.handle_command({"cmd": "status"})
        status = events[-1]
        assert status["type"] == "status"
        assert status["version"] == SERVICE_VERSION
        assert status["pid"] == os.getpid()
        assert status["paused"] is False
        assert (status["done"], status["computed"], status["cached"]) == (3, 2, 1)
        assert status["failures"] == 1
        assert status["queued"] == 1


def test_retry_file_dogrulamasi() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        service, events = _service(folder)
        gecersiz = [
            "",
            ".",
            "..",
            "../a.pdf",
            "sub/a.pdf",
            "sub\\a.pdf",
            "C:/a.pdf",
            "a:b.pdf",
            "a.txt",
            "yok.pdf",
            "a.pdf ",
        ]
        for name in gecersiz:
            service.handle_command({"cmd": "retry_file", "file": name})
            assert events[-1]["type"] == "watch_error", name
            assert service._dequeue() is None, name
        # Dosya adı yerine JSON sayı gelirse de güvenli davranır.
        service.handle_command({"cmd": "retry_file", "file": 5})
        assert events[-1]["type"] == "watch_error"

        service.handle_command({"cmd": "retry_file", "file": "a.pdf"})
        assert events[-1]["type"] == "file_queued"
        assert events[-1]["reason"] == "retry"
        assert "a.pdf" in service.force_reextract
        assert service._dequeue() == "a.pdf"
        # Kuyruk boşaldıktan sonra aynı dosya yeniden istenebilir.
        service.handle_command({"cmd": "retry_file", "file": "a.pdf"})
        assert service._dequeue() == "a.pdf"


def test_rescan_komutu_yeni_dosyayi_kuyruga_alir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        service, events = _service(folder)
        service.handle_command({"cmd": "rescan"})
        assert events[-1]["type"] == "rescanned"
        assert events[-1]["queued_new"] == 0
        (folder / "yeni.pdf").write_bytes(b"aaa")
        service.handle_command({"cmd": "rescan"})
        assert events[-1]["queued_new"] == 1
        assert events[-1]["queued"] == 1


def test_stdin_loop_bozuk_satir_ve_eof() -> None:
    import io

    with tempfile.TemporaryDirectory() as tmp:
        service, events = _service(Path(tmp))
        original_stdin = sys.stdin
        sys.stdin = io.StringIO("bu json degil\n\n{\"cmd\": \"status\"}\n")
        try:
            service._stdin_loop()  # EOF'ta döner ve stop_event'i set eder
        finally:
            sys.stdin = original_stdin
        kinds = [e["type"] for e in events]
        assert kinds == ["bad_command", "status"], kinds
        assert events[0]["line"] == "bu json degil"
        assert service.stop_event.is_set()


# ---------------------------------------------------------------------------
# Klasör tarama / tek dosya işleme
# ---------------------------------------------------------------------------


def test_poll_once_degisiklik_silme_ve_yeniden_adlandirma() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        pdf = folder / "a.pdf"
        pdf.write_bytes(b"aaa")
        service, events = _service(folder)

        assert service.poll_once(reason_initial=True) == 1
        assert events[-1]["type"] == "file_queued"
        assert events[-1]["reason"] == "initial"
        assert service.poll_once() == 0  # değişiklik yok
        assert service._dequeue() == "a.pdf"  # kuyruk boşalsın (in_queue temizlenir)

        # Yeniden adlandırma: eski ad takipten düşer, yeni ad kuyruğa girer.
        pdf.rename(folder / "b.pdf")
        assert service.poll_once() == 1
        assert set(service.known) == {"b.pdf"}
        assert events[-1]["reason"] == "created"
        assert service._dequeue() == "b.pdf"  # kuyruk boşalsın (in_queue temizlenir)

        # Aynı ad, farklı boyut -> "modified".
        (folder / "b.pdf").write_bytes(b"bbbb")
        assert service.poll_once() == 1
        assert events[-1]["reason"] == "modified"

        # Kuyrukta bekleyen ad tekrar eklenmez.
        assert service.poll_once() == 0


def test_snapshot_pdf_dosyalarini_suzer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"a")
        (folder / "BUYUK.PDF").write_bytes(b"b")
        (folder / "notlar.txt").write_bytes(b"c")
        (folder / "sahte.pdf").mkdir()  # klasör: PDF sayılmaz
        (folder / "alt").mkdir()
        service, _ = _service(folder)
        assert set(service.snapshot_pdfs()) == {"a.pdf", "BUYUK.PDF"}


def test_okunamayan_klasor_watch_error_uretir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, events = _service(Path(tmp) / "yok")
        assert service.snapshot_pdfs() == {}
        assert service.poll_once(reason_initial=True) == 0
        assert [e["type"] for e in events] == ["watch_error", "watch_error"]
        assert "klasör okunamadı" in events[0]["error"]


def test_process_one_force_no_cache_ve_hata_yollari() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        pdf = folder / "a.pdf"
        pdf.write_bytes(b"aaa")
        service, _ = _service(folder)
        calls: list[str] = []
        original = svc_module.extract_policy_fast

        def fake(pdf_path: str, *, max_pages: int = 7) -> PolicyExtraction:
            calls.append(Path(pdf_path).name)
            return _fake_extraction(pdf_path)

        svc_module.extract_policy_fast = fake  # type: ignore[assignment]
        try:
            first = service.process_one(pdf)
            assert first["from_cache"] is False
            assert first["record"]["sha256"] == file_sha256(pdf)
            service._persist(first["record"])
            assert service.process_one(pdf)["from_cache"] is True
            assert service.process_one(pdf, force=True)["from_cache"] is False
            assert calls == ["a.pdf", "a.pdf"]

            def boom(pdf_path: str, *, max_pages: int = 7) -> PolicyExtraction:
                raise RuntimeError("patladı")

            svc_module.extract_policy_fast = boom  # type: ignore[assignment]
            outcome = service.process_one(pdf, force=True)
            assert outcome["from_cache"] is False
            assert outcome["record"]["error"] == "patladı"
            assert outcome["record"]["sha256"] == file_sha256(pdf)
            assert service.stats.failures == 1
        finally:
            svc_module.extract_policy_fast = original  # type: ignore[method-assign]


def test_process_one_no_cache_ayarinda_onbellegi_yok_sayar() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        pdf = folder / "a.pdf"
        pdf.write_bytes(b"aaa")
        service, _ = _service(folder, no_cache=True)
        service.cache["a.pdf"] = {
            "file": "a.pdf",
            "sha256": file_sha256(pdf),
            "police_no": "ESKI",
            "company": "axa",
        }
        original = svc_module.extract_policy_fast
        svc_module.extract_policy_fast = _fake_extraction  # type: ignore[assignment]
        try:
            outcome = service.process_one(pdf)
        finally:
            svc_module.extract_policy_fast = original  # type: ignore[method-assign]
        assert outcome["from_cache"] is False
        assert outcome["record"]["police_no"] == "P-a.pdf"


def test_persist_dosya_adi_olmayan_kaydi_yazmaz() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        service, _ = _service(folder)
        assert service._persist({"sha256": "h", "police_no": "1"}) is False
        assert not (folder / ".metadata").exists()


# ---------------------------------------------------------------------------
# run(): kapanış, kilit, önbellek sıfırlama
# ---------------------------------------------------------------------------


def _run_until_done(service: WatchService, count: int, *, timeout: float = 15.0) -> threading.Thread:
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    deadline = time.time() + timeout
    while service.stats.done < count and time.time() < deadline:
        time.sleep(0.05)
    service.stop_event.set()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "servis takılı kalmamalı"
    return thread


def test_run_klasor_yoksa_fatal_doner() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "yok"
        service, events = _service(missing)
        assert service.run() == 2
        assert events[-1]["type"] == "fatal"
        assert events[-1]["error"].startswith("klasör bulunamadı")


def test_run_canli_kilit_varsa_fatal_doner() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        lock = folder / ".policy-extract.lock"
        lock.write_text("424242\n", encoding="utf-8")
        service, events = _service(folder)
        original = svc_module.parent_alive
        svc_module.parent_alive = lambda pid: True  # type: ignore[assignment]
        try:
            assert service.run() == 2
        finally:
            svc_module.parent_alive = original  # type: ignore[method-assign]
        assert events[-1]["type"] == "fatal"
        assert "başka bir serve" in events[-1]["error"]
        assert lock.is_file(), "başkasının kilidi silinmemeli"


def test_run_no_cache_bayrakleri_taze_baslar() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        (folder / ".metadata").write_text(
            json.dumps({"file": "eski.pdf", "police_no": "0"}) + "\n", encoding="utf-8"
        )
        (folder / ".not-found-metadata").write_text(
            json.dumps({"file": "eski.pdf", "police_no": None, "company": None}) + "\n",
            encoding="utf-8",
        )
        service, _ = _service(folder, no_cache=True)
        original = svc_module.extract_policy_fast
        svc_module.extract_policy_fast = _fake_extraction  # type: ignore[assignment]
        try:
            _run_until_done(service, 1)
        finally:
            svc_module.extract_policy_fast = original  # type: ignore[method-assign]
        assert service.stats.done == 1
        meta = (folder / ".metadata").read_text(encoding="utf-8")
        assert "eski.pdf" not in meta, meta
        assert "a.pdf" in meta
        nf = folder / ".not-found-metadata"
        assert not nf.exists() or "eski.pdf" not in nf.read_text(encoding="utf-8")


def test_run_hello_ve_bye_alanlari() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "a.pdf").write_bytes(b"aaa")
        service, events = _service(folder)
        original = svc_module.extract_policy_fast
        svc_module.extract_policy_fast = _fake_extraction  # type: ignore[assignment]
        try:
            _run_until_done(service, 1)
        finally:
            svc_module.extract_policy_fast = original  # type: ignore[method-assign]

        hello = next(e for e in events if e["type"] == "hello")
        assert hello["version"] == SERVICE_VERSION
        assert hello["pid"] == os.getpid()
        assert hello["folder"] == str(folder)
        assert hello["total_queued"] == 1
        assert hello["parent_pid"] == 0
        assert any(e["type"] == "idle" for e in events)

        bye = events[-1]
        assert bye["type"] == "bye"
        assert bye["persist_error"] is None
        assert bye["done"] == 1
        assert bye["computed"] == 1
        assert bye["cached"] == 0
        assert bye["failures"] == 0
        assert not (folder / ".policy-extract.lock").exists()


def test_run_no_lock_klasor_kilidini_atlar() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / ".metadata").write_text("", encoding="utf-8")
        service, events = _service(folder, no_lock=True)
        _run_until_done(service, 0, timeout=5.0)
        assert events[-1]["type"] == "bye"
        assert not (folder / ".policy-extract.lock").exists()


# ---------------------------------------------------------------------------
# Kilit, ebeveyn takibi, kararlılık beklemesi
# ---------------------------------------------------------------------------


def test_kilit_bayat_icerik_ve_yabanci_pid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        lock = folder / ".policy-extract.lock"

        # 1) Bozuk içerik: bayat sayılır, devralınır.
        lock.write_text("bozuk icerik\n", encoding="utf-8")
        acquired = acquire_folder_lock(folder)
        assert acquired is not None
        assert acquired.read_text(encoding="utf-8").strip() == str(os.getpid())

        # 2) Kendi pid'imiz bayat sayılır: yeniden alınır, sonra bırakılır.
        again = acquire_folder_lock(folder)
        assert again is not None
        release_folder_lock(again)
        assert not lock.exists()

        # 3) Yabancı canlı pid: alınamaz ve sahibi olmadığımız kilit silinmez.
        lock.write_text("424242\n", encoding="utf-8")
        original = svc_module.parent_alive
        svc_module.parent_alive = lambda pid: True  # type: ignore[assignment]
        try:
            assert acquire_folder_lock(folder) is None
            release_folder_lock(lock)
            assert lock.is_file()
        finally:
            svc_module.parent_alive = original  # type: ignore[method-assign]


def test_parent_alive_kenar_durumlari() -> None:
    assert parent_alive(0) is True  # pid verilmemiş: takip kapalı
    assert parent_alive(-5) is True
    assert parent_alive(os.getpid()) is True
    assert parent_alive(999999999) is False  # geçersiz/ölü pid


def test_parent_watch_loop_ebeveyn_yoksa_kapatir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, events = _service(Path(tmp), parent_pid=999999999)
        thread = threading.Thread(target=service._parent_watch_loop, daemon=True)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert service.stop_event.is_set()
        assert events[-1]["type"] == "parent_gone"
        assert events[-1]["parent_pid"] == 999999999

        # parent_pid<=0 ise döngü hiçbir şey yapmaz.
        idle_service, idle_events = _service(Path(tmp), parent_pid=0)
        idle_service._parent_watch_loop()
        assert idle_events == []


def test_wait_for_stable_kararlilik_ve_iptal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        pdf = folder / "a.pdf"
        pdf.write_bytes(b"a" * 100)

        assert wait_for_stable(pdf, checks=1) is True  # sadece varlık kontrolü
        assert wait_for_stable(folder / "yok.pdf", checks=1) is False

        stop = threading.Event()
        stop.set()
        assert wait_for_stable(pdf, checks=3, interval_ms=10, stop=stop) is False

        # Bekleme sırasında silinen dosya: False döner, takılmaz.
        stop2 = threading.Event()

        def deleter() -> None:
            time.sleep(0.06)
            pdf.unlink(missing_ok=True)

        worker = threading.Thread(target=deleter)
        worker.start()
        try:
            assert wait_for_stable(pdf, checks=8, interval_ms=50, stop=stop2) is False
        finally:
            stop2.set()
            worker.join(timeout=5)


# ---------------------------------------------------------------------------
# Metadata yardımcıları ve yapılandırma varsayılanları
# ---------------------------------------------------------------------------


def test_metadata_yardimcilari() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        nested = folder / "alt" / ".metadata"
        append_record(nested, {"file": "b.pdf", "police_no": "2", "company": "axa"})
        append_record(nested, {"file": "a.pdf", "police_no": None, "company": None})
        assert nested.is_file()
        assert len(nested.read_text(encoding="utf-8").splitlines()) == 2

        cache = {
            "b.pdf": {"file": "b.pdf", "police_no": "2", "company": "axa"},
            "a.pdf": {"file": "a.pdf", "police_no": None, "company": None},
        }
        compact_metadata(nested, cache)
        assert [json.loads(line)["file"] for line in nested.read_text(encoding="utf-8").splitlines()] == [
            "a.pdf",
            "b.pdf",
        ]  # sıralı tekilleştirme

        nf = folder / "alt" / ".not-found-metadata"
        rewrite_not_found(nf, cache)
        assert [json.loads(line)["file"] for line in nf.read_text(encoding="utf-8").splitlines()] == [
            "a.pdf"
        ]

        # Boş önbellek + dosya yoksa boş dosya üretilmez.
        empty_meta = folder / "bos" / ".metadata"
        empty_nf = folder / "bos" / ".not-found-metadata"
        compact_metadata(empty_meta, {})
        rewrite_not_found(empty_nf, {})
        assert not empty_meta.exists()
        assert not empty_nf.exists()


def test_load_cache_last_wins_son_satir_kazanir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        meta = Path(tmp) / ".metadata"
        meta.write_text(
            json.dumps({"file": "a.pdf", "police_no": "1"})
            + "\n"
            + json.dumps({"file": "a.pdf", "police_no": "2"})
            + "\n",
            encoding="utf-8",
        )
        assert svc_module.load_cache_last_wins(meta)["a.pdf"]["police_no"] == "2"


def test_compact_if_needed_gerekmedikce_dosyaya_dokunmaz() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        meta = folder / ".metadata"
        record = {"file": "a.pdf", "sha256": "h", "police_no": "1", "company": "axa"}
        meta.write_text(json.dumps(record) + "\n", encoding="utf-8")
        before = meta.stat().st_mtime_ns
        service, _ = _service(folder)
        service.cache = {"a.pdf": record}
        service._compact_if_needed()
        assert meta.stat().st_mtime_ns == before  # yazım yok

        # .metadata hiç yoksa sessizce döner.
        service2, _ = _service(folder, meta_path=folder / "yok" / ".metadata")
        service2._compact_if_needed()


def test_emit_event_jsonl_turkce_karakterleri_korur() -> None:
    import io as _io

    stream = _io.StringIO()
    svc_module.emit_event({"type": "file_done", "file": "örnek poliçe.pdf", "done": 1}, stream=stream)
    assert stream.getvalue().endswith("\n")
    payload = json.loads(stream.getvalue())
    assert payload["file"] == "örnek poliçe.pdf"
    assert "\\u" not in stream.getvalue(), "ensure_ascii=False beklenir"


def test_service_config_varsayilanlari() -> None:
    cfg = ServiceConfig(
        folder=Path("klasor"),
        meta_path=Path("klasor/.metadata"),
        not_found_path=Path("klasor/.not-found-metadata"),
    )
    assert cfg.max_pages == 7
    assert cfg.no_cache is False
    assert cfg.poll_interval == 2.0
    assert cfg.stable_checks == 3
    assert cfg.stable_interval_ms == 500
    assert cfg.stable_grace_secs == 10.0
    assert cfg.parent_pid == 0
    assert cfg.compact_every == 200
    assert cfg.handle_stdin is True
    assert cfg.verbose is False
    assert cfg.stream_events is False
    assert cfg.no_lock is False


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





