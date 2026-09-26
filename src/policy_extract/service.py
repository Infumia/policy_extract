"""Flutter'dan yönetilen uzun süreli servis modu.

Kısa özet (Flutter entegrasyonu için):
- Exe tek seferlik çalışıp çıkmaz; klasörü izler, kuyruktaki PDF'leri
  sırayla işler, yeni gelen dosyaları da kuyruğa alır.
- Olaylar stdout'a JSONL (satır başına bir JSON) yazılır, loglar stderr'e
  gider. Flutter stdout satırlarını parse eder.
- Komutlar stdin'e JSONL yazılır: {"cmd": "shutdown"|"status"|"pause"|"resume"|"rescan"}.
- Flutter kapanınca exe'nin de kapanması için iki mekanizma vardır:
  1. stdin pipe'ı kapanırsa (EOF) servis kendini kapatır,
  2. --parent-pid verilirse ebeveyn süreç ölünce servis kapanır.

.metadata yazımı artımlıdır (her dosyada append + periyodik compact),
böylece 70bin dosyalık taramada yeni gelen dosyalar kaybolmaz ve
kayıtlar anlık olarak Flutter/.metadata okuyucularına görünür.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from policy_extract.extractor import PolicyExtraction, extract_policy_fast, file_sha256

SERVICE_VERSION = "0.5.0"


# ---------------------------------------------------------------------------
# Event / log helpers (Flutter protokolü)
# ---------------------------------------------------------------------------


# Varsayılan sessiz stdout: yalnızca istek-yanıt + yaşam döngüsü olayları akar
# (hello/status/paused/resumed/rescanned/bye/fatal/watch_error/...).
# Dosya başı ara olaylar (progress/file_done/...) yalnızca
# --stream-events ile akar; normalde Flutter durumu `status` komutuyla
# ve kayıtları .metadata dosyasından okur.
_STREAMED_TYPES = frozenset(
    {"progress", "file_done", "file_cached", "file_error", "file_queued", "idle"}
)


def emit_event(payload: dict[str, Any], *, stream=None) -> None:
    """stdout'a tek satır JSON yazar (Flutter bunu dinler)."""
    out = stream or sys.stdout
    out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    out.flush()


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Ebeveyn süreç takibi (Flutter kapanınca exe de kapansın)
# ---------------------------------------------------------------------------


def parent_alive(pid: int) -> bool:
    """pid yaşıyorsa True. Kontrol edilemiyorsa False (güvenli tarafta kal)."""
    if pid <= 0:
        return True
    try:
        if os.name == "nt":
            # Ek bağımlılık (psutil) olmadan Win32 API ile kontrol.
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                if not ok:
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 0)
            return True
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Servis konfigürasyonu
# ---------------------------------------------------------------------------


@dataclass
class ServiceConfig:
    folder: Path
    meta_path: Path
    not_found_path: Path
    max_pages: int = 7
    no_cache: bool = False
    poll_interval: float = 2.0
    stable_checks: int = 3
    stable_interval_ms: int = 500
    stable_grace_secs: float = 10.0
    parent_pid: int = 0
    compact_every: int = 200
    emit: Callable[[dict[str, Any]], None] = field(default=emit_event)
    handle_stdin: bool = True  # False: stdin komut/EOF takibi yapma (test/gömülü kullanım)
    verbose: bool = False  # True: detay logları stderr'e yaz (varsayılan tam sessiz)
    stream_events: bool = False  # True: dosya başı ara olayları da stdout'a yaz
    no_lock: bool = False  # True: tek-instance klasör kilidini atla (özel kurulum)


@dataclass
class ServiceStats:
    computed: int = 0
    cached: int = 0
    failures: int = 0
    done: int = 0  # bu oturumda biten dosya sayısı


# ---------------------------------------------------------------------------
# Dosya kararlılığı: yarım yazılmış (kopyalanmakta olan) PDF'i bekle
# ---------------------------------------------------------------------------


