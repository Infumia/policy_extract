"""Update orchestration: check / download / install (stdlib only)."""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from policy_extract.github_source import (
    fetch_github_release_url,
    fetch_github_update_info,
    is_github_releases_url,
)
from policy_extract.updating_errors import UpdateError
from policy_extract.updating_http import http_get_json
from policy_extract.versioning import compare_versions

_DOWNLOAD_CHUNK_SIZE = 1024 * 256
_HASH_CHUNK_SIZE = 1024 * 1024


def get_current_version() -> str:
    from policy_extract.version import SERVICE_VERSION

    return SERVICE_VERSION


def fetch_update_info(api_url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Read update info from a custom JSON API (or a GitHub release URL)."""
    if is_github_releases_url(api_url):
        return fetch_github_release_url(api_url, timeout=timeout)
    info = http_get_json(api_url, timeout=timeout)
    if not isinstance(info, dict):
        raise UpdateError("update API response must be a JSON object.")
    return _validate_custom_info(info)


def _validate_custom_info(info: dict[str, Any]) -> dict[str, Any]:
    version = info.get("version")
    download_url = info.get("download_url")
    if not isinstance(version, str) or not version.strip():
        raise UpdateError("update API response misses 'version'.")
    if not isinstance(download_url, str) or not download_url.strip():
        raise UpdateError("update API response misses 'download_url'.")
    sha256 = info.get("sha256")
    if sha256 is not None and (not isinstance(sha256, str) or not sha256.strip()):
        raise UpdateError("update API 'sha256' is invalid.")
    return {
        "version": version.strip(),
        "download_url": download_url.strip(),
        "sha256": sha256.strip().lower() if isinstance(sha256, str) else None,
        "notes": info.get("notes"),
    }


def check_for_update(
    api_url: str = "",
    *,
    current_version: str | None = None,
    timeout: float = 15.0,
    github_repo: str = "",
) -> dict[str, Any]:
    """Answer 'is there an update?' as a single JSON-able dict."""
    current = current_version or get_current_version()
    if github_repo.strip():
        latest = fetch_github_update_info(github_repo, timeout=timeout)
    elif api_url.strip():
        latest = fetch_update_info(api_url, timeout=timeout)
    else:
        raise UpdateError("check requires --update-info-url or --github-repo.")
    available = compare_versions(current, latest["version"]) < 0
    return {
        "current_version": current,
        "latest_version": latest["version"],
        "download_url": latest["download_url"],
        "sha256": latest["sha256"],
        "notes": latest.get("notes"),
        "update_available": available,
    }


def sha256_of_file(path: str | Path, *, chunk_size: int = _HASH_CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_update(
    url: str,
    dest: str | Path,
    *,
    expected_sha256: str | None = None,
    timeout: float = 120.0,
    progress_cb: Callable[[int, int | None], None] | None = None,
    chunk_size: int = _DOWNLOAD_CHUNK_SIZE,
) -> dict[str, Any]:
    """Download the new exe and verify sha256. Partial files are deleted."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    received, total = _stream_to_file(url, tmp, timeout, chunk_size, progress_cb)
    actual = sha256_of_file(tmp)
    if expected_sha256 and actual.lower() != expected_sha256.strip().lower():
        tmp.unlink(missing_ok=True)
        raise UpdateError(
            f"sha256 mismatch: expected {expected_sha256[:16]}…, "
            f"downloaded {actual[:16]}… (file deleted, old version kept)."
        )
    if received == 0:
        tmp.unlink(missing_ok=True)
        raise UpdateError("downloaded file is empty.")
    os.replace(tmp, dest)
    return {
        "path": str(dest),
        "size": received,
        "sha256": actual,
        "verified": bool(expected_sha256),
    }


def _stream_to_file(
    url: str,
    tmp: Path,
    timeout: float,
    chunk_size: int,
    progress_cb: Callable[[int, int | None], None] | None,
) -> tuple[int, int | None]:
    request = urllib.request.Request(url, headers={"User-Agent": "policy-extract-updater"})
    received = 0
    total: int | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_total = response.headers.get("Content-Length")
            total = int(raw_total) if raw_total and raw_total.isdigit() else None
            with tmp.open("wb") as handle:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    if progress_cb is not None:
                        progress_cb(received, total)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"download failed: {exc}") from exc
    return received, total


def install_update(
    staged: str | Path, target: str | Path, *, keep_backup: bool = True
) -> dict[str, Any]:
    """Swap the verified staged exe into place (keeps a .bak by default)."""
    staged = Path(staged)
    target = Path(target)
    if not staged.is_file():
        raise UpdateError(f"staged file missing: {staged}")
    if staged.stat().st_size == 0:
        raise UpdateError("staged file is empty, install aborted.")
    backup = _backup_target(target) if target.is_file() and keep_backup else None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
    except OSError as exc:
        raise UpdateError(
            f"install failed ({target}): {exc}. "
            "If the serve process is still running, stop it first."
        ) from exc
    return {"target": str(target), "backup": str(backup) if backup else None}


def _backup_target(target: Path) -> Path:
    backup = target.with_suffix(target.suffix + ".bak")
    try:
        shutil.copy2(target, backup)
    except OSError as exc:
        raise UpdateError(f"could not back up ({target}): {exc}") from exc
    return backup
