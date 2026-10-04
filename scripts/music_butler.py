#!/usr/bin/env python3
"""Constrained, transactional audio ingestion. No shell evaluation."""
import argparse
import contextlib
import datetime
import difflib
import email.utils
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
MAX_REQUEST = 262144
MAX_RESPONSE = 1048576
DEFAULT_HOSTS = ("archive.org,upload.wikimedia.org,www.youtube.com,youtube.com,youtu.be,"
                 "m.youtube.com,music.youtube.com")
YOUTUBE_HOSTS = {"www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}
MAX_SONGS = 100
SONG_KEYS = {"artist", "title", "url", "album", "artists", "year", "artwork_url", "origin"}
# Why a release was added: asked for, from a playlist export, from a taste seed, or discovered.
ORIGINS = ("request", "playlist", "seed", "discovery")
# Batch imports continue past these per-release failures; anything else stops the batch.
CONTINUE_CODES = {65, 69}


class Failure(Exception):
    def __init__(self, reason, code=65, item=None, detail=None):
        super().__init__(reason)
        self.code = code
        self.item = item  # 1-based song number in a "songs" request
        self.detail = detail  # redacted tool error line, e.g. "ERROR: [youtube] ...: Sign in to confirm ..."


def emit(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def text(value, name, limit=300):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Failure("invalid_" + name)
    if any(unicodedata.category(c).startswith("C") for c in value):
        raise Failure("control_character_" + name)
    return unicodedata.normalize("NFC", value.strip())


def url(value, hosts, resolve=False):
    value = text(value, "url", 2048)
    p = urllib.parse.urlsplit(value)
    if (p.scheme != "https" or p.hostname not in hosts or p.username or p.password
            or p.port not in (None, 443) or p.fragment or "|" in value):
        raise Failure("source_url_not_allowed")
    if any(k.lower() in {"token", "key", "password", "auth", "signature", "cookie"}
           for k, _ in urllib.parse.parse_qsl(p.query)):
        raise Failure("credential_like_source_url")
    if resolve:
        try:
            addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
                raise Failure("non_public_source_address")
        except OSError as exc:
            raise Failure("source_dns_failed", 69) from exc
    return value


def allowed_hosts():
    # Tolerate "a.org, b.org" in .env; empty entries never match a real hostname.
    return {h.strip().lower() for h in os.environ.get("DOWNLOAD_ALLOWED_HOSTS", DEFAULT_HOSTS).split(",") if h.strip()}


def encoding(data):
    fmt = data.get("format", "mp3")
    if fmt not in ("mp3", "flac"):
        raise Failure("invalid_format")
    quality = data.get("quality", "192" if fmt == "mp3" else "5")
    if quality not in (("128", "192", "256", "320") if fmt == "mp3" else tuple(map(str, range(9)))):
        raise Failure("invalid_quality")
    return fmt, quality


def validate(data, resolve=False):
    """One release (the original contract): kind/artist/album/tracks."""
    if not isinstance(data, dict) or set(data) - {"kind", "artist", "album", "format", "quality", "tracks"}:
        raise Failure("invalid_request_keys")
    if data.get("kind") not in ("song", "album"):
        raise Failure("invalid_kind")
    out = {k: text(data.get(k), k) for k in ("artist", "album")}
    out["kind"] = data["kind"]
    out["format"], out["quality"] = encoding(data)
    tracks = data.get("tracks")
    if not isinstance(tracks, list) or not 1 <= len(tracks) <= 100 or (out["kind"] == "song" and len(tracks) != 1):
        raise Failure("invalid_track_count")
    hosts = allowed_hosts()
    out["tracks"] = []
    seen = set()
    for track in tracks:
        if not isinstance(track, dict) or set(track) - {"title", "url", "artwork_url"}:
            raise Failure("invalid_track_keys")
        t = {"title": text(track.get("title"), "title"),
             "url": url(track.get("url"), hosts, resolve)}
        if t["url"] in seen:
            raise Failure("duplicate_source_url")
        seen.add(t["url"])
        if "artwork_url" in track:
            t["artwork_url"] = url(track["artwork_url"], hosts, resolve)
        out["tracks"].append(t)
    return out


def blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def artist_list(value):
    """All performers, main artist first: ["HUNTR/X", "EJAE", ...]."""
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise Failure("invalid_artists")
    return [text(v, "artists") for v in value]


def release_year(value):
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1000 <= value <= 2100:
        raise Failure("invalid_year")
    return value


def display_artist(artists):
    return artists[0] if len(artists) == 1 else ", ".join(artists[:-1]) + " & " + artists[-1]


def validate_songs(data, resolve=False):
    """Flat song list, the easy format for agents:
    {"songs": [{artist, title, url, album?, artists?, year?, artwork_url?}]}.

    Songs that share artist and album become one album release, in list order.
    A song without an album is its own single, named after the song.
    """
    if set(data) - {"songs", "format", "quality", "origin"}:
        raise Failure("invalid_request_keys")
    fmt, quality = encoding(data)
    default_origin = origin(data.get("origin", "request"))
    songs = data.get("songs")
    if not isinstance(songs, list) or not 1 <= len(songs) <= MAX_SONGS:
        raise Failure("invalid_song_count")
    hosts = allowed_hosts()
    releases, seen = {}, set()
    for n, song in enumerate(songs, 1):
        try:
            if not isinstance(song, dict) or set(song) - SONG_KEYS:
                raise Failure("invalid_song_keys")
            artist = text(song.get("artist"), "artist")
            title = text(song.get("title"), "title")
            album = None if blank(song.get("album")) else text(song.get("album"), "album")
            t = {"title": title, "url": url(song.get("url"), hosts, resolve), "song": n}
            if t["url"] in seen:
                raise Failure("duplicate_source_url")
            seen.add(t["url"])
            if not blank(song.get("artwork_url")):
                t["artwork_url"] = url(song["artwork_url"], hosts, resolve)
            if song.get("artists") not in (None, []):
                t["artists"] = artist_list(song["artists"])
            if not blank(song.get("year")):
                t["year"] = release_year(song["year"])
            song_origin = origin(song.get("origin", default_origin))
        except Failure as exc:
            exc.item = n
            raise
        key = ("album", artist.casefold(), album.casefold()) if album else ("single", n)
        release = releases.setdefault(key, {"artist": artist, "album": album or title, "origin": song_origin,
                                            "format": fmt, "quality": quality, "tracks": []})
        release["tracks"].append(t)
    out = list(releases.values())
    for release in out:
        release["kind"] = "song" if len(release["tracks"]) == 1 else "album"
    return out


def origin(value):
    if value not in ORIGINS:
        raise Failure("invalid_origin")
    return value


def parse(data, resolve=False):
    """Any accepted request -> list of validated releases."""
    if isinstance(data, dict) and "songs" in data:
        return validate_songs(data, resolve)
    return [validate(data, resolve)]


def describe(release):
    out = {"artist": release["artist"], "album": release["album"], "kind": release["kind"],
           "titles": [t["title"] for t in release["tracks"]]}
    if release.get("notes"):
        out["notes"] = release["notes"]
    return out


def slug(value):
    value = re.sub(r"[^\w -]", "_", value, flags=re.UNICODE).strip(" ._-")
    # Limit bytes as well as characters, including for multibyte filesystems.
    return value.encode("utf-8")[:80].decode("utf-8", "ignore") or "untitled"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure("http_redirect_refused", 69)


class AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    """Artwork only: archive.org/download always redirects to an iaNNN.us.archive.org mirror."""
    max_redirections = 3

    def __init__(self, hosts):
        super().__init__()
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = (urllib.parse.urlsplit(newurl).hostname or "").lower()
        if not (host in self.hosts or ("archive.org" in self.hosts and host.endswith(".archive.org"))):
            raise Failure("http_redirect_refused", 69)
        # Same HTTPS/port/credential/public-address policy as the approved URL.
        url(newurl, {host}, resolve=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(address, *, data=None, headers=None, limit=MAX_RESPONSE, redirect_hosts=None):
    req = urllib.request.Request(address, data=data, headers=headers or {})
    # Never leak scan credentials through ambient proxies or redirects.
    redirects = AllowlistRedirect(redirect_hosts) if redirect_hosts else NoRedirect()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), redirects)
    with opener.open(req, timeout=20) as response:
        body = response.read(limit + 1)
        if len(body) > limit:
            raise Failure("response_too_large", 69)
        return body


def tool_error(log):
    """The tool's last ERROR line with URLs removed (they can be signed), or None."""
    log.seek(0)
    lines = [line for line in log.read(MAX_RESPONSE).decode("utf-8", "replace").splitlines()
             if line.startswith("ERROR:") or line.startswith("[error]")]
    if not lines:
        return None
    return re.sub(r"\S*://\S+", "<url>", lines[-1])[:300]


def run(argv, cwd, env, timeout=900, stdout=None):
    # Child process group guarantees timeout/cancellation also kills ffmpeg.
    # Provider output may contain expiring signed URLs: only a redacted ERROR line leaves.
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=stdout or log, stderr=log, start_new_session=True)
        try:
            if proc.wait(timeout=timeout) != 0:
                raise Failure("media_tool_failed", 69, detail=tool_error(log))
        except subprocess.TimeoutExpired as exc:
            raise Failure("media_tool_timeout", 69) from exc
        finally:
            # A provider can leave descendants even after the parent exits.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def capture(argv, cwd, env, timeout=120, limit=8 * MAX_RESPONSE):
    """run() that returns the child's stdout as parsed JSON lines (yt-dlp --dump-json).

    Callers must copy out only known fields: raw metadata can hold signed media URLs.
    """
    with tempfile.TemporaryFile() as out:
        run(argv, cwd, env, timeout=timeout, stdout=out)
        out.seek(0)
        data = out.read(limit + 1)
    if len(data) > limit:
        raise Failure("response_too_large", 69)
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def ytdlp_base():
    # No user config/plugins/cache/geo tricks; Node for YouTube's JS challenges.
    return ["yt-dlp", "--ignore-config", "--no-plugin-dirs", "--no-progress", "--no-warnings",
            "--no-cache-dir", "--no-geo-bypass", "--js-runtimes", "node", "--socket-timeout", "30",
            "--retries", "2"]


def encode(source, dest, request, cwd, env):
    args = ["ffmpeg", "-nostdin", "-v", "error", "-n", "-protocol_whitelist", "file,pipe",
            "-f", "flac", "-i", str(source),
            "-map", "0:a:0", "-vn", "-map_metadata", "-1"]
    if request["format"] == "mp3":
        args += ["-c:a", "libmp3lame", "-b:a", request["quality"] + "k"]
    else:
        args += ["-c:a", "flac", "-compression_level", request["quality"]]
    run(args + [str(dest)], cwd, env)


def download_agent():
    return text(os.environ.get("DOWNLOAD_USER_AGENT") or f"music-butler/{VERSION}", "download_user_agent")


DOWNLOAD_RETRY_PAUSE = 20  # seconds; YouTube refuses some downloads during a burst of imports


