# Music Butler Stack — architecture and deployment blueprint

## Scope and deliberately conservative defaults

This bundle gives one household a private library, not an entitlement to a
commercial catalog. The operator (or their agent) supplies source URLs and
accurate metadata. Do not circumvent paywalls, DRM, access controls, region
restrictions, or authentication. No cookies, browser credential extraction,
user impersonation, or premium-audio ripping is provided.

A request that only says “get this artist/album” starts a confirmation and source
selection conversation with the agent. It does **not** start unrestricted
searching and downloading. This sacrifices automatic catalog matching to avoid
importing the wrong recording. Each album track needs an explicitly
approved URL; artist-wide requests and remote playlist expansion are unsupported.

### Architecture

```text
User's existing chat channel
        │  request / exact release confirmation
Existing agent (Hermes, local Ollama, or any SKILL.md-aware agent)
        │  fixed executor, bounded JSON stdin; no model text as shell
        ▼
scripts/download_music.sh → music_butler.py
        │  one library-wide advisory lock; clean tool HOME/environment
        ├─ yt-dlp (explicitly approved source only)
        ├─ FFmpeg → MP3 / FLAC; Mutagen → authoritative tags / JPEG cover
        ├─ LRCLIB GET /api/get → synchronized same-basename .lrc, if available
        └─ private sibling staging → fsync → atomic whole-release directory rename
                                 │
                                 ▼
MUSIC_LIBRARY_PATH/music/Artist--Album--UUID/    → Navidrome /music (read-only)
     audio + .lrc + provenance.json          /data (writable SQLite + cache)
                                 │             │
                                 └─ scan request / watcher / scheduled scan
                                               │ OpenSubsonic + streaming
                                               ▼
                                        Tempo on Android
```

Only Navidrome starts with ordinary `docker compose up -d`. The `tools` profile
provides a **one-shot** downloader; `hermes` provides an optional **execution
sandbox for an existing agent**, not another agent gateway or a pre-integrated
Telegram bot. Explicit `docker compose run downloader` automatically activates
the service's profile [D1]. No extra database, Redis, reverse proxy, task queue,
transcoder daemon or outbound lyrics service is needed.

## Upstream evidence and version policy

Researched against current documentation and tagged source on 2026-09-23. The
compatibility reference is **Navidrome 0.64.1**, not an invented REST endpoint.
The published `deluan/navidrome:0.64` minor tag was checked and **does not exist**;
`0.64.1` was checked successfully. Thus the default uses that available release
tag, an explicit exception to preferring major/minor tags. Override
`NAVIDROME_IMAGE` after reviewing releases, preferably with an audited digest for
immutable deployments. No `latest` tag is used.

The tooling image uses published `python:3.12-slim-bookworm` and
`node:24-bookworm-slim` (only the Node binary is copied); Python minor / Node major
tracks are supported build bases. Node is passed explicitly to yt-dlp for modern
YouTube JavaScript support [Y1]. No runtime auto-update/download script is used.
`requirements.in` fixes yt-dlp and Mutagen versions. `requirements.lock`
contains exact **transitive** versions and package hashes, resolved for Python
3.12/Linux. Rebuild via `pip install -r requirements.lock`. Upgrading a
downloader requires an explicit reviewed lock refresh and tests, since upstream
sites change often.

```sh
uv pip compile requirements.in --python-version 3.12 --python-platform linux \
  --generate-hashes -o requirements.lock
```

Compromises: Debian security packages and major/minor base tags intentionally
float; image builds are **not byte-for-byte reproducible**. For that, record
image digests and use a maintained Debian snapshot policy. Review dependencies
regularly with your chosen scanner; passing tests is not a vulnerability audit.
The orchestration code is MIT for easy reuse; each container/component and all
imported media retain their separate upstream licenses.

## Deployment and storage / UID-GID plan

Use Docker Engine/Desktop with Compose v2+ (long bind syntax and
`create_host_path: false` support), Bash, and host Python 3. Run as an ordinary
user. `./scripts/setup.sh --start` is the first-start command; after documented
setup, `docker compose up -d` suffices. Setup validates Compose **before**
creating configured bind paths, never sources `.env`, never recursively chowns,
and never overwrites an existing `.env`. Review custom paths before running.
`./scripts/setup.sh` alone provisions without starting services.

