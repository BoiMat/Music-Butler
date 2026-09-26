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
| yt-dlp and spotDL `--help`, plus actual installed argument parsers in tests | accepted bundled flags; official Spotify API flag and empty lyrics provider list asserted |
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
  credential-like URLs, controls, private DNS, missing rights assertion.
- Quoted/no-shell argument construction, Unicode/path traversal slugging,
  actual Bash stdin forwarding and malformed JSON exit 65.
- Real media subprocess creation and SIGTERM cancellation/reaping.
- Cancellation propagation through otherwise non-fatal artwork/lyrics/scan and
  spotDL fallback paths; early rejection of unsupported host Python versions.
- Installed yt-dlp/spotDL argument parsers; spotDL official API configuration,
  missing-secret refusal and same-approved-URL fallback (no API access).
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
- SpotDL 4.5.2 defaults to an unofficial client; inspection led to forcing the
  official API and requiring operator credentials. No default embedded secret
  or unofficial authentication flow is used.
- Direct audio URLs can lack extractor duration metadata: the yt-dlp filter now
  permits unknown duration, with actual decoded duration checked before publish.
- A third-party `PendingDeprecationWarning` from Starlette's `multipart` import
  occurs when loading spotDL argument definitions. Tests pass; no web service
  is started. This is not a dependency security audit.
- Two combined terminal invocations were held for approval by the runner's
  command guard (chmod-plus-execution, and a multi-command Docker invocation).
  No user was present; they were not approved or relied upon as test results.
  File permissions and the independently permitted validations/build/smoke calls
  above provided the actual evidence.

## Post-merge findings (2026-09-26)

First real `./scripts/setup.sh --start` in the main checkout exposed two bugs
that the offline/`config`-only checks above missed; both fixed with regression
assertions in `tests/test_config.py`:

- `LIBRARY_PATH` collided with the compiler variable that macOS CLT `python3`
  exports (`/usr/local/lib`); Compose prefers the process environment over
  `.env`, so setup tried to provision `/usr/local/lib`. Renamed to
  `MUSIC_LIBRARY_PATH`.
- Unquoted `tmpfs: [/tmp:rw,noexec,...]` is a YAML flow list, so the commas
  produced five mounts and `docker compose up` failed with
  `invalid mount path: 'mode=1777'`. Now quoted.

After the fixes: 21 host tests pass (ShellCheck included), `docker compose up -d`
brings Navidrome to `healthy` on `127.0.0.1:4533`, and the built downloader
container returns `valid` for `config/request.example.json` under `--validate`.

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

## Not tested / deployment acceptance still required

- A real authorized remote download, live Spotify API credentials or live LRCLIB
  lookup: no approved recording/credentials supplied; mocks used, not fabrication.
- Authenticated Navidrome scan/indexing/end-to-end playback: disposable server
  had no user account; production credentials were not available or used.
- Android/Tempo lyrics, offline cache behavior or word-level karaoke: no device.
- Hermes gateway/Telegram/chat tool adapter or full separate Hermes agent:
  existing instance must be configured by its operator; optional sandbox only.
- TLS/VPN, hostile-network egress enforcement, hardware crash/power-loss recovery,
  backup restoration, NFS/SMB or multi-host writers, and vulnerability scanning.
- `docker compose up -d` against a permanent deployment: intentionally not run;
  equivalent server startup settings were smoke-tested in an isolated container.

Before real use: review rights evidence, configure real LRCLIB identification if
wanted, create the first admin locally, set Tempo's non-admin account, verify
filesystem permissions/quota, test an approved recording, verify indexing and
lyrics on the actual phone, and perform an encrypted backup/restore drill.