def acquire(track, request, work, env):
    try:
        return download(track, work, env)
    except Failure as exc:
        if str(exc) != "media_tool_failed":
            raise
        emit("warning", reason="download_retry", title=track["title"], detail=exc.detail)
        time.sleep(DOWNLOAD_RETRY_PAUSE)
        for leftover in work.iterdir():
            if leftover.is_file() and not leftover.is_symlink():
                leftover.unlink()
        return download(track, work, env)


def download(track, work, env):
    source = work / "source.flac"
    # Archives such as Wikimedia reject bots posing as browsers; YouTube needs yt-dlp's default.
    agent = []
    if urllib.parse.urlsplit(track["url"]).hostname not in YOUTUBE_HOSTS:
        agent = ["--user-agent", download_agent()]
    run([*ytdlp_base(), "--no-playlist", "--fragment-retries", "2", "--max-filesize", "500M",
         "--match-filter", "!is_live & duration <? 3601", "-f", "bestaudio/best", "-x", "--audio-format", "flac", *agent,
         # The source's own thumbnail is the cover fallback (source_thumbnail); a failed
         # thumbnail download is only a yt-dlp warning. No conversion step that could fail.
         "--write-thumbnail",
         "-o", str(work / "source.%(ext)s"), "-o", "thumbnail:" + str(work / "thumb.%(ext)s"),
         "--", track["url"]], work, env)
    if not source.is_file() or source.is_symlink():
        raise Failure("no_audio_output", 69)
    return source, "yt-dlp"


MAX_LOOKUPS = 10
MUSIC_RESULTS = 3  # YouTube Music "songs": studio audio with the release's own names
SEARCH_RESULTS = 3  # plain YouTube videos, the fallback
ENRICH_PER_SEARCH = 2
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Bracketed parts naming the upload, not the recording: "(Official Audio)", "[HD]".
LABEL = re.compile(r"\b(?:official|lyrics?|audio|video|visuali[sz]er|mv|m/v|hd|hq|4k|8k|videoclip|clip)\b", re.I)
# ...unless they also name a different recording: "(Live Video)" stays "(Live Video)".
RECORDING = re.compile(r"\b(?:live|remix|mix|edit|acoustic|version|cover|instrumental|demo)\b", re.I)
TRAILING_LABEL = re.compile(r"\s+(?:official\s+)?(?:lyrics?\s+video|music\s+video|lyrics?|video|audio|visuali[sz]er)\s*$", re.I)
BRACKETED_FEAT = re.compile(r"\s*[(\[]\s*(?:feat|ft|featuring)\.?\s+([^)\]]+?)\s*[)\]]", re.I)
BARE_FEAT = re.compile(r"\s+(?:feat|ft|featuring)\.?\s+(.+?)(?=\s+[-–—]\s+|\s*[(\[|]|$)", re.I)
DASHED_VERSION = re.compile(r"\s+[-–—]\s+([^-–—()]*\b(?:remix|mix|edit|version|live|acoustic|remaster(?:ed)?)\b[^-–—()]*)$", re.I)
QUOTED = re.compile(r"[\"“”'‘’«»](.+)[\"“”'‘’«»]")


def is_label(inner):
    """True for "(Official Video)" or "(Official Video Remastered)", not "(Live)" or "(Remastered 2011)"."""
    if not LABEL.search(inner) or RECORDING.search(inner):
        return False
    return not re.search(r"\bremaster(?:ed)?\b", inner, re.I) or bool(re.search(r"\bvideo\b", inner, re.I))


def guess_song(video_title):
    """Best-effort artist/title from a video title such as "Artist - Song (Official Video) ft. X".

    Only a suggestion: YouTube Music results carry the release's real names instead.
    """
    value = (video_title or "").split(" | ")[0].strip()
    featured = [m.strip() for m in BRACKETED_FEAT.findall(value) + BARE_FEAT.findall(value)]
    value = BARE_FEAT.sub("", BRACKETED_FEAT.sub("", value))
    value = re.sub(r"\s*[(\[【]([^()\[\]【】]*)[)\]】]", lambda m: "" if is_label(m.group(1)) else m.group(0), value)
    artist, title = None, value
    parts = re.split(r"\s+[-–—]\s+", value, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        artist, title = parts[0].strip(), parts[1].strip()
    title = DASHED_VERSION.sub(r" (\1)", title)
    previous = None
    while previous != title:
        previous, title = title, TRAILING_LABEL.sub("", title).strip()
    quoted = QUOTED.fullmatch(title)
    title = (quoted.group(1) if quoted else title).strip()
    if featured:
        title += " (feat. " + " & ".join(featured) + ")"
    song = {"title": clip(title)} if clip(title) else {}
    if artist and clip(artist):
        song = {"artist": clip(artist), **song}
    return song or None


def release_song(entry):
    """The release's own names from a YouTube Music / "Artist - Topic" upload, or None."""
    if not isinstance(entry, dict):
        return None
    artists = entry.get("artists") if isinstance(entry.get("artists"), list) else [entry.get("artist")]
    artists = [a for a in (clip(a) for a in artists[:20]) if a]
    title = clip(entry.get("track"))
    if not artists or not title:
        return None
    song = {"artist": artists[0], "title": title}
    if len(artists) > 1:
        song["artists"] = artists
    if clip(entry.get("album")):
        song["album"] = clip(entry.get("album"))
    year = entry.get("release_year")
    if not (isinstance(year, int) and not isinstance(year, bool)):
        year = description_year(entry.get("description"))
    if isinstance(year, int) and not isinstance(year, bool) and 1000 <= year <= 2100:
        song["year"] = year
    return song


def description_year(description):
    """The release year from a YouTube Music auto-generated description, when yt-dlp
    leaves release_year empty: "Released on: 2021-06-25", else the release's
    "℗ 2021" line (the year of that release, e.g. a remaster's)."""
    if not isinstance(description, str):
        return None
    found = (re.search(r"^Released on: (\d{4})-\d{2}-\d{2}", description, re.M)
             or re.search(r"℗ (\d{4})\b", description))
    return int(found.group(1)) if found else None


def validate_lookup(data, resolve=False):
    """{"search": ["artist title", ...], "urls": [...]} -> normalized lookup request."""
    if not isinstance(data, dict) or set(data) - {"search", "urls"}:
        raise Failure("invalid_lookup_keys")
    searches, urls = data.get("search", []), data.get("urls", [])
    searches = [searches] if isinstance(searches, str) else searches
    urls = [urls] if isinstance(urls, str) else urls
    if (not isinstance(searches, list) or not isinstance(urls, list)
            or not 1 <= len(searches) + len(urls) <= MAX_LOOKUPS):
        raise Failure("invalid_lookup_count")
    hosts = allowed_hosts()
    # Search results are returned as www.youtube.com links, which must be importable.
    if searches and "www.youtube.com" not in hosts:
        raise Failure("youtube_search_not_allowed")
    out = {"search": [], "urls": []}
    for n, item in enumerate([*searches, *urls], 1):
        try:
            if n <= len(searches):
                out["search"].append(text(item, "search", 200))
            else:
                out["urls"].append(url(item, hosts, resolve))
        except Failure as exc:
            exc.item = n
            raise
    return out


def clip(value, limit=200):
    if not isinstance(value, str):
        return None
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C")).strip()
    return value[:limit] or None


def video_summary(entry, given_url=None):
    """Only known, harmless fields: raw yt-dlp metadata can contain signed media URLs."""
    if not isinstance(entry, dict) or entry.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        return None
    duration = entry.get("duration")
    duration = round(duration) if isinstance(duration, (int, float)) and math.isfinite(duration) else None
    if duration is not None and not 1 <= duration <= 3600:
        return None  # the importer would refuse it anyway
    vid = entry.get("id")
    if isinstance(vid, str) and YOUTUBE_ID.match(vid) and (entry.get("ie_key") or entry.get("extractor_key") or "Youtube") == "Youtube":
        link = f"https://www.youtube.com/watch?v={vid}"
    elif given_url:
        link = given_url
    else:
        return None
    summary = {"title": clip(entry.get("title")), "channel": clip(entry.get("channel") or entry.get("uploader")),
               "duration": duration, "url": link}
    # YouTube Music uploads ("Artist - Topic") carry the release's own names;
    # anything else gets names guessed from the video title.
    song = release_song(entry)
    summary["names"] = "release" if song else "guessed"
    song = song or guess_song(summary["title"])
    if song:
        summary["song"] = song
    return summary


def lookup(query):
    """Search YouTube and/or read metadata for URLs. Nothing is downloaded or written."""
    with tempfile.TemporaryDirectory(prefix="lookup-") as tmp:
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "XDG_CONFIG_HOME": tmp,
               "XDG_CACHE_HOME": tmp, "TMPDIR": tmp, "LANG": "C.UTF-8"}
        full = [*ytdlp_base(), "--no-playlist", "--dump-json", "--ignore-no-formats-error"]

        def details(link):
            entries = capture([*full, "--", link], tmp, env)
            return entries[0] if entries else {}

        def guarded(fn, failure):
            try:
                return fn()
            except (Failure, OSError, ValueError) as exc:
                if isinstance(exc, Failure) and exc.code in (130, 143):
                    raise
                return {"error": failure}

        def enrich(r):
            # Full metadata (artists, album, year, duration) needs one request per result.
            extra = guarded(lambda: video_summary(details(r["url"])) or {}, "details_failed")
            if extra.get("names") == "release":
                r.update({k: extra[k] for k in ("title", "channel", "duration", "names", "song") if extra.get(k)})
            elif r.get("duration") is None and extra.get("duration"):
                r["duration"] = extra["duration"]
            return r

        def search(q):
            # YouTube Music "songs" first: studio audio with the release's artist/title/album.
            music = guarded(lambda: capture(
                [*ytdlp_base(), "--flat-playlist", "--dump-json", "--playlist-items", f"1:{MUSIC_RESULTS}", "--",
                 "https://music.youtube.com/search?q=" + urllib.parse.quote(q) + "#songs"], tmp, env), "search_failed")
            music = [enrich(r) for r in map(video_summary, music if isinstance(music, list) else []) if r]
            music = [r for r in music if r["names"] == "release"]
            videos = capture([*ytdlp_base(), "--flat-playlist", "--dump-json", "--",
                              f"ytsearch{SEARCH_RESULTS}:{q}"], tmp, env)
            seen = {r["url"] for r in music}
            videos = [r for r in map(video_summary, videos) if r and r["url"] not in seen]
            topic = [r for r in videos if (r["channel"] or "").endswith(" - Topic")][:ENRICH_PER_SEARCH]
            for r in topic:
                enrich(r)
            for r in music:
                r["source"] = "youtube_music"
            return {"query": q, "results": music + videos}

        def one_url(link):
            return video_summary(details(link), given_url=link) or {"url": link, "error": "not_importable"}

        searches = [{"query": q, **guarded(lambda: search(q), "search_failed")} for q in query["search"]]
        urls = [{"url": u, **guarded(lambda: one_url(u), "lookup_failed")} for u in query["urls"]]
    emit("lookup", search=searches, urls=urls)


