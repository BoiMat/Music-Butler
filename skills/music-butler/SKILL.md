---
name: music-butler
description: Add one or more songs or albums (by name, link or sent audio file) to the user's self-hosted Navidrome music library (Music Butler Stack), report the result, check library status, record the user's music taste, import a whole Spotify playlist (CSV), make a playlist from songs already in the library, or make an encrypted backup to a USB drive. Use when the user asks to download, import or add music to their library, asks what is in it, says what music they like or dislike, wants a playlist imported or made from their own songs (e.g. make me a chill playlist), or asks for a music backup.
---

# Music Butler

You add music to the user's private library. `REPO` is the repository folder.
The only command you run is `REPO/scripts/butler.sh`. It never takes search
words: every command takes the id of a file you write first.

To add songs, ALWAYS these 4 steps, all songs of the message together:
1. Write a lookup file `REPO/requests/lookup-<id>.json`: `{"search": ["Artist - Title", ...]}`
2. Run `REPO/scripts/butler.sh lookup <id>` and pick one result per song.
3. Write a request file `REPO/requests/approved-<id>.json` with the picked songs.
4. Run `REPO/scripts/butler.sh add <id>` once, then report.

The id is the part between `lookup-`/`approved-` and `.json`: lowercase
letters, digits and dashes, e.g. `djo-2`. Use a new id every time.

## Step 1: Look up

One search text per song, `Artist - Title` (with ` - `). A link the user
gave goes in `"urls"` (no search for it). An audio file the user sent goes in
`"files"`: its file name, e.g. `audio_063dae991426.mp3`:

```json
{"search": ["Daft Punk - Get Lucky", "Scott Joplin - The Entertainer"],
 "urls": ["https://..."], "files": ["audio_063dae991426.mp3"]}
```

For an album, search each track (`Artist - Track title`). Not sure of the track
list? Ask first. Run `REPO/scripts/butler.sh lookup <id>`; it downloads nothing.
Its last line lists `results` per search:

```json
{"title": "Golden", "channel": "HUNTR/X - Topic", "duration": 195,
 "url": "https://www.youtube.com/watch?v=9_bTl2vvYQg", "names": "release",
 "source": "youtube_music",
 "song": {"artist": "HUNTR/X", "title": "Golden", "album": "Golden", "year": 2025,
          "artists": ["HUNTR/X", "EJAE", "AUDREY NUNA", "REI AMI", "KPop Demon Hunters Cast"]}}
```

Each search, url and file can also have `"musicbrainz"`, the music database:
`{"match": "found", "title": ..., "artists": [...], "year": 2019}`. Its
`artists` are the performers only and `year` is when the song first came out.

`urls` and `files` give one result each, with `song`. A file's `song` comes
from its own tags.

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
With `"musicbrainz": {"match": "found"}`, take `year` and `artists` from it
instead. For a sent file, write `"file": "<file name>"` instead of `url`.
Write it with your file tool to `REPO/requests/approved-<id>.json`:

```json
{"songs": [
  {"artist": "HUNTR/X", "title": "Golden", "album": "Golden", "year": 2025,
   "artists": ["HUNTR/X", "EJAE", "AUDREY NUNA", "REI AMI", "KPop Demon Hunters Cast"],
   "url": "https://www.youtube.com/watch?v=9_bTl2vvYQg"},
  {"artist": "Scott Joplin", "title": "The Entertainer",
   "url": "https://archive.org/download/ITEM/entertainer.flac"},
  {"artist": "skeletalhypnagogia380", "title": "Rise, Player!", "file": "audio_063dae991426.mp3"}
]}
```

Never write made-up names such as `Artist Name` or `Song Name`: they are
refused (`placeholder_artist`). No names at all (a file without tags): ask
the user.

Only with `"names": "guessed"`, fix the `song`: the user named the artist =
use theirs (a guessed `artist` can be the uploader's channel); remove `(Official Audio)`, `(Official Video)`, `(Lyrics)`,
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
- `deferred`: YouTube blocked downloads for now. They are NOT added yet but are
  retried on their own in the background; the user gets a message. Never add
  them again yourself.
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
- `file_not_found`: use the sent file's name exactly, e.g. `audio_063dae991426.mp3`.
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

## Playlist from the library

For "make me a playlist of ..." from songs the user has. Download nothing.

1. Run `REPO/scripts/butler.sh songs 1`, then `songs 2`, `songs 3`... until
   `next` says `last page`. Each line is `number. Artist - Title (year, ...)`.
2. Pick 20-40 songs that fit (or the number asked for), best first.
3. Write `REPO/requests/mix-<id>.json`: `{"name": "Rainy Day", "songs": [12, 40, 7]}`
4. Run `REPO/scripts/butler.sh mix <id>`. Reply: name, song count, it is in Tempo.

Changing a playlist: write ALL its songs again under the same name.
`playlist_name_taken_choose_another`: use another name.

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
