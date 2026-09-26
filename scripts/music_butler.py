#!/usr/bin/env python3
"""Constrained, transactional authorized-audio ingestion. No shell evaluation."""
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
DEFAULT_HOSTS = "archive.org,upload.wikimedia.org,www.youtube.com,youtube.com,youtu.be"
YOUTUBE_HOSTS = {"www.youtube.com", "youtube.com", "youtu.be"}


class Failure(Exception):
    def __init__(self, reason, code=65):
        super().__init__(reason)
        self.code = code


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


def validate(data, resolve=False):
    if not isinstance(data, dict) or set(data) - {"kind", "artist", "album", "format", "quality", "tracks"}:
        raise Failure("invalid_request_keys")
    if data.get("kind") not in ("song", "album"):
        raise Failure("invalid_kind")
    out = {k: text(data.get(k), k) for k in ("artist", "album")}
    out["kind"] = data["kind"]
    out["format"] = data.get("format", "mp3")
    if out["format"] not in ("mp3", "flac"):
        raise Failure("invalid_format")
    out["quality"] = data.get("quality", "192" if out["format"] == "mp3" else "5")
    if out["quality"] not in (("128", "192", "256", "320") if out["format"] == "mp3" else tuple(map(str, range(9)))):
        raise Failure("invalid_quality")
    tracks = data.get("tracks")
    if not isinstance(tracks, list) or not 1 <= len(tracks) <= 100 or (out["kind"] == "song" and len(tracks) != 1):
        raise Failure("invalid_track_count")
    hosts = set(os.environ.get("DOWNLOAD_ALLOWED_HOSTS", DEFAULT_HOSTS).split(","))
    out["tracks"] = []
    seen = set()
    for track in tracks:
        if not isinstance(track, dict) or set(track) - {"title", "url", "authorization", "license", "spotify_url", "artwork_url"}:
            raise Failure("invalid_track_keys")
        t = {"title": text(track.get("title"), "title"),
             "url": url(track.get("url"), hosts, resolve),
             "authorization": text(track.get("authorization"), "authorization", 1000)}
        if track.get("license") not in ("public-domain", "user-authorized"):
            raise Failure("authorization_required")
        t["license"] = track["license"]
        if t["url"] in seen:
            raise Failure("duplicate_source_url")
        seen.add(t["url"])
        if "spotify_url" in track:
            s = url(track["spotify_url"], {"open.spotify.com"}, resolve)
            if not re.fullmatch(r"/track/[A-Za-z0-9]{22}", urllib.parse.urlsplit(s).path):
                raise Failure("spotify_track_url_required")
            if urllib.parse.urlsplit(t["url"]).hostname not in YOUTUBE_HOSTS:
                raise Failure("spotdl_requires_explicit_youtube_pair")
            t["spotify_url"] = s
        if "artwork_url" in track:
            t["artwork_url"] = url(track["artwork_url"], hosts, resolve)
        out["tracks"].append(t)
    return out


def slug(value):
    value = re.sub(r"[^\w -]", "_", value, flags=re.UNICODE).strip(" ._-")
    # Limit bytes as well as characters, including for multibyte filesystems.
    return value.encode("utf-8")[:80].decode("utf-8", "ignore") or "untitled"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure("http_redirect_refused", 69)


def fetch(address, *, data=None, headers=None, limit=MAX_RESPONSE):
    req = urllib.request.Request(address, data=data, headers=headers or {})
    # Never leak scan credentials through ambient proxies or redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=20) as response:
        body = response.read(limit + 1)
        if len(body) > limit:
            raise Failure("response_too_large", 69)
        return body


def run(argv, cwd, env):
    # Child process group guarantees timeout/cancellation also kills ffmpeg.
    # Provider output may contain expiring signed URLs: never publish it.
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)
        try:
            if proc.wait(timeout=900) != 0:
                raise Failure("media_tool_failed", 69)
        except subprocess.TimeoutExpired as exc:
            raise Failure("media_tool_timeout", 69) from exc
        finally:
            # A provider can leave descendants even after the parent exits.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def encode(source, dest, request, cwd, env):
    args = ["ffmpeg", "-nostdin", "-v", "error", "-n", "-protocol_whitelist", "file,pipe",
            "-f", "flac", "-i", str(source),
            "-map", "0:a:0", "-vn", "-map_metadata", "-1"]
    if request["format"] == "mp3":
        args += ["-c:a", "libmp3lame", "-b:a", request["quality"] + "k"]
    else:
        args += ["-c:a", "flac", "-compression_level", request["quality"]]
    run(args + [str(dest)], cwd, env)


