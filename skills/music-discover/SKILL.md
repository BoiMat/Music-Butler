---
name: music-discover
description: Grow the user's self-hosted Navidrome music library (Music Butler Stack) on a schedule. Import songs queued from their playlists, discover new songs they may like from their taste seeds and listening, and remove songs they rated 1 star. Use for a scheduled or requested "bootstrap" or "weekly discovery" run, not for songs the user names.
---

# Music Discover

You choose new music for the user's private library, like a weekly discovery
playlist. `REPO` is the repository folder. The only command you run is
`REPO/scripts/butler.sh`. The task says the mode:
- **bootstrap**: fill the library. Add up to **25** songs.
- **weekly**: add **15** songs the user has never heard.

## 1. Read what to choose from

```
REPO/scripts/butler.sh discover
```

It downloads nothing. The last line is `{"event": "discover", ...}`:

- `library_songs`: how many songs the library has.
- `taste`: the user's own words. `notes` is free text, e.g. "70s Italian pop, no
  trap". `artists` are artists the user likes, some with a `note`, e.g.
  `{"name": "Mina", "note": "older songs"}`. `blocked_artists`: never add these.
- `queue`: songs from the user's own playlists. The user already likes them.
  Each has `artist`, `title`, `search` (the text to look up), and often `album`,
  `year` and `duration` (seconds).
- `candidates`: songs found in the "radio" of songs the user loves, best first.
  Each has `artist`, `title`, `url`, `duration`, `score`, `because` (the loved
  songs that led here) and `new_artist` (`true` = no song by this artist in the
  library yet). The names can be guessed from a video title and can be wrong
  or swapped (`"I Follow Rivers - Lykke Li"`). Step 3 finds the real names.
- `stale`: releases to remove: the songs the user rated 1 star. Nothing else
  is ever removed. Each has `id`, `artist`, `titles`.
- `listening`: `unavailable` means play counts could not be read (then `stale`
  is empty).

## 2. Remove 1-star songs

If `stale` is not empty, write `REPO/requests/remove-<id>.json` with their ids
and run `REPO/scripts/butler.sh remove <id>`:

```json
{"releases": ["6ecde713cdaa50265160dc35a1e61e7f"]}
```

Only remove ids listed in `stale`. Nothing is deleted: they go to the library
trash, and discovery never offers them again. In the report, list them as
"removed, rated 1 star".

## 3. Choose the songs

Choose in this order until you have the mode's number of songs:

1. **bootstrap only: `queue`**, in list order. Each is origin `playlist`.
2. **bootstrap only: artist seeds.** For an artist in `taste.artists` with few
   or no songs in the library, choose 2 or 3 of their best-known songs that fit
   the artist's `note` (e.g. "older songs" = their classic era). You may use your
   own music knowledge here; the lookup in Step 4 confirms each song. Origin `seed`.
3. **`candidates`**, origin `discovery`. Go down the list and choose songs that
   fit `taste.notes` and what the library already holds. In weekly mode, at
   least 5 of the 15 must have `"new_artist": true`, and at most 2 songs per
   artist. Skip:
   - anything by a `blocked_artists` artist or against `taste.notes`;
   - soundtrack clips, trailers, fan uploads, covers, remixes of a song the
     library already has, and anything that is not a real song release;
   - the same song twice.

## 4. Look the songs up, pick one result each, add them

This is the music-butler flow. Its rules apply here too.

1. Lookup: write `REPO/requests/lookup-<id>.json` with at most 10 searches per
   file (`artist title`; for queue songs, their `search`), then run
   `REPO/scripts/butler.sh lookup <id>`. For more than 10 songs, use several
   files with different ids.
2. For each song, pick one result. Prefer a `"source": "youtube_music"` result
   whose `song` is that song; else an `"Artist - Topic"` result
   (`"names": "release"`); else "Official Audio". For a queue song with a
   `duration`, the result must be within about 5 seconds of it. Never pick a
   live, sped up, slowed, nightcore, karaoke, cover or instrumental version.
   If no result is clearly the song, drop it. Do not ask the user: this run
   is unattended.
3. Write ONE `REPO/requests/approved-<id>.json` for all chosen songs. Copy each
   result's `url` and `song` (`artist`, `title`, `album`, `year`, `artists`)
   exactly, and give each song its `origin`:

```json
{"songs": [
  {"artist": "Lykke Li", "title": "I Follow Rivers", "album": "Wounded Rhymes", "year": 2011,
   "url": "https://www.youtube.com/watch?v=...", "origin": "discovery"},
  {"artist": "Mina", "title": "Grande grande grande", "url": "https://www.youtube.com/watch?v=...",
   "origin": "seed"}
]}
```

   Song keys: `artist`, `title`, `url`, `album`, `artists`, `year`, `origin`.
   `origin` is `playlist`, `seed` or `discovery`.
4. Run `REPO/scripts/butler.sh add <id>` once and wait until it exits (it can
   take many minutes). The last line is `"event": "summary"`: `added`,
   `failed` (with `reason`), `not_attempted`, and `skipped` (songs already in
   the library, which is fine). On `busy_retry_later`, wait a minute and run
   the same `add` again. Never add a song listed under `added` again.

## 5. Report

Keep it short. It is sent to the user's phone:
- Added: `Artist - Title`, and for discoveries `(because you like <because[0]>)`.
  Group by origin: from your playlists / from your artists / new discoveries.
- Removed: songs rated 1 star, moved to the trash.
- Failed songs in one line, with their `reason`.
- Bootstrap: how many songs the library has now (`library_songs` + added).
- Ending: "Like songs in Tempo to get more like them; 1 star removes a song.
  New songs are in the *Discover Weekly* playlist."

If nothing could be added, say why in one line.

## Never

- Run `docker`, `restic`, `yt-dlp` or Python yourself, or any command but `butler.sh`.
- Add pipes, redirections or extra flags to `butler.sh`.
- Remove releases that are not in `stale`. Retag during a discovery run.
- Edit files under `storage/`, `.env`, `compose.yaml` or scripts.
- Make up URLs or names. Every `url` and `song` comes from a lookup you ran.
- Print secrets.
