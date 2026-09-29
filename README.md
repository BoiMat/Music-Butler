# Music Butler Stack

A private, self-hosted music library: **Navidrome → Tempo (Android)**, with an
optional, constrained download tool for your **existing Hermes Agent** (or a
local Ollama model, see [`scripts/agent.py`](scripts/agent.py)).

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
server. With Tailscale, `tailscale serve --bg 4533` gives a tailnet-only HTTPS URL
while Navidrome stays on loopback (enable HTTPS certificates in the Tailscale admin
console first). Create a **non-admin** playback account for Tempo.

## Import an approved song or album

```sh
docker compose --profile tools build downloader
mkdir -p requests
cp config/request.example.json requests/approved.json
# Edit every placeholder: accurate metadata and exact audio URLs.
# Never execute the example unchanged.
docker compose run --rm -T downloader --validate < requests/approved.json
docker compose run --rm -T downloader < requests/approved.json
```

For an album, set `kind` to `album` and list **each track in order**.
No artist-wide crawling or automatic search-to-download. MP3 quality is
`128|192|256|320`; FLAC compression is `0`–`8` (strings). Lossy-to-FLAC
conversion cannot restore quality.

Set `LRCLIB_USER_AGENT` in `.env` to your application name/version and **real
contact email or project URL** to enable synchronized lyrics. Blank disables
lookups. Optional `artwork_url` per track supplies an authorized image. It
follows the source URL policy; redirects are followed only to allowlisted hosts
(and, when `archive.org` is allowed, its `*.archive.org` download mirrors). Missing
artwork or synced lyrics does not prevent import.

Success emits a JSON `complete` event with files, hashes, lyrics/artwork status
and scan status. The **whole album** appears atomically in the library.
Navidrome's watcher plus a one-minute schedule arranges discovery without
credentials. Optional admin `startScan` is described in the blueprint; a scan
request is **not** a guarantee that the library is already indexed.

## Local Ollama agent