def wait_for_stable(
    pdf: Path,
    *,
    checks: int = 3,
    interval_ms: int = 500,
    stop: threading.Event | None = None,
) -> bool:
    """Dosya boyutu `checks` kez üst üste aynıysa True, silindiyse False."""
    if checks <= 1:
        return pdf.is_file()
    last_size: int | None = None
    steady = 0
    for _ in range(max(checks * 5, checks)):
        if stop is not None and stop.is_set():
            return False
        try:
            size = pdf.stat().st_size
        except OSError:
            return False
        if last_size is not None and size == last_size:
            steady += 1
            if steady >= checks - 1:
                return True
        else:
            steady = 0
        last_size = size
        time.sleep(interval_ms / 1000.0)
    return pdf.is_file()


# ---------------------------------------------------------------------------
# .metadata artımlı yazım (append + compact)
# ---------------------------------------------------------------------------


def load_cache_last_wins(meta_path: Path) -> dict[str, dict]:
    """Aynı dosya birden çok kez yazıldıysa son satır kazanır."""
    from policy_extract.cli import load_metadata_cache

    return load_metadata_cache(meta_path)


def append_record(meta_path: Path, record: dict) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with meta_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def compact_metadata(meta_path: Path, cache: dict[str, dict]) -> None:
    """Birimşen kayıtları tekilleştirip sıralı yazar (atomik replace)."""
    if not cache and not meta_path.is_file():
        return  # yazacak kayıt yoksa boş dosya üretme
    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as fh:
        for name in sorted(cache):
            fh.write(json.dumps(cache[name], ensure_ascii=False) + "\n")
    os.replace(tmp, meta_path)


def rewrite_not_found(not_found_path: Path, cache: dict[str, dict]) -> None:
    from policy_extract.cli import is_not_found_record

    if not cache and not not_found_path.is_file():
        return  # yazacak kayıt yoksa boş dosya üretme
    not_found_path.parent.mkdir(parents=True, exist_ok=True)
    with not_found_path.open("w", encoding="utf-8") as fh:
        for name in sorted(cache):
            record = cache[name]
            if is_not_found_record(record):
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def acquire_folder_lock(folder: Path) -> Path | None:
    """Tek-instance koruması: klasör başına bir serve.

    Başarılıysa kilit yolu döner; klasör doluysa (canlı pid) None döner.
    Ölü pid'den kalma bayat kilit devralınır (kill sonrası takılma olmaz).
    """
    lock = folder / ".policy-extract.lock"
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                raw = lock.read_text(encoding="utf-8").strip()
            except OSError:
                raw = ""
            try:
                pid = int(raw.split()[0])
            except (ValueError, IndexError):
                pid = 0
            if pid > 0 and pid != os.getpid() and parent_alive(pid):
                return None  # gerçekten çalışan bir serve var
            try:
                lock.unlink()  # bayat kilit, temizle ve tekrar dene
            except OSError:
                return None
            continue
        except OSError:
            return None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()}\n")
        except OSError:
            return None
        return lock
    return None


def release_folder_lock(lock: Path) -> None:
    """Kilidi yalnızca sahibi bırakır (başkasının kilidine dokunmaz)."""
    try:
        raw = lock.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if raw.split()[0:1] == [str(os.getpid())]:
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Watch servisi
# ---------------------------------------------------------------------------


