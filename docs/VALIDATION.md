# Validation record

Run date: **2026-09-23**, in an isolated git worktree on branch
`night/music-butler-stack-bootstrap`.

Environment observed: macOS 26.5.2 (25F84), test Python **3.12.13**, ShellCheck
**0.11.0**, Docker Compose **v5.5.1**, Linux **arm64** tooling image. No changes
were made to the upstream main checkout. No push or production deployment.

## Checks actually executed

| Check | Actual result |
|---|---|
| `bash -n scripts/download_music.sh` | exit 0; also checked setup/validation scripts |
| ShellCheck on `scripts/*.sh` | exit 0, no findings |
| Python `compileall` on scripts/tests | exit 0 |
| `bash scripts/validate.sh` | exit 0; final host run **21 tests, all passed**, 5.493 s |
| `docker compose --env-file .env.example --profile tools --profile hermes config --quiet` | exit 0; all profiles and example validated |
| `./scripts/setup.sh` (without `--start`) | exit 0; generated ignored `.env`, provisioned this worktree's storage/secrets directories, printed “Configuration valid; storage provisioned” |
| `docker compose config --quiet` using generated `.env` | exit 0 |
| yt-dlp `--help`, plus the actual installed argument parser in tests | accepted bundled flags asserted |
| `uv pip compile ... --generate-hashes` | resolved **63 packages**, wrote exact/hash-locked transitive requirements |
| Tooling Docker build, including `pip --require-hashes` | succeeded, `Image music-butler-tools:local Built` |
| `python3 tests/container_smoke.py` | exit 0; **21 tests run in container, 20 passed, 1 intentionally skipped**, 4.675 s; Navidrome smoke passed |
| Executable permissions | `scripts/download_music.sh`, setup/validate scripts and Python helper set to **0755** |
| `git diff --check` / staged diff review | no whitespace errors; only intended new project files staged |

Container test skip: Docker/Compose CLI is deliberately **not installed inside
the downloader image**. Its configuration test runs successfully on the host;
no Docker socket was mounted to defeat the isolation.

Final build image observed:
`sha256:7097db6199ccc6f96d5b7027faebc736ec28e14767391a1503178556a6131a56`.
This is a local validation image ID, not a registry-published artifact or a
promise that floating base tags rebuild to that ID.

### Real disposable Navidrome smoke output

```text
PASS: Navidrome HTTP ready, non-root/read-only mounts/no ports, unauthenticated startScan rejected.
NOT TESTED: authenticated scan, real library indexing, provider downloads, Tempo or Hermes gateway.
PASS: disposable containers and temporary Navidrome storage removed.
```

The selected `deluan/navidrome:0.64.1` image was actually pulled and started with
the Compose Navidrome environment, dedicated temporary data/music directories,
non-root UID/GID, read-only root/music mounts, dropped capabilities and
`--network none`. HTTP availability was polled from inside that container; no
host port was published. A POST to `/rest/startScan.view` without credentials
returned a failed Subsonic response. The smoke script stopped the server and
removed its temporary state. This proves startup/HTTP/authentication rejection,
**not** authenticated scan completion, media indexing or playback.

Registry digest observed:
`deluan/navidrome@sha256:df22d661b8d0322c33754d999b567150df9e131a47ae53be71fed0154bf8cef5`.
The suggested minor tag `deluan/navidrome:0.64` returned **“no such manifest”**;
the available release tag was used instead. Primary tagged source was inspected
for admin-only `startScan`, scan response behavior and the container's `curl`.
See BLUEPRINT for source URLs rather than relying on an invented `/api/scan`.

### What the offline tests exercise

- Valid single-song/album schema; invalid enums, unknown keys, invalid URLs,
  credential-like URLs, controls, private DNS.
- Quoted/no-shell argument construction, Unicode/path traversal slugging,
  actual Bash stdin forwarding and malformed JSON exit 65.
- Real media subprocess creation and SIGTERM cancellation/reaping.
- Cancellation propagation through otherwise non-fatal artwork/lyrics/scan
  paths; early rejection of unsupported host Python versions.
- Installed yt-dlp argument parser (bundled flags, no API access).
- Generated two-second sine waves → **real FFmpeg MP3 and FLAC**, real Mutagen
  metadata/cover embedding, final audio hash verification.
- Generated image → real FFmpeg JPEG normalization → MP3 APIC / FLAC PICTURE.
- Whole-album publication visibility, rollback on second-track failure,
  collision-safe repeat imports, real advisory lock contention and symlink
  rejection, low-space rejection and post-publication reconciliation warning.
- Mocked LRCLIB synced lyrics, plain-only/404 handling and 429 cooldown;
  mocked Subsonic POST token authentication, response/admin failure and unsafe
  secret permissions. No test lyrics are represented as real artist lyrics.
