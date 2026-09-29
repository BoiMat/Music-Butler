---
name: music-butler
description: Add a song or album to the user's self-hosted Navidrome music library (Music Butler Stack), report the import result, check library status, or make an encrypted backup to a USB drive. Use when the user asks to download, import or add music to their library, asks what is in it, or asks for a music backup.
---

# Music Butler

You add music to the user's private library. Work is done in fixed steps:
find the source → write a request file → validate → import → report.
You never run free-text commands; the only command script is `scripts/butler.sh`
(run from the repo root, called `REPO` below).

## Step 1 — Find the source

1. Identify one exact recording: artist, title, and the version the user
   meant. If they said an album, use that album.
2. Search the allowed hosts for a direct file or page URL. Allowed hosts
   (no others): `archive.org`, `upload.wikimedia.org`, `www.youtube.com`,
   `youtube.com`, `youtu.be`.
3. Prefer an existing full recording (audio file on archive.org or
   Wikimedia Commons) over a YouTube video.
4. Compare at least two candidates when available. Never take the first
   search result blindly, and never expand to playlists or whole catalogues.

### Ask the user first only in these cases
- More than one different recording fits and you had to choose between them.
- The best match is a different version: cover, live, remix, edit, sped-up,
  karaoke, instrumental, excerpt, or a different performer.
- For an album, the track list or order does not match the release.
When asking, show the source URL and say what differs. Otherwise, proceed
and describe the choice in your final report.

## Step 2 — Write the request file

If `REPO/requests/` does not exist, create it first.
Generate `<id>` as 32 lowercase hex characters (a random UUID with dashes
removed). Write UTF-8 JSON (with your file tool, not `echo`/heredoc) to:

```
REPO/requests/approved-<id>.json
```

Exact schema — no other keys allowed:

```json
{
  "kind": "song",
  "artist": "Artist Name",
  "album": "Release or single name",
  "format": "mp3",
  "quality": "192",
  "tracks": [
    {
      "title": "Track Title",
      "url": "https://archive.org/download/item/track.mp3",
      "artwork_url": "https://upload.wikimedia.org/..."
    }
  ]
}
```

Rules:
- `kind`: `"song"` (exactly 1 track) or `"album"` (1–100 tracks, in order).
- `format`: `"mp3"` (default) or `"flac"`. MP3 `quality`: `"128"`, `"192"`
  (default), `"256"`, `"320"`. FLAC `quality`: `"0"`–`"8"` (default `"5"`).
- Every `url` must be HTTPS and on an allowed host, ≤ 2048 characters.
- `artwork_url` is optional; include it only if the source really provides
  the image for that release. It must be on an allowed host too.
- Text fields ≤ 300 characters, no control characters.
- If a host is missing from the allowlist, tell the user; do not use the host.

## Step 3 — Validate (offline, no download)

```
REPO/scripts/butler.sh validate <id>
```

- Output contains `{"event": "valid", ...}` → proceed to Step 4.
- Exit code 65 (invalid) → fix only the request JSON (titles, URLs, keys)
  and validate again. Never edit scripts, `.env`, or `compose.yaml`.
- Exit code 64/66 → wrong id or missing file; check the filename.

## Step 4 — Import (download, encode, tag, publish)

```
REPO/scripts/butler.sh import <id>
```

- It may take minutes. Wait for it to exit. Do not start a second import.
- Exit code 75 = another import is already running; wait, then retry.
- The output is JSON lines. `started` and `track_staged` are progress, NOT
  success. Success means: exit code 0 AND a `"event": "complete"` line.

Exit codes: 65 invalid request · 69 download or tool failure (nothing was
imported) · 73 storage problem · 75 import already running · 70 internal
error · 64/66 wrong id or missing file · 130/143 interrupted.

## Step 5 — Report truthfully

- Say "added" only on success. Report: track count, format, and each
  track's `lyrics` and `artwork` status from the output.
- `scan` field: `requested` → "added; Navidrome is indexing it now".
  `scheduled` → "added; it will appear within about a minute".
  `scheduled_after_scan_error` → "added; immediate scan failed, the scheduled
  scan will pick it up; the operator may want to check scanning".
- `lyrics` not `synced` → say so plainly (`unavailable`, `instrumental`,
  `disabled`, `rate_limited`, `lookup_failed`). Never write lyrics yourself.
- Missing artwork → say so; never substitute another image.
- If you see `published_reconcile_before_retry`, files may already be in the
  library: say so and do not retry blindly. Re-importing creates a second copy.
- The import does not prove it plays on the phone: suggest refreshing the app
  (e.g. Tempo).

## Other commands

```
REPO/scripts/butler.sh status    # Navidrome health + releases in the library
REPO/scripts/butler.sh drives    # external drives currently mounted
REPO/scripts/butler.sh backup <drive>   # encrypted backup to that drive
```

Backup flow: run `drives`; if empty, ask the user to plug in the USB drive;
if several, ask which one. Run `backup <drive>`; the first time it creates the
repository. Report success only if the output ends with `Restore test passed`.
The encryption password lives in the operator's keychain or password file —
never print, request or store it.

## Never

Run `docker`, `restic`, `yt-dlp` or Python directly; use pipes, redirections,
substitutions or extra flags in `butler.sh` calls; print secrets; edit `.env`,
`compose.yaml`, scripts or anything under `storage/`; delete audio; change
permissions; restart services; install packages. On failure, report the
request id and the sanitized `reason` from the output.
