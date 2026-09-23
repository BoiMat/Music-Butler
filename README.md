# Music Butler Stack

A private, self-hosted music library: **Navidrome → Tempo (Android)**, with an
optional, constrained download tool for your **existing Hermes Agent**.
Only import audio you are authorized to download, or verified public-domain
recordings. A Spotify subscription or a search result is **not** authorization.

## Start

Requirements: Docker Engine/Desktop with Compose v2+, Bash and Python 3 on the
host, and writable local storage. Linux is recommended; Docker Desktop on macOS
is supported for development. Clone this repository, enter its directory, then:

```sh
./scripts/setup.sh --start
```

This generates `.env` with your UID/GID, provisions bind directories, validates
Compose, and runs `docker compose up -d`. No root/sudo or bundled secrets.
For custom disks, copy `.env.example` to `.env`, set paths and numeric IDs first;
run `./scripts/setup.sh`, then `docker compose up -d`.

Open **http://localhost:4533** immediately and create the first admin account.
Keep the default loopback binding until onboarding is complete. Use a VPN or a
TLS reverse proxy for your phone; `localhost` on Android is the phone, not the
server. Create a **non-admin** playback account for Tempo.

## Import an approved song or album

```sh
docker compose --profile tools build downloader
mkdir -p requests
cp config/request.example.json requests/approved.json
# Edit every placeholder: accurate metadata, exact authorized audio URLs,
# per-track permission/license evidence. Never execute the example unchanged.
docker compose run --rm -T downloader --validate < requests/approved.json
docker compose run --rm -T downloader < requests/approved.json
```

For an album, set `kind` to `album` and list **each authorized track in order**.
No artist-wide crawling, automatic search-to-download, playlist expansion, or
Spotify audio extraction. MP3 quality is `128|192|256|320`; FLAC compression is
`0`–`8` (strings). Lossy-to-FLAC conversion cannot restore quality.

Set `LRCLIB_USER_AGENT` in `.env` to your application name/version and **real
contact email or project URL** to enable synchronized lyrics. Blank disables
lookups. Optional `artwork_url` per track supplies an authorized image. Missing
artwork or synced lyrics does not prevent import.

Success emits a JSON `complete` event with files, hashes, lyrics/artwork status
and scan status. The **whole album** appears atomically in the library.
Navidrome's watcher plus a one-minute schedule arranges discovery without
credentials. Optional admin `startScan` is described in the blueprint; a scan
request is **not** a guarantee that the library is already indexed.

## Hermes and Tempo

- Copy/adapt [the Hermes system prompt](config/hermes-music-butler-system-prompt.md)
  into your **existing** instance, with an operator-approved fixed executor.
  JSON goes to the script's stdin; never interpolate a chat message into a shell.
- Optional `hermes` profile starts an **execution sandbox**, not another Hermes
  agent or messaging gateway: `docker compose --profile hermes up -d hermes-exec`.
  See the [security boundary and installation choices](BLUEPRINT.md#hermes-boundary).
- Install [Tempo](https://github.com/CappielloAntonio/tempo/releases), add your
  reachable HTTPS/VPN Navidrome URL and playback credentials, refresh the library,
  play a song and open the lyrics panel. Synced lyrics highlight lines when the
  server's OpenSubsonic lyrics extension and matching LRC are available.

## Repository map

```text
.
├── README.md / BLUEPRINT.md / LICENSE
├── compose.yaml / .env.example / .gitignore / .dockerignore
├── requirements.in / requirements.lock
├── config/
│   ├── request.example.json
│   └── hermes-music-butler-system-prompt.md
├── docker/Dockerfile
├── scripts/
│   ├── setup.sh
│   ├── validate.sh
│   ├── download_music.sh
│   └── music_butler.py
├── tests/
│   ├── test_pipeline.py
│   ├── test_config.py
│   └── container_smoke.py
├── docs/VALIDATION.md
└── storage/                         # ignored, created at setup
    ├── navidrome/                   # SQLite, cache, settings
    └── library/
        ├── .staging/                # NOT mounted into Navidrome
        ├── logs/                    # private per-request audit receipts
        └── music/Artist--Album--ID/ # audio + same-name LRC + provenance
```

## Operate and validate

```sh
docker compose ps
docker compose logs --since 10m navidrome
bash -n scripts/download_music.sh
# Host tests: Python 3.12, ffmpeg, locked dependencies (see validation guide).
./scripts/validate.sh
docker compose --env-file .env.example --profile tools --profile hermes config --quiet
```

[BLUEPRINT.md](BLUEPRINT.md) covers the exact request contract, upstream evidence,
permissions, secrets, recovery, backups, legal constraints and limitations.
[docs/VALIDATION.md](docs/VALIDATION.md) records **what was actually tested**.
The bundle is MIT-licensed; upstream software and imported content retain their
own licenses. This is not Spotify, a streaming subscription, or an entitlement
to download copyrighted recordings.
