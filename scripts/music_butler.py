#!/usr/bin/env python3
"""Constrained, transactional audio ingestion. No shell evaluation."""
import argparse
import contextlib
import datetime
import email.utils
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
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
# Batch imports continue past these per-release failures; anything else stops the batch.
CONTINUE_CODES = {65, 69}


class Failure(Exception):
    def __init__(self, reason, code=65, item=None):
        super().__init__(reason)
        self.code = code
        self.item = item  # 1-based song number in a "songs" request


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


def validate_songs(data, resolve=False):
    """Flat song list, the easy format for agents: {"songs": [{artist, title, url, album?, artwork_url?}]}.

    Songs that share artist and album become one album release, in list order.
    A song without an album is its own single, named after the song.
    """
    if set(data) - {"songs", "format", "quality"}:
        raise Failure("invalid_request_keys")
    fmt, quality = encoding(data)
    songs = data.get("songs")
    if not isinstance(songs, list) or not 1 <= len(songs) <= MAX_SONGS:
        raise Failure("invalid_song_count")
    hosts = allowed_hosts()
    releases, seen = {}, set()
    for n, song in enumerate(songs, 1):
        try:
            if not isinstance(song, dict) or set(song) - {"artist", "title", "url", "album", "artwork_url"}:
                raise Failure("invalid_song_keys")
            artist = text(song.get("artist"), "artist")
            title = text(song.get("title"), "title")
            album = None if blank(song.get("album")) else text(song.get("album"), "album")
            t = {"title": title, "url": url(song.get("url"), hosts, resolve)}
            if t["url"] in seen:
                raise Failure("duplicate_source_url")
            seen.add(t["url"])
            if not blank(song.get("artwork_url")):
                t["artwork_url"] = url(song["artwork_url"], hosts, resolve)
        except Failure as exc:
            exc.item = n
            raise
        key = ("album", artist.casefold(), album.casefold()) if album else ("single", n)
        release = releases.setdefault(key, {"artist": artist, "album": album or title,
                                            "format": fmt, "quality": quality, "tracks": []})
        release["tracks"].append(t)
    out = list(releases.values())
    for release in out:
        release["kind"] = "song" if len(release["tracks"]) == 1 else "album"
    return out


def parse(data, resolve=False):
    """Any accepted request -> list of validated releases."""
    if isinstance(data, dict) and "songs" in data:
        return validate_songs(data, resolve)
    return [validate(data, resolve)]


def describe(release):
    return {"artist": release["artist"], "album": release["album"], "kind": release["kind"],
            "titles": [t["title"] for t in release["tracks"]]}


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


def run(argv, cwd, env, timeout=900, stdout=None):
    # Child process group guarantees timeout/cancellation also kills ffmpeg.
    # Provider output may contain expiring signed URLs: never publish it.
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=stdout or log, stderr=log, start_new_session=True)
        try:
            if proc.wait(timeout=timeout) != 0:
                raise Failure("media_tool_failed", 69)
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


def acquire(track, request, work, env):
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
SEARCH_RESULTS = 5
ENRICH_PER_SEARCH = 2
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


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
    # YouTube Music uploads ("Artist - Topic") carry clean artist/track names.
    for key in ("artist", "track"):
        if clip(entry.get(key)):
            summary[key] = clip(entry.get(key))
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

        def search(q):
            entries = capture([*ytdlp_base(), "--flat-playlist", "--dump-json", "--",
                               f"ytsearch{SEARCH_RESULTS}:{q}"], tmp, env)
            results = [r for r in map(video_summary, entries) if r]
            topic = [r for r in results if (r["channel"] or "").endswith(" - Topic")][:ENRICH_PER_SEARCH]
            for r in topic:
                extra = guarded(lambda: video_summary(details(r["url"])) or {}, "details_failed")
                r.update({k: extra[k] for k in ("artist", "track") if k in extra})
            return {"query": q, "results": results}

        def one_url(link):
            return video_summary(details(link), given_url=link) or {"url": link, "error": "not_importable"}

        searches = [{"query": q, **guarded(lambda: search(q), "search_failed")} for q in query["search"]]
        urls = [{"url": u, **guarded(lambda: one_url(u), "lookup_failed")} for u in query["urls"]]
    emit("lookup", search=searches, urls=urls)


