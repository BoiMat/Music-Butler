---
name: music-butler
description: Add one or more songs or albums to the user's self-hosted Navidrome music library (Music Butler Stack), report the result, check library status, record the user's music taste, import a whole Spotify playlist (CSV), or make an encrypted backup to a USB drive. Use when the user asks to download, import or add music to their library, asks what is in it, says what music they like or dislike, wants a playlist imported, or asks for a music backup.
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
real titles. Then run it with the id, the part between `lookup-` and `.json`
(for `REPO/requests/lookup-djo-2.json` the id is `djo-2`):

```
REPO/scripts/butler.sh lookup <id>
```

It downloads nothing. The last line lists, per search, `results`, each with
`title`, `channel`, `duration` (seconds), `url`, `names` and `song`:

```json
{"title": "Golden", "channel": "HUNTR/X - Topic", "duration": 195,
 "url": "https://www.youtube.com/watch?v=9_bTl2vvYQg", "names": "release",
 "source": "youtube_music",
 "song": {"artist": "HUNTR/X", "title": "Golden", "album": "Golden", "year": 2025,
          "artists": ["HUNTR/X", "EJAE", "AUDREY NUNA", "REI AMI", "KPop Demon Hunters Cast"]}}
```

YouTube Music results (`"source": "youtube_music"`) come first. `song` holds the
names to tag. `"names": "release"` means they are the release's own names.
`"names": "guessed"` means they were guessed from a video title.
If a search has `"error"` or no good result, try once more with a shorter text
(just artist and title, no extra words). If that fails too, ask the user for a link.

For a whole album, search each track separately (`artist track title`). If you
are not sure of the exact track list and order, ask the user first.

### Step 2: Pick one result per song

Pick in this order:
1. A `"source": "youtube_music"` result whose `song` is the song asked for. This
   is the studio audio with the release's own names, cover and length.
2. Another result with `"names": "release"` (an `"Artist - Topic"` channel).
3. "Official Audio" on the artist's own channel.
4. A "Lyrics" / "Lyric Video" upload.
Avoid music videos, live performances, "extended", "sped up", "slowed", "8D",
"nightcore", covers, karaoke, instrumentals and uploads much longer than the
others. If the user wants such a version, pick it.

The user may spell the artist differently from the release (`Huntrix` for
`HUNTR/X`, `Gabri Ponte` for `Gabry Ponte`). That is still the same song.
A remix is a different song: pick it only if the user asked for the remix.

Use only a `url` from the results or from the user. Never invent or edit a URL.
Never add a YouTube playlist or channel link. If the user gives an archive.org or Wikimedia
link, use that link.

Ask the user before continuing only if:
- no result is the song itself (only covers, live, remixes or other versions), or
- two results are different recordings and you cannot tell which is meant, or
- an album's track list or order does not match the real release.
Show the URL and say what differs. Otherwise continue.

### Step 3: Write the request file

For each song, copy the chosen result's `url` and its `song` object
(`artist`, `title`, and `album`, `year`, `artists` when present) EXACTLY.
Do not change their spelling or capitals. These become the tags in the library.

Only if the chosen result has `"names": "guessed"`, check its `song` against the
video title and the user's words, and fix what is wrong:
- `artist` is missing: use the artist the user named.
- REMOVE video labels still in the title: `(Official Audio)`, `(Official Video)`,
  `(Lyrics)`, `(Lyric Video)`, `(Audio)`, `(Visualizer)`, `[HD]`, `[4K]`, and
  anything after ` | `.
- KEEP the parts that name a different recording: `feat. X` (write it as
  `(feat. X)`), `Remix`, `Live`, `Acoustic`, `Remastered 2011`, `Radio Edit`.

Choose a new id: lowercase letters, digits and dashes, e.g. `joplin-2-songs`.
Use a new id every time. Write UTF-8 JSON with your file tool (not `echo`) to
`REPO/requests/approved-<id>.json` (create `REPO/requests/` if missing):

```json
{
  "songs": [
    {"artist": "HUNTR/X", "title": "Golden", "album": "Golden", "year": 2025,
     "artists": ["HUNTR/X", "EJAE", "AUDREY NUNA", "REI AMI", "KPop Demon Hunters Cast"],
     "url": "https://www.youtube.com/watch?v=9_bTl2vvYQg"},
    {"artist": "Scott Joplin", "title": "The Entertainer",
     "url": "https://archive.org/download/ITEM/entertainer.flac"}
  ]
}
```

