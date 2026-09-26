"""Long-running watch service driven by a desktop host app.

- Watches a folder, processes queued PDFs in order, picks up new files.
- Events go to stdout as JSONL; logs go to stderr (silent by default).
- Commands arrive on stdin as JSONL: shutdown | status | pause | resume |
  rescan | retry_file.
"""

from __future__ import annotations

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

from policy_extract.events import STREAMED_TYPES as _STREAMED_TYPES
from policy_extract.events import emit_event, log
from policy_extract.folder_lock import release_folder_lock as _release_folder_lock
from policy_extract.folder_lock import acquire_folder_lock as _acquire_folder_lock_raw
from policy_extract.models import PolicyExtraction
from policy_extract.pdf_io import extract_policy_fast, hash_file as file_sha256
from policy_extract.process_utils import parent_alive
from policy_extract.records import (
    append_record,
    compact_metadata,
    is_not_found_record,
    load_metadata_cache,
    rewrite_not_found,
)
from policy_extract.stability import wait_for_stable

SERVICE_VERSION = "0.5.0"

__all__ = [
    "SERVICE_VERSION",
    "ServiceConfig",
    "ServiceStats",
    "WatchService",
    "acquire_folder_lock",
    "append_record",
    "compact_metadata",
    "emit_event",
    "extract_policy_fast",
    "file_sha256",
    "load_cache_last_wins",
    "log",
    "parent_alive",
    "release_folder_lock",
    "rewrite_not_found",
    "wait_for_stable",
]


def load_cache_last_wins(meta_path: Path) -> dict[str, dict]:
    """Metadata cache where the last line for a file wins."""
    return load_metadata_cache(meta_path)


def acquire_folder_lock(folder: Path) -> Path | None:
    """Single-instance guard that respects the patchable `parent_alive`."""
    return _acquire_folder_lock_raw(folder, alive_fn=parent_alive)


def release_folder_lock(lock: Path) -> None:
    _release_folder_lock(lock)


# ---------------------------------------------------------------------------
# Service configuration
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
    handle_stdin: bool = True
    verbose: bool = False
    stream_events: bool = False
    no_lock: bool = False


@dataclass
class ServiceStats:
    computed: int = 0
    cached: int = 0
    failures: int = 0
    done: int = 0


# ---------------------------------------------------------------------------
# Watch service (single responsibility: orchestrate queue -> extract -> persist)
# ---------------------------------------------------------------------------


