"""Exe update helper (orchestrated by Flutter).

Flow (Flutter drives, this module does the work):
  1. --check-update   -> learn the version at the API, is there an update?
  2. --fetch-update   -> download the new exe + verify sha256 (staging).
  3. --install-update -> swap the staged file with the target exe (with backup).

No `requests` on purpose: stdlib (urllib) is enough, keeps the exe small.
Network errors surface as UpdateError; the CLI turns them into JSON + exit code.

Two update sources are supported:
  a) Custom JSON API: {"version": "0.4.0", "download_url": "https://...",
     "sha256": "hex...", "notes": "..."}.
  b) GitHub Releases: --github-repo OWNER/REPO (or a
     https://api.github.com/repos/OWNER/REPO/releases/... URL as
     --update-info-url). The release must contain a Windows .exe asset;
     a "<exe>.sha256" sidecar asset is used for verification when present.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


class UpdateError(Exception):
    """Network / validation / install error. Message is safe to show in Flutter."""


GITHUB_API_BASE = "https://api.github.com"

# Preferred release asset names, in order. The release workflow uploads the
# first one; older/alternative names are accepted for backwards compatibility.
PREFERRED_EXE_ASSETS = (
    "policy-extract-windows-x64.exe",
    "policy-extract.exe",
)


def parse_version_parts(version: str) -> tuple[list[int], str]:
    """'1.2.3' -> ([1,2,3], ''); '1.2b' -> ([1,2], 'b'). Karşılaştırma içindir."""
    version = version.strip().lstrip("vV")
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", version)
    if not match:
        return [], version
    nums = [int(p) for p in match.group(1).split(".")]
    return nums, match.group(2).strip()


def compare_versions(a: str, b: str) -> int:
    """a < b -> -1, eşit -> 0, a > b -> 1. Eksik basamak 0 sayılır."""
    nums_a, rest_a = parse_version_parts(a)
    nums_b, rest_b = parse_version_parts(b)
    width = max(len(nums_a), len(nums_b))
    nums_a += [0] * (width - len(nums_a))
    nums_b += [0] * (width - len(nums_b))
    if nums_a != nums_b:
        return -1 if nums_a < nums_b else 1
    if rest_a == rest_b:
        return 0
    # '1.0' > '1.0-rc1': sonek yoksa daha yenidir.
    if not rest_a:
        return 1
    if not rest_b:
        return -1
    return -1 if rest_a < rest_b else 1


def get_current_version() -> str:
    from policy_extract.service import SERVICE_VERSION

    return SERVICE_VERSION


def _http_get_json(url: str, *, timeout: float) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "policy-extract-updater"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdateError(f"update API unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UpdateError(f"update API response is not JSON: {exc}") from exc


def fetch_update_info(api_url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Read update info from a custom JSON API.

    Expected response: {"version": "0.4.0", "download_url": "https://..."}
    Optional: {"sha256": "hex...", "notes": "..."}.

    If `api_url` is a GitHub Releases API URL it is handled as a GitHub
    source instead (see fetch_github_update_info).
    """
    if is_github_releases_url(api_url):
        return fetch_github_release_url(api_url, timeout=timeout)
    try:
        info = _http_get_json(api_url, timeout=timeout)
    except UpdateError:
        raise
    if not isinstance(info, dict):
        raise UpdateError("update API response must be a JSON object.")
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


def is_github_releases_url(url: str) -> bool:
    return "api.github.com/repos/" in url and "/releases/" in url


def parse_github_repo(value: str) -> str:
    """Normalize OWNER/REPO from 'owner/repo', github.com URL or API URL."""
    v = value.strip().removesuffix(".git").strip()
    # NOTE: api.github.com must be checked first: it also contains
    # "github.com/" as a substring (api.github.com/repos/...).
    m = re.search(r"api\.github\.com/repos/([^/]+/[^/]+)", v)
    if m:
        return m.group(1).strip("/")
    m = re.search(r"github\.com/([^/]+/[^/]+)", v)
    if m:
        return m.group(1).strip("/")
    if re.fullmatch(r"[^/\s]+/[^/\s]+", v):
        return v
    raise UpdateError(
        f"invalid GitHub repo '{value}'. Expected OWNER/REPO "
        "(e.g. infumia/policy-extract)."
    )


def _pick_exe_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_name = {a.get("name", ""): a for a in assets if isinstance(a, dict)}
    for preferred in PREFERRED_EXE_ASSETS:
        if preferred in by_name:
            return by_name[preferred]
    windows_exes = [
        a for a in assets
        if isinstance(a, dict)
        and str(a.get("name", "")).lower().endswith(".exe")
        and "windows" in str(a.get("name", "")).lower()
    ]
    if windows_exes:
        return sorted(windows_exes, key=lambda a: str(a.get("name")))[0]
    any_exes = [
        a for a in assets
        if isinstance(a, dict) and str(a.get("name", "")).lower().endswith(".exe")
    ]
    if any_exes:
        return sorted(any_exes, key=lambda a: str(a.get("name")))[0]
    return None


