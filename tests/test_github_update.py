"""GitHub Releases update source + version single-source tests.

Run: pytest  (or python tests/test_github_update.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from policy_extract.updater import (  # noqa: E402
    UpdateError,
    _parse_github_release,
    _pick_exe_asset,
    fetch_github_release_url,
    is_github_releases_url,
    parse_github_repo,
)


def test_parse_github_repo_variants() -> None:
    assert parse_github_repo("infumia/policy_extract") == "infumia/policy_extract"
    assert parse_github_repo("https://github.com/infumia/policy_extract") == "infumia/policy_extract"
    assert parse_github_repo("https://github.com/infumia/policy_extract.git") == "infumia/policy_extract"
    assert (
        parse_github_repo("https://api.github.com/repos/infumia/policy_extract/releases/latest")
        == "infumia/policy_extract"
    )
    for bad in ("", "justoneword", "a/b/c", "https://example.com/x"):
        try:
            parse_github_repo(bad)
        except UpdateError:
            pass
        else:
            raise AssertionError(f"{bad!r} should raise UpdateError")


def test_is_github_releases_url() -> None:
    assert is_github_releases_url("https://api.github.com/repos/o/r/releases/latest")
    assert is_github_releases_url("https://api.github.com/repos/o/r/releases/tags/v1")
    assert not is_github_releases_url("https://api.example.com/extractor/latest")
    assert not is_github_releases_url("https://github.com/o/r/releases")


def test_pick_exe_asset_prefers_canonical_name() -> None:
    assets = [
        {"name": "other-tool.exe", "browser_download_url": "https://x/other.exe"},
        {"name": "policy-extract.exe", "browser_download_url": "https://x/old.exe"},
        {"name": "policy-extract-windows-x64.exe", "browser_download_url": "https://x/new.exe"},
    ]
    assert _pick_exe_asset(assets)["browser_download_url"] == "https://x/new.exe"
    assert _pick_exe_asset([]) is None
    assert _pick_exe_asset([{"name": "notes.txt"}]) is None


def test_parse_github_release_missing_exe() -> None:
    try:
        _parse_github_release({"tag_name": "v1.0.0", "assets": [{"name": "a.zip"}]})
    except UpdateError:
        pass
    else:
        raise AssertionError("release without exe must raise")


def test_fetch_github_release_with_sha_sidecar() -> None:
    import hashlib
    import http.server
    import threading

    payload = b"fake-exe-bytes" * 500
    digest = hashlib.sha256(payload).hexdigest()
    state: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/releases/latest":
                body = (
                    '{"tag_name": "v9.9.9", "body": "notes here", "assets": ['
                    f'{{"name": "policy-extract-windows-x64.exe", '
                    f'"browser_download_url": "http://127.0.0.1:{state["port"]}/exe"}}, '
                    f'{{"name": "policy-extract-windows-x64.exe.sha256", '
                    f'"browser_download_url": "http://127.0.0.1:{state["port"]}/exe.sha256"}}'
                    "]}"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/exe":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/exe.sha256":
                body = f"{digest}  policy-extract-windows-x64.exe\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    state["port"] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        info = fetch_github_release_url(
            f"http://127.0.0.1:{state['port']}/releases/latest", timeout=10.0
        )
        assert info["version"] == "9.9.9", info
        assert info["download_url"].endswith("/exe"), info
        assert info["sha256"] == digest, info
        assert info["notes"] == "notes here", info
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_fetch_github_release_without_sidecar_sha_none() -> None:
    import http.server
    import threading

    state: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = (
                '{"tag_name": "v1.2.3", "assets": ['
                f'{{"name": "policy-extract-windows-x64.exe", '
                f'"browser_download_url": "http://127.0.0.1:{state["port"]}/exe"}}'
                "]}"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    state["port"] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        info = fetch_github_release_url(
            f"http://127.0.0.1:{state['port']}/x", timeout=10.0
        )
        assert info["version"] == "1.2.3", info
        assert info["sha256"] is None, info
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_version_single_source() -> None:
    import re

    root = Path(__file__).resolve().parents[1]
    service_version = re.search(
        r'SERVICE_VERSION\s*=\s*"([^"]+)"',
        (root / "src" / "policy_extract" / "service.py").read_text(encoding="utf-8"),
    ).group(1)
    pyproject_version = re.search(
        r'^version\s*=\s*"([^"]+)"',
        (root / "pyproject.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    ).group(1)
    assert service_version == pyproject_version, (
        f"SERVICE_VERSION ({service_version}) != pyproject ({pyproject_version})"
    )


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