Rules:
- Only these song keys: `artist`, `title`, `url`, `album`, `artists`, `year`, `artwork_url`.
- `artist` is the main artist. `artists` lists everyone, main artist first.
- Songs with the same `artist` and `album` become one album, in list order.
  Songs without `album` become singles named after the song.
- Every `url` is from Step 2, and different from the others.
- `artwork_url` is optional. Leave it out: the upload's cover is used automatically.
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

An `added` or `failed` release can have `notes`:
- `artist_spelling_from_library` / `album_spelling_from_library`: the library
  already had this name with other capitals or punctuation, so the library's
  spelling (`used`) was used. Tell the user.
- `similar_artist_in_library`: a similar but different artist (`existing`) is
  already in the library. Tell the user; it may be the same artist spelled
  differently.

If there is no summary line, nothing was downloaded. Read the `"event": "error"` line:
- `"reason": "already_in_library"` with `"song": N`: song N is already in the
  library. Tell the user, remove that song from the list, write a new file with
  the others and run `add` again (if any are left).
- Any other `"song": N` means song number N in your list is wrong (see `reason`).
  Fix that song, write a new file, and run `add` again.
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

## Taste and playlists (for weekly discovery)

A scheduled job adds new songs the user may like, starting from their taste.
Use this when the user talks about their taste, not about specific songs to add now.

**"I like X", "more songs like X", "older songs by X", "never suggest X",
"I like 70s Italian pop"**: write `REPO/requests/seeds-<id>.json` and run
`REPO/scripts/butler.sh seeds <id>`:

```json
{"artists": [{"name": "Mina", "note": "older songs"}, "Lucio Battisti"],
 "songs": [{"artist": "Djo", "title": "End of Beginning"}],
 "notes": "70s-80s Italian pop and modern indie; no trap",
 "block_artists": ["Ava Max"]}
```

All keys are optional. Use only what the user said:
- An artist the user likes goes in `artists`, not in `notes`.
- `notes` is ONE short new sentence about taste in general (genres, eras,
  languages, dislikes), in the user's words. It is ADDED to the earlier notes,
  so never repeat them. Leave `notes` out if the message only names artists
  or songs. Only if the user says to forget their old taste, add `"clear_notes": true`.
- `block_artists` = never suggest them. `remove_artists` = stop using them as seeds.
The last line of the output shows all current seeds; tell the user briefly.
Do not add these songs now; the discovery job picks them up.

**"Import my Spotify playlist"**: you cannot read Spotify. The user exports the
playlist as CSV at https://exportify.app (works for their own and followed
playlists) and either sends you the file or saves it as
`REPO/requests/spotify-<name>.csv`.

1. If the user sent a CSV file, run `REPO/scripts/butler.sh inbox`. It prints
   `New playlist: <name> (<n> songs)` for each new file. If the user names a
   playlist that is already saved, its `<name>` is the part after `spotify-`.
2. Run `REPO/scripts/butler.sh playlist <name>`, once per playlist. It starts
   the import in the background and returns at once. If it says `was not
   started`, tell the user that line and stop.
3. Otherwise send ONE reply of one or two lines: the import started, the user gets a
   short report every 20 songs, and at the end the playlist appears in Tempo
   under the same name. The reports go to the user directly, not to
   you: never promise to follow up or send an update yourself. Then your turn
   is over: do not look up or add these songs, and do not run the command again.

If the user asks how an import is going, run `butler.sh playlist <name>` again:
while it runs, it prints the last report. Songs listed as "no certain match" or
failed can be added one by one the normal way when the user asks for them.

## Other commands

```
REPO/scripts/butler.sh lookup <id>     # search / read links (Step 1), downloads nothing
REPO/scripts/butler.sh status          # is Navidrome up + releases in the library
REPO/scripts/butler.sh inbox           # pick up playlist CSV files the user sent
REPO/scripts/butler.sh playlist <name> # import a whole playlist in the background
REPO/scripts/butler.sh discover        # current taste seeds, playlist queue and discovery candidates
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
- Make up search results, URLs, names, albums or years. Every `url` and `song`
  comes from a lookup you ran or from the user.

On any failure, tell the user the `reason` from the output. Never pretend a
command worked.