def spotdl_config(env):
    """Opt-in official API only; never use spotDL's default unofficial client."""
    path = Path(os.environ.get("SPOTDL_CREDENTIALS_FILE", "/run/music-secrets/spotify.json"))
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise Failure("spotdl_credentials_unavailable", 69)
        data = json.loads(path.read_text(encoding="utf-8"))
        if set(data) != {"client_id", "client_secret"}:
            raise Failure("invalid_spotdl_credentials", 69)
        config: dict = {key: text(data[key], key, 200) for key in data}
        config.update(load_config=True, use_official_api=True, no_cache=True,
                      lyrics_providers=[], cookie_file=None, user_auth=False)
        # v4.5.2 uses HOME rather than XDG_CONFIG_HOME; legacy path works on both OSes.
        directory = Path(env["HOME"]) / ".spotdl"
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / "config.json"
        target.write_text(json.dumps(config), encoding="utf-8")
        target.chmod(0o600)
    except (OSError, ValueError, TypeError) as exc:
        raise Failure("spotdl_credentials_unavailable", 69) from exc


def download_agent():
    return text(os.environ.get("DOWNLOAD_USER_AGENT") or f"music-butler/{VERSION}", "download_user_agent")


def acquire(track, request, work, env):
    source = work / "source.flac"
    provider = "yt-dlp"
    if track.get("spotify_url") and os.environ.get("ENABLE_SPOTDL", "false") == "true":
        if shutil.which("spotdl"):
            try:
                spotdl_config(env)
                run(["spotdl", "download", track["url"] + "|" + track["spotify_url"],
                     "--use-official-api", "--config", "--no-cache", "--skip-album-art",
                     "--format", "flac", "--output", str(source), "--threads", "1",
                     "--yt-dlp-args", "--js-runtimes node --no-geo-bypass --no-playlist --socket-timeout 30",
                     "--max-retries", "1", "--lyrics", "--log-level", "ERROR"], work, env)
                if not source.is_file():
                    raise Failure("spotdl_no_output", 69)
                provider = "spotdl"
            except Failure as exc:
                if exc.code in (130, 143):
                    raise
                emit("warning", reason="spotdl_failed_using_same_authorized_url")
                for item in work.iterdir():
                    if item.is_file():
                        item.unlink()
        else:
            emit("warning", reason="spotdl_not_installed_using_yt_dlp")
    if not source.exists():
        # Archives such as Wikimedia reject bots posing as browsers; YouTube needs yt-dlp's default.
        agent = []
        if urllib.parse.urlsplit(track["url"]).hostname not in YOUTUBE_HOSTS:
            agent = ["--user-agent", download_agent()]
        run(["yt-dlp", "--ignore-config", "--no-plugin-dirs", "--no-playlist",
             "--no-progress", "--no-warnings", "--no-cache-dir", "--no-geo-bypass",
             "--js-runtimes", "node", "--socket-timeout", "30", "--retries", "2", "--fragment-retries", "2",
             "--max-filesize", "500M", "--match-filter", "!is_live & duration <? 3601",
             "-f", "bestaudio/best", "-x", "--audio-format", "flac", *agent,
             "-o", str(work / "source.%(ext)s"), "--", track["url"]], work, env)
    if not source.is_file() or source.is_symlink():
        raise Failure("no_audio_output", 69)
    return source, provider


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
        raw.write_bytes(fetch(track["artwork_url"], headers={"User-Agent": download_agent()}, limit=5242880))
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
            time.sleep(0.5)
            query = urllib.parse.urlencode({"artist_name": request["artist"],
                "track_name": track["title"], "album_name": request["album"], "duration": round(duration)})
            body = json.loads(fetch("https://lrclib.net/api/get?" + query,
                                    headers={"User-Agent": self.agent}))
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
                    duration = tag_audio(dest, request, track, n, len(request["tracks"]), cover)
                    lyric_status = lyrics.get(request, track, duration, dest)
                    with dest.open("rb") as audio_file:
                        digest = hashlib.file_digest(audio_file, "sha256").hexdigest()
                    receipt["tracks"].append({"file": dest.name, "sha256": digest, "provider": provider,
                        "lyrics": lyric_status, "duration": duration, "artwork": "embedded" if cover else "unavailable"})
                    emit("track_staged", number=n, lyrics=lyric_status, artwork=bool(cover))
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
        except BaseException:
            receipt["status"] = "published_reconcile_required" if published else "failed"
            receipt["path"] = str(final) if published else None
            with contextlib.suppress(OSError):
                log_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
            if published:
                emit("warning", reason="published_reconcile_before_retry", path=str(final))
            raise


def interrupted(signum, frame):
    raise Failure("interrupted", 128 + signum)


def main():
    if sys.version_info < (3, 11):
        emit("error", code=69, reason="python3.11_or_newer_required")
        return 69
    os.umask(0o027)
    parser = argparse.ArgumentParser(description="Read an authorized request JSON from stdin; no arbitrary commands/URLs as flags.")
    parser.add_argument("--validate", action="store_true", help="Validate JSON offline without downloads or filesystem writes")
    args = parser.parse_args()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise Failure("request_too_large")
        request = validate(json.loads(raw), resolve=not args.validate)
        if args.validate:
            emit("valid", kind=request["kind"], tracks=len(request["tracks"]))
        else:
            ingest(request)
        return 0
    except Failure as exc:
        emit("error", code=exc.code, reason=str(exc))
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