def _parse_github_release(data: dict[str, Any]) -> dict[str, Any]:
    tag = data.get("tag_name") or data.get("name") or ""
    if not isinstance(tag, str) or not tag.strip():
        raise UpdateError("GitHub release has no tag_name.")
    version = tag.strip().lstrip("vV")
    assets = data.get("assets") or []
    if not isinstance(assets, list):
        raise UpdateError("GitHub release assets are malformed.")
    exe = _pick_exe_asset(assets)
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


def _fetch_sha256_sidecar(assets: list[dict[str, Any]], exe_name: str, *, timeout: float) -> str | None:
    """Download '<exe>.sha256' sidecar if the release provides one."""
    candidates = {f"{exe_name}.sha256", f"{exe_name}.sha256.txt", "sha256.txt", "checksums.txt"}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        url = asset.get("browser_download_url")
        if name in candidates and isinstance(url, str) and url.strip():
            req = urllib.request.Request(url.strip(), headers={"User-Agent": "policy-extract-updater"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    text = resp.read(16 * 1024).decode("utf-8", errors="replace").strip()
            except (urllib.error.URLError, OSError, TimeoutError):
                return None
            m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
            return m.group(1).lower() if m else None
    # Fallback: any asset with 'sha256' in its name; only trust it when it
    # contains exactly one hash (avoids picking the wrong line of a bundle).
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", "")).lower()
        url = asset.get("browser_download_url")
        if "sha256" in name and isinstance(url, str) and url.strip():
            req = urllib.request.Request(url.strip(), headers={"User-Agent": "policy-extract-updater"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    text = resp.read(16 * 1024).decode("utf-8", errors="replace").strip()
            except (urllib.error.URLError, OSError, TimeoutError):
                return None
            hashes = re.findall(r"\b([0-9a-fA-F]{64})\b", text)
            if len(hashes) == 1:
                # If it is a "HASH  FILENAME" line, make sure it names our exe.
                if exe_name and exe_name not in text and "checksums" in name:
                    return None
                return hashes[0].lower()
            return None
    return None


def fetch_github_release_url(release_url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Fetch a GitHub release JSON URL (e.g. .../releases/latest)."""
    data = _http_get_json(release_url, timeout=timeout)
    if not isinstance(data, dict):
        raise UpdateError("GitHub release response must be a JSON object.")
    parsed = _parse_github_release(data)
    assets = parsed.pop("_assets")
    exe_name = parsed.pop("_exe_asset_name")
    parsed["sha256"] = _fetch_sha256_sidecar(assets, exe_name, timeout=timeout)
    return parsed


def fetch_github_update_info(repo: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Fetch the latest release of OWNER/REPO from the GitHub API."""
    slug = parse_github_repo(repo)
    return fetch_github_release_url(
        f"{GITHUB_API_BASE}/repos/{slug}/releases/latest", timeout=timeout
    )


def check_for_update(
    api_url: str = "",
    *,
    current_version: str | None = None,
    timeout: float = 15.0,
    github_repo: str = "",
) -> dict[str, Any]:
    """Flutter's question: 'is there an update?' Answer is a single JSON dict."""
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


def sha256_of_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_update(
    url: str,
    dest: str | Path,
    *,
    expected_sha256: str | None = None,
    timeout: float = 120.0,
    progress_cb: Callable[[int, int | None], None] | None = None,
    chunk_size: int = 1024 * 256,
) -> dict[str, Any]:
    """Download the new exe, verify sha256 if given. Errors -> UpdateError + partial deleted."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "policy-extract-updater"})
    digest = hashlib.sha256()
    received = 0
    total: int | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_total = response.headers.get("Content-Length")
            total = int(raw_total) if raw_total and raw_total.isdigit() else None
            with tmp.open("wb") as fh:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress_cb is not None:
                        progress_cb(received, total)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"download failed: {exc}") from exc

    actual = digest.hexdigest()
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


def install_update(
    staged: str | Path, target: str | Path, *, keep_backup: bool = True
) -> dict[str, Any]:
    """Swap the verified staged exe into the target path.

    Flutter must have stopped the serve process first (file lock).
    The old exe is kept as `.bak`; if the target is not writable the old
    version is preserved.
    """
    staged = Path(staged)
    target = Path(target)
    if not staged.is_file():
        raise UpdateError(f"staged file missing: {staged}")
    if staged.stat().st_size == 0:
        raise UpdateError("staged file is empty, install aborted.")
    backup: Path | None = None
    if target.is_file() and keep_backup:
        backup = target.with_suffix(target.suffix + ".bak")
        try:
            shutil.copy2(target, backup)
        except OSError as exc:
            raise UpdateError(f"could not back up ({target}): {exc}") from exc
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
    except OSError as exc:
        raise UpdateError(
            f"install failed ({target}): {exc}. "
            "If the serve process is still running, stop it first."
        ) from exc
    return {"target": str(target), "backup": str(backup) if backup else None}