| Host setting / default | Container | Access and purpose |
|---|---|---|
| `NAVIDROME_DATA_PATH=./storage/navidrome` | Navidrome `/data` | read/write SQLite, settings, artwork/transcoding cache |
| `MUSIC_LIBRARY_PATH=./storage/library` | tooling `/library` | read/write staging, music and request receipts |
| `MUSIC_LIBRARY_PATH/music` | Navidrome `/music` | **read-only**, only published releases |
| `SECRETS_PATH=./secrets` | tooling `/run/music-secrets` | read-only secret files; directory may be empty |
| tmpfs | each `/tmp` | size-limited, noexec/nosuid ephemeral scratch |

`LOCAL_UID`/`LOCAL_GID` are numeric IDs, default 1000:1000 in the example. Setup
generates the **current host user's** IDs (including on macOS) for a fresh `.env`.
All containers use that UID/GID for predictable shared access; there is no
LinuxServer-style `PUID`/`PGID` entrypoint assumption. If you configure a different
service UID, pre-provision directories as administrator with the correct owner,
then test writes as that UID. `chmod 777` is not a fix. Use one dedicated service
account/group on a multi-user host; do not run the helpers as root.

The Bash wrapper sets `umask 027`; private stage directories are 0700, completed
release directories 0750, created audio/receipts normally 0640, secrets 0600.
Navidrome can read because its numeric UID/GID matches. The Docker image's
fallback user is 1000:1000, overridden by Compose. All services drop capabilities,
set `no-new-privileges`, use read-only root filesystems and explicit bridge
networking. Navidrome has `unless-stopped`; one-shot imports have `restart: no`
(no duplicate auto-downloads); the optional exec sandbox uses `unless-stopped`.
CPU/memory/PID limits are starting points; tune for your library. Healthcheck
uses `curl` (verified in the selected upstream Dockerfile [N5]) on the root UI:
it checks HTTP availability, **not** correct login or completed scanning. Batch
jobs have exit codes, not fake healthchecks; idle exec-only sandbox has no server
to probe. Docker does not automatically restart a merely unhealthy container.

Staging is `/library/.staging`, a **sibling** of `/library/music`, not a hidden
folder in Navidrome's mount. Both must live on the same local filesystem/device;
the script checks this and refuses cross-device publication. Do not put `music`
on a separately mounted dataset, use symlinked managed paths, or use NFS/SMB
where rename/locking/durability semantics differ. Use an ordinary ext4/XFS/APFS
filesystem, leave headroom, and apply a filesystem quota to the library account.
Docker Desktop's shared-volume locking/permissions/durability differ from Linux;
validate your actual filesystem before relying on crash guarantees.

## Network and permission model

Port defaults to **127.0.0.1:4533**, not all interfaces. Complete first-admin
creation locally before exposure. Keep administration private. For Tempo,
prefer a VPN (e.g. WireGuard/Tailscale) or a TLS reverse proxy configured by the
operator, with account brute-force protections. Never publish raw HTTP/login
tokens on the Internet. If binding a LAN/VPN IP using `BIND_ADDRESS`, restrict
it in the host firewall. Container-side `127.0.0.1` is **not** the Docker host;
tooling uses internal `http://navidrome:4533`. Loopback on your phone is the phone.
No service gets host networking, Docker socket, privileged mode, SSH keys or your
home directory. Public sharing and Navidrome insights/Deezer enrichment are off.

This is a **trusted single-operator executor**, not a public upload/download API:

- Input is bounded JSON with an exact key schema. Only enum formats/quality,
  fixed output templates and argument arrays are used. No `eval`, `shell=True`,
  arbitrary postprocessors, output directories, plugin flags or cookies.
- `DOWNLOAD_ALLOWED_HOSTS` is an operator-owned comma-separated exact-host
  allowlist (no wildcards). Initial source/artwork URLs require HTTPS, no userinfo,
  fragments, non-443 port, controls, pipe, or obvious credential query keys.
  Public DNS addresses are checked at execution. Existing tool configs/plugins
  are ignored; child HOME/XDG state is temporary, and scan/model secrets are not
  inherited by media subprocesses.