def tag_audio(path, request, track, number, total, cover):
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, APIC
    from mutagen.flac import FLAC, Picture
    if path.suffix == ".mp3":
        audio = MP3(path)
        tags = ID3()
        for cls, value in ((TIT2, track["title"]), (TPE1, request["artist"]),
                           (TPE2, request["artist"]), (TALB, request["album"]),
                           (TRCK, f"{number}/{total}")):
            tags.add(cls(encoding=3, text=[value]))
        if cover:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        tags.save(path, v2_version=3)
    else:
        audio = FLAC(path)
        audio.clear()
        audio.update({"title": track["title"], "artist": request["artist"],
                      "albumartist": request["artist"], "album": request["album"],
                      "tracknumber": str(number), "tracktotal": str(total)})
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", cover
            audio.add_picture(pic)
        audio.save()
    duration = audio.info.length
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
            synced = body.get("syncedLyrics")
            if isinstance(synced, str) and re.search(r"\[\d{2,}:\d{2}(?:\.\d+)?\]", synced):
                dest.with_suffix(".lrc").write_text(synced, encoding="utf-8")
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

    def call(self, endpoint, params):
        time.sleep(0.5)  # sequential lookups with ≥500 ms spacing
        return json.loads(fetch(f"https://lrclib.net/api/{endpoint}?" + urllib.parse.urlencode(params),
                                headers={"User-Agent": self.agent}))

    def search(self, artist, title, duration):
        """Exact artist+title (case-insensitive) within ±2 s that has synced lyrics, else None."""
        results = self.call("search", {"artist_name": artist, "track_name": title})
        if not isinstance(results, list):
            return None
        for item in results:
            if (isinstance(item, dict) and isinstance(item.get("duration"), (int, float))
                    and abs(item["duration"] - duration) <= 2
                    and str(item.get("artistName", "")).casefold() == artist.casefold()
                    and str(item.get("trackName", "")).casefold() == title.casefold()
                    and isinstance(item.get("syncedLyrics"), str)):
                return item
        return None


def scan():
    base = os.environ.get("NAVIDROME_URL", "").rstrip("/")
    user = os.environ.get("NAVIDROME_USER", "")
    password_file = os.environ.get("NAVIDROME_PASSWORD_FILE", "")
    if not base or not user or not password_file:
        return "scheduled"
    try:
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
                                      "c": "music-butler", "f": "json"}).encode()
        result = json.loads(fetch(base + "/rest/startScan.view", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}))
        response = result.get("subsonic-response", {})
        if response.get("status") != "ok" or not isinstance(response.get("scanStatus"), dict):
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


def ingest(request):
    root = Path(os.environ.get("LIBRARY_ROOT", "./storage/library")).absolute()
    # Parent mounts/ownership are provisioned by the administrator, not the agent.
    if not root.is_dir() or root.is_symlink():
        raise Failure("provision_library_root_first", 73)
    root = root.resolve()
    music = safe_dir(root / "music")
    staging = safe_dir(root / ".staging")
    logs = safe_dir(root / "logs")
    if music.stat().st_dev != staging.stat().st_dev:
        raise Failure("staging_must_share_filesystem", 73)
    fd = os.open(root / ".ingest.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Failure("busy_retry_later", 75) from exc
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
            with contextlib.suppress(OSError):
                log_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
            if published:
                emit("warning", reason="published_reconcile_before_retry", path=str(final))
                with contextlib.suppress(AttributeError):
                    exc.butler_published = str(final)
            raise


def interrupted(signum, frame):
    raise Failure("interrupted", 128 + signum)


def import_all(releases):
    """Import releases one by one and end with ONE `summary` line an agent can report from.

    Exit 0 only if every release was added. Invalid/unavailable sources (65/69) skip
    to the next release; storage, lock, interrupt or internal errors stop the batch.
    """
    added, failed, first_code = [], [], 0
    not_attempted = []
    for i, release in enumerate(releases):
        try:
            result = ingest(release)
            added.append({**describe(release), "scan": result["scan"],
                          "songs": [{"title": t["title"], "lyrics": t["lyrics"], "artwork": t["artwork"]}
                                    for t in result["tracks"]]})
            continue
        except Failure as exc:
            code, reason, published = exc.code, str(exc), getattr(exc, "butler_published", None)
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
            failed.append({**describe(release), "reason": reason, "code": code})
        if code not in CONTINUE_CODES:
            not_attempted = [describe(r) for r in releases[i + 1:]]
            break
    emit("summary", ok=not failed and not not_attempted and not first_code,
         added=added, failed=failed, not_attempted=not_attempted)
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
    args = parser.parse_args()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise Failure("request_too_large")
        if args.lookup:
            lookup(validate_lookup(json.loads(raw), resolve=True))
            return 0
        releases = parse(json.loads(raw), resolve=not args.validate)
        if args.validate:
            emit("valid", releases=[describe(r) for r in releases],
                 tracks=sum(len(r["tracks"]) for r in releases))
            return 0
        return import_all(releases)
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