- Compose profile defaults, loopback port, non-root UID/GID, read-only mounts,
  dropped capabilities, no Docker socket, explicit scan schedule.

Fixture downloads and API responses are explicitly **mocks**, not evidence of
remote acquisition. Capacity is mocked for tiny fixtures, with a separate
low-space test; the real importer retains its 2 GiB minimum-free-space check.

## Issues found and resolved / warnings

- Final diff review found that optional-service error handlers could swallow a
  cancellation signal. They now re-raise interrupt/termination failures; a new
  regression test covers all four paths. Final host/container reruns passed.
- An initial container test attempt correctly hit the production free-space
  guard because its tiny fixture library was in a 128 MiB tmpfs. The test fixture
  now mocks available capacity (and separately tests rejection), rather than
  weakening production checks. The final container run passed.
- Direct audio URLs can lack extractor duration metadata: the yt-dlp filter now
  permits unknown duration, with actual decoded duration checked before publish.
- Two combined terminal invocations were held for approval by the runner's
  command guard (chmod-plus-execution, and a multi-command Docker invocation).
  No user was present; they were not approved or relied upon as test results.
  File permissions and the independently permitted validations/build/smoke calls
  above provided the actual evidence.

## Live deployment (2026-09-26 – 2026-09-27)

The stack was then deployed for real on a macOS host (Docker Desktop) and used
from an Android phone.

### Bugs found and fixed

Each has a regression assertion in `tests/test_config.py` or `tests/test_pipeline.py`:

- `LIBRARY_PATH` collided with the compiler variable that macOS CLT `python3`
  exports (`/usr/local/lib`); Compose prefers the process environment over
  `.env`, so setup tried to provision `/usr/local/lib`. Renamed to
  `MUSIC_LIBRARY_PATH`.
- Unquoted `tmpfs: [/tmp:rw,noexec,...]` is a YAML flow list, so the commas
  produced five mounts and `docker compose up` failed with
  `invalid mount path: 'mode=1777'`. Now quoted. `compose config` alone did not
  catch this.
- Wikimedia's robot policy returned 403 to yt-dlp's default Chrome UA and to
  `Python-urllib` artwork fetches. Non-YouTube downloads and artwork now send
  `DOWNLOAD_USER_AGENT`, which must include contact info for Wikimedia.

### What was verified live

| Area | Result |
|---|---|
| Setup and server | `./scripts/setup.sh`, `docker compose up -d`; Navidrome `healthy` on `127.0.0.1:4533` |
| Imports | Public-domain recordings from upload.wikimedia.org (Joplin *Maple Leaf Rag*, 1916 piano roll, with Commons artwork embedded) and archive.org (Caruso *Over There*, 1918, FLAC source) completed with `complete` events |
| Scan and indexing | Authenticated `startScan` returned `requested`; Navidrome `search3` listed the imported songs |
| Lyrics | Live LRCLIB lookup returned synced lyrics for a well-known song; the imported recordings honestly reported `unavailable` (instrumental / not in LRCLIB) |
| Phone | Tempo on Android connected through `tailscale serve` (tailnet-only HTTPS, Navidrome still on loopback) with a non-admin account; imports showed up and played (user-confirmed) |
| Agent skill | Hermes, via its chat gateway and `skills/music-butler`, found a public-domain *The Entertainer* on Commons, wrote and validated the request, ran `butler.sh import` in the background and reported the result |
| Backups | `scripts/backup.sh` against a temporary restic repository: backup, prune, `restic check`, and `restore-test` with all audio hashes identical; Navidrome `backup create` copy passed SQLite `integrity_check` |
| Tests | 21 host tests pass, ShellCheck clean on all scripts |

## Review fixes (2026-09-29) — NOT yet executed

A code review found, and this revision changes, the following. They were
written on a Windows machine that cannot run the stack, so **none of this has
been run yet**. Rerun `scripts/validate.sh` and the container smoke test before
relying on it.

- The counts above ("21 tests") no longer match the repository: the suite at
  review time had 20 test methods (a rights-assertion test described earlier no
  longer existed). This revision adds `tests/test_agent.py` and two importer tests.
- `scripts/agent.py`: added a `butler_write_request` tool (the model previously
  had no way to write request files), argument checking so a bad tool call can't
  crash the loop, no agent-side timeout for imports/backups, and process-group
  termination on timeout/Ctrl+C. Ollama HTTP errors are reported by cause, and
  `write-request` now goes straight to the model.
- Artwork: `archive.org/download` URLs redirect to `*.archive.org` mirrors,
  which the no-redirect client always refused. Artwork fetches now follow up to 3
  redirects to allowlisted hosts, re-checking policy and public DNS for each hop.
- `DOWNLOAD_ALLOWED_HOSTS` entries are stripped and lowercased.
- `scripts/validate.sh` checked only the first script with `bash -n`; it now
  checks each one.