[`scripts/agent.py`](scripts/agent.py) is a small, standard-library-only chat
agent that runs a local [Ollama](https://ollama.com) model on your own machine
(16 GB is plenty for a 7–8 B model). It loads the
[agent skill](skills/music-butler/SKILL.md) as its system prompt, writes request
files through a structured tool and calls the same fixed `scripts/butler.sh`
commands, so nothing new reaches Docker or a shell.

```sh
ollama pull qwen2.5:7b          # or any small model you prefer
ollama serve                    # if not already running
python3 scripts/agent.py        # from this repository
```

The agent has **no web access**, so you supply the source. For example: "add
Scott Joplin, The Entertainer, single 'The Entertainer', from
https://upload.wikimedia.org/.../The_Entertainer.ogg". It writes
`requests/approved-<id>.json`, validates it, and imports it. If you don't give
a URL, it asks for one. Typing `write-request` lets you paste a request JSON
yourself and have the agent validate and import it.

Configuration is environment: `OLLAMA_MODEL` (default `qwen2.5:7b`),
`OLLAMA_BASE_URL` (default `http://localhost:11434`), `BUTLER_TIMEOUT`
seconds (default `3600`) for Ollama replies and the short `validate`/`status`/`drives`
commands. Imports and backups have no agent-side timeout because the importer
limits each download/encode step itself. Ctrl+C sends SIGTERM to the running
`butler.sh` and its `docker compose` client; check `status` before retrying an
import. The agent still needs Docker/Compose and the same
host prerequisites as the manual import above.

## AI agent skill and Tempo

[`skills/music-butler/SKILL.md`](skills/music-butler/SKILL.md) is a standard
agent skill (`SKILL.md` with `name`/`description` front matter) for imports,
library status and backups. Its commands are relative to this checkout; copy or
symlink the folder into your agent's skills directory and replace `REPO` with the
checkout path if the agent does not run from it. Typical locations (check your
agent's docs): Claude Code `~/.claude/skills/`, Codex `~/.codex/skills/`, GitHub
Copilot `.github/skills/`, Hermes `~/.hermes/skills/<category>/`.

- Agents run only [`scripts/butler.sh`](scripts/butler.sh), which accepts a request
  id or a drive name, never free text. Request data is a JSON file.
- Configure your agent's command approval so it may write
  `requests/approved-*.json` and run `scripts/butler.sh` and nothing broader. The
  skill text guides the model; it is **not** an OS security boundary. An agent with
  unrestricted terminal access keeps those powers, and anyone who can message it
  can ask for imports.
- Optional `hermes` profile starts an **execution sandbox**, not an agent or
  messaging gateway: `docker compose --profile hermes up -d hermes-exec`.
  See the [security boundary](BLUEPRINT.md#hermes-boundary).
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
│   └── request.example.json
├── skills/music-butler/SKILL.md     # agent skill (any SKILL.md-aware agent)
├── docker/Dockerfile
├── scripts/
│   ├── butler.sh                    # fixed agent entry point
│   ├── agent.py                     # local Ollama chat agent
│   ├── backup.sh                    # encrypted restic backups
│   ├── setup.sh
│   ├── validate.sh
│   ├── download_music.sh
│   └── music_butler.py
├── tests/
│   ├── test_pipeline.py
│   ├── test_config.py
│   ├── test_agent.py
│   └── container_smoke.py
├── docs/VALIDATION.md
└── storage/                         # ignored, created at setup
    ├── navidrome/                   # SQLite, cache, settings
    └── library/
        ├── .staging/                # NOT mounted into Navidrome
        ├── logs/                    # private per-request audit receipts
        └── music/Artist--Album--ID/ # audio + same-name LRC + provenance
```

## Backups

`scripts/backup.sh` makes an encrypted [restic](https://restic.net) snapshot of
the library (audio, lyrics, provenance, receipts), a consistent Navidrome
database copy (`navidrome backup create`), `.env`, `secrets/` and `requests/`.
It keeps 7 daily, 4 weekly and 12 monthly snapshots and verifies a sample of
the repository after each run. If `BACKUP_REPOSITORY` points at an unmounted
drive, the run is skipped.

The encryption password comes from `RESTIC_PASSWORD_FILE` (or
`RESTIC_PASSWORD_COMMAND`) if set, otherwise from the macOS Keychain.

macOS:

```sh
brew install restic
security add-generic-password -a music-butler -s music-butler-restic \
  -l 'Music Butler restic backup' -T /usr/bin/security -w "$(openssl rand -base64 32)"
```

Linux:

```sh
sudo apt install restic          # or your distribution's package
mkdir -p ~/.config/music-butler
(umask 077; openssl rand -base64 32 > ~/.config/music-butler/restic-password)
export RESTIC_PASSWORD_FILE=~/.config/music-butler/restic-password   # e.g. in ~/.profile
```

Then, on either system:

```sh
# Set BACKUP_REPOSITORY in .env, then:
./scripts/backup.sh init
./scripts/backup.sh            # back up now
./scripts/backup.sh restore-test
```

**Copy the password into a password manager** (macOS:
`security find-generic-password -a music-butler -s music-butler-restic -w`;
Linux: the password file). Without it the backup cannot be decrypted, so losing
the machine would also lose the backup.

`restore-test` restores the latest snapshot's `music/` tree and compares every
file hash with the live library. It needs free space about the size of the
library in `$TMPDIR` (or in `RESTORE_TEST_DIR` if set) and refuses to start
without it. `scripts/butler.sh drives` / `backup <drive>` find USB drives under
`/Volumes` on macOS and `/run/media/$USER`, `/media/$USER` or `/media` on Linux.

To restore, first export `RESTIC_REPOSITORY` (the `BACKUP_REPOSITORY` path) and
the password as above (`RESTIC_PASSWORD_FILE`, or on macOS
`RESTIC_PASSWORD_COMMAND='security find-generic-password -a music-butler -s music-butler-restic -w'`).
Then run `restic restore latest --target /tmp/restore`, copy the library and
`.env`/`secrets` back, and with Navidrome stopped run
`docker compose run --rm navidrome backup restore --backup-file /data/backup/<file>.db`
(the image's entrypoint is already `/app/navidrome`).

## Operate and validate

```sh
docker compose ps
docker compose logs --since 10m navidrome
# Host tests: Python 3.12, ffmpeg, locked dependencies (see validation guide).
./scripts/validate.sh
docker compose --env-file .env.example --profile tools --profile hermes config --quiet
```

[BLUEPRINT.md](BLUEPRINT.md) covers the exact request contract, upstream evidence,
permissions, secrets, recovery, backups and limitations.
[docs/VALIDATION.md](docs/VALIDATION.md) records **what was actually tested**.
The bundle is MIT-licensed; upstream software and imported content retain their
own licenses.
