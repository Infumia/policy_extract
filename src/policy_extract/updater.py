"""Exe update helper facade (implementation in focused modules).

This module keeps the legacy single-module surface: tests monkeypatch
attributes such as ``updater.fetch_github_update_info`` and expect
``updater.check_for_update`` to honor the patch. Therefore cross-function
calls below resolve through this module's globals (patchable), while the
heavy lifting delegates to focused submodules.
"""

from __future__ import annotations

from typing import Any, Callable

from policy_extract.github_source import GITHUB_API_BASE, PREFERRED_EXE_ASSETS
from policy_extract.github_source import fetch_github_release_url as _github_release_url
from policy_extract.github_source import fetch_github_update_info as _github_update_info
from policy_extract.github_source import fetch_sha256_sidecar as _fetch_sha256_sidecar
from policy_extract.github_source import is_github_releases_url
from policy_extract.github_source import parse_github_release as _parse_github_release
from policy_extract.github_source import parse_github_repo
from policy_extract.github_source import pick_exe_asset as _pick_exe_asset
from policy_extract.update_service import download_update as _download_update
from policy_extract.update_service import get_current_version
from policy_extract.update_service import install_update as _install_update
from policy_extract.update_service import sha256_of_file
from policy_extract.updating_errors import UpdateError
from policy_extract.updating_http import http_get_json as _http_get_json
from policy_extract.versioning import compare_versions, parse_version_parts

__all__ = [
    "GITHUB_API_BASE",
    "PREFERRED_EXE_ASSETS",
    "UpdateError",
    "check_for_update",
    "compare_versions",
    "download_update",
    "fetch_github_release_url",
    "fetch_github_update_info",
    "fetch_update_info",
    "get_current_version",
    "install_update",
    "is_github_releases_url",
    "parse_github_repo",
    "parse_version_parts",
    "sha256_of_file",
]


def fetch_github_update_info(repo: str, *, timeout: float = 15.0) -> dict[str, Any]:
    return _github_update_info(repo, timeout=timeout)


def fetch_github_release_url(release_url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    return _github_release_url(release_url, timeout=timeout)


def fetch_update_info(api_url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    from policy_extract.update_service import _validate_custom_info

    if is_github_releases_url(api_url):
        return fetch_github_release_url(api_url, timeout=timeout)
    info = _http_get_json(api_url, timeout=timeout)
    if not isinstance(info, dict):
        raise UpdateError("update API response must be a JSON object.")
    return _validate_custom_info(info)


def check_for_update(
    api_url: str = "",
    *,
    current_version: str | None = None,
    timeout: float = 15.0,
    github_repo: str = "",
) -> dict[str, Any]:
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


def download_update(
    url: str,
    dest,
    *,
    expected_sha256: str | None = None,
    timeout: float = 120.0,
    progress_cb: Callable[[int, int | None], None] | None = None,
    chunk_size: int = 1024 * 256,
) -> dict[str, Any]:
    return _download_update(
        url,
        dest,
        expected_sha256=expected_sha256,
        timeout=timeout,
        progress_cb=progress_cb,
        chunk_size=chunk_size,
    )


def install_update(staged, target, *, keep_backup: bool = True) -> dict[str, Any]:
    return _install_update(staged, target, keep_backup=keep_backup)
