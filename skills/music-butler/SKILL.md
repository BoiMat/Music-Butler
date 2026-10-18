---
name: music-butler
description: Add one or more songs or albums to the user's self-hosted Navidrome music library (Music Butler Stack), report the result, check library status, record the user's music taste, import a whole Spotify playlist (CSV), or make an encrypted backup to a USB drive. Use when the user asks to download, import or add music to their library, asks what is in it, says what music they like or dislike, wants a playlist imported, or asks for a music backup.
---

# Music Butler

You add music to the user's private library. `REPO` is the repository folder.
The only command you run is `REPO/scripts/butler.sh`. It never takes search
words: every command takes the id of a file you write first.

To add songs, ALWAYS these 4 steps, all songs of the message together:
1. Write a lookup file `REPO/requests/lookup-<id>.json`: `{"search": ["artist title", ...]}`
2. Run `REPO/scripts/butler.sh lookup <id>` and pick one result per song.
3. Write a request file `REPO/requests/approved-<id>.json` with the picked songs.
4. Run `REPO/scripts/butler.sh add <id>` once, then report.

The id is the part between `lookup-`/`approved-` and `.json`: lowercase
letters, digits and dashes, e.g. `djo-2`. Use a new id every time.

## Step 1: Look up

One search text per song, `artist title`. Add links the user gave as `"urls"`:

```json
{"search": ["Daft Punk Get Lucky", "Scott Joplin The Entertainer"], "urls": ["https://..."]}
```

For an album, search each track (`artist track title`). Not sure of the track
list? Ask first. Run `REPO/scripts/butler.sh lookup <id>`; it downloads nothing.
Its last line lists `results` per search:

```json
{"title": "Golden", "channel": "HUNTR/X - Topic", "duration": 195,
 "url": "https://www.youtube.com/watch?v=9_bTl2vvYQg", "names": "release",
 "source": "youtube_music",
 "song": {"artist": "HUNTR/X", "title": "Golden", "album": "Golden", "year": 2025,
          "artists": ["HUNTR/X", "EJAE", "AUDREY NUNA", "REI AMI", "KPop Demon Hunters Cast"]}}
```

A search with `"error"` or no good result: search once more with just artist
and title. Still nothing: ask the user for a link.

## Step 2: Pick one result per song

Best first:
1. `"source": "youtube_music"` whose `song` is the song asked for.
2. `"names": "release"` (an `Artist - Topic` channel).
3. "Official Audio" on the artist's channel.
4. A "Lyrics" / "Lyric Video" upload.

Avoid music videos, live, extended, sped up, slowed, 8D, nightcore, covers,
karaoke, instrumentals, and uploads much longer than the others, unless the
user asked for that version. A remix is a different song. Different spelling
by the user (`Huntrix` = `HUNTR/X`) is the same song.

Use only a `url` from the results or from the user. Never invent or edit one.
Never use a YouTube playlist or channel link.

Ask the user (show the URL, say what differs) only if no result is the song
itself, or two results are different recordings, or an album's tracks do not
match. Otherwise continue.

## Step 3: Write the request file

For each song copy the picked `url` and its `song` (`artist`, `title`, and
`album`, `year`, `artists` when present) EXACTLY: same spelling and capitals.
Write it with your file tool to `REPO/requests/approved-<id>.json`:

```json
{"songs": [
  {"artist": "HUNTR/X", "title": "Golden", "album": "Golden", "year": 2025,
   "artists": ["HUNTR/X", "EJAE", "AUDREY NUNA", "REI AMI", "KPop Demon Hunters Cast"],
   "url": "https://www.youtube.com/watch?v=9_bTl2vvYQg"},
  {"artist": "Scott Joplin", "title": "The Entertainer",
   "url": "https://archive.org/download/ITEM/entertainer.flac"}
]}
```

Only with `"names": "guessed"`, fix the `song`: no `artist` = use the one the
user named; remove `(Official Audio)`, `(Official Video)`, `(Lyrics)`,
`(Lyric Video)`, `(Audio)`, `(Visualizer)`, `[HD]`, `[4K]` and anything after
` | `; keep `(feat. X)`, `Remix`, `Live`, `Acoustic`, `Remastered`, `Radio Edit`.

