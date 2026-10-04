# Music Butler reference

The details behind the [README](../README.md): request format, tagging,
library fix-ups, discovery rules, agent setup, backups and validation.

## Import approved songs and albums

```sh
docker compose --profile tools build downloader
mkdir -p requests
cp config/request.example.json requests/approved-mine.json
# Edit every placeholder: accurate metadata and exact audio URLs.
# Never execute the example unchanged.
./scripts/butler.sh add mine     # validates, then imports; nothing downloads unless all is valid
```

One request can hold many songs:
`{"songs": [{"artist", "title", "url", "album"?, "artists"?, "year"?, "artwork_url"?}, ...], "format"?, "quality"?}`.
`artist` is the main (album) artist; `artists` lists everyone on the track, main artist first.
Songs that share artist and album become one album in list order. Songs without
`album` become singles named after the song. Releases are imported one after
another. A bad or unavailable source skips to the next release; storage or lock
problems stop the batch. The original single-release format (`kind`, `artist`,
`album`, `tracks`) still works; see the [blueprint](../BLUEPRINT.md#exact-downloader-contract).
`butler.sh lookup` only returns search candidates: YouTube Music songs first,
then YouTube videos. Each result carries a `song` object with the names to tag:
the release's own artist(s), title, album and year for YouTube Music / "Artist -
Topic" uploads (`"names": "release"`), or names guessed from the video title
with labels like "(Official Video)" removed (`"names": "guessed"`). The agent
chooses and copies `song` and `url`; every imported track is an explicit URL in the request. No artist-wide crawling or
playlist expansion. MP3 quality is
`128|192|256|320`; FLAC compression is `0`–`8` (strings). Lossy-to-FLAC
conversion cannot restore quality.

**Tags** (title, artist, album artist, album, track n/total, year, and a
multi-valued `ARTISTS` tag) always come from the request, which the agent fills
from lookup's `song`; the downloaded file's own metadata is never used. MP3s use
ID3v2.4 so artist names containing "/" (`HUNTR/X`) stay whole. Genre and disc
numbers are not set.

**Consistency:** before anything downloads, the request is checked against the
library. An artist or album that matches an existing one apart from case,
punctuation or accents takes the library's spelling; a close but different
artist is reported as a `similar_artist_in_library` note. A song already in the
library (same main artist and title, or the same YouTube video) is refused with
`already_in_library`.

**Cover art:** an optional `artwork_url` (on an allowed host) is used first.
Otherwise the source's own thumbnail (for YouTube, the video thumbnail) is
centre-cropped to a square and embedded. For "Official Audio"/art-track uploads
that is the release cover; for music videos it is a video frame. The receipt
says which (`embedded`, `source_thumbnail`, `unavailable`). Artwork redirects
are followed only to allowlisted hosts (plus `*.archive.org` download mirrors
when `archive.org` is allowed).

**Lyrics:** set `LRCLIB_USER_AGENT` in `.env` to your application name/version
and **real contact email or project URL** to enable synchronized lyrics. Blank
disables lookups. The lookup matches on artist, title and the downloaded
file's duration (±2 s). A YouTube upload with an intro, outro or dialogue has a
different duration and usually gets no lyrics, which is why the skill prefers
"Official Audio" uploads. Missing artwork or synced lyrics does not prevent import. LRCLIB answers 503 under load
and gains lyrics over time, so busy or failed requests are retried, a release entry with
only plain lyrics falls back to the same strict search, and `butler.sh lyrics` (run by the
weekly audit) retries every song that still has none.

Each release emits a JSON `complete` event with files, hashes, lyrics/artwork
status and scan status. The last line is always one `summary` event listing what
was `added`, what `failed`, and what was `not_attempted`. Retry only the latter
two; re-adding creates duplicates. Each **whole album** appears atomically in the library.
Navidrome's watcher plus a one-minute schedule arranges discovery without
credentials. Optional admin `startScan` is described in the blueprint; a scan
request is **not** a guarantee that the library is already indexed.

**Fixing the library later** (no re-download):

```sh
./scripts/butler.sh library [page]   # releases, 20 per page: id, tags, cover/lyrics status (JSON)
./scripts/butler.sh lyrics           # retry synced lyrics for every song without them
./scripts/butler.sh retag <id>       # requests/retag-<id>.json: {"changes": [{"release", "track"?,
                                     #   "artist"?, "artists"?, "album"?, "title"?, "year"?,
                                     #   "cover_from"? (a YouTube link) | "artwork_url"?}]}
./scripts/butler.sh remove <id>      # requests/remove-<id>.json: {"releases": [...]} -> library .trash
```

`retag` rewrites tags on a copy and swaps it in; a new main `artist` keeps the
featured artists. `remove` only moves releases to `<library>/.trash`; delete
that folder yourself when you are sure. Both take the import lock and request a scan.
[`skills/music-audit/SKILL.md`](../skills/music-audit/SKILL.md) lets a stronger
model do this periodically: read the library, confirm names with lookups, fix
what is certain and report the rest.

**Artist pictures** come from Navidrome's Deezer agent, by artist name (Navidrome
sends artist names to Deezer). Set `DEEZER_ENABLED=false` in `.env` to turn it off.