- `scripts/butler.sh` finds drives on Linux (`/run/media/$USER`, `/media`);
  `scripts/backup.sh` skips missing optional paths, handles a never-started
  Navidrome, restores only `music/` for `restore-test` after a free-space check,
  and falls back to `sha256sum` when there is no `shasum`.
- README restore command corrected to `docker compose run --rm navidrome backup restore ...`.

## Small-model / multi-song changes (2026-09-29) — partially executed

Goal: the skill and `scripts/agent.py` work with a ~8 B local model and with
several songs in one message.

- New request shape `{"songs": [...]}` (grouped into releases by the importer),
  sequential batch import ending in one `summary` line, `butler.sh add <id>`
  (validate then import), readable request ids `[a-z0-9-]{1,64}`.
- Agent: one `butler_add` tool (flat song list), compact results (summary or
  errors only), `num_ctx` 16384, temperature 0.2. SKILL.md rewritten as short
  imperative steps around the one file + one command flow.
- Cover fallback: yt-dlp `--write-thumbnail`, centre-square crop → `source_thumbnail`.
- LRCLIB: strict `/api/search` fallback after a 404 from `/api/get`.
- Default allowlist adds `m.youtube.com`, `music.youtube.com`. An existing
  `.env` keeps its own `DOWNLOAD_ALLOWED_HOSTS` until edited.

Actually run, on Windows 11 with Python 3.12, FFmpeg on PATH, and mutagen 1.48.1 +
yt-dlp 2026.8.19 in a venv. POSIX-only calls were stubbed (`flock`, `killpg`,
`O_NOFOLLOW`, directory fsync), so **locking, signals and fsync were not tested**:

- 23 of the importer tests passed, including real FFmpeg MP3/FLAC encodes, real
  thumbnail crop to 180×180 from a 320×180 image, thumbnail embedding in an
  ingest, batch continue/stop logic, songs grouping and per-song errors, the
  strict lyrics fallback, and the real yt-dlp parser accepting the new flags.
- 4 agent tests passed (the process-group test needs POSIX and was skipped).
- `bash -n` on every script.

Not run: the bash-script CLI tests, ShellCheck, Docker/Compose, a real YouTube
download (whether `-x` keeps the written thumbnail is taken from yt-dlp's
documented behaviour, not observed), live LRCLIB search, and any real small model.

## Lookup and title cleaning (2026-09-29) — partially executed

- `--lookup` / `butler.sh lookup <id>` / agent `butler_lookup`: YouTube search
  (`ytsearch5:`) and link inspection, whitelisted output fields only, with
  YouTube Music "Topic" results enriched with clean `artist`/`track`.
- SKILL.md: lookup → pick (Topic > official audio > lyric video, avoid
  videos/live/extended) → clean titles (drop video labels, keep feat./remix/live/
  remaster) → add. Songs asked for one by one get no `album`.

Actually run on Windows (same setup and stubs as above): 28 importer tests and
5 agent tests pass (1 POSIX-only skip), including the real yt-dlp parser on the
lookup flags and `capture()` against a real child process. A **live
metadata-only lookup** also ran (nothing downloaded) for "Daft Punk Get Lucky"
and "Scott Joplin The Entertainer". It returned 5 real candidates each; the
"Scott Joplin - Topic" result came back enriched with `artist: Scott Joplin`,
`track: The Entertainer`. Windows needed `SYSTEMROOT` added to the child
environment for sockets. The Linux container does not; this was injected only
in the test harness.

Not run: lookup inside the container, and a real small model choosing results
and cleaning titles.

## Reproduce

Local (Python 3.12 recommended, importer requires at least 3.11; host FFmpeg required):

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements.lock
uv pip install --python .venv/bin/python shellcheck-py==0.11.0.1
bash scripts/validate.sh
```

The production lock is Linux-targeted; the host validation used the pinned
primary versions on macOS. For the authoritative Linux dependency set and
FFmpeg, build and test the container instead:

```sh
docker compose --env-file .env.example --profile tools build downloader
python3 tests/container_smoke.py
```

The smoke action requires a working Docker daemon, pulls the configured
Navidrome image, runs disposable containers, mounts this source read-only and
cleans up temporary server storage. It does not start the default Compose
project, publish ports, download music or contact chat/model services.

## Not tested / still required

- YouTube sources.
- An album (multi-track) import against a live source; albums are covered only by
  the offline tests.
- Synced lyrics shown in Tempo for an imported song (none of the imported
  recordings had LRCLIB lyrics), Tempo offline cache, word-level karaoke.
- A backup to a real external drive, and a full disaster restore
  (`navidrome backup restore` plus library) onto a fresh host.
- Public exposure through a TLS reverse proxy or Tailscale Funnel.
- Hostile-network egress enforcement, crash/power-loss recovery, NFS/SMB or
  multi-host writers, and vulnerability scanning.
- Linux hosts: the live deployment was macOS with Docker Desktop.