- **Important SSRF limit:** URL/DNS preflight is defense-in-depth, not an egress
  sandbox. yt-dlp can follow provider redirects and CDN manifests, and DNS
  can rebind. Compose's bridge is not a firewall. For hostile users/providers,
  enforce destination/IP restrictions with a managed egress proxy/firewall or
  separate VM; deny RFC1918, loopback, link-local/cloud metadata and host routes,
  apart from the intended Navidrome connection. Do not claim the application
  allowlist alone blocks every SSRF path.
- The helper's LRCLIB/scan HTTP client refuses redirects; the artwork client
  follows at most 3, only to allowlisted hosts (plus `*.archive.org` download
  mirrors when `archive.org` is allowed), each re-checked with the HTTPS/port/
  credential/public-DNS policy. All have bounded response size and timeout. FFmpeg normalization pins input demuxers and limits
  protocols; media parsers still process untrusted bytes. Keep them patched.
- Advisory lock protects cooperating writers, not a malicious same-UID process.
  A compromised downloader can damage its writable library; Navidrome cannot.
  Root/host compromise, network attacker with HTTP access, malicious operator
  configuration, and concurrent manual changes by the same UID are outside
  this boundary. Keep code and the executor's PATH administrator-controlled.

## Exact downloader contract

Entrypoint: executable `scripts/download_music.sh [--validate]`, reads one UTF-8
JSON document from **stdin** (maximum 256 KiB). It uses strict Bash and a Python
3.12 helper for safe JSON parsing, cross-platform POSIX `flock`, HTTP, Mutagen,
process groups and atomic directory operations rather than brittle shell JSON
parsing. Runtime tools: `python3`, `ffmpeg`, `yt-dlp`, Mutagen;
Node for yt-dlp's supported JS challenges. A local installation must provision
these in a trusted PATH; `.env` is Compose configuration, not automatically
loaded by the script.

See [`config/request.example.json`](config/request.example.json). Full schema:

| Key | Rules |
|---|---|
| `kind` | `song` (one track) or `album` (1–100 ordered tracks) |
| `artist`, `album` | required strings, 1–300 characters, no controls |
| `format` | `mp3` (default) or `flac` |
| `quality` | string: MP3 `128/192/256/320`, default `192`; FLAC `0`–`8`, default `5` |
| `tracks` | array; order becomes `tracknumber/total` |
| track `title` | required, same text constraints |
| track `url` | exact allowlisted HTTPS audio/video URL, ≤2048 characters |
| track `artwork_url` | optional image URL, same source URL policy |

Unknown keys, duplicate source URLs and wrong types are rejected. `--validate`
is offline schema/policy validation; it does **not** check download availability,
network reachability or dependencies. Normal execution does DNS/dependency
preflight too. Environment knobs are configured by the operator, not request
JSON: `LIBRARY_ROOT`, `DOWNLOAD_ALLOWED_HOSTS`, `DOWNLOAD_USER_AGENT`,
`LRCLIB_USER_AGENT`, `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD_FILE`.

### Transaction and normalization

1. Validate request and preflight source DNS, ensure safe local managed
   directories, acquire nonblocking per-library `.ingest.lock` (a busy caller
   exits 75), then check free space and tools. Locks release on process exit;
   **never delete the lock file to unlock**.
2. Allocate a UUID job directory in `.staging` on the destination filesystem.
   Require at least 2 GiB free initially. Provider attempts have a 15-minute
   timeout with bounded network retries; child process groups are killed on
   cancellation/timeouts. Limits are **not hard disk quotas**: downloaded and
   transcoded files can exceed estimated sizes, especially album FLAC output.
3. Default yt-dlp: no playlists, config/plugins/cache/geo-bypass, best audio,
   500 MB input size hint, reject known live/overlong recordings. Unknown source
   duration is allowed at fetch and checked after decoding. Canonical intermediate
   FLAC → FFmpeg final MP3 CBR or FLAC compression → Mutagen title, artist,
   albumartist, album and track number/total. Supports 1–3600 second tracks;
   long-form mixes/audiobooks are intentionally not supported. Re-encoding lossy
   audio loses quality; FLAC never creates true lossless audio from a lossy source.
4. Final authoritative tags always come from the user-confirmed request,
   not fuzzy provider metadata.