## Taste bootstrap and weekly discovery

The library can grow without you naming every song. You give a direction once;
an agent then adds songs from your playlists and songs you have not heard yet.

```sh
./scripts/butler.sh seeds <id>       # requests/seeds-<id>.json: {"artists": ["Name" | {"name", "note"}],
                                     #   "songs": [{"artist", "title"}], "notes": "..." (added to
                                     #   earlier notes; "clear_notes": true resets them),
                                     #   "block_artists": [...], "remove_artists": [...]}
./scripts/butler.sh playlist <id>    # import requests/spotify-<id>.csv (an https://exportify.app export) now
./scripts/butler.sh inbox            # copy new CSVs from PLAYLIST_INBOX into requests/
./scripts/butler.sh queue <id>       # or queue it for the daily bootstrap job, 25 songs a day
./scripts/butler.sh discover         # what to add next (JSON); downloads nothing
```

- **Playlists**: `playlist` imports a Spotify playlist exported as CSV with
  Exportify (any CSV with track and artist columns works) in the background and
  returns at once. Python, not the agent, matches each song: a YouTube Music
  release by the same main artist (or one of the credited artists) with the same
  title, at most 3 s longer or shorter than on Spotify (without a Spotify length,
  the exact title). Songs without such a match are reported as "no certain
  match" and not guessed; add them by name if you want them. Every call of the
  importer handles 20 songs; after each, a report (added, failed with the reason,
  not matched, how many are left) goes to `requests/spotify-<id>.log` and to the
  `PLAYLIST_NOTIFY` command (e.g. `hermes send`). Progress is kept in
  `<library>/discovery/imports/<id>.json`, so a stopped import continues where it
  stopped and failed songs are not retried; a busy library (another import) is
  waited for. `playlist <id>` again while it runs prints the last report.
  `inbox` picks up CSVs the user sent to the agent: Hermes saves Telegram files
  in its profile's `cache/documents`, which `PLAYLIST_INBOX` points to.
  `queue` is the slow alternative: the daily bootstrap job adds 25 queued songs
  a day.
