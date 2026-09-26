"""Updater uç durum testleri: sürüm karşılaştırma, API hataları, indirme, kurulum.

Ağ erişimi yok: tüm HTTP testleri 127.0.0.1'de açılan yerel bir sunucuya gider.

Çalıştırma:  python tests/test_updater_edge_cases.py
(pytest varsa `pytest tests/test_updater_edge_cases.py` ile de çalışır.)
"""

from __future__ import annotations

import hashlib
import http.server
import json
import sys
import tempfile
import threading
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "src"))

import policy_extract.updater as updater
from policy_extract.updater import (
    PREFERRED_EXE_ASSETS,
    UpdateError,
    _fetch_sha256_sidecar,
    _parse_github_release,
    _pick_exe_asset,
    check_for_update,
    compare_versions,
    download_update,
    fetch_update_info,
    install_update,
    is_github_releases_url,
    parse_github_repo,
    parse_version_parts,
    sha256_of_file,
)


class _LocalServer:
    """Minimal yerel HTTP sunucusu: {yol: (durum, gövde[, Content-Length])} + istek kaydı."""

    def __init__(
        self,
        routes: dict[str, tuple],
        headers: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.routes = routes
        self.headers = headers or {}
        self.requests: list[str] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.requests.append(self.path)
                entry = owner.routes.get(self.path)
                if entry is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                status, body = entry[0], entry[1]
                send_length = entry[2] if len(entry) > 2 else True
                self.send_response(status)
                for key, value in owner.headers.get(self.path, {}).items():
                    self.send_header(key, value)
                if send_length:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _LocalServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def _json_route(payload: object, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# Sürüm ayrıştırma / karşılaştırma
# ---------------------------------------------------------------------------


def test_parse_version_parts_varyantlari() -> None:
    for value, expected in [
        ("1.2.3", ([1, 2, 3], "")),
        ("v1.2.3", ([1, 2, 3], "")),
        ("V1.2.3", ([1, 2, 3], "")),
        ("1.2b", ([1, 2], "b")),
        ("1.2.3-rc1", ([1, 2, 3], "-rc1")),
        ("1", ([1], "")),
        ("1.", ([1], ".")),
        ("", ([], "")),
        ("abc", ([], "abc")),
    ]:
        assert parse_version_parts(value) == expected, (value, parse_version_parts(value))


def test_compare_versions_kenar_durumlari() -> None:
    for a, b, expected in [
        ("0.4.0", "0.4.0", 0),
        ("0.4", "0.4.0", 0),
        ("v1.2.3", "1.2.3", 0),
        (" 1.0 ", "1.0", 0),
        ("01.2", "1.2", 0),
        ("1.0.0.0", "1", 0),
        ("", "", 0),
        ("0.3.0", "0.4.0", -1),
        ("0.4.1", "0.4.0", 1),
        ("1.10.0", "1.9.9", 1),
        ("1.2.3.4.5", "1.2.3", 1),
        ("1.0", "1.0-rc1", 1),  # sonek yoksa daha yenidir
        ("1.0-rc1", "1.0", -1),
        ("1.0a", "1.0b", -1),
        ("1.2b", "1.2", -1),  # ön sürüm, sürümden eski sayılır
        ("abc", "1.0", -1),
    ]:
        assert compare_versions(a, b) == expected, (a, b, compare_versions(a, b))


# ---------------------------------------------------------------------------
# fetch_update_info
# ---------------------------------------------------------------------------


def test_fetch_update_info_basarili_ve_normalize() -> None:
    digest = hashlib.sha256(b"payload").hexdigest().upper()
    payload = {
        "version": " 9.9.9 ",
        "download_url": " https://example.com/x.exe ",
        "sha256": f" {digest} ",
        "notes": "notlar",
    }
    with _LocalServer({"/info": _json_route(payload)}) as server:
        info = fetch_update_info(server.url("/info"), timeout=5.0)
    assert info == {
        "version": "9.9.9",
        "download_url": "https://example.com/x.exe",
        "sha256": digest.lower(),
        "notes": "notlar",
    }


def test_fetch_update_info_eksik_ve_gecersiz_alanlar() -> None:
    payloads: list[tuple[dict, str]] = [
        ({"download_url": "https://x/y.exe"}, "version"),
        ({"version": "   ", "download_url": "https://x/y.exe"}, "version"),
        ({"version": 1.0, "download_url": "https://x/y.exe"}, "version"),
        ({"version": "1.0.0"}, "download_url"),
        ({"version": "1.0.0", "download_url": "  "}, "download_url"),
        ({"version": "1.0.0", "download_url": "https://x/y.exe", "sha256": 12345}, "sha256"),
        ({"version": "1.0.0", "download_url": "https://x/y.exe", "sha256": "  "}, "sha256"),
    ]
    routes = {f"/p{index}": _json_route(payload) for index, (payload, _) in enumerate(payloads)}
    with _LocalServer(routes) as server:
        for index, (payload, alan) in enumerate(payloads):
            try:
                fetch_update_info(server.url(f"/p{index}"), timeout=5.0)
            except UpdateError as exc:
                assert alan in str(exc), (payload, exc)
            else:
                raise AssertionError(f"{payload} hata vermeli")


def test_fetch_update_info_json_nesnesi_degilse_hata() -> None:
    with _LocalServer({"/dizi": _json_route([1, 2, 3])}) as server:
        try:
            fetch_update_info(server.url("/dizi"), timeout=5.0)
        except UpdateError as exc:
            assert "JSON object" in str(exc)
        else:
            raise AssertionError("JSON dizi kabul edilmemeli")


def test_fetch_update_info_ulasilamaz_ve_bos_yanit() -> None:
    try:
        fetch_update_info("http://127.0.0.1:1/kapali", timeout=2.0)
    except UpdateError as exc:
        assert "unreachable" in str(exc)
    else:
        raise AssertionError("ulaşılamaz API hata vermeli")

    with _LocalServer({"/bos": (200, b""), "/html": (200, b"<html>degil json</html>")}) as server:
        for path in ("/bos", "/html"):
            try:
                fetch_update_info(server.url(path), timeout=5.0)
            except UpdateError as exc:
                assert "not JSON" in str(exc), path
            else:
                raise AssertionError(f"{path} JSON hatası vermeli")


def test_fetch_update_info_github_url_yonlendirir() -> None:
    payload = {
        "tag_name": "v5.0.0",
        "body": "github notlari",
        "assets": [
            {
                "name": "policy-extract-windows-x64.exe",
                "browser_download_url": "https://example.com/app.exe",
            }
        ],
    }
    path = "/api.github.com/repos/o/r/releases/latest"
    assert is_github_releases_url(f"http://example.com{path}") is True
    with _LocalServer({path: _json_route(payload)}) as server:
        info = fetch_update_info(server.url(path), timeout=5.0)
    assert info["version"] == "5.0.0"
    assert info["download_url"] == "https://example.com/app.exe"
    assert info["sha256"] is None
    assert info["notes"] == "github notlari"


# ---------------------------------------------------------------------------
# check_for_update
# ---------------------------------------------------------------------------


def test_check_for_update_guncelleme_var_yok() -> None:
    digest = hashlib.sha256(b"x").hexdigest()
    routes = {
        "/yeni": _json_route(
            {"version": "0.4.10", "download_url": "https://x/y.exe", "sha256": digest}
        ),
        "/ayni": _json_route({"version": "0.4.9", "download_url": "https://x/y.exe"}),
        "/eski": _json_route({"version": "0.4.8", "download_url": "https://x/y.exe"}),
        "/sonek": _json_route({"version": "0.5.0-rc1", "download_url": "https://x/y.exe"}),
    }
    with _LocalServer(routes) as server:
        yeni = check_for_update(server.url("/yeni"), current_version="0.4.9", timeout=5.0)
        assert yeni["update_available"] is True
        assert yeni["current_version"] == "0.4.9"
        assert yeni["latest_version"] == "0.4.10"
        assert yeni["sha256"] == digest
        assert yeni["download_url"] == "https://x/y.exe"

        for path in ("/ayni", "/eski"):
            sonuc = check_for_update(server.url(path), current_version="0.4.9", timeout=5.0)
            assert sonuc["update_available"] is False, path
        # "0.5.0-rc1" > "0.4.9" -> güncelleme var.
        assert (
            check_for_update(server.url("/sonek"), current_version="0.4.9", timeout=5.0)[
                "update_available"
            ]
            is True
        )


def test_check_for_update_kaynak_yoksa_hata() -> None:
    try:
        check_for_update("", current_version="0.4.9")
    except UpdateError as exc:
        assert "github-repo" in str(exc)
    else:
        raise AssertionError("kaynaksız çağrı hata vermeli")


def test_check_for_update_github_repo_yolu() -> None:
    calls: list[tuple[str, float]] = []

    def fake(repo: str, *, timeout: float = 15.0) -> dict:
        calls.append((repo, timeout))
        return {
            "version": "1.0.0",
            "download_url": "https://x/y.exe",
            "sha256": None,
            "notes": None,
        }

    original = updater.fetch_github_update_info
    updater.fetch_github_update_info = fake  # type: ignore[assignment]
    try:
        result = check_for_update(
            github_repo="infumia/policy-extract", current_version="0.4.9", timeout=5.0
        )
    finally:
        updater.fetch_github_update_info = original  # type: ignore[method-assign]
    assert calls == [("infumia/policy-extract", 5.0)]
    assert result["update_available"] is True
    assert result["latest_version"] == "1.0.0"
    assert result["notes"] is None


# ---------------------------------------------------------------------------
# download_update
# ---------------------------------------------------------------------------


def test_download_update_dogrular_ve_ilerleme_bildirir() -> None:
    payload = b"policy-extract" * 1000
    digest = hashlib.sha256(payload).hexdigest()
    progress: list[tuple[int, int | None]] = []
    with _LocalServer({"/exe": (200, payload)}) as server:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "staging" / "app.exe"  # klasör yok: oluşturulur
            result = download_update(
                server.url("/exe"),
                dest,
                expected_sha256=f"  {digest.upper()}  ",  # büyük harf + boşluk kabul
                timeout=5.0,
                progress_cb=lambda received, total: progress.append((received, total)),
                chunk_size=1024,
            )
            assert result["path"] == str(dest)
            assert result["size"] == len(payload)
            assert result["sha256"] == digest
            assert result["verified"] is True
            assert dest.read_bytes() == payload
            assert not dest.with_suffix(dest.suffix + ".part").exists()
    assert progress, "ilerleme bildirilmedi"
    assert [received for received, _ in progress] == sorted(received for received, _ in progress)
    assert progress[-1] == (len(payload), len(payload))


def test_download_update_content_length_yoksa_total_none() -> None:
    payload = b"chunked-govde" * 500
    progress: list[tuple[int, int | None]] = []
    with _LocalServer({"/exe": (200, payload, False)}) as server:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "app.exe"
            result = download_update(
                server.url("/exe"),
                dest,
                timeout=5.0,
                progress_cb=lambda received, total: progress.append((received, total)),
                chunk_size=1024,
            )
            assert result["size"] == len(payload)
            assert result["verified"] is False  # sha verilmedi
            assert dest.read_bytes() == payload
    assert progress[-1] == (len(payload), None)
    assert all(total is None for _, total in progress)


def test_download_update_sha_uyusmazligi_hedefi_yazmaz() -> None:
    payload = b"yeni surum" * 100
    wrong = hashlib.sha256(b"eski surum").hexdigest()
    with _LocalServer({"/exe": (200, payload)}) as server:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "app.exe"
            try:
                download_update(server.url("/exe"), dest, expected_sha256=wrong, timeout=5.0)
            except UpdateError as exc:
                assert "sha256 mismatch" in str(exc)
                assert "file deleted" in str(exc)
            else:
                raise AssertionError("sha uyuşmazlığı hata vermeli")
            assert not dest.exists()
            assert list(Path(tmp).iterdir()) == [], "kısmi .part dosyası kalmamalı"


def test_download_update_bos_govde_ve_ulasilamayan_url() -> None:
    with _LocalServer({"/bos": (200, b"")}) as server:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "app.exe"
            try:
                download_update(server.url("/bos"), dest, timeout=5.0)
            except UpdateError as exc:
                assert "empty" in str(exc)
            else:
                raise AssertionError("boş gövde hata vermeli")
            assert not dest.exists()

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "yok.exe"
        try:
            download_update("http://127.0.0.1:1/yok", dest, timeout=2.0)
        except UpdateError as exc:
            assert "download failed" in str(exc)
        else:
            raise AssertionError("erişilemeyen URL hata vermeli")
        assert list(Path(tmp).iterdir()) == []


# ---------------------------------------------------------------------------
# install_update / sha256_of_file
# ---------------------------------------------------------------------------


def test_install_update_yedek_alir_ve_degistirir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        target = folder / "app.exe"
        target.write_bytes(b"eski surum")
        staged = folder / "staging" / "app.exe"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"yeni surum")

        result = install_update(staged, target)
        assert result["target"] == str(target)
        backup = Path(str(result["backup"]))
        assert backup.name == "app.exe.bak"
        assert backup.read_bytes() == b"eski surum"
        assert target.read_bytes() == b"yeni surum"
        assert not staged.exists(), "staging taşındıktan sonra kalmamalı"


def test_install_update_yedek_almaz_ve_hedef_klasoru_olusturur() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        staged = folder / "app.exe"
        staged.write_bytes(b"yeni surum")
        target = folder / "yeni" / "klasorde" / "app.exe"  # hedef klasör yok

        result = install_update(staged, target, keep_backup=False)
        assert result["backup"] is None
        assert target.read_bytes() == b"yeni surum"
        assert not (folder / "yeni" / "klasorde" / "app.exe.bak").exists()


def test_install_update_hata_yollari() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        try:
            install_update(folder / "yok.exe", folder / "app.exe")
        except UpdateError as exc:
            assert "staged file missing" in str(exc)
        else:
            raise AssertionError("eksik staging hata vermeli")

        bos = folder / "bos.exe"
        bos.write_bytes(b"")
        try:
            install_update(bos, folder / "app.exe")
        except UpdateError as exc:
            assert "staged file is empty" in str(exc)
        else:
            raise AssertionError("boş staging hata vermeli")
        assert not (folder / "app.exe").exists()


def test_sha256_of_file_hashlib_ile_ayni() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "veri.bin"
        data = b"x" * 5000
        path.write_bytes(data)
        assert sha256_of_file(path) == hashlib.sha256(data).hexdigest()
        assert sha256_of_file(str(path), chunk_size=7) == sha256_of_file(path)






# ---------------------------------------------------------------------------
# GitHub release: varlık seçimi ve ayrıştırma
# ---------------------------------------------------------------------------


def test_pick_exe_asset_oncelikleri() -> None:
    # 1) Tercih edilen ad kazanır.
    assets = [
        {"name": "other-tool.exe", "browser_download_url": "u1"},
        {"name": "policy-extract.exe", "browser_download_url": "u2"},
        {"name": "policy-extract-windows-x64.exe", "browser_download_url": "u3"},
    ]
    assert _pick_exe_asset(assets)["browser_download_url"] == "u3"
    assert PREFERRED_EXE_ASSETS[0] == "policy-extract-windows-x64.exe"

    # 2) Tercih yoksa "windows" içeren exe, alfabetik olarak ilki.
    only_windows = [
        {"name": "zzz-windows.exe", "browser_download_url": "u2"},
        {"name": "aaa-windows.exe", "browser_download_url": "u1"},
    ]
    assert _pick_exe_asset(only_windows)["browser_download_url"] == "u1"

    # 3) Hiçbiri yoksa herhangi bir exe (büyük harf .EXE de olur).
    any_exe = [
        {"name": "b.EXE", "browser_download_url": "u2"},
        {"name": "a.exe", "browser_download_url": "u1"},
    ]
    assert _pick_exe_asset(any_exe)["browser_download_url"] == "u1"

    # 4) exe yoksa None; bozuk girdiler çökmez.
    assert _pick_exe_asset([]) is None
    assert _pick_exe_asset([{"name": "notes.txt"}]) is None
    assert _pick_exe_asset(["bozuk", 5, {"name": "ok.exe", "browser_download_url": "u"}])["name"] == "ok.exe"


def test_parse_github_release_varyantlari() -> None:
    exe = "policy-extract-windows-x64.exe"
    parsed = _parse_github_release(
        {
            "tag_name": " v2.1.0 ",
            "body": "notlar",
            "assets": [{"name": exe, "browser_download_url": " https://x/app.exe "}],
        }
    )
    assert parsed["version"] == "2.1.0"  # "v" öneki ve boşluk temizlenir
    assert parsed["download_url"] == "https://x/app.exe"
    assert parsed["notes"] == "notlar"

    # tag_name yoksa name'e düşer.
    named = _parse_github_release(
        {"name": "v3.0.0", "assets": [{"name": exe, "browser_download_url": "https://x/app.exe"}]}
    )
    assert named["version"] == "3.0.0"

    for payload, beklenti in [
        ({}, "tag_name"),
        ({"tag_name": "v1.0.0", "assets": "gecersiz"}, "assets"),
        ({"tag_name": "v1.0.0", "assets": [{"name": "a.zip"}]}, "no .exe asset"),
        ({"tag_name": "v1.0.0", "assets": [{"name": "a.exe"}]}, "download URL"),
        (
            {"tag_name": "v1.0.0", "assets": [{"name": "a.exe", "browser_download_url": "  "}]},
            "download URL",
        ),
    ]:
        try:
            _parse_github_release(payload)
        except UpdateError as exc:
            assert beklenti in str(exc), (payload, exc)
        else:
            raise AssertionError(f"{payload} hata vermeli")



def test_sha256_sidecar_cesitli_isimlerle_bulunur() -> None:
    exe = "policy-extract-windows-x64.exe"
    digest = hashlib.sha256(b"exe").hexdigest()
    diger = hashlib.sha256(b"diger").hexdigest()
    routes = {
        "/exe.sha256": (200, f"{digest}  {exe}\n".encode()),
        "/exe.sha256.txt": (200, f"{digest}\n".encode()),
        "/sha256.txt": (200, f"{digest}  {exe}\n".encode()),
        # Gerçek dağıtımda checksums.txt yalnızca hedef exe satırını taşır.
        "/checksums.txt": (200, f"{digest}  {exe}\n".encode()),
        "/coklu.txt": (200, f"{digest}  {exe}\n{diger}  diger.exe\n".encode()),
        "/hashsiz.txt": (200, b"bu dosyada hash yok"),
    }
    with _LocalServer(routes) as server:
        # <exe>.sha256 ve <exe>.sha256.txt doğrudan tercih edilir.
        for suffix in (".sha256", ".sha256.txt"):
            assets = [
                {"name": exe, "browser_download_url": "https://x/app.exe"},
                {"name": exe + suffix, "browser_download_url": server.url("/exe" + suffix)},
            ]
            assert _fetch_sha256_sidecar(assets, exe, timeout=5.0) == digest, suffix

        for generic in ("sha256.txt", "checksums.txt"):
            assets = [
                {"name": exe, "browser_download_url": "https://x/app.exe"},
                {"name": generic, "browser_download_url": server.url("/" + generic)},
            ]
            assert _fetch_sha256_sidecar(assets, exe, timeout=5.0) == digest, generic

        # sha256 içeren başka dosyada birden çok hash varsa güvenilmez.
        assets = [
            {"name": exe, "browser_download_url": "https://x/app.exe"},
            {"name": "build_sha256.txt", "browser_download_url": server.url("/coklu.txt")},
        ]
        assert _fetch_sha256_sidecar(assets, exe, timeout=5.0) is None

        # Hash içermeyen sidecar -> None.
        assets = [
            {"name": exe, "browser_download_url": "https://x/app.exe"},
            {"name": "exe.sha256", "browser_download_url": server.url("/hashsiz.txt")},
        ]
        assert _fetch_sha256_sidecar(assets, exe, timeout=5.0) is None

        # Erişilemeyen sidecar -> None (indirme hatası yutulur).
        assets = [
            {"name": exe, "browser_download_url": "https://x/app.exe"},
            {"name": "exe.sha256", "browser_download_url": "http://127.0.0.1:1/yok"},
        ]
        assert _fetch_sha256_sidecar(assets, exe, timeout=2.0) is None

        # Sidecar hiç yoksa None.
        assert _fetch_sha256_sidecar([{"name": exe}], exe, timeout=5.0) is None


def test_is_github_releases_url_varyantlari() -> None:
    assert is_github_releases_url("https://api.github.com/repos/o/r/releases/latest") is True
    assert is_github_releases_url("https://api.github.com/repos/o/r/releases/tags/v1") is True
    assert is_github_releases_url("https://api.github.com/repos/o/r/releases") is False
    assert is_github_releases_url("https://github.com/o/r/releases/latest") is False
    assert is_github_releases_url("https://cdn.example.com/extractor/latest") is False


def test_parse_github_repo_ek_varyantlar() -> None:
    assert parse_github_repo("Infumia/Policy-Extract") == "Infumia/Policy-Extract"
    assert parse_github_repo("  infumia/policy_extract  ") == "infumia/policy_extract"
    assert parse_github_repo("https://github.com/infumia/policy_extract/") == "infumia/policy_extract"
    assert parse_github_repo("https://api.github.com/repos/o/r/releases/tags/v1.0") == "o/r"
    for bad in ["", "tekkelime", "a/b/c", "a b/c", "https://cdn.example.com/x", "https://github.com/"]:
        try:
            parse_github_repo(bad)
        except UpdateError:
            pass
        else:
            raise AssertionError(f"{bad!r} UpdateError vermeli")


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