Songs with the same `artist` and `album` become one album, in list order.

## Step 4: Add and report

Run `REPO/scripts/butler.sh add <id>` once. It downloads one song after another
and can take many minutes: wait, run nothing else. Report from its LAST line.

`"event": "summary"`:
- `added`: now in the library. Say "added" only for these. Never add them again.
- `failed`: NOT added; tell the `reason`. `busy_retry_later`: wait a minute, retry.
- `not_attempted`: NOT added, the run stopped early.
- To retry, write a NEW file with only the failed and not attempted songs.
- `"warning": "published_check_before_retry"`: it IS added; the operator should check it.
- `notes`: `artist_spelling_from_library` / `album_spelling_from_library` = the
  library's spelling (`used`) was kept; `similar_artist_in_library` = a similar
  artist (`existing`) exists. Tell the user.
- `lyrics`: `synced` = saved; anything else = no lyrics. Never write lyrics.
- `artwork`: `embedded` / `source_thumbnail` = cover saved; `unavailable` = none.
- Say it appears in the app (e.g. Tempo) within about a minute.

`"event": "error"` (nothing was downloaded):
- `already_in_library` with `"song": N`: tell the user, write a new file without
  song N, run `add` again.
- other `"song": N`: song N is wrong (`reason`). Fix it in a new file, run `add` again.
- `Invalid id` / `Missing ...`: do what the message says.

## Taste (for weekly discovery)

A weekly job adds songs the user may like. When the user talks about taste
("I like X", "more like X", "never suggest X", "I like 70s Italian pop"), not
about songs to add now, write `REPO/requests/seeds-<id>.json` and run
`REPO/scripts/butler.sh seeds <id>`:

```json
{"artists": [{"name": "Mina", "note": "older songs"}, "Lucio Battisti"],
 "songs": [{"artist": "Djo", "title": "End of Beginning"}],
 "notes": "70s-80s Italian pop; no trap",
 "block_artists": ["Ava Max"]}
```

All keys optional; use only what the user said. Liked artists go in `artists`.
`notes` = ONE new short sentence about taste in general; it is added to the
old notes, so never repeat them; leave it out if only artists or songs were
named. `"clear_notes": true` only if the user says to forget their taste.
`block_artists` = never suggest; `remove_artists` = stop using as seeds.
Tell the user the seeds from the last line briefly. Do not add these songs now.

## Spotify playlists

You cannot read Spotify. The user exports a playlist as CSV at
https://exportify.app and sends you the file.

1. Run `REPO/scripts/butler.sh inbox`. It prints `New playlist: <name> (<n> songs)`.
   A playlist saved earlier: `<name>` is the part after `spotify-`.
2. Run `REPO/scripts/butler.sh playlist <name>` once. It starts in the background.
3. If it says `was not started`, tell the user that line. Otherwise reply ONCE,
   in one or two lines: it started, a report comes every 20 songs, and at the end
   the playlist appears in Tempo. The reports reach the user directly: never
   promise updates. Then stop: do not add these songs or run it again.

"How is the import going?": run `butler.sh playlist <name>` again; it prints
the last report. Failed or "no certain match" songs can be added one by one
the normal way.

## Other commands

```
REPO/scripts/butler.sh status          # is Navidrome up + releases in the library
REPO/scripts/butler.sh discover        # taste seeds, playlist queue, discovery candidates
REPO/scripts/butler.sh drives          # USB drives plugged in
REPO/scripts/butler.sh backup <drive>  # encrypted backup to that drive
```

Backup: run `drives`. Nothing: ask the user to plug in the drive. Several: ask
which. Run `backup <drive>`; it worked only if the output ends with
`Restore test passed`. Never print or ask for the backup password.

## Never

- Run anything but `butler.sh`; add pipes, redirections or flags to it.
- Edit `.env`, `compose.yaml`, scripts or `storage/`; delete audio; restart services.
- Print secrets.
- Make up results, URLs, names, albums or years.
- Pretend a command worked. On failure, tell the user the `reason`.