class WatchService:
    def __init__(self, config: ServiceConfig) -> None:
        self.cfg = config
        self.stop_event = threading.Event()
        self.paused = threading.Event()  # set() ise duraklatılmış
        self.cmd_queue: queue.Queue[dict] = queue.Queue()
        self.pending: deque[str] = deque()
        self.in_queue: set[str] = set()
        self.force_reextract: set[str] = set()
        self.known: dict[str, tuple[int, float]] = {}  # dosya -> (boyut, mtime)
        self.cache: dict[str, dict] = {}
        self.stats = ServiceStats()
        self._lock = threading.Lock()

    # -- kuyruk ----------------------------------------------------------

    def enqueue(self, name: str, *, reason: str = "created") -> bool:
        with self._lock:
            if name in self.in_queue:
                return False
            self.in_queue.add(name)
            self.pending.append(name)
            queued = len(self.pending)
        self._emit({"type": "file_queued", "file": name, "reason": reason, "queued": queued})
        return True

    def _dequeue(self) -> str | None:
        with self._lock:
            if not self.pending:
                return None
            name = self.pending.popleft()
            self.in_queue.discard(name)
            return name

    def _queued_count(self) -> int:
        with self._lock:
            return len(self.pending)

    def _vlog(self, msg: str) -> None:
        """Serve modu varsayılan tam sessizdir; bu satırlar yalnızca
        --verbose ile stderr'e yazılır. Flutter durum bilgisini stdout
        olaylarından alır, stderr'i dinlemez."""
        if self.cfg.verbose:
            log(msg)

    def _emit(self, payload: dict[str, Any]) -> None:
        """Ara olayları sessiz modda yutar, yanıt/yaşam döngüsü geçirir."""
        if self.cfg.stream_events or payload.get("type") not in _STREAMED_TYPES:
            self.cfg.emit(payload)

    # -- tek dosya işleme (cli.run_batch ile aynı önbellek kuralı) --------

    def process_one(self, pdf: Path, *, force: bool = False) -> dict:
        from policy_extract.cli import is_not_found_record, record_from_extraction

        digest: str | None
        try:
            digest = file_sha256(str(pdf))
        except OSError:
            digest = None

        cached = None if self.cfg.no_cache or force else self.cache.get(pdf.name)
        if (
            cached is not None
            and "error" not in cached
            and cached.get("sha256") == digest
        ):
            self.stats.cached += 1
            return {"record": cached, "from_cache": True}

        if cached is not None and is_not_found_record(cached):
            self._vlog(f"  -> not-found kaydı, yeniden hesaplanıyor: {pdf.name}")
        try:
            result: PolicyExtraction = extract_policy_fast(str(pdf), max_pages=self.cfg.max_pages)
            record = record_from_extraction(result, sha256=digest)
            self.stats.computed += 1
            return {"record": record, "from_cache": False}
        except Exception as exc:
            self.stats.failures += 1
            try:
                digest = file_sha256(str(pdf))
            except OSError:
                pass
            return {"record": {"file": pdf.name, "sha256": digest, "error": str(exc)}, "from_cache": False}

    def _needs_stability_wait(self, pdf: Path) -> bool:
        """Yalnızca taze yazılmış dosya kararlılık bekler.

        mtime'ı grace süresinden eski dosya artık yazılmıyordur; kopyası
        bitmiş demektir, doğrudan sha+önbellek kontrolüne geçilir. Bu olmadan
        70binlik backlog dosya başı ~1 sn uyuyarak saatlerce beklerdi.
        """
        try:
            age = time.time() - pdf.stat().st_mtime
        except OSError:
            return False
        return age < max(self.cfg.stable_grace_secs, 0.0)

    def _persist(self, record: dict) -> bool:
        """Kaydı önbelleğe alıp dosyaya ekler. Gerçekten yazdıysa True.

        Aynı kayıt tekrar hesaplandıysa (not-found retry gibi) dosyaya
        dokunulmaz: çift satırın kökü buydu.
        """
        wrote = False
        name = record.get("file")
        if isinstance(name, str):
            if self.cache.get(name) != record:
                self.cache[name] = record
                append_record(self.cfg.meta_path, record)
                wrote = True
            else:
                self.cache[name] = record
        if self.stats.done % max(self.cfg.compact_every, 1) == 0:
            compact_metadata(self.cfg.meta_path, self.cache)
            rewrite_not_found(self.cfg.not_found_path, self.cache)
        return wrote

    def _compact_if_needed(self) -> None:
        """Açılışta çift satır varsa tekilleştir (önceki kill'den kalmış olabilir).

        Satır sayısı kayıt sayısından fazlaysa dosyada tekrar vardır;
        bu tek seferlik ucuz temizlik sonrası serve sıfır yazımla çalışır.
        """
        try:
            with self.cfg.meta_path.open("r", encoding="utf-8") as fh:
                lines = sum(1 for _ in fh)
        except OSError:
            return
        if lines > len(self.cache):
            compact_metadata(self.cfg.meta_path, self.cache)
            rewrite_not_found(self.cfg.not_found_path, self.cache)

    def _install_signal_handlers(self) -> None:
        """Ctrl+C / kapatma sinyali -> o anki dosyayı bitirip toparlanarak çık.

        Yalnızca ana thread'de kurulur (serve prosesi). İkinci sinyalde
        varsayılan davranışa dönülür (sert çıkış hâlâ mümkün).
        """
        if threading.current_thread() is not threading.main_thread():
            return

        def _graceful(signum: int, frame: object) -> None:
            try:
                signal.signal(signal.SIGINT, signal.default_int_handler)
            except (OSError, ValueError):
                pass
            self._vlog("kapatma sinyali alındı, toparlanıyor...")
            self.stop_event.set()

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _graceful)
            except (OSError, ValueError, RuntimeError):
                pass

    # -- klasör tarama (polling; exe için ek bağımlılıksız) ---------------

    def snapshot_pdfs(self) -> dict[str, Path]:
        try:
            entries = list(self.cfg.folder.iterdir())
        except OSError as exc:
            self._emit({"type": "watch_error", "error": f"klasör okunamadı: {self.cfg.folder} ({exc})"})
            self._vlog(f"klasör okunamadı: {self.cfg.folder} ({exc})")
            return {}
        return {p.name: p for p in entries if p.is_file() and p.suffix.lower() == ".pdf"}

    def poll_once(self, *, reason_initial: bool = False) -> int:
        current = self.snapshot_pdfs()
        added = 0
        for name, path in current.items():
            try:
                stat = path.stat()
                sig = (stat.st_size, stat.st_mtime)
            except OSError:
                continue
            old = self.known.get(name)
            if old is None:
                self.known[name] = sig
                if self.enqueue(name, reason="initial" if reason_initial else "created"):
                    added += 1
            elif old != sig:
                self.known[name] = sig
                if self.enqueue(name, reason="modified"):
                    added += 1
        # Silinenleri known'dan temizle (cache'de tutmaya devam et).
        for name in list(self.known):
            if name not in current:
                del self.known[name]
        return added

    # -- stdin komutları ---------------------------------------------------

    def handle_command(self, cmd: dict) -> None:
        action = str(cmd.get("cmd", "")).strip().lower()
        if action == "shutdown":
            self._vlog("kapatma komutu alındı (stdin).")
            self.stop_event.set()
        elif action == "pause":
            self.paused.set()
            self._emit({"type": "paused"})
        elif action == "resume":
            self.paused.clear()
            self._emit({"type": "resumed"})
        elif action == "rescan":
            added = self.poll_once()
            self._emit({"type": "rescanned", "queued_new": added, "queued": self._queued_count()})
        elif action == "retry_file":
            name = cmd.get("file")
            if (
                not isinstance(name, str)
                or name in {"", ".", ".."}
                or Path(name).name != name
                or Path(name).is_absolute()
                or "/" in name
                or "\\" in name
                or ":" in name
                or not name.lower().endswith(".pdf")
                or not (self.cfg.folder / name).is_file()
            ):
                self._emit({"type": "watch_error", "error": "geçersiz PDF adı"})
            else:
                with self._lock:
                    self.force_reextract.add(name)
                self.enqueue(name, reason="retry")
        elif action == "status":
            self._emit(
                {
                    "type": "status",
                    "done": self.stats.done,
                    "computed": self.stats.computed,
                    "cached": self.stats.cached,
                    "failures": self.stats.failures,
                    "queued": self._queued_count(),
                    "paused": self.paused.is_set(),
                    "version": SERVICE_VERSION,
                    "pid": os.getpid(),
                }
            )
        else:
            self._emit({"type": "unknown_command", "command": cmd})

    def _stdin_loop(self) -> None:
        """stdin her satırda bir JSON komut bekler; EOF Flutter kapandı demektir."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                self._emit({"type": "bad_command", "line": line[:200]})
                continue
            if isinstance(cmd, dict):
                self.handle_command(cmd)
        # Pipe kapandı -> Flutter tarafı gitti, biz de kapan.
        # (Zaten planlı kapanıyorsak ek satır yazma.)
        if not self.stop_event.is_set():
            self._vlog("stdin kapandı (ebeveyn gitti), servis duruyor.")
        self.stop_event.set()

    def _parent_watch_loop(self) -> None:
        pid = self.cfg.parent_pid
        if pid <= 0:
            return
        while not self.stop_event.wait(2.0):
            if not parent_alive(pid):
                self._vlog(f"ebeveyn süreç öldü (pid={pid}), servis duruyor.")
                self._emit({"type": "parent_gone", "parent_pid": pid})
                self.stop_event.set()
                return

    # -- ana döngü ----------------------------------------------------------

    def run(self) -> int:
        from policy_extract.cli import is_not_found_record

        folder = self.cfg.folder
        if not folder.is_dir():
            self._emit({"type": "fatal", "error": f"klasör bulunamadı: {folder}"})
            return 2

        self.cache = load_cache_last_wins(self.cfg.meta_path)
        if self.cfg.no_cache:
            # Batch ile aynı kural: önbellek yok sayılıyorsa eski kayıtlar
            # baştan çöpe çıkar, dosya şişmez (bellek de sıfırlanır).
            self.cache = {}
            self.cfg.meta_path.unlink(missing_ok=True)
            self.cfg.not_found_path.unlink(missing_ok=True)
        else:
            self._compact_if_needed()  # önceki kill'den kalan çift satırları temizle

        lock_path: Path | None = None
        if not self.cfg.no_lock:
            lock_path = acquire_folder_lock(folder)
            if lock_path is None:
                self._emit(
                    {
                        "type": "fatal",
                        "error": f"klasör başka bir serve tarafından kullanılıyor: {folder}",
                    }
                )
                return 2
        self._install_signal_handlers()

        if self.cfg.handle_stdin:
            threading.Thread(target=self._stdin_loop, name="stdin-commands", daemon=True).start()
        threading.Thread(target=self._parent_watch_loop, name="parent-watch", daemon=True).start()

        initial = sorted(self.snapshot_pdfs().values(), key=lambda p: p.name)
        for path in initial:
            try:
                stat = path.stat()
                self.known[path.name] = (stat.st_size, stat.st_mtime)
            except OSError:
                continue
            self.enqueue(path.name, reason="initial")

        self._emit(
            {
                "type": "hello",
                "version": SERVICE_VERSION,
                "pid": os.getpid(),
                "folder": str(folder),
                "total_queued": self._queued_count(),
                "parent_pid": self.cfg.parent_pid,
            }
        )
        self._vlog(f"servis başladı: {len(initial)} PDF kuyrukta, izleniyor: {folder}")

        last_poll = 0.0
        idle_announced = False

        while not self.stop_event.is_set():
            if self.paused.is_set():
                time.sleep(0.2)
                continue

            now = time.monotonic()
            if now - last_poll >= self.cfg.poll_interval:
                self.poll_once()
                last_poll = now

            name = self._dequeue()
            if name is None:
                if not idle_announced:
                    self._emit(
                        {
                            "type": "idle",
                            "done": self.stats.done,
                            "computed": self.stats.computed,
                            "cached": self.stats.cached,
                            "failures": self.stats.failures,
                        }
                    )
                    idle_announced = True
                # Kuyruk boşken CPU yakmamak için kısa uyku (poll aralığını geçmez).
                self.stop_event.wait(min(0.5, self.cfg.poll_interval))
                continue
            idle_announced = False

            pdf = folder / name
            if not pdf.is_file():
                self._vlog(f"atlandı (silinmiş): {name}")
                continue
            if self._needs_stability_wait(pdf) and not wait_for_stable(
                pdf,
                checks=self.cfg.stable_checks,
                interval_ms=self.cfg.stable_interval_ms,
                stop=self.stop_event,
            ):
                if self.stop_event.is_set():
                    break
                self._vlog(f"atlandı (kararsız/silinmiş): {name}")
                continue

            total_hint = self.stats.done + self._queued_count() + 1
            self._emit(
                {
                    "type": "progress",
                    "file": name,
                    "done": self.stats.done + 1,
                    "total_hint": total_hint,
                    "queued": self._queued_count(),
                }
            )
            self._vlog(f"[{self.stats.done + 1}] {name}...")

            try:
                with self._lock:
                    force = name in self.force_reextract
                    self.force_reextract.discard(name)
                outcome = self.process_one(pdf, force=force)
                record = outcome["record"]
                self.stats.done += 1
                if not outcome["from_cache"]:
                    # Önbellek isabeti dosyaya yazılmaz: kayıt zaten orada.
                    # Her açılışta aynı satırları tekrar append etmek dosya şişirir.
                    wrote = self._persist(record)
                else:
                    wrote = False
            except Exception as exc:  # güvenlik ağı: tek dosya servisi öldürmesin
                self.stats.failures += 1
                self.stats.done += 1
                record = {"file": name, "sha256": None, "error": f"işlenemedi: {exc}"}
                try:
                    wrote = self._persist(record)  # hata kaydı da metadata'ya düşsün
                except Exception:
                    wrote = False  # disk doluysa bile servis yaşar, devam eder
                outcome = {"record": record, "from_cache": False}
                self._vlog(f"HATA {name}: {exc}")

            if "error" in record:
                self._emit({"type": "file_error", "file": name, "error": record.get("error"), "sha256": record.get("sha256"), "done": self.stats.done})
                self._vlog(f"HATA {name}: {record.get('error')}")
            elif outcome["from_cache"]:
                self._emit({"type": "file_cached", "record": record, "done": self.stats.done})
                self._vlog(f"  -> önbellekten (sha256 eşleşti): {name}")
            else:
                self._emit({"type": "file_done", "record": record, "done": self.stats.done})
                self._vlog(f"  -> police_no={record.get('police_no')} company={record.get('company')}")
                if wrote and is_not_found_record(record):
                    # Aynı sonuç tekrar hesaplandıysa dosyaya dokunma;
                    # çift satırın ikinci kökü buydu.
                    append_record(self.cfg.not_found_path, record)

        # Kapanış: metadata'yı tekilleştir, Flutter'a veda et, kilidi bırak.
        # persist_error bye olayına gömülür; stderr'e yazılmaz.
        try:
            persist_error: str | None = None
            try:
                compact_metadata(self.cfg.meta_path, self.cache)
                rewrite_not_found(self.cfg.not_found_path, self.cache)
            except OSError as exc:
                persist_error = str(exc)
                self._vlog(f"kapanışta metadata yazılamadı: {exc}")
            self._emit(
                {
                    "type": "bye",
                    "done": self.stats.done,
                    "computed": self.stats.computed,
                    "cached": self.stats.cached,
                    "failures": self.stats.failures,
                    "persist_error": persist_error,
                }
            )
            self._vlog(
                f"servis durdu: {self.stats.done} dosya "
                f"({self.stats.computed} hesaplandı, {self.stats.cached} önbellekten, "
                f"{self.stats.failures} hata)."
            )
        finally:
            if lock_path is not None:
                release_folder_lock(lock_path)
        return 0