5. Optional approved artwork: bounded HTTP download (5 MiB), FFmpeg first-frame
   JPEG ≤600×600, Mutagen ID3 APIC (MP3) / FLAC PICTURE; failure is nonfatal. No
   blind image search. MP3 uses ID3v2.3; sidecar LRC is preferred to embedding
   unreliable SYLT conversions. Source chapter/disc/date/MusicBrainz tags are not
   inferred. Multi-disc releases can be supplied in one ordered list but do not
   retain separate disc numbering in this initial implementation.
6. LRCLIB lookup (below), hash finished tracks, write `provenance.json` with
   source, request metadata, provider and per-track outcomes.
   `fsync` completed files, set release directory 0750 and rename **the entire
   release** once into `music/Artist--Album--UUID`. UUIDs avoid collisions even
   when different titles slugify identically. There is no overwrite/update API;
   repeated requests produce separate folders, not idempotent deduplication.
7. Request scan, record a private job receipt and emit final `complete` JSON.
   If any album track fails before publication, publish **none** of the album.
   Cleanup traps remove staging on normal errors/TERM/INT. A power loss/SIGKILL
   can leave an orphan; recovery below. Rename gives atomic visibility, but
   power-loss durability still depends on the host filesystem/device semantics.

stdout is **JSON Lines**, including `valid`, `started`, `track_staged`, `warning`,
`complete`, `error`. Consumers must require exit 0 **and** `complete`; intermediate
track events are not imported files. `complete` reports request ID,
absolute folder path, per-track filename/hash/lyrics/artwork/provider and `scan` result.

| Exit | Meaning |
|---|---|
| 0 | validation succeeded, or audio published successfully (lyrics/scan warnings may remain) |
| 2 | argument-parser misuse |
| 65 | invalid input/policy |
| 69 | missing tool/dependency, unavailable provider/media, timeout |
| 73 | storage or runtime I/O problem |
| 75 | library lock busy; retry later |
| 70 | unexpected internal error |
| 130 / 143 | interrupted / terminated |

A failure after rename emits `published_reconcile_before_retry` when possible;
check the ID in the published folder/receipt before retrying. Never infer from
an interrupted terminal session that nothing was published.

## LRCLIB synchronized lyrics and Tempo

LRCLIB's documented `GET https://lrclib.net/api/get` accepts `track_name`,
`artist_name`, recommended `album_name` and measured duration (±2-second match
window). No API key is needed [L1]. Supply a real identifying `LRCLIB_USER_AGENT`
(application/version/contact or project URL); it is empty by default, so there
is no fake contact address. The client issues sequential lookups with ≥500 ms
spacing, honors 429 `Retry-After` (seconds or HTTP date) via a persistent cooldown
file, and does not retry in a tight loop. 404, network errors, malformed responses,
instrumental records and plain-only lyrics are **not** audio failures.

Only nonempty timestamped `syncedLyrics` is saved in UTF-8 as
`001-Title.lrc` beside `001-Title.mp3`/`.flac`. No inaccurate fallback search,
manual timestamp fabrication, LRCLIB publication, or lyrics upload. LRCLIB also
supports newer formats, but this implementation deliberately uses conventional
line-level LRC. Enabling lookups discloses track/artist/album/duration and source
IP to LRCLIB; downloaded lyrics may have independent copyright restrictions.

Navidrome's documented `LyricsPriority` includes same-basename sidecars; Compose
sets `.lrc,embedded` [N2]. Tempo's upstream source implements structured synced
lyrics and requests `getLyricsBySongId` [T1, O3]. This is karaoke-style **line
highlighting**, not a guarantee of word/syllable timing, translations or perfect
timing for alternate recordings. Verify your installed server/client versions.

Android checklist:

1. Install Tempo from its official releases; create a non-admin Navidrome user.
2. Add the HTTPS or trusted VPN/LAN server base URL (e.g. your own VPN hostname
   and port, **not** Docker's `navidrome` service name), username and password.
3. Refresh/synchronize the library after discovery. Open a track's player lyrics
   panel. Test with a known authorized track and its correctly named LRC.
4. If lyrics are absent, check the receipt, same basename, readable permissions,
   matching title/artist/album/duration, server lyrics support and client cache.
   Refresh/reconnect as needed. Offline cached media/lyrics behavior depends on
   Tempo version/settings and has not been assumed or guaranteed here.

## Navidrome scan mechanism and authentication

**Do not call `/api/scan`.** It is not the portable supported interface verified
for this integration. Use Subsonic/OpenSubsonic
**`POST /rest/startScan.view`**, form encoded with `u,t,s,v,c,f` (API 1.16.1,
JSON response). Navidrome 0.64.1 registers `startScan` behind **adminOnly** in
`server/subsonic/api.go`; `library_scanning.go` implements the scan [N3, N4].
The OpenSubsonic startScan contract is documented [O1]. HTTP 200 alone is not
success: the helper checks `subsonic-response.status == "ok"` and a scanStatus
object. It reports **requested**, not “finished”; even the source notes that a
fast/asynchronous scan may yield a status snapshot. `getScanStatus` is the
supported follow-up inspection endpoint, not an invented poll URL.

Token authentication is the documented legacy-compatible scheme [O2]: a fresh
random salt `s`, `t = MD5(password + salt)`, username `u`, client `c`, version `v`,
format `f=json`. MD5 is used **because this protocol requires it**, not as a new
password-storage design. It is not encryption; TLS is required off-host.
Parameters are in a POST body to avoid normal URL access logs. Do not enable
proxy/body debug logging; salts/tokens can still be sensitive/replayed. Redirects
are refused so credentials cannot be forwarded to another origin. Older/proxy
servers rejecting this method fall back to scheduled scanning rather than
leaking credentials via an improvised endpoint. A configurable Navidrome base
path is supported in local `NAVIDROME_URL`; Compose targets its bundled server.

**Recommended default: no scan credentials at all.** Compose explicitly enables
`ND_SCANNER_ENABLED=true`, `ND_SCANNER_WATCHERWAIT=5s`, and
`ND_SCANNER_SCHEDULE='@every 1m'` [N2]. The watcher usually discovers a rename
promptly; the scheduled scan covers missed events. Large scans can take longer
than the interval, so this is not a one-minute SLA. Blank `NAVIDROME_USER` reports
`scheduled`. Do not disable the schedule if you rely on credential-free fallback.
For huge libraries, choose a longer interval knowingly.

Optional immediate scan setup (operator only):

- Create a dedicated strong-password Navidrome **admin** account for scanning.
  There is no claim that the selected server provides a scan-only role. It is a
  powerful credential; consider whether seconds saved justify exposing it to
  the downloader. Never use Tempo's playback account or your primary admin.
- Store only that password in `secrets/navidrome_password`, mode **0600**, owned
  by the service UID; use an editor/secret manager, not a password in shell
  history, command arguments, `.env`, Git or chat. Set `NAVIDROME_USER` in `.env`.
  An empty secrets directory is valid until this feature is enabled.
- Docker Compose local bind files do not magically enforce secret permissions;
  the helper rejects group/world-readable secret files and symlinks. `*_FILE`
  here is our helper's contract, **not** an undocumented Navidrome environment
  feature. Read-only directory mounting does not prevent the helper reading
  another secret in that directory; put only these optional helper credentials
  there, never model/provider/chat credentials.
- Network/auth/admin-role errors yield `scheduled_after_scan_error` **without
  rolling back valid audio**. Fix credentials separately; do not redownload.
- Operator recovery: the documented `navidrome scan --full` command exists [N6].
  To avoid competing processes/DB access during repair: stop Navidrome, run
  `docker compose run --rm navidrome scan --full`, then `docker compose up -d`.
  This is an operator action, never a command handed to Hermes. A routine restart
  is unnecessary; no unverified restart-as-scan claim is used.

## Agent boundary

Recommended: **use your existing agent** (Hermes [H1], Claude Code, Codex,
Copilot or any agent that loads `SKILL.md` skills) with the supplied
[agent skill](skills/music-butler/SKILL.md). The skill defines the permitted file
and command operations, when to ask the user, the request schema,
truthful reporting and backup behavior. Its only executor is
[`scripts/butler.sh`](scripts/butler.sh), which accepts a 32-hex request id or a
drive name and nothing else, so no chat text reaches Docker or a shell. But **a
skill is not a security enforcement layer**: restrict the agent's command policy
to writing `requests/approved-*.json` and running `scripts/butler.sh`, and remember
that anyone who can message the agent can ask it for imports. An unrestricted
terminal does not become safe just because the skill says so.

Local executor: use Python 3.11+ (3.12 is used in the image). Install the
helper/tool dependencies in a trusted directory and
configure its environment, library UID/GID and private paths. Permit request
writes only in the request inbox and a fixed script argv/stdin operation. Keep
repository/scripts, PATH, allowlist and secrets administrator-owned. Do not give
the model filesystem management, arbitrary downloader flags or sudo access.

`butler.sh` runs each import as a one-shot container
(`docker compose run --rm -T downloader < requests/approved-<id>.json`). An
alternative for operators who prefer a persistent sandbox:

```sh
# Optional persistent execution environment (same image, no model/chat secrets):
docker compose --profile hermes up -d hermes-exec
# A fixed adapter permits only this executable/stdin contract:
docker compose exec -T hermes-exec /opt/music-butler/scripts/download_music.sh \
  < requests/approved-<id>.json
```

No Docker socket is mounted in these containers. An external adapter that talks
to Docker is itself privileged and must not accept arbitrary Docker arguments.
The optional environment has `sleep infinity` as its idle process; it is **not a
full Hermes installation**. This is intentional because the user already has
Hermes, model credentials and a messaging channel, and bootstrapping another
broad agent weakens the API/script boundary. If a genuinely separate Hermes
agent is wanted, install it using upstream instructions in a separately managed
profile/environment and connect the same fixed executor. No fake Hermes image,
auto-installed gateway, Telegram credentials or unverified config keys are shipped.

## Logging, recovery and backups

Each completed/failed job writes a private `logs/<request-id>.json` receipt;
completed releases also contain `provenance.json`. Receipts include source
URLs, file hashes and outcomes. They are **sensitive personal
listening/provenance data**, not public telemetry. Keep request files out of Git;
remove/retain them according to policy. Provider raw stdout/stderr is deliberately
not exposed (it may contain signed URLs); a media-tool failure reason can be
less detailed as a result. Never enable verbose logs in a public support paste.
No credentials go in status JSON. Container json-file logs rotate at 10 MB × 3;
per-request receipts do **not** auto-rotate: archive/prune them under the backup
policy, preserving necessary provenance. Monitor free space, unhealthy server,
job failure counts, stale staging and scanner logs with your host monitoring.

Recovery playbook:

- **Busy:** wait; do not unlink `.ingest.lock` (that would create a second lock
  inode and defeat serialization). Confirm no active downloader before maintenance.
- **Pre-publication failure:** fix input/dependency/disk/network and retry the
  confirmed request. The whole album was rolled back, not partially exposed.
- **Killed/power-loss job:** stop executor activity; inspect `.staging`, receipts
  and matching UUID release folders. Staging is never scanned. Remove confirmed
  abandoned job directories manually; do not follow symlinks or blanket-delete
  the music tree. Retry only after verifying the ID wasn't already published.
- **Post-publication receipt/scan failure:** reconcile `provenance.json` and audio
  hashes; request/await scan without redownloading. Published audio is retained.
- **Lyrics/artwork unavailable:** audio is usable. Obtain an authorized correct
  LRC/cover separately as operator and request rescan/refresh; this initial tool
  has no automatic enrichment-retry/update command. Do not run the same download
  merely to retry lyrics, since that duplicates the release.
- **Disk full:** preserve published releases, stop new jobs, free cache/abandoned
  staging only after inspection. Recheck UID/GID, quotas and mount device IDs.
- **Upgrade trouble:** stop imports, back up first, review upstream release notes,
  rebuild/test in an isolated clone. Database migrations may not be reversible;
  rollback image **and matching pre-upgrade database**, not just the image.

Backup the music tree (including LRC and provenance), private receipts, `.env`,
private scan/API secrets, and **all of `/data` needed for restore**. Encrypt secret
backups. Cache may be excluded if you accept regeneration; preserve the SQLite
DB, playlists/users and settings. Recommended simple consistent backup: stop
imports and Navidrome, snapshot/copy both library and data as a set, record image
versions/digests and UID/GID, then restart. Never copy a live SQLite DB while
ignoring its WAL/SHM; use a supported SQLite online-backup tool if downtime is
unacceptable. Keep multiple generations and an off-host encrypted copy. Do not
back up `.staging` as finished media. Test restore to alternate directories and
loopback port; verify login, playlists, representative audio hashes and LRC
before replacing production. Secrets may be revoked/rotated on restore.

[`scripts/backup.sh`](scripts/backup.sh) implements this without downtime:
`navidrome backup create` makes a consistent online SQLite copy, then restic
takes an encrypted, deduplicated snapshot of the library, receipts, DB copies,
`.env`, `secrets/` and `requests/` (retention 7 daily / 4 weekly / 12 monthly,
5 % data verification per run). `restore-test` restores the latest snapshot to a
temporary directory and compares every audio hash. It runs on demand
(`scripts/butler.sh backup <drive>` for a USB drive); scheduling is left to the
operator.

## Validation and explicit limitations

[docs/VALIDATION.md](docs/VALIDATION.md) is the execution record. Deterministic
unit/integration tests mock remote providers/APIs but use real generated audio,
FFmpeg and Mutagen; they do **not** represent commercial-media downloads.
Validate Compose, shell syntax/lint, provider argument parsing, MP3/FLAC tags,
artwork, transaction rollback, collision safety, locks, scan token/error handling
and lyrics rate limits before upgrading. Deployment acceptance additionally
requires testing your actual disk, authenticated Navidrome indexing, Tempo device,
network exposure and backup restore. The live results of one real deployment
(imports, indexing, Tempo over a Tailscale HTTPS proxy, backups, an agent-driven
import) are recorded in VALIDATION; they are not a claim of legal correctness.

Not included: commercial catalog search, DRM/cookie support,
artist discovery crawler, deduplication, robust malicious-tenant isolation,
hard disk quotas, guaranteed album metadata matching, guaranteed lyrics, word
karaoke timing, scheduled/off-site backups, TLS/VPN deployment, push
notifications, or a full unrestricted Hermes container. The initial workflow is
bounded per-track/per-release, single-library/single-disc metadata, and assumes
operator-controlled storage/configuration. Hosts/providers may become unavailable;
never silently replace a track to manufacture success.

## Primary sources

- **[N1]** Navidrome Docker deployment: https://www.navidrome.org/docs/installation/docker/
- **[N2]** Configuration reference (Scanner.*, LyricsPriority, sharing/insights): https://www.navidrome.org/docs/usage/configuration/options/
- **[N3]** Selected release router, including `adminOnly` for `startScan`: https://github.com/navidrome/navidrome/blob/v0.64.1/server/subsonic/api.go
- **[N4]** Selected release scan implementation: https://github.com/navidrome/navidrome/blob/v0.64.1/server/subsonic/library_scanning.go
- **[N5]** Selected release container packages: https://github.com/navidrome/navidrome/blob/v0.64.1/Dockerfile
- **[N6]** Supported scan CLI: https://www.navidrome.org/docs/usage/admin/cli/scan/
- **[O1]** OpenSubsonic scan: https://opensubsonic.netlify.app/docs/endpoints/startscan/ and https://opensubsonic.netlify.app/docs/endpoints/getscanstatus/
- **[O2]** Subsonic API introduction, authentication and GET/POST contract: https://www.subsonic.org/pages/api.jsp
- **[O3]** OpenSubsonic lyrics: https://opensubsonic.netlify.app/docs/extensions/songlyrics/ and https://opensubsonic.netlify.app/docs/endpoints/getlyricsbysongid/
- **[L1]** LRCLIB API and client identification/rate limits: https://lrclib.net/docs
- **[Y1]** yt-dlp options, dependencies, metadata, output: https://github.com/yt-dlp/yt-dlp/blob/master/README.md
- **[F1]** FFmpeg CLI: https://ffmpeg.org/ffmpeg.html
- **[M1]** Mutagen ID3: https://mutagen.readthedocs.io/en/latest/user/id3.html and FLAC: https://mutagen.readthedocs.io/en/latest/api/flac.html
- **[T1]** Tempo: https://github.com/CappielloAntonio/tempo and player implementation: https://github.com/CappielloAntonio/tempo/blob/main/app/src/main/java/com/cappielloantonio/tempo/ui/fragment/PlayerLyricsFragment.java
- **[H1]** Hermes authoritative documentation: https://hermes-agent.nousresearch.com/docs/
- **[D1]** Compose profiles: https://docs.docker.com/compose/how-tos/profiles/ and services reference: https://docs.docker.com/reference/compose-file/services/

Some formerly plausible documentation URLs now return 404 (including
Navidrome `/docs/usage/library/lyrics/` and OpenSubsonic
`/docs/overview/authentication/`); they are **not** used as evidence here.
