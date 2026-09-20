# policy-extract

Extract structured JSON (`policy number`, `endorsement number`, `insurer`) from
Turkish insurance PDFs. Single fast engine (pypdf, layout-preserving text),
plus a long-running `--serve` mode designed for desktop app integration
(watch a folder, report results as JSON on stdout).

## Download (Windows)

Grab the latest `policy-extract-windows-x64.exe` from
[Releases](https://github.com/infumia/policy-extract/releases):

Verify (optional, PowerShell):

```powershell
$exe = "policy-extract-windows-x64.exe"
$expected = (Get-Content "$exe.sha256" -Raw).Split()[0]
$actual = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
if ($expected.ToLower() -ne $actual) { throw "sha256 mismatch" }
.\$exe --version
```

## Install from source

```powershell
python -m venv .venv
.venv/Scripts/Activate
pip install -e .
```

Requires Python >= 3.10. Runtime deps: `pypdf`, `fonttools` (needed by pypdf
for CFF/Type1-font PDFs). No other dependencies — intentionally stdlib-only
beyond those two so the PyInstaller exe stays small.

## Usage

```powershell
# single file: JSON to stdout
python -m policy_extract.cli sample.pdf

# single file, explicit flag (handy in tests)
python -m policy_extract.cli --file "C:\policies\sample.pdf"

# folder: scan first 7 pages (default), one JSON object per line (JSONL)
python -m policy_extract.cli ./policies

# change / remove the page limit
python -m policy_extract.cli ./policies --max-pages 5
python -m policy_extract.cli ./policies --max-pages 0

# custom .metadata path
python -m policy_extract.cli ./policies -o ./policies/.metadata

# ignore the cache, recompute everything
python -m policy_extract.cli ./policies --no-cache

# write to file, drop tables (full_text is off by default)
python -m policy_extract.cli sample.pdf -o out.json --no-tables

# opt in to full_text
python -m policy_extract.cli sample.pdf -o out.json --with-text
```

## Output

```json
{
  "source_file": "sample.pdf",
  "police_no": "0001-0110-06857993",
  "police_no_source": "table",
  "zeyil_no": null,
  "zeyil_no_source": null,
  "company": "allianz",
  "company_confidence": "high",
  "company_scores": {"allianz": 28},
  "full_text": "...",
  "tables": [...]
}
```

- `police_no_source`: `inline` = from `Policy No: 0000` text,
  `table` = from a table headed `Policy No`.
- `zeyil_no` (endorsement no): from `Endorsement No` / `Zeyil No`.
  `0`, `00`, `0/0` mean "base policy", so they return `null`.
- `company`: scored dictionary match, never a single-word search —
  domain (+10), legal title (+5), brand (+2, +1 for generic words like
  `gunes`/`turkiye`), header bonus. Below-threshold or tied scores return
  `null` + `unknown`. `company_scores` explains the decision.

## How it works

One engine: **fast path (pypdf)** — layout-preserving text extraction of a
digital PDF, policy number / insurer found in text. Takes seconds, downloads
no extra models.

Folder scans keep all results in `.metadata`. Each line carries the file's
`sha256`; on rescan, unchanged **complete** records are reused as-is
(`--no-cache` disables this). Records missing `police_no` or `company`
(also written to `.not-found-metadata`) are retried every scan, so they
heal automatically as the extractor improves.

## Tests

Pure functions are tested:

```powershell
.venv\Scripts\python.exe tests/test_extractor.py
# or, if pytest is installed:
pytest
```

### Real-data test (optional)

Point at your own policy folder; tests only **read** it, never write
`.metadata`/`.lock` there (serve tests copy samples to temp):

```powershell
$env:POLICY_EXTRACT_TESTDATA_DIR = "C:\policies"
$env:POLICY_EXTRACT_TESTDATA_MAX = "20"  # default 20, stride sampling
.venv\Scripts\python.exe tests/test_extractor.py
```

What it checks: no file crashes the extractor, record shape is consistent
(`police_no`→`police_no_source`, `company`→confidence/scores), two reads give
the same result, two serve passes keep line counts stable. Without the env var
these two tests SKIP, the rest of the suite is unaffected.

## Service mode

Long-running loop instead of one-shot scanning:

```powershell
# Watch a folder, queue existing PDFs, pick up new ones as they arrive.
# Events go to stdout as JSONL, logs to stderr.
python -m policy_extract.cli ./policies --serve

# Tuned: poll every 1s, exit when the parent dies
python -m policy_extract.cli ./policies --serve --poll-interval 1 --parent-pid 1234

# Version (the host app compares it against the release API)
python -m policy_extract.cli --version
```

Service behavior:

- On start, queues every PDF in the folder, processes them in order.
- New/changed PDFs arriving mid-run are queued automatically — nothing is
  lost while 70k files are being processed.
- `.metadata` is written incrementally: only **changed** records are
  appended; repeats (cache hits or identical not-found retries) don't touch
  the file. Every 200 computations and on every shutdown the file is
  deduplicated (compact); on start, stale duplicate lines are cleaned once.
  `--no-cache` wipes old records first (same rule as one-shot batch).
- **One serve per folder**: a `.policy-extract.lock` is taken on start, a
  second serve is rejected with `fatal` + exit 2 (double writers would corrupt
  metadata). A stale lock from a dead process is taken over automatically.
  `--no-lock` skips this (special setups only).
- Ctrl+C / close signals shut down gracefully (closing compact + `bye`);
  hard kills are healed by the startup cleanup.
- Half-written files are not processed: files with **mtime fresher than
  10s** wait until their size is stable `stable-checks` times in a row
  (`--stable-checks 3 --stable-interval-ms 500`); older files go straight to
  sha+cache. That keeps a 690-file cached rescan at seconds instead of
  minutes (`--stable-grace-secs` tunes it).
- Shutdown: writing `{"cmd":"shutdown"}` to stdin, closing the stdin pipe
  (host app exited), or death of the `--parent-pid` PID finishes the current
  file, compacts `.metadata`, then exits.
- stdout is **quiet by default**: only `hello`, command replies (`status`,
  `paused`, ...) and `bye` flow. File records are read from `.metadata`,
  counters from a `{"cmd":"status"}` reply (polling model). The old per-file
  event stream is available via `--stream-events` (debug/tests).
- stderr is **fully silent**: not a single byte (`--verbose` excepted).

### stdout event protocol (JSONL, one JSON per line)

Quiet by default (`hello` + command replies + `bye`); starred rows only flow
with `--stream-events`.

| type                               | content                                                           |
|------------------------------------|-------------------------------------------------------------------|
| `hello`                            | `version`, `pid`, `folder`, `total_queued`, `parent_pid`          |
| `progress` *                       | `file`, `done`, `total_hint`, `queued`                            |
| `file_done` *                      | `record` (police_no/company...), `done`                           |
| `file_cached` *                    | cached `record`, `done`                                           |
| `file_error` *                     | `file`, `error`, `done` (record is also in `.metadata`)           |
| `file_queued` *                    | `file`, `reason` (`initial`/`created`/`modified`), `queued`       |
| `watch_error`                      | `error` when the folder is unreadable (watching continues)        |
| `idle` *                           | queue empty, watching (`done`, `computed`, `cached`, `failures`)  |
| `status`                           | reply to `status` (counters + `paused` + `version`)               |
| `paused` / `resumed` / `rescanned` | command acknowledgements                                          |
| `parent_gone`                      | parent died, shutting down                                        |
| `bye`                              | closing counters + `persist_error` (set when metadata unwritable) |

Polling model: the host sends periodic `{"cmd":"status"}`
(`queued == 0` means caught up) and reads finished records from `.metadata`.
Pass `--stream-events` for live per-file events (debug).

### Error model (what the host should do)

A single bad file never kills the service; a safety net keeps the loop alive.

| situation                                    | signal                                                                             | host action                                                          |
|----------------------------------------------|------------------------------------------------------------------------------------|----------------------------------------------------------------------|
| Broken/unreadable PDF                        | `file_error` (stream) + `.metadata` `{"file":..,"error":..}` + `status.failures`++ | None needed; optionally show a "broken" badge                        |
| Fields not found (null)                      | `.not-found-metadata` + automatic retry next scan                                  | None                                                                 |
| Folder unreadable (USB unplugged etc.)       | `watch_error`, watching continues, self-heals on return                            | None; show "folder unreachable" if prolonged                         |
| Folder missing at startup                    | `fatal` + exit code 2, process ends                                                | Show error, try restarting the process                               |
| Unexpected internal error                    | `file_error`, service survives                                                     | None                                                                 |
| Process died (exit code != 0 or pipe closed) | no `bye`, `exitCode`                                                               | Restart the process (serve is idempotent: resumes where it left off) |

### stdin commands (JSONL)

```json
{"cmd": "status"}
{"cmd": "pause"}
{"cmd": "resume"}
{"cmd": "rescan"}
{"cmd": "shutdown"}
```

### Host side (sketch)

```dart
void test() async {
  final proc = await Process.start('policy-extract.exe', [
    folder, '--serve', '--parent-pid', pid.toString(),
  ]);
  proc.stdout.transform(utf8.decoder).transform(const LineSplitter()).listen((line) {
    final event = jsonDecode(line) as Map<String, dynamic>;
    switch (event['type']) {
      case 'hello':
      /* version check, ready */
        break;
      case 'status':
      /* counters: done/queued/paused */
        break;
      case 'bye':
      /* service closed */
        break;
    }
  });
  // Polling: periodic status for counters, finished records from .metadata.
  Timer.periodic(const Duration(seconds: 1), (_) {
    proc.stdin.writeln(jsonEncode({'cmd': 'status'}));
  });
  // On close:
  proc.stdin.writeln(jsonEncode({'cmd': 'shutdown'}));
  // When the app exits the stdin pipe closes -> the exe exits by itself.
  // Extra safety: pass --parent-pid so the exe exits when the parent dies.
  // For live per-file events (debug): add '--stream-events'.
}
```

## Update system

The host owns check-and-swap; the heavy lifting (`check`/`fetch`/`install`)
is done by `updater.py`. No extra dependencies (stdlib `urllib`).

### 1. Recommended: GitHub Releases source

Each `v*` tag publishes `policy-extract-windows-x64.exe` plus a
`policy-extract-windows-x64.exe.sha256` sidecar. The host asks the exe:

```powershell
# a) Is there an update? (stdout: single-line JSON)
policy-extract.exe --check-update --github-repo infumia/policy-extract
# -> {"current_version":"0.4.8","latest_version":"0.5.0",
#     "download_url":"https://github.com/.../*.exe",
#     "sha256":"...","update_available":true}

# b) If so: stop the serve process (stdin {"cmd":"shutdown"} + wait for exit),
#    then download (stdout: fetch_progress ... + fetch_done JSONL)
policy-extract.exe --fetch-update --github-repo infumia/policy-extract --dest %AppData%\App\staging\policy-extract.exe
# sha mismatch -> exit 1 + staging deleted, old exe untouched.

# c) Install (old exe backed up to .bak, staging moved to target)
policy-extract.exe --install-update --staged %AppData%\App\staging\policy-extract.exe --target %AppData%\App\policy-extract.exe
# -> {"type":"install_done","target":"...","backup":"....bak"}

# d) Start serve with the new exe, verify with --version.
```

On the host side, when `update_available == true`: shutdown → fetch (drive a
progress bar from per-line `fetch_progress`) → install → restart `--serve`
with the new exe. If the target is not writable (serve still running etc.)
install exits 1 with an explanatory error; the `.bak` file is the rollback.

A GitHub Releases API URL also works directly:

```powershell
policy-extract.exe --check-update --update-info-url https://api.github.com/repos/infumia/policy-extract/releases/latest
```

### 2. Alternative: custom JSON API

```http
GET {update-info-url}  ->  application/json
```

```json
{
  "version": "0.4.0",
  "download_url": "https://cdn.example.com/extractor/policy-extract-0.4.0.exe",
  "sha256": "hex...",
  "notes": "optional release notes"
}
```

`version` + `download_url` are required, `sha256` is strongly recommended
(without it the download is installed unverified; `--sha256` can pass it
manually). Same `--check-update` / `--fetch-update` flow with
`--update-info-url`, or a direct download with
`--fetch-update --url <direct-link> --sha256 <hex> --dest ...`.

## Exe build

Local build:

```powershell
powershell -ExecutionPolicy Bypass -File ./build_exe.ps1
# Output: dist/policy-extract.exe
dist/policy-extract.exe --version
dist/policy-extract.exe ./policies --serve
```

Release build (CI): push a tag, GitHub Actions builds the exe on Windows,
renames it to `policy-extract-windows-x64.exe`, writes the `.sha256`
sidecar, and attaches both to the GitHub Release. See
`.github/workflows/release.yml`.

## Versioning

Single source of truth: `SERVICE_VERSION` in `src/policy_extract/service.py`.
`pyproject.toml`'s `version` must match it, and release tags are `v` +
that version (e.g. `v0.4.8`). CI fails the release if they diverge.

## License

MIT — see [LICENSE](LICENSE).
