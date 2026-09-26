"""GitHub Releases source: repo parsing, asset picking, sidecar sha."""

from __future__ import annotations

import re
import urllib.request
from typing import Any

from policy_extract.updating_errors import UpdateError
from policy_extract.updating_http import http_get_json

GITHUB_API_BASE = "https://api.github.com"

PREFERRED_EXE_ASSETS = (
    "policy-extract-windows-x64.exe",
    "policy-extract.exe",
)

_SIDE_CAR_CANDIDATES = ("sha256",)
_SHA_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
_MAX_SIDECAR_BYTES = 16 * 1024


def is_github_releases_url(url: str) -> bool:
    return "api.github.com/repos/" in url and "/releases/" in url


def parse_github_repo(value: str) -> str:
    """Normalize OWNER/REPO from 'owner/repo', github.com or API URL."""
    cleaned = value.strip().removesuffix(".git").strip()
    # api.github.com must be checked first: it contains "github.com/" too.
    match = re.search(r"api\.github\.com/repos/([^/]+/[^/]+)", cleaned)
    if match:
        return match.group(1).strip("/")
    match = re.search(r"github\.com/([^/]+/[^/]+)", cleaned)
    if match:
        return match.group(1).strip("/")
    if re.fullmatch(r"[^/\s]+/[^/\s]+", cleaned):
        return cleaned
    raise UpdateError(
        f"invalid GitHub repo '{value}'. Expected OWNER/REPO "
        "(e.g. infumia/policy-extract)."
    )


def pick_exe_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_name = {a.get("name", ""): a for a in assets if isinstance(a, dict)}
    for preferred in PREFERRED_EXE_ASSETS:
        if preferred in by_name:
            return by_name[preferred]
    windows_exes = [
        a
        for a in assets
        if isinstance(a, dict)
        and str(a.get("name", "")).lower().endswith(".exe")
        and "windows" in str(a.get("name", "")).lower()
    ]
    if windows_exes:
        return sorted(windows_exes, key=lambda a: str(a.get("name")))[0]
    any_exes = [
        a
        for a in assets
        if isinstance(a, dict) and str(a.get("name", "")).lower().endswith(".exe")
    ]
    if any_exes:
        return sorted(any_exes, key=lambda a: str(a.get("name")))[0]
    return None


def parse_github_release(data: dict[str, Any]) -> dict[str, Any]:
    tag = data.get("tag_name") or data.get("name") or ""
    if not isinstance(tag, str) or not tag.strip():
        raise UpdateError("GitHub release has no tag_name.")
    version = tag.strip().lstrip("vV")
    assets = data.get("assets") or []
    if not isinstance(assets, list):
        raise UpdateError("GitHub release assets are malformed.")
    exe = pick_exe_asset(assets)
    if exe is None:
        raise UpdateError("GitHub release contains no .exe asset.")
    download_url = exe.get("browser_download_url")
    if not isinstance(download_url, str) or not download_url.strip():
        raise UpdateError("GitHub .exe asset has no download URL.")
    return {
        "version": version,
        "download_url": download_url.strip(),
        "_exe_asset_name": str(exe.get("name", "")),
        "_assets": assets,
        "notes": data.get("body"),
    }


def _download_text(url: str, *, timeout: float) -> str | None:
    request = urllib.request.Request(url.strip(), headers={"User-Agent": "policy-extract-updater"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(_MAX_SIDECAR_BYTES).decode("utf-8", errors="replace").strip()
    except Exception:
        return None


def _fetch_exact_sidecar(
    assets: list[dict[str, Any]], exe_name: str, *, timeout: float
) -> str | None:
    candidates = {
        f"{exe_name}.sha256",
        f"{exe_name}.sha256.txt",
        "sha256.txt",
        "checksums.txt",
    }
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        url = asset.get("browser_download_url")
        if name in candidates and isinstance(url, str) and url.strip():
            text = _download_text(url, timeout=timeout)
            if text is None:
                return None
            match = _SHA_RE.search(text)
            return match.group(1).lower() if match else None
    return None


def fetch_sha256_sidecar(
    assets: list[dict[str, Any]], exe_name: str, *, timeout: float
) -> str | None:
    """Download '<exe>.sha256' sidecar when the release provides one."""
    exact = _fetch_exact_sidecar(assets, exe_name, timeout=timeout)
    if exact is not None or _has_exact_candidate(assets, exe_name):
        return exact
    return _fetch_fallback_sidecar(assets, exe_name, timeout=timeout)


def _has_exact_candidate(assets: list[dict[str, Any]], exe_name: str) -> bool:
    candidates = {
        f"{exe_name}.sha256",
        f"{exe_name}.sha256.txt",
        "sha256.txt",
        "checksums.txt",
    }
    return any(
        isinstance(a, dict) and str(a.get("name", "")) in candidates for a in assets
    )


def _fetch_fallback_sidecar(
    assets: list[dict[str, Any]], exe_name: str, *, timeout: float
) -> str | None:
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", "")).lower()
        url = asset.get("browser_download_url")
        if "sha256" not in name or not isinstance(url, str) or not url.strip():
            continue
        text = _download_text(url, timeout=timeout)
        if text is None:
            return None
        hashes = _SHA_RE.findall(text)
        if len(hashes) != 1:
            return None
        if exe_name and exe_name not in text and "checksums" in name:
            return None
        return hashes[0].lower()
    return None


def fetch_github_release_url(release_url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    data = http_get_json(release_url, timeout=timeout)
    if not isinstance(data, dict):
        raise UpdateError("GitHub release response must be a JSON object.")
    parsed = parse_github_release(data)
    assets = parsed.pop("_assets")
    exe_name = parsed.pop("_exe_asset_name")
    parsed["sha256"] = fetch_sha256_sidecar(assets, exe_name, timeout=timeout)
    return parsed


def fetch_github_update_info(repo: str, *, timeout: float = 15.0) -> dict[str, Any]:
    slug = parse_github_repo(repo)
    return fetch_github_release_url(
        f"{GITHUB_API_BASE}/repos/{slug}/releases/latest", timeout=timeout
    )
