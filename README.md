# Music Butler

Ask for a song by name and it shows up in your own music server: tagged, with
cover art and synced lyrics, ready to play on your phone. It also grows the
library while you listen: it imports your Spotify playlists, then adds a weekly
batch of songs picked from what you star and play, and removes what you skip.

Music Butler connects [Navidrome](https://www.navidrome.org) (server),
[Tempo](https://github.com/CappielloAntonio/tempo) (Android player) and an AI
agent you already use (a local [Ollama](https://ollama.com) model, Claude Code,
Codex, [Hermes](https://hermes-agent.nousresearch.com) over Telegram...). The agent gets one
narrow command, [`scripts/butler.sh`](scripts/butler.sh). It never gets a shell
or Docker.

<!-- Screenshots: docs/screenshots/ (Tempo with "Discover Weekly", a chat request, a weekly report). -->

```text
you: "add Get Lucky by Daft Punk and Mr. Brightside"
 └─ agent ─ butler.sh lookup ─▶ YouTube Music search (yt-dlp, in a container)
          └ butler.sh add ────▶ download ▶ tag ▶ cover ▶ LRCLIB lyrics ▶ library
                                                            │
                         Tempo on your phone ◀── Navidrome ◀┘
```

## What it does

- **Add songs by name**, one or many at a time, in chat. Names, album and year
  come from YouTube Music release data. Duplicates and near-duplicate artist
  spellings are caught before anything downloads.
- **Clean files**: ID3v2.4 / FLAC tags (including multi-artist), square cover
  art, time-synced `.lrc` lyrics. Each album appears in the library all at once.
- **Whole playlists at once**: send a Spotify playlist (exported with
  [Exportify](https://exportify.app)) to the bot. It imports every song in the
  background and reports every 20 songs. Each song is matched to YouTube Music
  by artist, title and length, and anything uncertain is listed instead of guessed.
  At the end the playlist appears in Tempo under its own name.
- **Taste**: a few artists and a few words ("Italian pop, no trap") steer
  discovery until your own likes and ratings take over.
- **Weekly discovery**: new songs from the YouTube Music radios of songs you
  liked or rated highly, at least 5 by artists new to you, in a
  *Discover Weekly* playlist. Giving a song 1★ is the only way it leaves the
  library; nothing is removed by age or play count.
- **Weekly audit**: a stronger model checks tags and covers and fixes what it is
  sure about, and lyrics are retried for songs that had none (LRCLIB grows).
- **Safe defaults**: hardened containers (read-only, no capabilities, non-root),
  Navidrome on loopback only, a download host allowlist, every import logged
  with hashes, and encrypted [restic](https://restic.net) backups.

## Quickstart

You need Docker with Compose v2, Bash and Python 3. Linux is the main target;
macOS with Docker Desktop works too.

**1. Start the server**

```sh
git clone https://github.com/BoiMat/Music-Butler.git
cd Music-Butler
./scripts/setup.sh --start                       # writes .env, creates storage/, starts Navidrome
docker compose --profile tools build downloader  # the download/tag container
```

Open **http://localhost:4533** and create the admin account. Then create a
second, **non-admin** account to listen with.

For synced lyrics, set `LRCLIB_USER_AGENT` in `.env` to an app name plus your
contact email or project URL, e.g. `music-butler/1.0 (you@example.com)`, as
[LRCLIB](https://lrclib.net) asks. While it is blank, lyrics lookups are off.

**2. Connect your phone**

Navidrome only listens on `127.0.0.1`. To reach it from your phone, use a VPN or
a TLS reverse proxy. With [Tailscale](https://tailscale.com),
`tailscale serve --bg 4533` gives a tailnet-only HTTPS URL. Enable HTTPS
certificates in the Tailscale admin console first. In
[Tempo](https://github.com/CappielloAntonio/tempo/releases), add that URL and
the non-admin account. Any Subsonic client works.

**3. Add songs**

The easiest agent is the bundled local one: no API key, no account.

```sh
ollama pull qwen2.5:7b
python3 scripts/agent.py
> add Get Lucky by Daft Punk and The Entertainer by Scott Joplin
```

Already use an agent? Give it the skills in [`skills/`](skills/):
`music-butler` (add songs), `music-audit` and `music-discover`. See
[agent setup](docs/REFERENCE.md#ai-agent-skill-and-tempo) for Claude Code,
Codex, Copilot and Hermes. You can also
[write a request file by hand](docs/REFERENCE.md#import-approved-songs-and-albums).

**4. Teach it your taste (optional)**

```sh
# Listening account, so discovery learns from your stars, ratings and plays:
#   .env: NAVIDROME_LISTENER_USER=<your non-admin user>
#   secrets/navidrome_listener_password (chmod 600)
./scripts/butler.sh playlist liked    # import requests/spotify-liked.csv (an Exportify export) now
./scripts/butler.sh seeds start       # requests/seeds-start.json: {"artists": [...], "notes": "..."}
```

A playlist import runs in the background, 20 songs at a time, and reports after
each batch to `requests/spotify-<id>.log`. To get the reports in your chat, and
to send playlists to the bot as files, set in `.env`:

```sh
PLAYLIST_NOTIFY=hermes -p music-butler send --to telegram --quiet   # any command reading stdin
PLAYLIST_INBOX=/home/you/.hermes/profiles/music-butler/cache/documents  # where Hermes saves files
```

Then schedule the runs below. Details of the
[discovery rules](docs/REFERENCE.md#taste-bootstrap-and-weekly-discovery).

## Scheduled runs

[`scripts/scheduled.sh`](scripts/scheduled.sh) runs an agent for one job and
prints its report:

| Job | Suggested schedule | What it does |
| --- | --- | --- |
| `bootstrap` | daily | adds up to 25 songs (playlist queue first) until the library holds `BOOTSTRAP_TARGET` songs and the queue is empty, then reports once and stays silent |
| `weekly` | weekly | removes songs you rated 1★, adds 15 new songs |
| `audit` | weekly | retries missing lyrics, fixes certain tag, cover and duplicate problems, reports the rest |

Choose the agent in `.env`:

- **Any agent CLI** that reads the task on stdin and prints its answer:
  ```sh
  SCHEDULE_AGENT=command
  SCHEDULE_COMMAND=claude -p --allowedTools Bash(scripts/butler.sh:*),Bash(./scripts/butler.sh:*),Read,Edit(requests/**)
  ```
  Then add to your crontab (`crontab -e`). The report goes wherever you send
  stdout (a log, `mail`, a webhook):
  ```cron
  0 8 * * *  /path/to/Music-Butler/scripts/scheduled.sh bootstrap >> ~/music-butler.log 2>&1
  0 10 * * 0 /path/to/Music-Butler/scripts/scheduled.sh audit     >> ~/music-butler.log 2>&1
  0 11 * * 0 /path/to/Music-Butler/scripts/scheduled.sh weekly    >> ~/music-butler.log 2>&1
  ```
- **Hermes** (reports arrive in Telegram or another chat):
  ```sh
  SCHEDULE_AGENT=hermes
  HERMES_PROFILE=music-butler
  SCHEDULE_PROVIDER=          # blank = the profile's default provider/model
  SCHEDULE_MODEL=
  SCHEDULE_MODEL_WEEKLY=      # optional stronger model for taste decisions
  ```
  `./scripts/install_hermes_skill.sh` installs the skills and the cron scripts
  `music-discover-bootstrap.sh`, `music-audit.sh` and `music-discover.sh` into the
  profile. Then:
  ```sh
  hermes -p music-butler cron create '0 8 * * *'  --name music-discover-bootstrap --script music-discover-bootstrap.sh --no-agent --deliver telegram --workdir "$PWD"
  hermes -p music-butler cron create '0 10 * * 0' --name music-library-audit      --script music-audit.sh              --no-agent --deliver telegram --workdir "$PWD"
  hermes -p music-butler cron create '0 11 * * 0' --name music-discover-weekly    --script music-discover.sh           --no-agent --deliver telegram --workdir "$PWD"
  ```
  Set `cron.script_timeout_seconds: 3600` in the profile's `config.yaml`, because
  a run imports songs for several minutes.

Discovery needs judgment, so a small local model does the playlist bootstrap
better than the weekly picks. A mid-size hosted model works well for `weekly`.

## Good to know

- **Sources.** Songs come from YouTube (Music) and the Internet Archive through
  [yt-dlp](https://github.com/yt-dlp/yt-dlp), only from explicit URLs and only
  from hosts in `DOWNLOAD_ALLOWED_HOSTS`. Whether you may download a given
  recording depends on where you live and on the source's terms: that is your
  call. This is a personal library tool, not a sharing service.
- **The agent is not a security boundary.** The skills tell it to run only
  `butler.sh`. Restrict it with your agent's own command approval. Anyone who can
  message your agent can ask for imports.
- **Privacy.** Navidrome sends artist names to Deezer for artist pictures
  (`DEEZER_ENABLED=false` turns that off), and lyrics lookups go to LRCLIB when
  `LRCLIB_USER_AGENT` is set.
- **Backups.** `scripts/backup.sh` makes encrypted restic snapshots to a USB
  drive or any repository. See [backups](docs/REFERENCE.md#backups).

## Documentation

- [docs/REFERENCE.md](docs/REFERENCE.md): request format, tagging, fix-ups
  (`library`, `retag`, `remove`), discovery rules, agent setup, backups, repo map.
- [BLUEPRINT.md](BLUEPRINT.md): design, security boundary, upstream evidence,
  recovery and limitations.
- [docs/VALIDATION.md](docs/VALIDATION.md): what was actually tested.

Tests: `./scripts/validate.sh` (host) and the container suite described in
the validation guide.

## License

MIT. Upstream software and imported content keep their own licenses.
