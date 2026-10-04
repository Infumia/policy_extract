"""Desktop pipes must use UTF-8 even when Windows selects a legacy code page.

Set POLICY_EXTRACT_TEST_EXE to run these checks against a frozen Windows exe.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "tests"))

from synthetic_data import write_pdf


def _command(*args: str) -> list[str]:
    exe = os.environ.get("POLICY_EXTRACT_TEST_EXE")
    return ([exe] if exe else [sys.executable, "-m", "policy_extract.cli"]) + list(args)


def _environment(encoding: str) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(_ROOT / "src"),
        "PYTHONIOENCODING": encoding,
        "PYTHONUTF8": "0",
        "PYTHONLEGACYWINDOWSSTDIO": "1",
    }


def _run(*args: str, encoding: str = "cp1252") -> subprocess.CompletedProcess:
    return subprocess.run(
        _command(*args), env=_environment(encoding), capture_output=True, timeout=30,
    )


def test_single_pdf_output_is_utf8_with_legacy_pipes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pdf = write_pdf(Path(tmp) / "Masaüstü" / "İşık Şahin ığüşöç.pdf", [
            "ALLIANZ SIGORTA\nPolicy No: 7777888999000",
        ])
        for encoding in ("cp1252", "cp1254", "ascii", "utf-8"):
            result = _run(str(pdf), encoding=encoding)
            assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
            payload = json.loads(result.stdout.decode("utf-8"))
            assert Path(payload["source_file"]).name == pdf.name
            assert payload["police_no"] == "7777888999000"
            assert pdf.name.encode("utf-8") in result.stdout


def test_batch_logs_are_utf8_with_legacy_pipes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pdf = write_pdf(Path(tmp) / "Masaüstü" / "İşık Şahin ığüşöç.pdf", [
            "ALLIANZ SIGORTA\nPolicy No: 7777888999000",
        ])
        result = _run(str(pdf.parent))
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert pdf.name in result.stderr.decode("utf-8")
        record = json.loads((pdf.parent / ".metadata").read_text(encoding="utf-8"))
        assert record["file"] == pdf.name
        assert record["police_no"] == "7777888999000"


def test_help_and_errors_are_utf8_before_argument_dispatch() -> None:
    result = _run("--help")
    assert result.returncode == 0, result.stderr
    assert "poliçeden" in result.stdout.decode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "Masaüstü" / "İşık.pdf"
        result = _run(str(missing))
        assert result.returncode == 2, result.stderr
        assert str(missing) in result.stderr.decode("utf-8")
        assert "Traceback" not in result.stderr.decode("utf-8")


def test_serve_processes_and_retries_turkish_names_with_legacy_pipes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pdf = write_pdf(Path(tmp) / "OneDrive" / "Masaüstü" / "İşık Şahin ığüşöç.pdf", [
            "ALLIANZ SIGORTA\nPolicy No: 7777888999000",
        ])
        for encoding in ("cp1252", "cp1254", "ascii"):
            with subprocess.Popen(
                _command(str(pdf.parent), "--serve", "--stream-events", "--verbose",
                         "--stable-checks", "1", "--poll-interval", "0.2", "--no-cache"),
                env=_environment(encoding), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as process:
                assert process.stdin and process.stdout and process.stderr
                lines: queue.Queue[bytes] = queue.Queue()

                def read_stdout() -> None:
                    for line in process.stdout:
                        lines.put(line)
                    lines.put(b"")

                reader = threading.Thread(target=read_stdout, daemon=True)
                reader.start()
                events: list[dict] = []

                def wait_for(kind: str) -> dict:
                    while True:
                        line = lines.get(timeout=20)
                        assert line, "service closed stdout before " + kind
                        event = json.loads(line.decode("utf-8"))
                        events.append(event)
                        if event["type"] == kind:
                            return event

                def send(command: dict) -> None:
                    process.stdin.write((json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8"))
                    process.stdin.flush()

                try:
                    assert wait_for("hello")["folder"] == str(pdf.parent)
                    first = wait_for("file_done")
                    assert first["record"]["file"] == pdf.name
                    assert first["record"]["police_no"] == "7777888999000"
                    assert wait_for("idle")["done"] == 1
                    # The host writes UTF-8 commands too: a legacy stdin decoder
                    # would corrupt this filename and reject retry_file.
                    send({"cmd": "retry_file", "file": pdf.name})
                    retried = wait_for("file_queued")
                    assert retried["file"] == pdf.name
                    assert retried["reason"] == "retry"
                    assert wait_for("file_done")["record"]["file"] == pdf.name
                    assert wait_for("idle")["done"] == 2
                    send({"cmd": "status"})
                    assert wait_for("status")["failures"] == 0
                    send({"cmd": "shutdown"})
                    assert wait_for("bye")["done"] == 2
                    assert process.wait(timeout=20) == 0
                    reader.join(timeout=5)
                    stderr = process.stderr.read().decode("utf-8")
                    assert pdf.name in stderr
                    assert "UnicodeEncodeError" not in stderr
                    assert events[0]["type"] == "file_queued"
                    records = (pdf.parent / ".metadata").read_text(encoding="utf-8").splitlines()
                    assert len(records) == 1
                    assert json.loads(records[0])["file"] == pdf.name
                    assert not (pdf.parent / ".policy-extract.lock").exists()
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                    reader.join(timeout=5)


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print(f"PASS {name}")