- **Discovery**: `discover` reads the YouTube Music radio of songs you love and
  ranks the songs found there by how many (and how loved) seeds lead to them. At
  most 2 songs per artist; songs already in the library, removed before or
  offered 4 times without being picked are skipped. Seeds are your songs in
  Navidrome: liked (starred) or 5 stars (weight 4), 4 stars (3), played twice
  (2), 3 stars, played once, asked for or imported from playlists (1); 1-2 stars
  is never a seed. Your `seeds` artists and songs steer only while fewer than 12
  songs are rated 4+, starred or played twice. After that, your listening steers.
  Ratings and plays are per Navidrome user: set `NAVIDROME_LISTENER_USER` to the
  account you play with (e.g. Tempo's) and put its password in
  `secrets/navidrome_listener_password` (mode 600).
- **Removing**: `discover` lists as `stale` every song you rated 1 star (2 stars
  stays, but never steers). Nothing leaves by age or play count: with shuffle over
  a growing library, a month without a play is chance, not a verdict. `remove`
  moves them to the trash and records them, so they are never suggested again.
- Imports carry an `origin` (`request`, `playlist`, `seed`, `discovery`). It is
  kept in `provenance.json`, shown by `library` and written as the comment tag
  `music-butler:<origin>`. Bulk origins skip songs already in the library
  instead of failing the request.
- *Discover Weekly* is a Navidrome smart playlist (`.nsp` in the music folder,
  written by `discover`) of the discoveries of the last 6 days (so last week's batch
  is gone by the Sunday 10:00 audit, before the new one lands): play it on
  Sunday and give 1 star to what you do not want. Smart playlists belong to the
  admin; `ND_DEFAULTPLAYLISTPUBLICVISIBILITY` makes it visible to the Tempo
  account. Navidrome re-evaluates a smart playlist only when its owner opens it,
  so `scheduled.sh` runs `butler.sh playlists` after every job: it waits for the
  scan and opens it as the admin.
- State lives in `<library>/discovery/` (`seeds.json`, `queue.json`,
  `history.json`) and is backed up with the library.

[`skills/music-discover/SKILL.md`](../skills/music-discover/SKILL.md) is the agent
side: it runs `discover`, removes 1-star songs, chooses the songs (playlist queue,
then well-known songs of seed artists, then candidates fitting your notes), looks
them up and adds them with their origin. Run it on a schedule with
[`scripts/scheduled.sh`](../README.md#scheduled-runs): a daily bootstrap job (25 songs a
day, playlist queue first, silent once the library holds `BOOTSTRAP_TARGET`
songs (150) and the queue is empty) and a weekly
job (15 songs, at least 5 by new artists).

## Local Ollama agent

[`scripts/agent.py`](../scripts/agent.py) is a small, standard-library-only chat
agent that runs a local [Ollama](https://ollama.com) model on your own machine
(16 GB is plenty for a 7–8 B model). It loads the
[agent skill](../skills/music-butler/SKILL.md) as its system prompt. It has two
music tools. `butler_lookup` searches YouTube for "artist title" texts and
returns candidates. `butler_add` takes a list of songs, writes the request file
and runs `scripts/butler.sh add`. The model only sees compact results, so a
small model can handle several songs in one message. Nothing new reaches Docker
or a shell.

```sh
ollama pull qwen2.5:7b          # or any small model you prefer
ollama serve                    # if not already running
python3 scripts/agent.py        # from this repository
```

Just name the songs: "add Get Lucky by Daft Punk and The Entertainer by Scott
Joplin". It looks them up, picks the best upload (YouTube Music "Topic" audio,
then official audio, then lyric videos), cleans the titles for tagging, adds
them, and reports. You can also paste links. Typing `write-request` lets you
paste a request JSON yourself; it is added directly and the model reports the
result.

Configuration is environment: `OLLAMA_MODEL` (default `qwen2.5:7b`),
`OLLAMA_BASE_URL` (default `http://localhost:11434`), `OLLAMA_NUM_CTX` (default
`16384`; Ollama's own default window is too small for the skill), and
`BUTLER_TIMEOUT` seconds (default `3600`) for Ollama replies and the short
`status`/`drives` commands (lookups are capped at 900 s). Imports and backups have no agent-side timeout because the importer
limits each download/encode step itself. Ctrl+C sends SIGTERM to the running
`butler.sh` and its `docker compose` client; check `status` before retrying an
import. The agent still needs Docker/Compose and the same
host prerequisites as the manual import above.

## AI agent skill and Tempo

[`skills/music-butler/SKILL.md`](../skills/music-butler/SKILL.md) is a standard
agent skill (`SKILL.md` with `name`/`description` front matter) for imports,
library status and backups. Its commands are relative to this checkout; copy or
symlink the folder into your agent's skills directory and replace `REPO` with the
checkout path if the agent does not run from it. Typical locations (check your
agent's docs): Claude Code `~/.claude/skills/`, Codex `~/.codex/skills/`, GitHub
Copilot `.github/skills/`.

**Hermes:** a dedicated profile keeps the skill out of your main agent.
`hermes profile create music-butler --no-skills`, then
`./scripts/install_hermes_skill.sh [profile]` (default `music-butler`) writes
the skill into that profile with `REPO` replaced by this checkout's absolute
path and a short note on Hermes' `write_file`/`terminal` tools. Re-run it after
editing `SKILL.md`. In the profile's `config.yaml`, set `terminal.cwd` to this
checkout and `terminal.timeout: 1800`, and add `TERMINAL_MAX_FOREGROUND_TIMEOUT=3600`
to its `.env` so long imports are not cut off. Limiting `platform_toolsets` to
`file`, `skills` and `terminal` keeps the prompt small. Tell the profile's
`SOUL.md` to call `skill_view` for `music-butler` first: gateways do not preload skills.
The installer also installs `music-audit` and `music-discover`. It also writes the profile's cron scripts, which run
[`scripts/scheduled.sh`](../scripts/scheduled.sh) (see
[Scheduled runs](../README.md#scheduled-runs)), so the audit and discovery can
use a stronger model while chat stays local. Set `cron.script_timeout_seconds`
high enough (3600) in the profile's `config.yaml` (an LLM cron job in a profile
whose default model is local Ollama picks the wrong Copilot API mode, so
scheduled runs go through no-agent scripts).
For a local Ollama model, Hermes needs at least 64k context and cannot set it
through Ollama's `/v1` API, so bake it into a model variant
(`FROM gemma4:latest` / `PARAMETER num_ctx 65536`, `ollama create gemma4-64k`)
and use `provider: custom`, `base_url: http://localhost:11434/v1`.

- Agents run only [`scripts/butler.sh`](../scripts/butler.sh), which accepts a request
  id or a drive name, never free text. Request data is a JSON file.
- Configure your agent's command approval so it may write
  `requests/approved-*.json`, `requests/lookup-*.json` (and, for the audit and
  discovery skills, `retag-`, `remove-` and `seeds-*.json`) and run
  `scripts/butler.sh` and nothing broader. The skill works without the agent's
  own web search: `butler.sh lookup` searches YouTube through yt-dlp in the
  downloader container. The
  skill text guides the model; it is **not** an OS security boundary. An agent with
  unrestricted terminal access keeps those powers, and anyone who can message it
  can ask for imports.
- Optional `hermes` profile starts an **execution sandbox**, not an agent or
  messaging gateway: `docker compose --profile hermes up -d hermes-exec`.
  See the [security boundary](../BLUEPRINT.md#hermes-boundary).
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
├── skills/music-audit/SKILL.md      # periodic library clean-up skill
├── skills/music-discover/SKILL.md   # playlist bootstrap + weekly discovery skill
├── docker/Dockerfile
├── scripts/
│   ├── butler.sh                    # fixed agent entry point
│   ├── agent.py                     # local Ollama chat agent
│   ├── install_hermes_skill.sh      # render the skills + cron scripts into a Hermes profile
│   ├── scheduled.sh                 # scheduled audit / bootstrap / weekly discovery runs
│   ├── backup.sh                    # encrypted restic backups
│   ├── setup.sh
│   ├── validate.sh
│   ├── download_music.sh
│   └── music_butler.py
├── tests/
│   ├── test_pipeline.py
│   ├── test_config.py
│   ├── test_agent.py
│   ├── test_scheduled.py
│   └── container_smoke.py
├── docs/REFERENCE.md / docs/VALIDATION.md
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

[BLUEPRINT.md](../BLUEPRINT.md) covers the exact request contract, upstream evidence,
permissions, secrets, recovery, backups and limitations.
[docs/VALIDATION.md](VALIDATION.md) records **what was actually tested**.
The bundle is MIT-licensed; upstream software and imported content retain their
own licenses.
