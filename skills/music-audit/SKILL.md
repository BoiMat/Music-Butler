---
name: music-audit
description: Audit the user's self-hosted Navidrome music library (Music Butler Stack) for wrong or inconsistent tags, duplicate songs and missing covers, fix what is certain, and report the rest. Use for a scheduled or requested library check-up, not for adding music.
---

# Music Audit

You keep the user's private music library tidy. `REPO` is the repository folder.
The only command you run is `REPO/scripts/butler.sh`. You never download new
music here; that is the music-butler skill's job.

## 1. Read the library

```
REPO/scripts/butler.sh library
```

The last line is `{"event": "library", "releases": [...]}`. Each release:

```json
{"id": "26849cee7e3214c10970dfd0bb892154", "artist": "Huntrix",
 "album": "Golden (Official Lyric Video)", "year": null,
 "tracks": [{"n": 1, "file": "001-Golden _Official Lyric Video.mp3",
             "title": "Golden (Official Lyric Video)", "artist": "Huntrix",
             "duration": 199, "cover": true, "lyrics": false,
             "url": "https://www.youtube.com/watch?v=yebNIHKAC4A"}]}
```

`artist` is the album artist. A track may also have `artists` (everyone on it).
`id` names the release in every command below.

## 2. Find problems

Check every release for:

1. **Video labels in titles or albums**: `(Official Audio)`, `(Official Video)`,
   `(Lyrics)`, `(Lyric Video)`, `[HD]`, `| Channel name`, etc. A single named
   after its video title has the label in `album` too.
2. **Artist spellings**: the same artist written differently across releases
   (`Huntrix` / `HUNTR/X`, `Gabri Ponte` / `Gabry Ponte`), or not as the artist
   writes it. The official spelling is the one YouTube Music uses.
3. **Wrong main artist**: e.g. a remix tagged as the remixer's song, where the
   release is by the original artists with `(Gabry Ponte Remix)` in the title.
4. **Albums**: a song tagged with its own title as album when it belongs to a
   real album or single you can confirm; the same album spelled differently.
5. **Duplicates**: the same recording twice (same artist and title after the
   fixes above, durations within a few seconds).
6. **Missing cover** (`"cover": false`) or a cover that is a video frame
   (the source was a music or lyric video, not an "Artist - Topic" upload).
7. **Missing year** where the release year is known.
8. **Songwriters listed as artists**: some labels put the writers in the
   artist credits, so a track's `artists` can hold people who do not perform
   on it, e.g. `["Bad Bunny", "Benito A. Martinez Ocasio", "Scott Dittrich", ...]`
   or `["Thegiornalisti", "Tommaso Paradiso", "Dario Faini"]`. Suspect it when a
   track has more than 4 names, when a name is the main artist's legal name or
   a member of the band that is the main artist, or when a name is a known
   songwriter or producer. Lookups return the same list, so they do not settle it.

To confirm the real names, album, year and a clean cover source, look the
song up. Write `REPO/requests/lookup-<id>.json` with
`{"search": ["artist title", ...]}` (up to 10) and run:

```
REPO/scripts/butler.sh lookup <id>
```

Results with `"source": "youtube_music"` and `"names": "release"` carry the
release's own `song` names (`artist`, `title`, `album`, `year`, `artists`).
Trust those over your memory.

## 3. Fix what is certain

Fix a problem yourself only when a lookup result or the library itself makes
the right value certain. Everything else goes in the report.

**Tags and covers**: write `REPO/requests/retag-<id>.json` and run
`REPO/scripts/butler.sh retag <id>`:

```json
{"changes": [
  {"release": "26849cee7e3214c10970dfd0bb892154", "artist": "HUNTR/X",
   "artists": ["HUNTR/X", "EJAE", "AUDREY NUNA", "REI AMI", "KPop Demon Hunters Cast"],
   "title": "Golden", "album": "Golden", "year": 2025,
   "cover_from": "https://www.youtube.com/watch?v=9_bTl2vvYQg"}
]}
```

- `release` is required, plus at least one field to change.
- `track` (1-based `n`) limits a change to one track. `title` needs `track`
  unless the release has one track. Without `track`, `artist`, `artists`,
  `album` and `year` apply to every track of the release.
- `artist` sets the album artist and replaces the old main artist in the track
  artist, keeping featured artists. `artists` sets everyone on the track.
- `year` may be `null` to remove it.
- `cover_from`: a YouTube link whose thumbnail becomes the cover (use an
  `"Artist - Topic"` / YouTube Music result: its thumbnail is the release
  cover). Or `artwork_url`: a direct image on an allowed host. Not both.
- The audio is never re-downloaded. Synced lyrics are unchanged.

**Duplicates**: keep the better copy (Topic/official source, has lyrics, has a
real cover, correct tags). Move the other to the library trash: write
`REPO/requests/remove-<id>.json` and run `REPO/scripts/butler.sh remove <id>`:

```json
{"releases": ["6ecde713cdaa50265160dc35a1e61e7f"]}
```

Only remove a release that is really a second copy of a song that stays in the
library. Never remove a release for any other reason. Nothing is deleted: the
release goes to the trash folder, from which the user can restore it.

**Songwriters in `artists`**: set `artists` to the performers only, with the
main artist first, e.g. `{"release": "...", "artists": ["Thegiornalisti"]}`.
Keep everyone who sings, raps or plays on the track or appears in the title
as `feat.`, `with`, `&` or `x`, and every name you are unsure about. Remove a
name only when you know who that person is and that they wrote or produced the
song rather than performed it. If no name is certain, report the track instead.

**A bad source**: when the audio itself is wrong (a live, sped-up or
music-video version the user did not ask for, or a different song), do not fix
it. Report it with the better YouTube Music link, so the user can replace it.

Each command ends with a `"event": "summary"` line: `changed` / `removed`,
`failed` (with `reason`) and `not_attempted`. `busy_retry_later` means an
import is running: wait a minute and retry the same file. `unknown_release`
means a wrong id.

## 4. Report

Keep it short, grouped by artist:
- Fixed: what changed (old -> new).
- Removed duplicates: what, and which copy stayed.
- Needs the user: problems you did not fix, with your suggestion and link.
If nothing needed fixing, say the library is tidy in one line.

## Never

- Run `docker`, `restic`, `yt-dlp` or Python yourself, or any command but `butler.sh`.
- Add pipes, redirections or extra flags to `butler.sh`.
- Add music (`butler.sh add`/`import`) during an audit.
- Edit files under `storage/`, `.env`, `compose.yaml` or scripts.
- Print secrets.
