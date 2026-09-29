---
name: music-butler
description: Add one or more songs or albums to the user's self-hosted Navidrome music library (Music Butler Stack), report the result, check library status, or make an encrypted backup to a USB drive. Use when the user asks to download, import or add music to their library, asks what is in it, or asks for a music backup.
---

# Music Butler

You add music to the user's private library. `REPO` is the repository folder.
The only command you ever run is `REPO/scripts/butler.sh`.

## Add music: 5 steps

Do ALL songs from the user's message together: one lookup, one request file, one `add`.

### Step 1: Look the songs up

Write `REPO/requests/lookup-<id>.json` (id rules in Step 3), with one search
text per song, `artist title`:

```json
{"search": ["Daft Punk Get Lucky", "Scott Joplin The Entertainer"]}
```

If the user gave links, add them as `"urls": ["https://..."]` to read their
real titles. Then run:

```
REPO/scripts/butler.sh lookup <id>
```

It downloads nothing. The last line lists, per search, up to 5 `results`, each
with `title`, `channel`, `duration` (seconds) and `url`. Results from a
`"<Artist> - Topic"` channel also have clean `artist` and `track` names.
If a search has `"error"` or no good result, try once more with a shorter text
(just artist and title, no extra words). If that fails too, ask the user for a link.

For a whole album, search each track separately (`artist track title`). If you
are not sure of the exact track list and order, ask the user first.

### Step 2: Pick one result per song

Pick in this order:
1. A `"<Artist> - Topic"` channel. This is the studio audio from YouTube Music,
   with the exact song length and no video parts.
2. "Official Audio" on the artist's own channel.
3. A "Lyrics" / "Lyric Video" upload.
Avoid music videos, live performances, "extended", "sped up", "slowed", "8D",
"nightcore", covers, karaoke, and uploads much longer than the others (they
have intros, dialogue or end credits). If the user wants such a version, pick it.

Use only a `url` from the results or from the user. Never invent or edit a URL.
Never add a playlist or channel. If the user gives an archive.org or Wikimedia
link, use that link.

Ask the user before continuing only if:
- no result is the song itself (only covers, live, remixes or other versions), or
- two results are different recordings and you cannot tell which is meant, or
- an album's track list or order does not match the real release.
Show the URL and say what differs. Otherwise continue.

### Step 3: Clean the names and write the request file

The `title` and `artist` you write become the tags in the library, exactly as written.
- If the result has `track` and `artist`, use them.
- Otherwise start from the video title and REMOVE: the `Artist - ` prefix, and
  `(Official Audio)`, `(Official Video)`, `(Official Music Video)`, `(Lyrics)`,
  `(Lyric Video)`, `(Audio)`, `(Visualizer)`, `[HD]`, `[4K]`, `(Remastered HD Video)`,
  and similar video labels.
- KEEP the parts that name a different recording: `feat. X` / `ft. X` (write it as
  `feat. X`), `Remix`, `Live`, `Acoustic`, `Remastered 2011`, `Radio Edit`, etc.
- For a Topic channel, the artist is the channel name without ` - Topic`.

Example: `Daft Punk - Get Lucky (Official Audio) ft. Pharrell Williams, Nile Rodgers`
becomes artist `Daft Punk`, title `Get Lucky (feat. Pharrell Williams & Nile Rodgers)`.

Choose a new id: lowercase letters, digits and dashes, e.g. `joplin-2-songs`.
Use a new id every time. Write UTF-8 JSON with your file tool (not `echo`) to
`REPO/requests/approved-<id>.json` (create `REPO/requests/` if missing):

```json
{
  "songs": [
    {"artist": "Scott Joplin", "title": "The Entertainer",
     "url": "https://www.youtube.com/watch?v=EQlPq4z2gBc"},
    {"artist": "Enrico Caruso", "title": "Over There", "album": "War Songs",
     "url": "https://archive.org/download/ITEM/01.flac"},
    {"artist": "Enrico Caruso", "title": "Second Track", "album": "War Songs",
     "url": "https://archive.org/download/ITEM/02.flac"}
  ]
}
```

Rules:
- Only these song keys: `artist`, `title`, `url`, `album`, `artwork_url`.
- Songs with the same `artist` and `album` become one album, in list order.
  Songs without `album` become singles.
- Leave `album` out for songs the user asked for one by one, even if you know
  the album. Use `album` only when the user asked for a whole album.
- Every `url` is from Step 2, and different from the others.
- `artwork_url` is optional. Leave it out unless you have the real cover on an
  allowed host. YouTube thumbnails are used as the cover automatically.
- Optional top-level `"format": "flac"` (default `mp3`) and `"quality"`
  (mp3 `"128"`, `"192"`, `"256"`, `"320"`; flac `"0"` to `"8"`).
- At most 100 songs. Text at most 300 characters.

### Step 4: Run it once

```
REPO/scripts/butler.sh add <id>
```

It checks the file, then downloads the songs one after another. This can take
many minutes. Wait until it exits. Run nothing else meanwhile.

### Step 5: Report from the LAST line

The last line of output tells you the result.

If it is `"event": "summary"`:
- `added`: these are now in the library. For each song, report `lyrics` and
  `artwork` (values below).
- `failed`: NOT added. Report the `reason`. `busy_retry_later` means another
  import was running: wait a minute, then retry.
- `not_attempted`: NOT added, because the run stopped early.
- To retry, write a NEW file with only the failed and not-attempted songs.
  Never add a song listed under `added` again, because that makes a duplicate.
- An `added` entry with `"warning": "published_check_before_retry"` IS in the
  library. Tell the user the operator should check it. Do not retry it.

If there is no summary line, nothing was downloaded. Read the `"event": "error"` line:
- `"song": N` means song number N in your list is wrong (see `reason`). Fix
  that song, write a new file, and run `add` again.
- Exit code 64 or 66 means a wrong id or a missing file. Check the file name.

Meanings to tell the user:
- `lyrics`: `synced` = synced lyrics saved. `unavailable`, `instrumental`,
  `disabled`, `rate_limited`, `lookup_failed` = no lyrics. Never write lyrics yourself.
- `artwork`: `embedded` = the given cover. `source_thumbnail` = the video or
  source thumbnail, cropped square. `unavailable` = no cover.
- `scan`: `requested` = "Navidrome is indexing it now". `scheduled` = "it
  appears within about a minute". `scheduled_after_scan_error` = "it appears
  within about a minute; the operator may want to check scanning".
- Suggest refreshing the music app (e.g. Tempo) on the phone.

Say "added" only for songs listed under `added`.

## Other commands

```
REPO/scripts/butler.sh lookup <id>     # search / read links (Step 1), downloads nothing
REPO/scripts/butler.sh status          # is Navidrome up + releases in the library
REPO/scripts/butler.sh drives          # USB drives currently plugged in
REPO/scripts/butler.sh backup <drive>  # encrypted backup to that drive
```

Backup: run `drives`. If it prints nothing, ask the user to plug in the USB
drive. If it prints several, ask which one. Run `backup <drive>`. It succeeded
only if the output ends with `Restore test passed`. Never print, ask for or
store the backup password.

## Never

- Run `docker`, `restic`, `yt-dlp` or Python yourself.
- Add pipes, redirections or extra flags to `butler.sh`.
- Edit `.env`, `compose.yaml`, scripts, or anything under `storage/`.
- Delete audio, change permissions, restart services or install packages.
- Print secrets.

On any failure, tell the user the `reason` from the output.