ID3_FRAMES = {"title": "TIT2", "artist": "TPE1", "albumartist": "TPE2", "album": "TALB",
              "year": "TDRC", "tracknumber": "TRCK"}
VORBIS_KEYS = {"title": "title", "artist": "artist", "albumartist": "albumartist", "album": "album",
               "year": "date", "artists": "artists", "comment": "comment"}


def write_tags(path, fields, cover=None, fresh=False):
    """Set the given tags (None removes one); keep the rest unless fresh.

    fields: title, artist (display text), artists (list, multi-valued ARTISTS tag),
    albumartist, album, year, tracknumber ((n, total)), comment. ID3v2.4 keeps multi-valued
    frames separate; v2.3 would join them with "/", which splits "HUNTR/X".
    """
    from mutagen import id3
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC, Picture
    if path.suffix == ".mp3":
        audio = MP3(path)
        tags = id3.ID3() if fresh or audio.tags is None else audio.tags
        for key, value in fields.items():
            if key == "comment":
                for frame in [k for k in tags.keys() if k.startswith("COMM")]:
                    del tags[frame]
                if value is not None:
                    tags.add(id3.COMM(encoding=3, lang="eng", desc="", text=[value]))
                continue
            frame = "TXXX:ARTISTS" if key == "artists" else ID3_FRAMES[key]
            tags.delall(frame)
            if value is None:
                continue
            if key == "artists":
                tags.add(id3.TXXX(encoding=3, desc="ARTISTS", text=list(value)))
            else:
                value = "%d/%d" % value if key == "tracknumber" else str(value)
                tags.add(getattr(id3, frame)(encoding=3, text=[value]))
        if cover:
            tags.delall("APIC")
            tags.add(id3.APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        tags.save(path, v2_version=4)
    else:
        audio = FLAC(path)
        if fresh:
            audio.clear()
            audio.clear_pictures()
        for key, value in fields.items():
            names = ("tracknumber", "tracktotal") if key == "tracknumber" else (VORBIS_KEYS[key],)
            for name in names:
                audio.pop(name, None)
            if value is None:
                continue
            if key == "tracknumber":
                audio["tracknumber"], audio["tracktotal"] = str(value[0]), str(value[1])
            else:
                audio[names[0]] = list(value) if key == "artists" else [str(value)]
        if cover:
            audio.clear_pictures()
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", cover
            audio.add_picture(pic)
        audio.save()
    return audio.info.length


def read_tags(path):
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    if path.suffix == ".mp3":
        audio = MP3(path)
        tags = audio.tags or {}

        def first(frame):
            return str(tags[frame].text[0]) if frame in tags and tags[frame].text else None
        out = {k: first(f) for k, f in ID3_FRAMES.items() if k != "tracknumber"}
        artists = tags.get("TXXX:ARTISTS")
        out["artists"] = [str(a) for a in artists.text] if artists else None
        comments = [f for k, f in tags.items() if k.startswith("COMM") and f.text]
        out["comment"] = str(comments[0].text[0]) if comments else None
        out["cover"] = bool(tags and tags.getall("APIC"))
    else:
        audio = FLAC(path)
        out = {k: (audio.get(v) or [None])[0] for k, v in VORBIS_KEYS.items() if k != "artists"}
        out["artists"] = list(audio.get("artists", [])) or None
        out["cover"] = bool(audio.pictures)
    out["duration"] = round(audio.info.length)
    return out


def origin_comment(value):
    """Navidrome smart playlists find discovered songs by this comment tag."""
    return None if value == "request" else "music-butler:" + value


def tag_audio(path, request, track, number, total, cover):
    artists = track.get("artists") or [request["artist"]]
    fields = {"title": track["title"], "artist": display_artist(artists),
              "artists": artists if len(artists) > 1 else None, "albumartist": request["artist"],
              "album": request["album"], "year": track.get("year"), "tracknumber": (number, total),
              "comment": origin_comment(request.get("origin", "request"))}
    duration = write_tags(path, fields, cover, fresh=True)
    if not math.isfinite(duration) or not 1 <= duration <= 3600:
        raise Failure("unsupported_audio_duration", 69)
    return duration


def artwork(track, work, env):
    if "artwork_url" not in track:
        return None
    try:
        raw = work / "cover.input"
        raw.write_bytes(fetch(track["artwork_url"], headers={"User-Agent": download_agent()}, limit=5242880,
                              redirect_hosts=allowed_hosts()))
        jpg = work / "cover.jpg"
        run(["ffmpeg", "-nostdin", "-v", "error", "-n", "-protocol_whitelist", "file,pipe",
             "-f", "image2", "-pattern_type", "none", "-i", str(raw),
             "-frames:v", "1", "-vf", "scale=600:600:force_original_aspect_ratio=decrease",
             str(jpg)], work, env)
        return jpg.read_bytes()
    except (Failure, OSError, urllib.error.URLError) as exc:
        if isinstance(exc, Failure) and exc.code in (130, 143):
            raise
        emit("warning", reason="artwork_unavailable")
        return None


def source_thumbnail(work, env):
    """Centre-square crop of the thumbnail yt-dlp saved (e.g. YouTube), or None.

    For "Official Audio"/art-track uploads the centre of the 16:9 thumbnail is the
    release cover; for other videos it is a frame, so the receipt labels it.
    """
    candidates = [p for p in sorted(work.glob("thumb.*"))
                  if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                  and p.is_file() and not p.is_symlink() and p.stat().st_size <= 5242880]
    if not candidates:
        return None
    jpg = work / "thumb-cover.jpg"
    try:
        run(["ffmpeg", "-nostdin", "-v", "error", "-n", "-protocol_whitelist", "file,pipe",
             "-f", "image2", "-pattern_type", "none", "-i", str(candidates[0]), "-frames:v", "1",
             "-vf", "crop=w=min(iw\\,ih):h=min(iw\\,ih),scale=w=min(600\\,iw):h=min(600\\,ih),setsar=1",
             str(jpg)], work, env)
        return jpg.read_bytes()
    except (Failure, OSError) as exc:
        if isinstance(exc, Failure) and exc.code in (130, 143):
            raise
        return None


LYRICS_RETRY_PAUSES = (5, 15)  # seconds before the 2nd and 3rd attempt


class Lyrics:
    def __init__(self, root):
        self.cooldown = root / "lyrics-retry-after"
        self.until = 0
        self.agent = os.environ.get("LRCLIB_USER_AGENT", "")

    def get(self, request, track, duration, dest):
        if not self.agent:
            return "disabled"
        try:
            if self.until > time.time() or (self.cooldown.exists() and float(self.cooldown.read_text()) > time.time()):
                return "rate_limited"
            try:
                body = self.call("get", {"artist_name": request["artist"], "track_name": track["title"],
                                         "album_name": request["album"], "duration": round(duration)})
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                # Album names of singles/uploads are often guesses and /api/get needs them to
                # match. Search by artist+title instead, with the same ±2 s duration rule.
                body = self.search(request["artist"], track["title"], duration)
                if body is None:
                    return "unavailable"
            else:
                if not self.has_synced(body):
                    # The release's own entry may hold only plain lyrics (or a wrong
                    # "instrumental" flag) while another upload of the same song, under
                    # the same strict match, has synced ones.
                    body = self.search(request["artist"], track["title"], duration) or body
            synced = body.get("syncedLyrics")
            if self.has_synced(body):
                lrc = dest.with_suffix(".lrc")
                temp = lrc.with_name("." + lrc.name + ".tmp")
                temp.write_text(synced, encoding="utf-8")
                os.replace(temp, lrc)  # the library is live: never show a half-written file
                return "synced"
            return "instrumental" if body.get("instrumental") else "unavailable"
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                delay = exc.headers.get("Retry-After", "60")
                try:
                    until = time.time() + max(1, int(delay))
                except ValueError:
                    try:
                        until = email.utils.parsedate_to_datetime(delay).timestamp()
                    except (TypeError, ValueError):
                        until = time.time() + 60
                self.until = until
                with contextlib.suppress(OSError):
                    self.cooldown.write_text(str(until))
                return "rate_limited"
            return "unavailable" if exc.code == 404 else "lookup_failed"
        except (OSError, ValueError, TypeError, AttributeError, Failure, urllib.error.URLError) as exc:
            if isinstance(exc, Failure) and exc.code in (130, 143):
                raise
            return "lookup_failed"

    @staticmethod
    def has_synced(body):
        synced = body.get("syncedLyrics")
        return isinstance(synced, str) and re.search(r"\[\d{2,}:\d{2}(?:\.\d+)?\]", synced) is not None

    def call(self, endpoint, params):
        address = f"https://lrclib.net/api/{endpoint}?" + urllib.parse.urlencode(params)
        # Sequential lookups with ≥500 ms spacing. LRCLIB often answers 503 or drops a
        # connection under load; that used to lose a song's lyrics for good, so those
        # are retried after growing pauses. 404/429 are real answers.
        for pause in (0.5, *LYRICS_RETRY_PAUSES):
            time.sleep(pause)
            try:
                return json.loads(fetch(address, headers={"User-Agent": self.agent}))
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or pause == LYRICS_RETRY_PAUSES[-1]:
                    raise
            except OSError:
                if pause == LYRICS_RETRY_PAUSES[-1]:
                    raise

    def search(self, artist, title, duration):
        """Exact title and an artist credit naming the main artist (case-insensitive),
        within ±2 s, that has synced lyrics, else None. LRCLIB often credits every
        performer ("HUNTR/X, EJAE & ...") where the library has the main artist.
        """
        results = self.call("search", {"artist_name": artist, "track_name": title})
        if not isinstance(results, list):
            return None
        for item in results:
            credit = str(item.get("artistName", "")).casefold() if isinstance(item, dict) else ""
            if (isinstance(item, dict) and isinstance(item.get("duration"), (int, float))
                    and abs(item["duration"] - duration) <= 2
                    and (credit == artist.casefold()
                         or artist.casefold() in re.split(r"\s*(?:,|&|\bfeat\.?|\bft\.?|\band\b|/(?=\s))\s*", credit))
                    and str(item.get("trackName", "")).casefold() == title.casefold()
                    and isinstance(item.get("syncedLyrics"), str)):
                return item
        return None


def subsonic(endpoint, params=None, limit=MAX_RESPONSE, account="NAVIDROME"):
    """One authenticated Subsonic API call to Navidrome: the "subsonic-response" dict,
    or None when that account is not configured. account "NAVIDROME" is the admin
    (scans); "NAVIDROME_LISTENER" is the account the user plays and rates with."""
    base = os.environ.get("NAVIDROME_URL", "").rstrip("/")
    user = os.environ.get(account + "_USER", "")
    password_file = os.environ.get(account + "_PASSWORD_FILE", "")
    if not base or not user or not password_file:
        return None
    p = urllib.parse.urlsplit(base)
    if (p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment
            or (p.scheme == "http" and p.hostname not in {"navidrome", "localhost", "127.0.0.1", "::1"})):
        raise Failure("unsafe_scan_url")
    secret = Path(password_file)
    if secret.is_symlink() or not secret.is_file() or secret.stat().st_mode & 0o077:
        raise Failure("unsafe_scan_secret_permissions")
    password = secret.read_text(encoding="utf-8").rstrip("\r\n")
    if not password or len(password) > 4096:
        raise Failure("invalid_scan_secret")
    salt = secrets.token_hex(16)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()  # Required by Subsonic, not password storage.
    body = urllib.parse.urlencode({"u": user, "t": token, "s": salt, "v": "1.16.1",
                                  "c": "music-butler", "f": "json", **(params or {})}, doseq=True).encode()
    result = json.loads(fetch(base + "/rest/" + endpoint + ".view", data=body, limit=limit,
                             headers={"Content-Type": "application/x-www-form-urlencoded"}))
    response = result.get("subsonic-response", {})
    if response.get("status") != "ok":
        raise Failure("navidrome_api_error", 69)
    return response


def scan():
    try:
        response = subsonic("startScan")
        if response is None:
            return "scheduled"
        if not isinstance(response.get("scanStatus"), dict):
            return "scheduled_after_scan_error"
        return "requested"
    except (OSError, ValueError, AttributeError, Failure, urllib.error.URLError) as exc:
        if isinstance(exc, Failure) and exc.code in (130, 143):
            raise
        return "scheduled_after_scan_error"


def safe_dir(path):
    if path.is_symlink():
        raise Failure("symlink_storage_refused", 73)
    path.mkdir(mode=0o750, exist_ok=True)
    if not path.is_dir():
        raise Failure("storage_not_directory", 73)
    return path


def sync_tree(path):
    for item in path.iterdir():
        if item.is_file():
            with item.open("rb") as fp:
                os.fsync(fp.fileno())
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def library_root():
    root = Path(os.environ.get("LIBRARY_ROOT", "./storage/library")).absolute()
    # Parent mounts/ownership are provisioned by the administrator, not the agent.
    if not root.is_dir() or root.is_symlink():
        raise Failure("provision_library_root_first", 73)
    return root.resolve()


@contextlib.contextmanager
def library_lock(root):
    """One writer at a time: imports, retags and removals."""
    fd = os.open(root / ".ingest.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Failure("busy_retry_later", 75) from exc
        yield


def ingest(request):
    root = library_root()
    music = safe_dir(root / "music")
    staging = safe_dir(root / ".staging")
    logs = safe_dir(root / "logs")
    if music.stat().st_dev != staging.stat().st_dev:
        raise Failure("staging_must_share_filesystem", 73)
    with library_lock(root):
        if shutil.disk_usage(root).free < 2 * 1024 ** 3:
            raise Failure("insufficient_free_space", 73)
        for tool in ("yt-dlp", "ffmpeg"):
            if not shutil.which(tool):
                raise Failure(tool + "_required", 69)
        try:
            import mutagen  # noqa: F401
        except ImportError as exc:
            raise Failure("mutagen_required", 69) from exc
        request_id = secrets.token_hex(16)
        final = music / (slug(request["artist"]) + "--" + slug(request["album"]) + "--" + request_id)
        receipt = {"version": VERSION, "request_id": request_id,
                   "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "request": request, "status": "staging", "tracks": []}
        log_path = logs / (request_id + ".json")
        published = False
        try:
            with tempfile.TemporaryDirectory(prefix=request_id + "-", dir=staging) as tmp:
                tmp = Path(tmp)
                ready = safe_dir(tmp / "ready")
                home = safe_dir(tmp / "home")
                # Deliberately exclude inherited cookies, proxies, credentials and provider config.
                env = {"PATH": os.environ["PATH"], "HOME": str(home), "XDG_CONFIG_HOME": str(home),
                       "XDG_CACHE_HOME": str(home), "TMPDIR": str(tmp), "LANG": "C.UTF-8"}
                lyrics = Lyrics(root)
                emit("started", request_id=request_id, tracks=len(request["tracks"]))
                for n, track in enumerate(request["tracks"], 1):
                    work = safe_dir(tmp / str(n))
                    source, provider = acquire(track, request, work, env)
                    dest = ready / (f"{n:03d}-" + slug(track["title"]) + "." + request["format"])
                    encode(source, dest, request, work, env)
                    cover = artwork(track, work, env)
                    art = "embedded" if cover else "unavailable"
                    if cover is None:
                        cover = source_thumbnail(work, env)
                        art = "source_thumbnail" if cover else art
                    duration = tag_audio(dest, request, track, n, len(request["tracks"]), cover)
                    lyric_status = lyrics.get(request, track, duration, dest)
                    with dest.open("rb") as audio_file:
                        digest = hashlib.file_digest(audio_file, "sha256").hexdigest()
                    receipt["tracks"].append({"title": track["title"], "file": dest.name, "sha256": digest,
                        "provider": provider, "lyrics": lyric_status, "duration": duration, "artwork": art})
                    emit("track_staged", number=n, lyrics=lyric_status, artwork=art)
                receipt["status"] = "complete"
                (ready / "provenance.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
                sync_tree(ready)
                if final.exists():
                    raise Failure("destination_collision", 73)
                # One directory rename publishes the ENTIRE album, audio + lyrics + provenance.
                os.rename(ready, final)
                published = True
                sync_tree(music)
            scan_status = scan()
            receipt.update({"scan": scan_status, "path": str(final)})
            log_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
            emit("complete", request_id=request_id, path=str(final), tracks=receipt["tracks"], scan=scan_status)
            return {"path": str(final), "tracks": receipt["tracks"], "scan": scan_status}
        except BaseException as exc:
            receipt["status"] = "published_reconcile_required" if published else "failed"
            receipt["path"] = str(final) if published else None
            receipt["error"] = str(exc) if isinstance(exc, Failure) else type(exc).__name__
            if getattr(exc, "detail", None):
                receipt["detail"] = exc.detail
            with contextlib.suppress(OSError):
                log_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
            if published:
                emit("warning", reason="published_reconcile_before_retry", path=str(final))
                with contextlib.suppress(AttributeError):
                    exc.butler_published = str(final)
            raise


# Agents read tool output through size limits (Hermes scales them down for small
# models), so the library comes in pages that stay small at any library size.
LIBRARY_PAGE = 20


def library_page(page):
    releases = read_library()
    pages = max(1, math.ceil(len(releases) / LIBRARY_PAGE))
    if not 1 <= page <= pages:
        emit("error", code=64, reason="no_such_page", pages=pages)
        return 64
    emit("library", page=page, pages=pages, releases_total=len(releases),
         releases=releases[(page - 1) * LIBRARY_PAGE:page * LIBRARY_PAGE])
    return 0


RELEASE_DIR = re.compile(r"--([0-9a-f]{32})$")
AUDIO_SUFFIXES = (".mp3", ".flac")


def norm(value):
    """Comparison key: "HUNTR/X" == "Huntr X", "Beyoncé" == "Beyonce"."""
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return "".join(c for c in value if c.isalnum())


def video_id(link):
    p = urllib.parse.urlsplit(link or "")
    if p.hostname not in YOUTUBE_HOSTS:
        return None
    vid = p.path.strip("/") if p.hostname == "youtu.be" else (urllib.parse.parse_qs(p.query).get("v") or [""])[0]
    return vid if YOUTUBE_ID.match(vid or "") else None


def release_dirs(music):
    return sorted(d for d in music.iterdir() if d.is_dir() and not d.is_symlink() and RELEASE_DIR.search(d.name))


def audio_files(folder):
    return sorted(p for p in folder.iterdir() if p.suffix in AUDIO_SUFFIXES and p.is_file() and not p.is_symlink())


def read_release(folder):
    """Tags are the truth (they may have been retagged); provenance adds the source URLs."""
    urls, origin, added = {}, "request", None
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
        receipt = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
        added = receipt.get("created")
        origin = receipt["request"].get("origin") if receipt["request"].get("origin") in ORIGINS else "request"
        for source, done in zip(receipt["request"]["tracks"], receipt["tracks"]):
            urls[done["file"]] = source["url"]
    tracks = []
    for n, path in enumerate(audio_files(folder), 1):
        try:
            tags = read_tags(path)
        except Exception:  # an unreadable file is reported, never fatal
            tracks.append({"n": n, "file": path.name, "error": "unreadable_tags"})
            continue
        track = {"n": n, "file": path.name, "title": tags["title"], "artist": tags["artist"],
                 "duration": tags["duration"], "cover": tags["cover"],
                 "lyrics": path.with_suffix(".lrc").is_file()}
        if tags["artists"]:
            track["artists"] = tags["artists"]
        if path.name in urls:
            track["url"] = urls[path.name]
        tracks.append(track)
    first = next((read_tags(p) for p in audio_files(folder)[:1]), {}) if tracks and "error" not in tracks[0] else {}
    return {"id": RELEASE_DIR.search(folder.name).group(1),
            "artist": first.get("albumartist") or first.get("artist"), "album": first.get("album"),
            "year": first.get("year"), "origin": origin, "added": added, "tracks": tracks}


def read_library():
    music = library_root() / "music"
    return [read_release(d) for d in release_dirs(music)] if music.is_dir() and not music.is_symlink() else []


def library_index(existing):
    """Names and sources already in the library, as comparison keys."""
    idx = {"artists": {}, "albums": {}, "songs": set(), "videos": set(), "keys": set(), "titles": {}}
    for rel in existing:
        if rel["artist"]:
            idx["artists"].setdefault(norm(rel["artist"]), rel["artist"])
            if rel["album"]:
                idx["albums"].setdefault((norm(rel["artist"]), norm(rel["album"])), rel["album"])
        for t in rel["tracks"]:
            if t.get("title"):
                idx["songs"].add((norm(rel["artist"]), norm(t["title"])))
                for artist in {rel["artist"], *(t.get("artists") or [])[:1]} - {None}:
                    idx["keys"].add(song_key(artist, t["title"]))
                    idx["titles"].setdefault(norm(base_title(t["title"])), set()).add(norm(main_artist(artist)))
            if video_id(t.get("url")):
                idx["videos"].add(video_id(t["url"]))
    return idx


def check_library(releases):
    """Match names already in the library and refuse songs that are already there.

    Same name apart from case/punctuation/accents: the library's spelling is used.
    A close but different name is only reported, as a note on the release.
    Bulk adds (origin other than "request") skip a song already there instead of
    refusing the whole request; the skipped songs are returned.
    """
    try:
        idx = library_index(read_library())
    except Failure:
        return []  # no library yet (e.g. offline validation on another machine)
    artists, albums, skipped = idx["artists"], idx["albums"], []
    for release in list(releases):
        notes = []
        key = norm(release["artist"])
        if key in artists and artists[key] != release["artist"]:
            notes.append({"note": "artist_spelling_from_library", "given": release["artist"], "used": artists[key]})
            release["artist"] = artists[key]
        elif key not in artists:
            close = [artists[k] for k in difflib.get_close_matches(key, list(artists), n=1, cutoff=0.8)]
            if close:
                notes.append({"note": "similar_artist_in_library", "given": release["artist"], "existing": close[0]})
        album = albums.get((key, norm(release["album"])))
        if album and album != release["album"]:
            notes.append({"note": "album_spelling_from_library", "given": release["album"], "used": album})
            release["album"] = album
        kept = []
        for n, track in enumerate(release["tracks"], 1):
            if (key, norm(track["title"])) in idx["songs"] or video_id(track["url"]) in idx["videos"]:
                if release.get("origin", "request") == "request":
                    exc = Failure("already_in_library")
                    exc.item = track.get("song", n)
                    raise exc
                skipped.append({"artist": release["artist"], "title": track["title"], "reason": "already_in_library"})
            else:
                kept.append(track)
        if not kept:
            releases.remove(release)
            continue
        if len(kept) != len(release["tracks"]):
            release["tracks"], release["kind"] = kept, "song" if len(kept) == 1 else "album"
        if notes:
            release["notes"] = notes
    return skipped


def validate_changes(data, resolve=False):
    """{"changes": [{release, track?, artist?, artists?, album?, title?, year?, artwork_url?, cover_from?}]}"""
    keys = {"release", "track", "artist", "artists", "album", "title", "year", "artwork_url", "cover_from"}
    if not isinstance(data, dict) or set(data) != {"changes"}:
        raise Failure("invalid_retag_keys")
    changes = data["changes"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= 500:
        raise Failure("invalid_change_count")
    hosts, out = allowed_hosts(), []
    for n, change in enumerate(changes, 1):
        try:
            if not isinstance(change, dict) or set(change) - keys or len(set(change) - {"release", "track"}) == 0:
                raise Failure("invalid_change_keys")
            if not isinstance(change.get("release"), str) or not re.fullmatch(r"[0-9a-f]{32}", change["release"]):
                raise Failure("invalid_release_id")
            c = {"release": change["release"]}
            if "track" in change:
                if isinstance(change["track"], bool) or not isinstance(change["track"], int) or change["track"] < 1:
                    raise Failure("invalid_track_number")
                c["track"] = change["track"]
            for key in ("artist", "album", "title"):
                if key in change:
                    c[key] = text(change[key], key)
            if "artists" in change:
                c["artists"] = artist_list(change["artists"])
            if "year" in change:
                c["year"] = None if change["year"] is None else release_year(change["year"])
            for key in ("artwork_url", "cover_from"):
                if key in change:
                    c[key] = url(change[key], hosts, resolve)
            if "artwork_url" in c and "cover_from" in c:
                raise Failure("one_cover_source_only")
        except Failure as exc:
            exc.item = n
            raise
        out.append(c)
    return out


def validate_removal(data):
    """{"releases": ["<release id>", ...]}"""
    if not isinstance(data, dict) or set(data) != {"releases"}:
        raise Failure("invalid_remove_keys")
    ids = data["releases"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
        raise Failure("invalid_release_count")
    for n, rid in enumerate(ids, 1):
        if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{32}", rid):
            exc = Failure("invalid_release_id")
            exc.item = n
            raise exc
    return list(dict.fromkeys(ids))


def find_release(music, rid):
    found = [d for d in release_dirs(music) if d.name.endswith("--" + rid)]
    if not found:
        raise Failure("unknown_release", 65)
    return found[0]


def new_cover(change, work, env):
    """Cover bytes from artwork_url, or from a video's thumbnail (cover_from), or None."""
    if "artwork_url" in change:
        return artwork(change, work, env)
    run([*ytdlp_base(), "--no-playlist", "--skip-download", "--write-thumbnail",
         "-o", "thumbnail:" + str(work / "thumb.%(ext)s"), "--", change["cover_from"]], work, env, timeout=120)
    return source_thumbnail(work, env)


def retag_file(path, fields, cover):
    """Tag a copy, then replace the original in one rename: never a half-written file."""
    temp = path.with_name(".retag-" + path.name)
    try:
        shutil.copyfile(path, temp)
        write_tags(temp, fields, cover)
        with temp.open("rb") as fp:
            os.fsync(fp.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def write_json(path, value):
    temp = path.with_name("." + path.name + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def change_fields(change, old):
    """Tag fields for one file. A new main `artist` also replaces the old main artist
    in the track artist, keeping featured artists."""
    fields = {k: change[k] for k in ("title", "album", "year") if k in change}
    if "artists" in change:
        artists = change["artists"]
        fields.update(artist=display_artist(artists), artists=artists if len(artists) > 1 else None)
    if "artist" in change:
        fields["albumartist"] = change["artist"]
        if "artists" not in change:
            main = old.get("albumartist") or old.get("artist")
            if old.get("artists") and old["artists"][0] == main:
                artists = [change["artist"], *old["artists"][1:]]
                fields.update(artist=display_artist(artists), artists=artists)
            elif not old.get("artist") or old["artist"] == main:
                fields["artist"] = change["artist"]
    return fields


def retag(changes):
    """Fix tags (and covers) of releases already in the library, without re-downloading."""
    root = library_root()
    music, staging = safe_dir(root / "music"), safe_dir(root / ".staging")
    changed, failed, not_attempted = [], [], []
    with library_lock(root):
        for i, change in enumerate(changes, 1):
            try:
                folder = find_release(music, change["release"])
                files = audio_files(folder)
                if "track" in change and change["track"] > len(files):
                    raise Failure("unknown_track", 65)
                targets = [files[change["track"] - 1]] if "track" in change else files
                if "title" in change and len(targets) != 1:
                    raise Failure("title_needs_track", 65)
                cover = None
                if "artwork_url" in change or "cover_from" in change:
                    with tempfile.TemporaryDirectory(prefix="retag-", dir=staging) as tmp:
                        home = safe_dir(Path(tmp) / "home")
                        env = {"PATH": os.environ["PATH"], "HOME": str(home), "XDG_CONFIG_HOME": str(home),
                               "XDG_CACHE_HOME": str(home), "TMPDIR": tmp, "LANG": "C.UTF-8"}
                        cover = new_cover(change, Path(tmp), env)
                    if cover is None:
                        raise Failure("cover_unavailable", 69)
                for path in targets:
                    retag_file(path, change_fields(change, read_tags(path)), cover)
                with contextlib.suppress(OSError, ValueError):
                    receipt = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
                    receipt.setdefault("retagged", []).append({
                        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "change": {k: v for k, v in change.items() if k != "release"}})
                    write_json(folder / "provenance.json", receipt)
                changed.append({"change": i, "release": change["release"], "files": len(targets)})
            except Failure as exc:
                failed.append({"change": i, "release": change["release"], "reason": str(exc)})
                if exc.code not in CONTINUE_CODES:
                    not_attempted = list(range(i + 1, len(changes) + 1))
                    break
    scan_status = scan() if changed else "not_needed"
    emit("summary", ok=not failed and not not_attempted, changed=changed, failed=failed,
         not_attempted=not_attempted, scan=scan_status)
    return 0 if not failed and not not_attempted else 65


def remove(ids):
    """Move releases to <library>/.trash (same filesystem, not scanned). Nothing is deleted."""
    root = library_root()
    music, trash = safe_dir(root / "music"), safe_dir(root / ".trash")
    removed, failed = [], []
    with library_lock(root):
        for rid in ids:
            try:
                folder = find_release(music, rid)
                gone = read_release(folder)
                os.rename(folder, trash / folder.name)
                removed.append(rid)
                with contextlib.suppress(Failure, OSError):
                    forget(root, gone)
            except Failure as exc:
                failed.append({"release": rid, "reason": str(exc)})
        if removed:
            sync_tree(music)
    scan_status = scan() if removed else "not_needed"
    emit("summary", ok=not failed, removed=removed, failed=failed, scan=scan_status)
    return 0 if not failed else 65


def missing_lyrics():
    """Retry LRCLIB, with the import's strict match, for every track without an .lrc.

    Lyrics are often added to LRCLIB after a song is imported, and a failed lookup
    used to be final. Nothing is downloaded but lyrics; tags are not touched.
    """
    root = library_root()
    music = safe_dir(root / "music")
    lyrics = Lyrics(root)
    if not lyrics.agent:
        emit("summary", ok=False, reason="lyrics_disabled", found=[], missing=[], scan="not_needed")
        return 64
    found, missing, not_attempted = [], [], []
    with library_lock(root):
        todo = [(folder, path) for folder in release_dirs(music) for path in audio_files(folder)
                if not path.with_suffix(".lrc").exists()]
        for i, (folder, path) in enumerate(todo):
            release = RELEASE_DIR.search(folder.name).group(1)
            try:
                tags = read_tags(path)
            except Exception:  # an unreadable file is reported, never fatal
                missing.append({"release": release, "file": path.name, "lyrics": "unreadable_tags"})
                continue
            artist = tags.get("albumartist") or tags.get("artist")
            if not artist or not tags.get("title"):
                missing.append({"release": release, "file": path.name, "lyrics": "no_tags"})
                continue
            status = lyrics.get({"artist": artist, "album": tags.get("album") or ""},
                                {"title": tags["title"]}, tags["duration"], path)
            entry = {"release": release, "artist": artist, "title": tags["title"]}
            if status == "synced":
                found.append(entry)
            else:
                missing.append({**entry, "lyrics": status})
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
                receipt = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
                for track in receipt["tracks"]:
                    if track["file"] == path.name and track.get("lyrics") != status:
                        track["lyrics"] = status
                        write_json(folder / "provenance.json", receipt)
            if status == "rate_limited":
                not_attempted = [{"release": RELEASE_DIR.search(f.name).group(1), "file": p.name}
                                 for f, p in todo[i + 1:]]
                break
    scan_status = scan() if found else "not_needed"
    emit("summary", ok=not not_attempted, found=found, missing=missing,
         not_attempted=not_attempted, scan=scan_status)
    return 0 if not not_attempted else 69


# ---- Discovery: taste seeds, playlist queue, YouTube radio candidates ----

RADIO_ITEMS = 25  # songs read from one YouTube Music radio
MAX_RADIO_SEEDS = 12  # radios per discover run (one yt-dlp call each)
TOP_SEEDS = 8  # of those, the best-loved seeds; the rest are picked at random for variety
MAX_CANDIDATES = 40
MAX_QUEUE_OUT = 30
PER_ARTIST = 2
STALE_DAYS = 28
PASS_AFTER = 4  # candidates offered this often without being added are dropped
MAX_QUEUE = 5000
MAX_PLAYLIST = 8 * MAX_REQUEST
MIN_SONG, MAX_SONG = 90, 600  # seconds; shorter clips and hour-long mixes are not songs
NOISE = re.compile(r"\b(?:covers?|covered|fan[ -]?(?:made|edit)|sped[ -]?up|slowed|nightcore|8d|karaoke|instrumental"
                   r"|reaction|mashup|hybrid|tutorial|lesson|amv|parody|\d+\s*hours?|1h|loop(?:ed)?)\b"
                   r"|\blive\s+(?:at|from|in|on|session|performance|version)\b|[(\[|]\s*live\b|\blive\s*[)\]]", re.I)
MAIN_ARTIST = re.compile(r"\s*(?:,|;|&|\bx\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\band\b|\bwith\b|\bvs\.?)\s*", re.I)
PLAYLISTS = {  # Navidrome smart playlists, kept in the library's music folder
    "Discover Weekly.nsp": {
        "name": "Discover Weekly", "comment": "Songs Music Butler discovered in the last week",
        "all": [{"contains": {"comment": "music-butler:discovery"}}, {"inTheLast": {"dateadded": 7}}],
        "sort": "dateadded", "order": "desc", "limit": 100},
}
# Smart playlists judge ratings and plays by their owner (the admin), not the listener,
# so "Discoveries to rate" is a normal playlist of the listener's, rebuilt by
# refresh_playlists. Its old smart version is removed.
TO_RATE = "Discoveries to rate"
OLD_PLAYLISTS = ("Discoveries to rate.nsp",)


SCAN_WAIT = 600  # seconds to wait for Navidrome to index new songs before refreshing playlists


def refresh_playlists():
    """Make Navidrome re-evaluate the smart playlists after songs were added.

    Navidrome evaluates a smart playlist only when its owner opens it, and the .nsp
    playlists belong to the admin, while the user listens with another account:
    without this, Tempo shows the songs of the last time the admin looked. Also rebuilds
    the listener's "Discoveries to rate", which a smart playlist cannot express.
    """
    ensure_playlists(safe_dir(library_root() / "music"))
    if scan() != "requested":
        emit("summary", ok=False, reason="admin_account_not_configured", refreshed=[])
        return 0
    deadline = time.time() + SCAN_WAIT
    while True:
        time.sleep(5)
        status = (subsonic("getScanStatus") or {}).get("scanStatus") or {}
        if not status.get("scanning"):
            break
        if time.time() > deadline:
            emit("summary", ok=False, reason="scan_still_running", refreshed=[])
            return 69
    names = {p["name"] for p in PLAYLISTS.values()}
    listener = os.environ.get("NAVIDROME_LISTENER_USER", "")
    refreshed = []
    for playlist in (subsonic("getPlaylists") or {}).get("playlists", {}).get("playlist", []):
        if playlist.get("name") in names:
            full = (subsonic("getPlaylist", {"id": playlist["id"]}) or {}).get("playlist") or {}
            refreshed.append({"name": playlist["name"], "songs": len(full.get("entry") or [])})
        elif (playlist.get("name") == TO_RATE and listener and playlist.get("owner") != listener
              and playlist.get("owner") == os.environ.get("NAVIDROME_USER")):
            subsonic("deletePlaylist", {"id": playlist["id"]})  # the old smart version
    to_rate_songs = rebuild_to_rate()
    if to_rate_songs is not None:
        refreshed.append({"name": TO_RATE, "songs": to_rate_songs})
    emit("summary", ok=len(refreshed) == len(names) + 1, refreshed=refreshed)
    return 0


def rebuild_to_rate():
    """Replace the listener's "Discoveries to rate" with the discoveries that still need
    a like, a rating or a second play. Returns its size, or None if listening is unknown."""
    releases = read_library()
    stats = listening(releases)
    if stats is None:
        return None
    songs = to_rate(releases, stats)
    account = "NAVIDROME_LISTENER" if os.environ.get("NAVIDROME_LISTENER_USER") else "NAVIDROME"
    owner = os.environ.get(account + "_USER")
    mine = [p for p in (subsonic("getPlaylists", account=account) or {}).get("playlists", {}).get("playlist", [])
            if p.get("name") == TO_RATE and p.get("owner") == owner]
    for extra in mine[1:]:
        subsonic("deletePlaylist", {"id": extra["id"]}, account=account)
    if mine and not songs:
        # Replacing with no songs is not clearing on every server: remove them one by one.
        current = (subsonic("getPlaylist", {"id": mine[0]["id"]}, account=account) or {}).get("playlist") or {}
        indexes = list(range(len(current.get("entry") or [])))
        if indexes:
            subsonic("updatePlaylist", {"playlistId": mine[0]["id"], "songIndexToRemove": indexes}, account=account)
    else:
        params = {"playlistId": mine[0]["id"]} if mine else {"name": TO_RATE}
        subsonic("createPlaylist", {**params, "songId": songs}, account=account)
    if mine:
        subsonic("updatePlaylist", {"playlistId": mine[0]["id"], "comment": TO_RATE_COMMENT}, account=account)
    else:
        created = [p for p in (subsonic("getPlaylists", account=account) or {}).get("playlists", {}).get("playlist", [])
                   if p.get("name") == TO_RATE and p.get("owner") == owner]
        for p in created[:1]:
            subsonic("updatePlaylist", {"playlistId": p["id"], "comment": TO_RATE_COMMENT}, account=account)
    return len(songs)


TO_RATE_COMMENT = ("Discoveries that leave 4 weeks after they were added unless you like them, rate "
                   "them (2-5 stars) or play them twice. 1 star removes a song at once.")


def main_artist(value):
    """"Shakira, Burna Boy" -> "Shakira"; used only for comparison keys."""
    return MAIN_ARTIST.split(value or "")[0].strip() or (value or "")


def base_title(value):
    """"Get Lucky (Radio Edit - feat. X)" -> "Get Lucky": versions and credits removed."""
    value = re.sub(r"\s*[(\[【][^)\]】]*[)\]】]", "", value or "")
    value = BARE_FEAT.sub("", re.split(r"\s+[-–—]\s+", value, maxsplit=1)[0])
    return value.strip() or (value or "")


def song_key(artist, title):
    return norm(main_artist(artist)) + "|" + norm(base_title(title))


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def discovery_path(root, name):
    return safe_dir(root / "discovery") / name


def read_state(root, name, default):
    path = discovery_path(root, name)
    if path.is_symlink():
        raise Failure("symlink_storage_refused", 73)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise Failure("discovery_state_unreadable", 73) from exc
    return value if isinstance(value, type(default)) else default


def forget(root, release):
    """Remember a removed release so discovery never suggests it again."""
    history = read_state(root, "history.json", {})
    for t in release["tracks"]:
        if not t.get("title"):
            continue
        artist = (t.get("artists") or [release["artist"] or t.get("artist") or ""])[0]
        history[song_key(artist, t["title"])] = {"artist": artist, "title": t["title"], "status": "removed",
                                                 "video": video_id(t.get("url")), "at": now().isoformat()}
    write_json(discovery_path(root, "history.json"), history)


def blocked(history):
    """Song keys and video ids discovery must not offer again."""
    keys, videos = set(), set()
    for key, rec in history.items():
        if not isinstance(rec, dict):
            continue
        if rec.get("status") in ("removed", "not_found") or rec.get("offered", 0) >= PASS_AFTER:
            keys.add(key)
            if isinstance(rec.get("video"), str):
                videos.add(rec["video"])
    return keys, videos


def validate_seeds(data):
    """{"artists": ["Name" | {"name", "note"}], "songs": [{"artist", "title"}], "notes": "...",
    "clear_notes": true, "block_artists": [...], "remove_artists": [...]}"""
    keys = {"artists", "songs", "notes", "clear_notes", "block_artists", "remove_artists"}
    if not isinstance(data, dict) or not data or set(data) - keys:
        raise Failure("invalid_seed_keys")
    out = {"artists": [], "songs": [], "block_artists": [], "remove_artists": []}
    for n, item in enumerate(data.get("artists", []), 1):
        item = {"name": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or set(item) - {"name", "note"}:
            raise Failure("invalid_seed_artist", item=n)
        artist = {"name": text(item.get("name"), "artist")}
        if not blank(item.get("note")):
            artist["note"] = text(item["note"], "note")
        out["artists"].append(artist)
    for n, item in enumerate(data.get("songs", []), 1):
        if not isinstance(item, dict) or set(item) - {"artist", "title"}:
            raise Failure("invalid_seed_song", item=n)
        out["songs"].append({"artist": text(item.get("artist"), "artist"), "title": text(item.get("title"), "title")})
    for key in ("block_artists", "remove_artists"):
        if not isinstance(data.get(key, []), list):
            raise Failure("invalid_" + key)
        out[key] = [text(v, "artist") for v in data.get(key, [])]
    if not blank(data.get("notes")):
        out["notes"] = text(data["notes"], "notes", 2000)
    if data.get("clear_notes") not in (None, False, True):
        raise Failure("invalid_clear_notes")
    out["clear_notes"] = data.get("clear_notes") is True
    if sum(len(out[k]) for k in ("artists", "songs", "block_artists", "remove_artists")) > 200:
        raise Failure("too_many_seeds")
    return out


def merge_seeds(change):
    root = library_root()
    with library_lock(root):
        seeds = read_state(root, "seeds.json", {})
        artists = {norm(a["name"]): a for a in seeds.get("artists", []) if isinstance(a, dict) and a.get("name")}
        songs = {song_key(s["artist"], s["title"]): s for s in seeds.get("songs", [])
                 if isinstance(s, dict) and s.get("artist") and s.get("title")}
        block = {norm(a): a for a in seeds.get("block_artists", []) if isinstance(a, str)}
        for artist in change["artists"]:
            old = artists.get(norm(artist["name"]), {})
            artists[norm(artist["name"])] = {**old, **artist}
            block.pop(norm(artist["name"]), None)
        for song in change["songs"]:
            songs.setdefault(song_key(song["artist"], song["title"]), song)
        for name in change["block_artists"]:
            block[norm(name)] = name
        for name in change["remove_artists"] + change["block_artists"]:
            artists.pop(norm(name), None)
            songs = {k: s for k, s in songs.items() if norm(main_artist(s["artist"])) != norm(main_artist(name))}
        seeds.update(artists=list(artists.values()), songs=list(songs.values()), block_artists=list(block.values()))
        # Notes grow line by line: a model never has to repeat the old ones to keep them.
        lines = [] if change["clear_notes"] else [n for n in str(seeds.get("notes") or "").splitlines() if n.strip()]
        if "notes" in change and norm(change["notes"]) not in map(norm, lines):
            lines.append(change["notes"])
        seeds["notes"] = "\n".join(lines)[-4000:]
        write_json(discovery_path(root, "seeds.json"), seeds)
    emit("seeds", **{k: seeds.get(k) for k in ("notes", "artists", "songs", "block_artists")})
    return 0


def column(header, *names):
    for name in names:
        if name in header:
            return header[name]
    return None


def parse_playlist(raw):
    """Exportify (Spotify) CSV, or any CSV with track and artist columns -> queue entries."""
    import csv
    try:
        rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
    except csv.Error as exc:
        raise Failure("invalid_playlist_csv") from exc
    if len(rows) < 2:
        raise Failure("empty_playlist")
    header = {h.strip().casefold(): i for i, h in enumerate(rows[0])}
    title_col = column(header, "track name", "track", "title", "song", "name")
    artist_col = column(header, "artist name(s)", "artist name", "artists", "artist")
    if title_col is None or artist_col is None:
        raise Failure("playlist_needs_track_and_artist_columns")
    album_col = column(header, "album name", "album")
    date_col = column(header, "release date", "album release date", "year")
    ms_col = column(header, "duration (ms)", "duration_ms", "track duration (ms)")
    out = []
    for row in rows[1:MAX_QUEUE + 1]:
        def cell(i):
            return clip(row[i]) if i is not None and i < len(row) else None
        title, credit = cell(title_col), cell(artist_col)
        if not title or not credit:
            continue
        artists = [a for a in (clip(a) for a in re.split(r"\s*;\s*" if ";" in credit else r",\s+", credit)) if a][:20]
        entry = {"artist": artists[0], "title": title, "search": clip(artists[0] + " " + base_title(title))}
        if len(artists) > 1:
            entry["artists"] = artists
        if cell(album_col):
            entry["album"] = cell(album_col)
        year = re.match(r"\d{4}", cell(date_col) or "")
        if year and 1000 <= int(year.group()) <= 2100:
            entry["year"] = int(year.group())
        ms = cell(ms_col)
        if ms and ms.isdigit() and 1000 <= int(ms) <= 3600000:
            entry["duration"] = round(int(ms) / 1000)
        out.append(entry)
    return out


def queue_playlist(raw):
    entries = parse_playlist(raw)
    root = library_root()
    with library_lock(root):
        idx = library_index(read_library())
        gone, _ = blocked(read_state(root, "history.json", {}))
        queue = [q for q in read_state(root, "queue.json", []) if isinstance(q, dict) and q.get("title")]
        queued = {song_key(q["artist"], q["title"]) for q in queue}
        counts = {"queued": 0, "in_library": 0, "duplicate": 0, "removed_before": 0}
        new = []
        for entry in entries:
            key = song_key(entry["artist"], entry["title"])
            reason = ("in_library" if key in idx["keys"] else "duplicate" if key in queued
                      else "removed_before" if key in gone else "queued")
            counts[reason] += 1
            if reason == "queued" and len(queue) + len(new) < MAX_QUEUE:
                new.append({**entry, "queued": now().isoformat(), "offered": 0})
                queued.add(key)
        queue = interleave(queue, new)
        write_json(discovery_path(root, "queue.json"), queue)
    emit("queued", **counts, queue=len(queue))
    return 0


def interleave(old, new):
    """Spread a new playlist evenly through the queue, so every import day mixes
    all playlists instead of working through them one after another."""
    spread = [((i + 0.5) / len(old), 0, q) for i, q in enumerate(old)]
    spread += [((j + 0.5) / len(new), 1, q) for j, q in enumerate(new)]
    return [q for _, _, q in sorted(spread, key=lambda item: item[:2])]


def track_stats(releases, songs):
    """Navidrome songs (fake paths) -> {(release id, track n): {"plays", "starred"}}, matched
    on album + title and the nearest duration."""
    index = {}
    for rel in releases:
        for t in rel["tracks"]:
            if t.get("title"):
                index.setdefault((norm(rel["album"]), norm(t["title"])), []).append((rel["id"], t["n"], t.get("duration") or 0))
    stats = {}
    for song in songs:
        found = index.get((norm(song.get("album")), norm(song.get("title"))))
        if not found:
            continue
        duration = song.get("duration") if isinstance(song.get("duration"), (int, float)) else 0
        rid, n, _ = min(found, key=lambda f: abs(f[2] - duration))
        plays = song.get("playCount") if isinstance(song.get("playCount"), int) else 0
        rating = song.get("userRating") if isinstance(song.get("userRating"), int) else 0
        entry = stats.setdefault((rid, n), {"plays": 0, "starred": False, "rating": 0, "ids": []})
        if isinstance(song.get("id"), str):
            entry["ids"].append(song["id"])
        entry["plays"] += plays
        entry["starred"] = entry["starred"] or bool(song.get("starred"))
        entry["rating"] = max(entry["rating"], rating)
    return stats


def listening(releases):
    """Play counts, stars and ratings of the listening account (e.g. the one Tempo uses;
    the admin account if none is configured), or None when Navidrome cannot be asked."""
    account = "NAVIDROME_LISTENER" if os.environ.get("NAVIDROME_LISTENER_USER") else "NAVIDROME"
    songs, offset = [], 0
    try:
        while offset < 50000:
            response = subsonic("search3", {"query": "", "songCount": 500, "songOffset": offset,
                                            "artistCount": 0, "albumCount": 0},
                                limit=16 * MAX_RESPONSE, account=account)
            if response is None:
                return None
            page = (response.get("searchResult3") or {}).get("song") or []
            songs += [s for s in page if isinstance(s, dict)]
            if len(page) < 500:
                break
            offset += 500
    except (OSError, ValueError, AttributeError, Failure, urllib.error.URLError) as exc:
        if isinstance(exc, Failure) and exc.code in (130, 143):
            raise
        return None
    return track_stats(releases, songs)


def disliked(s):
    """1 star: the user wants the song gone."""
    return s.get("rating", 0) == 1


def seed_weight(s, origin):
    """Liked (starred) or 5 stars 4; 4 stars 3; played twice 2; 3 stars, played, asked for
    or from a playlist 1. 1-2 stars ("keep, but not this"), or discovered and not yet
    heard: not a seed."""
    rating = s.get("rating", 0)
    if rating in (1, 2):
        return 0
    if rating == 5 or s.get("starred"):
        return 4
    if rating == 4:
        return 3
    if s.get("plays", 0) >= 2:
        return 2
    return 1 if rating or s.get("plays") or origin != "discovery" else 0


def library_seeds(releases, stats):
    """Radio seeds from the library, weighted by seed_weight."""
    seeds = []
    for rel in releases:
        for t in rel["tracks"]:
            vid = video_id(t.get("url"))
            if not vid or not t.get("title"):
                continue
            weight = seed_weight(stats.get((rel["id"], t["n"]), {}), rel.get("origin", "request"))
            if weight:
                seeds.append({"name": f"{rel['artist']} - {base_title(t['title'])}", "video": vid, "weight": weight})
    return seeds


def pick_seeds(own, users, rng):
    """While few songs are starred or played, the user's seeds steer; then listening does."""
    strong = [s for s in own if s["weight"] >= 2]
    pool = strong + (users if len(strong) < MAX_RADIO_SEEDS else [])
    rest = [s for s in own if s["weight"] < 2] + (users if len(strong) >= MAX_RADIO_SEEDS else [])
    rng.shuffle(pool)
    pool.sort(key=lambda s: -s["weight"])
    chosen = pool[:TOP_SEEDS]
    others = pool[TOP_SEEDS:] + rest
    rng.shuffle(others)
    return chosen + others[:MAX_RADIO_SEEDS - len(chosen)]


def clean_channel(channel):
    channel = re.sub(r"\s+and \d+ more$|\s*-\s*Topic$|VEVO$|\s+Official$", "", channel or "", flags=re.I)
    return channel.strip() or None


def radio_candidate(entry):
    """One radio item -> {"artist", "title", "url", "duration", "names"}, or None for noise."""
    s = video_summary(entry)
    if not s or not s.get("title") or NOISE.search(s["title"]) or NOISE.search(s.get("channel") or ""):
        return None
    if s["duration"] is not None and not MIN_SONG <= s["duration"] <= MAX_SONG:
        return None
    channel = s.get("channel") or ""
    if channel.endswith(" - Topic"):
        artist, title, names = channel[:-len(" - Topic")], s["title"], "release"
    else:
        song = s.get("song") or {}
        artist, title, names = song.get("artist") or clean_channel(channel), song.get("title") or s["title"], "guessed"
    if not clip(artist) or not clip(title):
        return None
    return {"artist": clip(artist), "title": clip(title), "url": s["url"], "duration": s["duration"], "names": names}


def radio(video, cwd, env):
    entries = capture([*ytdlp_base(), "--flat-playlist", "--dump-json", "--playlist-items", f"1:{RADIO_ITEMS}", "--",
                       f"https://www.youtube.com/watch?v={video}&list=RDAMVM{video}"], cwd, env)
    return [c for c in map(radio_candidate, entries) if c]


def first_song(query, cwd, env):
    """Video id of the top YouTube Music song for a search, or None."""
    found = capture([*ytdlp_base(), "--flat-playlist", "--dump-json", "--playlist-items", "1", "--",
                     "https://music.youtube.com/search?q=" + urllib.parse.quote(query) + "#songs"], cwd, env)
    return next((video_id(s["url"]) for s in map(video_summary, found) if s), None)


def near_known(c, titles):
    """Same title as a library song by a similar artist name ("HUNTRIX" / "HUNTR/X")."""
    artist = norm(main_artist(c["artist"]))
    return any(difflib.SequenceMatcher(None, artist, known).ratio() >= 0.8
               for known in titles.get(norm(base_title(c["title"])), ()))


def score(radios, skip_keys, skip_videos, skip_artists, titles=None):
    """radios: [(seed, [candidate, ...])] -> candidates ranked by how many (and how loved)
    seeds' radios hold them, higher in the radio counting more."""
    found = {}
    for seed, items in radios:
        for pos, c in enumerate(items):
            key = song_key(c["artist"], c["title"])
            if (key in skip_keys or video_id(c["url"]) in skip_videos
                    or norm(main_artist(c["artist"])) in skip_artists or near_known(c, titles or {})):
                continue
            entry = found.setdefault(key, {**c, "score": 0.0, "because": [], "key": key})
            if c["names"] == "release" and entry["names"] != "release":
                entry.update(c)  # a Topic upload's names beat names guessed from a video title
            entry["score"] += seed["weight"] * (1 - pos / (2 * RADIO_ITEMS))
            if seed["name"] not in entry["because"]:
                entry["because"].append(seed["name"])
    ranked, per_artist = [], {}
    for c in sorted(found.values(), key=lambda c: -c["score"]):
        artist = norm(main_artist(c["artist"]))
        if per_artist.get(artist, 0) >= PER_ARTIST:
            continue
        per_artist[artist] = per_artist.get(artist, 0) + 1
        ranked.append({**c, "score": round(c["score"], 2), "because": c["because"][:3]})
        if len(ranked) >= MAX_CANDIDATES:
            break
    return ranked


def kept(s):
    """A discovery stays past STALE_DAYS once it is liked, rated or played twice."""
    return s.get("plays", 0) >= 2 or s.get("starred") or s.get("rating")


def to_rate(releases, stats):
    """Navidrome ids of discovered songs that are not liked, rated or played twice yet:
    the ones that leave after STALE_DAYS unless the user does something. Newest first."""
    songs = []
    for rel in releases:
        if rel.get("origin") != "discovery":
            continue
        for t in rel["tracks"]:
            s = stats.get((rel["id"], t["n"]))
            if s and s["ids"] and not kept(s):
                songs.append((rel.get("added") or "", s["ids"][0]))
    return [song for _, song in sorted(songs, reverse=True)]


def stale_releases(releases, stats, today):
    """Releases to remove: any rated 1 star, and discoveries older than STALE_DAYS that
    are not liked, not rated and played fewer than 2 times."""
    out = []
    for rel in releases:
        heard = [stats.get((rel["id"], t["n"]), {}) for t in rel["tracks"]]
        if not (heard and all(disliked(s) for s in heard)):
            if rel.get("origin") != "discovery" or not rel.get("added"):
                continue
            try:
                added = datetime.datetime.fromisoformat(rel["added"])
            except ValueError:
                continue
            if (today - added).days < STALE_DAYS:
                continue
            if any(kept(s) for s in heard):
                continue
        out.append({"id": rel["id"], "artist": rel["artist"], "titles": [t.get("title") for t in rel["tracks"]],
                    "added": rel["added"][:10]})
    return out


def ensure_playlists(music):
    """Write Music Butler's smart playlists; Navidrome re-reads a changed file."""
    for name in OLD_PLAYLISTS:
        with contextlib.suppress(OSError):
            if (music / name).is_file() and not (music / name).is_symlink():
                (music / name).unlink()
    for name, rules in PLAYLISTS.items():
        path, content = music / name, json.dumps(rules, indent=2)
        with contextlib.suppress(OSError):
            if path.is_symlink() or (path.exists() and path.read_text(encoding="utf-8") == content):
                continue
            path.write_text(content, encoding="utf-8")


def discover():
    """Everything the discovery agent chooses from. Downloads nothing; updates only the
    discovery state (queue bookkeeping, offer counts, resolved seed ids)."""
    root = library_root()
    music = safe_dir(root / "music")
    ensure_playlists(music)
    releases = read_library()
    idx = library_index(releases)
    stats = listening(releases)
    seeds = read_state(root, "seeds.json", {})
    history = read_state(root, "history.json", {})
    skip_keys, skip_videos = blocked(history)
    skip_keys |= idx["keys"]
    skip_videos |= idx["videos"]
    skip_artists = {norm(main_artist(a)) for a in seeds.get("block_artists", []) if isinstance(a, str)}
    queue = [q for q in read_state(root, "queue.json", []) if isinstance(q, dict) and q.get("title")]
    queue = [q for q in queue if song_key(q["artist"], q["title"]) not in idx["keys"]]
    offer = queue[:MAX_QUEUE_OUT]
    skip_keys |= {song_key(q["artist"], q["title"]) for q in queue}

    users = [{"name": f"{s['artist']} - {s['title']}", "query": f"{s['artist']} {s['title']}", "seed": s, "weight": 2}
             for s in seeds.get("songs", []) if isinstance(s, dict) and s.get("artist") and s.get("title")]
    users += [{"name": a["name"], "query": a["name"], "seed": a, "weight": 2}
              for a in seeds.get("artists", []) if isinstance(a, dict) and a.get("name")]
    for u in users:
        u["video"] = u["seed"].get("video") if YOUTUBE_ID.match(str(u["seed"].get("video", ""))) else None
    chosen = pick_seeds(library_seeds(releases, stats or {}), users, random.Random())
    radios, errors = [], []
    with tempfile.TemporaryDirectory(prefix="discover-") as tmp:
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "XDG_CONFIG_HOME": tmp,
               "XDG_CACHE_HOME": tmp, "TMPDIR": tmp, "LANG": "C.UTF-8"}
        for seed in chosen:
            try:
                if not seed.get("video"):
                    seed["video"] = first_song(seed["query"], tmp, env)
                if seed["video"]:
                    radios.append((seed, radio(seed["video"], tmp, env)))
            except (Failure, OSError, ValueError) as exc:
                if isinstance(exc, Failure) and exc.code in (130, 143):
                    raise
                errors.append({"seed": seed["name"], "reason": "radio_failed"})
    candidates = score(radios, skip_keys, skip_videos, skip_artists, idx["titles"])
    known = {norm(main_artist(a)) for a in idx["artists"].values()}
    for c in candidates:
        c["new_artist"] = norm(main_artist(c["artist"])) not in known
    resolved = {u["name"]: u["video"] for u in users if u.get("video") and not u["seed"].get("video")}

    with library_lock(root):
        # Re-read under the lock: an import, removal or seeds change may have run meanwhile.
        history = read_state(root, "history.json", {})
        stamp = now().isoformat()
        kept = []
        for n, q in enumerate(queue):
            if n < len(offer):
                q["offered"] = q.get("offered", 0) + 1
            if q.get("offered", 0) > PASS_AFTER:
                history[song_key(q["artist"], q["title"])] = {"artist": q["artist"], "title": q["title"],
                                                              "status": "not_found", "at": stamp}
            else:
                kept.append(q)
        # Radio candidates only count as passed over once the queue is empty.
        if not offer:
            for c in candidates:
                rec = history.setdefault(c["key"], {"artist": c["artist"], "title": c["title"], "status": "offered"})
                rec.update(offered=rec.get("offered", 0) + 1, video=video_id(c["url"]), at=stamp)
        write_json(discovery_path(root, "queue.json"), kept)
        write_json(discovery_path(root, "history.json"), history)
        if resolved:  # remember each seed's song, so its radio needs no search next time
            fresh = read_state(root, "seeds.json", {})
            for s in [s for s in fresh.get("songs", []) + fresh.get("artists", []) if isinstance(s, dict)]:
                name = f"{s.get('artist')} - {s.get('title')}" if "title" in s else s.get("name")
                if name in resolved:
                    s["video"] = resolved[name]
            write_json(discovery_path(root, "seeds.json"), fresh)
    for c in candidates:
        del c["key"]
    emit("discover", library_songs=sum(len(r["tracks"]) for r in releases),
         listening="ok" if stats is not None else "unavailable",
         taste={"notes": seeds.get("notes", ""), "artists": [{k: a[k] for k in ("name", "note") if k in a}
                                                              for a in seeds.get("artists", []) if isinstance(a, dict)],
                "blocked_artists": seeds.get("block_artists", [])},
         queue=[{k: q[k] for k in ("artist", "title", "album", "year", "duration", "search") if k in q} for q in offer],
         queue_total=len(kept), radio_seeds=[s["name"] for s, _ in radios], candidates=candidates,
         stale=stale_releases(releases, stats, now()) if stats is not None else [], errors=errors)
    return 0


def interrupted(signum, frame):
    raise Failure("interrupted", 128 + signum)


def import_all(releases, skipped=()):
    """Import releases one by one and end with ONE `summary` line an agent can report from.

    Exit 0 only if every release was added. Invalid/unavailable sources (65/69) skip
    to the next release; storage, lock, interrupt or internal errors stop the batch.
    """
    added, failed, first_code = [], [], 0
    not_attempted = []
    for i, release in enumerate(releases):
        detail = None
        try:
            result = ingest(release)
            added.append({**describe(release), "scan": result["scan"],
                          "songs": [{"title": t["title"], "lyrics": t["lyrics"], "artwork": t["artwork"]}
                                    for t in result["tracks"]]})
            continue
        except Failure as exc:
            code, reason, published = exc.code, str(exc), getattr(exc, "butler_published", None)
            detail = exc.detail
        except OSError as exc:
            code, reason, published = 73, "filesystem_or_runtime_error", getattr(exc, "butler_published", None)
        except Exception as exc:  # never dump provider URLs/secrets into the transcript
            code, reason, published = 70, "unexpected_internal_error", getattr(exc, "butler_published", None)
        emit("error", code=code, reason=reason, release=i + 1)
        first_code = first_code or code
        if published:
            # The audio IS in the library; only the receipt/scan step failed. Never re-import it.
            added.append({**describe(release), "warning": "published_check_before_retry"})
        else:
            failed.append({**describe(release), "reason": reason, "code": code,
                           **({"detail": detail} if detail else {})})
        if code not in CONTINUE_CODES:
            not_attempted = [describe(r) for r in releases[i + 1:]]
            break
    emit("summary", ok=not failed and not not_attempted and not first_code,
         added=added, failed=failed, not_attempted=not_attempted, **({"skipped": list(skipped)} if skipped else {}))
    return first_code


def main():
    if sys.version_info < (3, 11):
        emit("error", code=69, reason="python3.11_or_newer_required")
        return 69
    os.umask(0o027)
    parser = argparse.ArgumentParser(description="Read an authorized request JSON from stdin; no arbitrary commands/URLs as flags.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate", action="store_true", help="Validate JSON offline without downloads or filesystem writes")
    mode.add_argument("--lookup", action="store_true",
                      help='Search/inspect sources from {"search": [...], "urls": [...]}; downloads nothing')
    mode.add_argument("--library", nargs="?", const=1, type=int, metavar="PAGE",
                      help=f"Print one page ({LIBRARY_PAGE} releases) of releases and their tags; reads no stdin")
    mode.add_argument("--retag", action="store_true",
                      help='Fix tags/covers from {"changes": [...]}; audio is not re-downloaded')
    mode.add_argument("--remove", action="store_true",
                      help='Move {"releases": [...]} to the library trash; nothing is deleted')
    mode.add_argument("--playlists", action="store_true",
                      help="Wait for the scan, then refresh the smart playlists as their owner; reads no stdin")
    mode.add_argument("--lyrics", action="store_true",
                      help="Retry synced lyrics for every track without them; reads no stdin")
    mode.add_argument("--discover", action="store_true",
                      help="List songs to add: playlist queue, radio candidates, stale discoveries; reads no stdin")
    mode.add_argument("--seeds", action="store_true", help='Merge taste seeds {"artists": [...], "songs": [...], "notes": ...}')
    mode.add_argument("--queue", action="store_true", help="Queue the songs of a playlist CSV (Exportify) for import")
    args = parser.parse_args()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        if args.library is not None:
            return library_page(args.library)
        if args.discover:
            return discover()
        if args.lyrics:
            return missing_lyrics()
        if args.playlists:
            return refresh_playlists()
        limit = MAX_PLAYLIST if args.queue else MAX_REQUEST
        raw = sys.stdin.buffer.read(limit + 1)
        if len(raw) > limit:
            raise Failure("request_too_large")
        if args.queue:
            return queue_playlist(raw)
        if args.seeds:
            return merge_seeds(validate_seeds(json.loads(raw)))
        if args.lookup:
            lookup(validate_lookup(json.loads(raw), resolve=True))
            return 0
        if args.retag:
            return retag(validate_changes(json.loads(raw), resolve=True))
        if args.remove:
            return remove(validate_removal(json.loads(raw)))
        releases = parse(json.loads(raw), resolve=not args.validate)
        skipped = check_library(releases)
        if args.validate:
            emit("valid", releases=[describe(r) for r in releases],
                 tracks=sum(len(r["tracks"]) for r in releases), **({"skipped": skipped} if skipped else {}))
            return 0
        return import_all(releases, skipped)
    except Failure as exc:
        emit("error", code=exc.code, reason=str(exc), **({"song": exc.item} if exc.item else {}))
        return exc.code
    except (ValueError, TypeError, UnicodeError):
        emit("error", code=65, reason="invalid_request_or_response")
        return 65
    except OSError:
        emit("error", code=73, reason="filesystem_or_runtime_error")
        return 73
    except Exception:
        # Never dump provider response URLs or secrets into the agent transcript.
        emit("error", code=70, reason="unexpected_internal_error")
        return 70


if __name__ == "__main__":
    sys.exit(main())