class WatchService:
    def __init__(self, config: ServiceConfig) -> None:
        self.cfg = config
        self.stop_event = threading.Event()
        self.paused = threading.Event()
        self.cmd_queue: queue.Queue[dict] = queue.Queue()
        self.pending: deque[str] = deque()
        self.in_queue: set[str] = set()
        self.force_reextract: set[str] = set()
        self.known: dict[str, tuple[int, float]] = {}
        self.cache: dict[str, dict] = {}
        self.stats = ServiceStats()
        self._lock = threading.Lock()

    # -- queue -----------------------------------------------------------

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

    def _take_force_flag(self, name: str) -> bool:
        with self._lock:
            force = name in self.force_reextract
            self.force_reextract.discard(name)
            return force

    # -- logging / events -------------------------------------------------

    def _vlog(self, msg: str) -> None:
        if self.cfg.verbose:
            log(msg)

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.cfg.stream_events or payload.get("type") not in _STREAMED_TYPES:
            self.cfg.emit(payload)

    # -- single file -------------------------------------------------------

    def process_one(self, pdf: Path, *, force: bool = False) -> dict:
        from policy_extract.records import is_not_found_record as _is_not_found
        from policy_extract.records import record_from_extraction

        digest = self._safe_digest(pdf)
        cached = None if self.cfg.no_cache or force else self.cache.get(pdf.name)
        # Serve cache rule (differs from batch): any non-error same-sha entry
        # is reused, including not-found records. This keeps re-runs quiet and
        # avoids duplicate .not-found lines; explicit retry uses force=True.
        if (
            cached is not None
            and "error" not in cached
            and cached.get("sha256") == digest
        ):
            self.stats.cached += 1
            return {"record": cached, "from_cache": True}

        if cached is not None and _is_not_found(cached):
            self._vlog(f"  -> not-found kaydı, yeniden hesaplanıyor: {pdf.name}")
        try:
            result: PolicyExtraction = extract_policy_fast(
                str(pdf), max_pages=self.cfg.max_pages
            )
            record = record_from_extraction(result, sha256=digest)
            self.stats.computed += 1
            return {"record": record, "from_cache": False}
        except Exception as exc:
            self.stats.failures += 1
            digest = self._safe_digest(pdf, fallback=digest)
            return {
                "record": {"file": pdf.name, "sha256": digest, "error": str(exc)},
                "from_cache": False,
            }

    def _safe_digest(self, pdf: Path, *, fallback: str | None = None) -> str | None:
        try:
            return file_sha256(str(pdf))
        except OSError:
            return fallback

    def _needs_stability_wait(self, pdf: Path) -> bool:
        """Only freshly written files wait for stability (old files are done)."""
        try:
            age = time.time() - pdf.stat().st_mtime
        except OSError:
            return False
        return age < max(self.cfg.stable_grace_secs, 0.0)

    # -- persistence -------------------------------------------------------

    def _persist(self, record: dict) -> bool:
        """Cache + append when changed. Returns True when bytes were written."""
        name = record.get("file")
        wrote = False
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
        """One-time startup cleanup of duplicate lines left by a hard kill."""
        try:
            with self.cfg.meta_path.open("r", encoding="utf-8") as handle:
                lines = sum(1 for _ in handle)
        except OSError:
            return
        if lines > len(self.cache):
            compact_metadata(self.cfg.meta_path, self.cache)
            rewrite_not_found(self.cfg.not_found_path, self.cache)

    # -- signals ------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
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

    # -- folder scan ---------------------------------------------------------

    def snapshot_pdfs(self) -> dict[str, Path]:
        try:
            entries = list(self.cfg.folder.iterdir())
        except OSError as exc:
            self._emit(
                {"type": "watch_error", "error": f"klasör okunamadı: {self.cfg.folder} ({exc})"}
            )
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
        for name in list(self.known):
            if name not in current:
                del self.known[name]
        return added

    # -- stdin commands (dispatch table keeps handle_command small) -----------

    def handle_command(self, cmd: dict) -> None:
        action = str(cmd.get("cmd", "")).strip().lower()
        handler = {
            "shutdown": self._cmd_shutdown,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "rescan": self._cmd_rescan,
            "retry_file": lambda: self._cmd_retry_file(cmd),
            "status": self._cmd_status,
        }.get(action, lambda: self._cmd_unknown(cmd))
        handler()

    def _cmd_shutdown(self) -> None:
        self._vlog("kapatma komutu alındı (stdin).")
        self.stop_event.set()

    def _cmd_pause(self) -> None:
        self.paused.set()
        self._emit({"type": "paused"})

    def _cmd_resume(self) -> None:
        self.paused.clear()
        self._emit({"type": "resumed"})

    def _cmd_rescan(self) -> None:
        added = self.poll_once()
        self._emit({"type": "rescanned", "queued_new": added, "queued": self._queued_count()})

    def _cmd_retry_file(self, cmd: dict) -> None:
        name = cmd.get("file")
        if not self._is_valid_retry_name(name):
            self._emit({"type": "watch_error", "error": "geçersiz PDF adı"})
            return
        assert isinstance(name, str)
        with self._lock:
            self.force_reextract.add(name)
        self.enqueue(name, reason="retry")

    def _is_valid_retry_name(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        if name in {"", ".", ".."} or Path(name).name != name:
            return False
        if Path(name).is_absolute() or "/" in name or "\\" in name or ":" in name:
            return False
        if not name.lower().endswith(".pdf"):
            return False
        return (self.cfg.folder / name).is_file()

    def _cmd_status(self) -> None:
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

    def _cmd_unknown(self, cmd: dict) -> None:
        self._emit({"type": "unknown_command", "command": cmd})

    def _stdin_loop(self) -> None:
        """Read one JSON command per stdin line; EOF means the host is gone."""
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

    # -- main loop (each phase is a small method) ------------------------------

    def run(self) -> int:
        folder = self.cfg.folder
        if not folder.is_dir():
            self._emit({"type": "fatal", "error": f"klasör bulunamadı: {folder}"})
            return 2

        self._init_cache()
        lock_path = self._acquire_lock()
        if lock_path is None and not self.cfg.no_lock:
            self._emit(
                {
                    "type": "fatal",
                    "error": f"klasör başka bir serve tarafından kullanılıyor: {folder}",
                }
            )
            return 2
        try:
            return self._serve_forever(lock_path)
        finally:
            if lock_path is not None:
                release_folder_lock(lock_path)

    def _init_cache(self) -> None:
        self.cache = load_cache_last_wins(self.cfg.meta_path)
        if self.cfg.no_cache:
            self.cache = {}
            self.cfg.meta_path.unlink(missing_ok=True)
            self.cfg.not_found_path.unlink(missing_ok=True)
        else:
            self._compact_if_needed()

    def _acquire_lock(self) -> Path | None:
        if self.cfg.no_lock:
            return None
        return acquire_folder_lock(self.cfg.folder)

    def _serve_forever(self, lock_path: Path | None) -> int:
        del lock_path  # released by run()'s finally block
        self._install_signal_handlers()
        if self.cfg.handle_stdin:
            threading.Thread(
                target=self._stdin_loop, name="stdin-commands", daemon=True
            ).start()
        threading.Thread(target=self._parent_watch_loop, name="parent-watch", daemon=True).start()
        self._enqueue_initial_pdfs()
        self._emit_hello()
        self._event_loop()
        self._shutdown()
        return 0

    def _enqueue_initial_pdfs(self) -> None:
        initial = sorted(self.snapshot_pdfs().values(), key=lambda p: p.name)
        for path in initial:
            try:
                stat = path.stat()
                self.known[path.name] = (stat.st_size, stat.st_mtime)
            except OSError:
                continue
            self.enqueue(path.name, reason="initial")

    def _emit_hello(self) -> None:
        self._emit(
            {
                "type": "hello",
                "version": SERVICE_VERSION,
                "pid": os.getpid(),
                "folder": str(self.cfg.folder),
                "total_queued": self._queued_count(),
                "parent_pid": self.cfg.parent_pid,
            }
        )
        self._vlog(
            f"servis başladı: {self._queued_count()} PDF kuyrukta, "
            f"izleniyor: {self.cfg.folder}"
        )

    def _event_loop(self) -> None:
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
                idle_announced = self._emit_idle_once(idle_announced)
                self.stop_event.wait(min(0.5, self.cfg.poll_interval))
                continue
            idle_announced = False
            self._process_queued_file(name)

    def _emit_idle_once(self, already_announced: bool) -> bool:
        if already_announced:
            return True
        self._emit(
            {
                "type": "idle",
                "done": self.stats.done,
                "computed": self.stats.computed,
                "cached": self.stats.cached,
                "failures": self.stats.failures,
            }
        )
        return True

    def _process_queued_file(self, name: str) -> None:
        pdf = self.cfg.folder / name
        if not pdf.is_file():
            self._vlog(f"atlandı (silinmiş): {name}")
            return
        if self._needs_stability_wait(pdf) and not wait_for_stable(
            pdf,
            checks=self.cfg.stable_checks,
            interval_ms=self.cfg.stable_interval_ms,
            stop=self.stop_event,
        ):
            if not self.stop_event.is_set():
                self._vlog(f"atlandı (kararsız/silinmiş): {name}")
            return
        self._emit_progress(name)
        try:
            force = self._take_force_flag(name)
            outcome = self.process_one(pdf, force=force)
            record = outcome["record"]
            self.stats.done += 1
            wrote = False if outcome["from_cache"] else self._persist(record)
        except Exception as exc:
            self._record_internal_failure(name, exc)
            return
        self._emit_outcome(name, record, outcome["from_cache"], wrote)

    def _emit_progress(self, name: str) -> None:
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

    def _record_internal_failure(self, name: str, exc: Exception) -> None:
        # Safety net: a single file never kills the service. Persist the error
        # when possible, then emit file_error like the normal error path.
        self.stats.failures += 1
        self.stats.done += 1
        record = {"file": name, "sha256": None, "error": f"işlenemedi: {exc}"}
        try:
            self._persist(record)
        except Exception:
            pass
        self._emit(
            {
                "type": "file_error",
                "file": name,
                "error": record.get("error"),
                "sha256": record.get("sha256"),
                "done": self.stats.done,
            }
        )
        self._vlog(f"HATA {name}: {exc}")

    def _emit_outcome(
        self, name: str, record: dict, from_cache: bool, wrote: bool
    ) -> None:
        if "error" in record:
            self._emit(
                {
                    "type": "file_error",
                    "file": name,
                    "error": record.get("error"),
                    "sha256": record.get("sha256"),
                    "done": self.stats.done,
                }
            )
            self._vlog(f"HATA {name}: {record.get('error')}")
        elif from_cache:
            self._emit({"type": "file_cached", "record": record, "done": self.stats.done})
            self._vlog(f"  -> önbellekten (sha256 eşleşti): {name}")
        else:
            self._emit({"type": "file_done", "record": record, "done": self.stats.done})
            self._vlog(
                f"  -> police_no={record.get('police_no')} company={record.get('company')}"
            )
            if wrote and is_not_found_record(record):
                append_record(self.cfg.not_found_path, record)

    def _shutdown(self) -> None:
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
            pass
