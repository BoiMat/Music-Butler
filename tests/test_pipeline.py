"""Offline tests. Generated sine waves are test data, NOT downloaded music."""
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("butler", ROOT / "scripts/music_butler.py")
assert spec is not None and spec.loader is not None
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def request():
    return {"kind": "song", "artist": "Test Artist", "album": "Test Album", "format": "mp3", "quality": "192",
            "tracks": [{"title": "Test Tone", "url": "https://archive.org/download/test/tone.wav"}]}


class Contract(unittest.TestCase):
    def test_valid_song_and_album(self):
        self.assertEqual(b.validate(request())["kind"], "song")
        r = request()
        r["kind"] = "album"
        r["tracks"].append({**r["tracks"][0], "url": "https://archive.org/download/test/two.wav"})
        self.assertEqual(len(b.validate(r)["tracks"]), 2)

    def test_bad_inputs(self):
        for change in [{"kind": "artist"}, {"format": "wav"}, {"quality": "--exec"},
                       {"tracks": []}, {"extra": "x"}, {"artist": "x\nx"}]:
            with self.subTest(change=change), self.assertRaises(b.Failure):
                b.validate({**request(), **change})
        for address in ["file:///etc/passwd", "http://archive.org/a", "https://localhost/a",
                        "https://archive.org.evil.test/a", "https://x@archive.org/a",
                        "https://archive.org:8443/a", "--exec=x", "https://archive.org/a|x",
                        "https://archive.org/a?token=secret"]:
            r = request()
            r["tracks"][0]["url"] = address
            with self.subTest(address=address), self.assertRaises((b.Failure, ValueError)):
                b.validate(r)

    def test_private_dns(self):
        with patch.object(b.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaisesRegex(b.Failure, "non_public"):
                b.validate(request(), resolve=True)

    def test_slug_and_shell_characters(self):
        for value in ["../../etc/passwd", "$(touch /tmp/pwned)", "漢" * 100, "..."]:
            slug = b.slug(value)
            self.assertNotIn("/", slug)
            self.assertLessEqual(len(slug.encode()), 80)
            self.assertNotIn(slug, (".", "..", ""))

    def test_cli_stdin(self):
        with patch.object(b.sys, "version_info", (3, 9)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(b.main(), 69)
        p = subprocess.run([str(ROOT / "scripts/download_music.sh"), "--validate"],
                           input=json.dumps(request()), text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(json.loads(p.stdout)["event"], "valid")
        p = subprocess.run([str(ROOT / "scripts/download_music.sh"), "--validate"],
                           input="not json", text=True, capture_output=True)
        self.assertEqual(p.returncode, 65)

    def test_cancel_kills_media_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "media.pid"
            child = f"import os,time,pathlib; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)"
            code = (f"import sys,signal,os; sys.path.insert(0,{str(ROOT / 'scripts')!r}); import music_butler as b\n"
                    "signal.signal(signal.SIGTERM,b.interrupted)\n"
                    f"try: b.run([sys.executable,'-c',{child!r}],{directory!r},dict(os.environ))\n"
                    "except b.Failure as exc: sys.exit(exc.code)\n")
            proc = subprocess.Popen([sys.executable, "-c", code])
            try:
                deadline = time.monotonic() + 5
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pidfile.exists(), "media process did not become ready")
                pid = int(pidfile.read_text())
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=5), 143)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_no_redirect(self):
        with self.assertRaises(b.Failure):
            b.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")

    def test_songs_format_groups_releases(self):
        songs = {"songs": [
            {"artist": "A", "title": "Single One", "url": "https://youtu.be/one"},
            {"artist": "B", "title": "Track 1", "album": "LP", "url": "https://archive.org/download/lp/1.flac"},
            {"artist": "A", "title": "Single Two", "url": "https://music.youtube.com/watch?v=two", "album": " "},
            {"artist": "b", "title": "Track 2", "album": "lp", "url": "https://archive.org/download/lp/2.flac",
             "artwork_url": ""}]}
        releases = b.parse(songs)
        self.assertEqual([(r["kind"], r["artist"], r["album"], [t["title"] for t in r["tracks"]]) for r in releases], [
            ("song", "A", "Single One", ["Single One"]),
            ("album", "B", "LP", ["Track 1", "Track 2"]),
            ("song", "A", "Single Two", ["Single Two"])])
        self.assertTrue(all(r["format"] == "mp3" and r["quality"] == "192" for r in releases))
        self.assertNotIn("artwork_url", releases[1]["tracks"][1])
        flac = b.parse({**songs, "format": "flac"})
        self.assertTrue(all(r["quality"] == "5" for r in flac))

    def test_songs_format_errors_name_the_song(self):
        good = {"artist": "A", "title": "T", "url": "https://youtu.be/one"}
        for songs, reason, item in [
                ([good, {**good, "url": "https://evil.test/x"}], "source_url_not_allowed", 2),
                ([good, {**good, "title": "Other"}], "duplicate_source_url", 2),
                ([{**good, "kind": "song"}], "invalid_song_keys", 1),
                ([{**good, "artist": ""}], "invalid_artist", 1)]:
            with self.subTest(reason=reason), self.assertRaises(b.Failure) as cm:
                b.parse({"songs": songs})
            self.assertEqual((str(cm.exception), cm.exception.item), (reason, item))
        for bad in [{"songs": []}, {"songs": [good] * 101}, {"songs": [good], "kind": "song"},
                    {"songs": [good], "format": "wav"}]:
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(b.Failure):
                b.parse(bad)

    def test_cli_songs_validate(self):
        body = {"songs": [{"artist": "A", "title": "T", "url": "https://youtu.be/one"},
                          {"artist": "A", "title": "U", "url": "https://youtu.be/two"}]}
        p = subprocess.run([str(ROOT / "scripts/download_music.sh"), "--validate"],
                           input=json.dumps(body), text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        event = json.loads(p.stdout)
        self.assertEqual((event["event"], len(event["releases"]), event["tracks"]), ("valid", 2, 2))
        body["songs"][1]["url"] = "http://youtu.be/two"
        p = subprocess.run([str(ROOT / "scripts/download_music.sh"), "--validate"],
                           input=json.dumps(body), text=True, capture_output=True)
        self.assertEqual(p.returncode, 65)
        self.assertEqual(json.loads(p.stdout)["song"], 2)

    def test_allowed_hosts_tolerates_spaces(self):
        with patch.dict(os.environ, {"DOWNLOAD_ALLOWED_HOSTS": "archive.org, YouTube.com ,"}):
            self.assertEqual(b.allowed_hosts(), {"archive.org", "youtube.com"})
            r = b.validate({**request(), "tracks": [{"title": "YT", "url": "https://youtube.com/watch?v=x"}]})
            self.assertEqual(len(r["tracks"]), 1)

    def test_artwork_redirect_policy(self):
        handler = b.AllowlistRedirect({"archive.org", "upload.wikimedia.org"})
        req = urllib.request.Request("https://archive.org/download/item/cover.jpg")
        public = [(2, 1, 6, "", ("207.241.224.2", 443))]
        with patch.object(b.socket, "getaddrinfo", return_value=public):
            new = handler.redirect_request(req, None, 302, "Found", {}, "https://ia800.us.archive.org/1/items/item/cover.jpg")
            self.assertEqual(new.full_url, "https://ia800.us.archive.org/1/items/item/cover.jpg")
            for target in ["https://evil.test/cover.jpg", "http://ia800.us.archive.org/cover.jpg",
                           "https://ia800.us.archive.org:8443/cover.jpg", "https://archive.org.evil.test/x"]:
                with self.subTest(target=target), self.assertRaises(b.Failure):
                    handler.redirect_request(req, None, 302, "Found", {}, target)
        with patch.object(b.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 443))]):
            with self.assertRaisesRegex(b.Failure, "non_public"):
                handler.redirect_request(req, None, 302, "Found", {}, "https://ia800.us.archive.org/cover.jpg")
        without_archive = b.AllowlistRedirect({"upload.wikimedia.org"})
        with self.assertRaises(b.Failure):
            without_archive.redirect_request(req, None, 302, "Found", {}, "https://ia800.us.archive.org/cover.jpg")


class NetworkMocks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_lyrics_synced_missing_plain_and_rate_limit(self):
        with patch.dict(os.environ, {"LRCLIB_USER_AGENT": "offline-test"}), patch.object(b.time, "sleep"):
            lyrics = b.Lyrics(self.root)
            dest = self.root / "test.mp3"
            with patch.object(b, "fetch", return_value=b'{"syncedLyrics":"[00:01.00]Test"}') as fetch:
                self.assertEqual(lyrics.get(request(), request()["tracks"][0], 2, dest), "synced")
                self.assertIn("duration=2", fetch.call_args.args[0])
                self.assertEqual(dest.with_suffix(".lrc").read_text(), "[00:01.00]Test")
            with patch.object(b, "fetch", return_value=b'{"plainLyrics":"Not synced"}'):
                self.assertEqual(lyrics.get(request(), request()["tracks"][0], 2, dest), "unavailable")
            with patch.object(b, "fetch", side_effect=urllib.error.HTTPError("", 404, "", {}, None)):
                self.assertEqual(lyrics.get(request(), request()["tracks"][0], 2, dest), "unavailable")
            with patch.object(b, "fetch", side_effect=urllib.error.HTTPError("", 429, "", {"Retry-After": "120"}, None)) as fetch:
                self.assertEqual(lyrics.get(request(), request()["tracks"][0], 2, dest), "rate_limited")
                self.assertEqual(lyrics.get(request(), request()["tracks"][0], 2, dest), "rate_limited")
                self.assertEqual(fetch.call_count, 1)
                self.assertGreater(float(lyrics.cooldown.read_text()), time.time())

    def test_validate_lookup(self):
        self.assertEqual(b.validate_lookup({"search": "Daft Punk Get Lucky"}),
                         {"search": ["Daft Punk Get Lucky"], "urls": []})
        self.assertEqual(b.validate_lookup({"urls": ["https://youtu.be/abc"]})["urls"], ["https://youtu.be/abc"])
        for bad, item in [({"search": ["ok", "x\ny"]}, 2), ({"search": ["ok"], "urls": ["https://evil.test/a"]}, 2),
                          ({"search": [""]}, 1)]:
            with self.subTest(bad=bad), self.assertRaises(b.Failure) as cm:
                b.validate_lookup(bad)
            self.assertEqual(cm.exception.item, item)
        for bad in [{}, {"search": []}, {"search": ["a"] * 11}, {"search": ["a"], "q": 1}, ["a"]]:
            with self.subTest(bad=bad), self.assertRaises(b.Failure):
                b.validate_lookup(bad)
        with patch.dict(os.environ, {"DOWNLOAD_ALLOWED_HOSTS": "archive.org"}):
            with self.assertRaisesRegex(b.Failure, "youtube_search_not_allowed"):
                b.validate_lookup({"search": ["a"]})

    def test_lookup_returns_only_safe_fields(self):
        flat = [
            {"id": "AAAAAAAAAAA", "ie_key": "Youtube", "title": "Song (Official Video) [HD]", "channel": "ArtistVEVO",
             "duration": 245.0, "url": "https://rr1---sn.googlevideo.com/signed?sig=SECRET"},
            {"id": "BBBBBBBBBBB", "ie_key": "Youtube", "title": "Song", "channel": "Artist - Topic", "duration": 231},
            {"id": "CCCCCCCCCCC", "ie_key": "Youtube", "title": "Live now", "channel": "X", "live_status": "is_live"},
            {"id": "DDDDDDDDDDD", "ie_key": "Youtube", "title": "10 hour loop", "channel": "Y", "duration": 36000},
            {"id": "UCchannel-not-a-video", "ie_key": "YoutubeTab", "title": "Artist", "channel": "Artist"}]
        full = {"id": "BBBBBBBBBBB", "extractor_key": "Youtube", "title": "Song", "channel": "Artist - Topic",
                "duration": 231, "artist": "Artist", "track": "Song​", "album": "LP",
                "formats": [{"url": "https://rr1---sn.googlevideo.com/signed?sig=SECRET"}]}
        calls = []
        def fake_capture(argv, cwd, env, **kwargs):
            calls.append(argv)
            self.assertNotIn("NAVIDROME_PASSWORD_FILE", env)
            self.assertEqual(argv[:3], ["yt-dlp", "--ignore-config", "--no-plugin-dirs"])
            if argv[-1].startswith("ytsearch"):
                self.assertEqual(argv[-2:], ["--", "ytsearch5:Artist Song"])
                return flat
            if argv[-1] == "https://youtu.be/broken":
                raise b.Failure("media_tool_failed", 69)
            return [full]
        with patch.object(b, "capture", side_effect=fake_capture), contextlib.redirect_stdout(io.StringIO()) as output:
            b.lookup({"search": ["Artist Song"], "urls": ["https://youtu.be/BBBBBBBBBBB", "https://youtu.be/broken"]})
        line = output.getvalue().strip()
        self.assertNotIn("SECRET", line)
        self.assertNotIn("album", line)  # album would make the model group singles into a fake album
        event = json.loads(line)
        results = event["search"][0]["results"]
        self.assertEqual([r["url"] for r in results], ["https://www.youtube.com/watch?v=AAAAAAAAAAA",
                                                       "https://www.youtube.com/watch?v=BBBBBBBBBBB"])
        self.assertEqual(results[1], {"title": "Song", "channel": "Artist - Topic", "duration": 231,
                                      "url": "https://www.youtube.com/watch?v=BBBBBBBBBBB",
                                      "artist": "Artist", "track": "Song"})
        self.assertNotIn("track", results[0])  # only Topic results are enriched
        self.assertEqual(event["urls"][0]["track"], "Song")
        self.assertEqual(event["urls"][1], {"url": "https://youtu.be/broken", "error": "lookup_failed"})
        self.assertEqual(len(calls), 4)  # search, 1 Topic enrichment, 2 urls

    def test_lookup_search_failure_is_per_query_but_interrupt_propagates(self):
        with patch.object(b, "capture", side_effect=b.Failure("media_tool_timeout", 69)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            b.lookup({"search": ["a", "b"], "urls": []})
        self.assertEqual(json.loads(output.getvalue())["search"],
                         [{"query": "a", "error": "search_failed"}, {"query": "b", "error": "search_failed"}])
        with patch.object(b, "capture", side_effect=b.Failure("interrupted", 143)), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(b.Failure):
            b.lookup({"search": ["a"], "urls": []})

    def test_real_ytdlp_parser_accepts_lookup_flags(self):
        import yt_dlp
        argvs = []
        with patch.object(b, "capture", side_effect=lambda argv, *a, **k: argvs.append(argv) or []), \
                contextlib.redirect_stdout(io.StringIO()):
            b.lookup({"search": ["Artist Song"], "urls": ["https://youtu.be/BBBBBBBBBBB"]})
        self.assertEqual(len(argvs), 2)
        for argv in argvs:
            yt_dlp.parse_options(argv[1:])

    def test_capture_parses_child_json_lines(self):
        code = "import json; print(json.dumps({'a': 1})); print(); print(json.dumps({'b': 2}))"
        self.assertEqual(b.capture([sys.executable, "-c", code], self.root, dict(os.environ)), [{"a": 1}, {"b": 2}])
        with self.assertRaisesRegex(b.Failure, "response_too_large"):
            b.capture([sys.executable, "-c", "print('x' * 100)"], self.root, dict(os.environ), limit=10)

    def test_lyrics_search_fallback_is_strict(self):
        def hit(**changes):
            return {"artistName": "Test Artist", "trackName": "Test Tone", "duration": 181.0,
                    "syncedLyrics": "[00:01.00]Found", **changes}
        not_found = urllib.error.HTTPError("", 404, "", {}, None)
        with patch.dict(os.environ, {"LRCLIB_USER_AGENT": "offline-test"}), patch.object(b.time, "sleep"):
            dest = self.root / "test.mp3"
            track = request()["tracks"][0]
            for results, expected in [
                    ([hit(artistName="Someone Else"), hit(duration=190.0), hit(syncedLyrics=None), hit()], "synced"),
                    ([hit(artistName="Someone Else"), hit(duration=184.0)], "unavailable"),
                    ({"unexpected": "object"}, "unavailable")]:
                with self.subTest(expected=expected), patch.object(
                        b, "fetch", side_effect=[not_found, json.dumps(results).encode()]) as fetch:
                    self.assertEqual(b.Lyrics(self.root).get(request(), track, 180.4, dest), expected)
                    self.assertIn("/api/search?", fetch.call_args_list[1].args[0])
                    self.assertNotIn("album_name", fetch.call_args_list[1].args[0])
            self.assertEqual(dest.with_suffix(".lrc").read_text(), "[00:01.00]Found")

    def test_import_all_continues_past_bad_sources_and_stops_on_storage(self):
        releases = b.parse({"songs": [
            {"artist": "A", "title": f"S{n}", "url": f"https://youtu.be/s{n}"} for n in range(1, 5)]})
        track = {"title": "x", "lyrics": "synced", "artwork": "source_thumbnail"}
        outcomes = iter([{"scan": "scheduled", "tracks": [{**track, "title": "S1"}], "path": "/p"},
                         b.Failure("media_tool_failed", 69), b.Failure("insufficient_free_space", 73)])
        def fake_ingest(release):
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(b, "ingest", side_effect=fake_ingest) as ingest, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = b.import_all(releases)
        self.assertEqual(code, 69)
        self.assertEqual(ingest.call_count, 3)
        summary = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(summary["event"], "summary")
        self.assertFalse(summary["ok"])
        self.assertEqual([r["album"] for r in summary["added"]], ["S1"])
        self.assertEqual(summary["added"][0]["songs"], [{**track, "title": "S1"}])
        self.assertEqual([(r["album"], r["reason"]) for r in summary["failed"]],
                         [("S2", "media_tool_failed"), ("S3", "insufficient_free_space")])
        self.assertEqual([r["album"] for r in summary["not_attempted"]], ["S4"])

    def test_import_all_reports_published_release_as_added(self):
        releases = b.parse({"songs": [{"artist": "A", "title": "S1", "url": "https://youtu.be/s1"}]})
        failure = OSError("scan write failed")
        failure.butler_published = "/library/music/A--S1--id"
        with patch.object(b, "ingest", side_effect=failure), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(b.import_all(releases), 73)
        summary = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(summary["added"][0]["warning"], "published_check_before_retry")
        self.assertEqual(summary["failed"], [])

    def test_scan_post_token_and_errors(self):
        secret = self.root / "password"
        secret.write_text("test-only-password\n")
        secret.chmod(0o600)
        env = {"NAVIDROME_URL": "http://navidrome:4533", "NAVIDROME_USER": "scan-test",
               "NAVIDROME_PASSWORD_FILE": str(secret)}
        with patch.dict(os.environ, env), patch.object(b, "fetch", return_value=b'{"subsonic-response":{"status":"ok","scanStatus":{}}}') as fetch:
            self.assertEqual(b.scan(), "requested")
            self.assertTrue(fetch.call_args.args[0].endswith("/rest/startScan.view"))
            body = urllib.parse.parse_qs(fetch.call_args.kwargs["data"].decode())
            self.assertNotIn("p", body)
            self.assertEqual(body["t"][0], hashlib.md5(("test-only-password" + body["s"][0]).encode()).hexdigest())
        with patch.dict(os.environ, env), patch.object(b, "fetch", return_value=b'{"subsonic-response":{"status":"failed","error":{"code":50}}}'):
            self.assertEqual(b.scan(), "scheduled_after_scan_error")
        secret.chmod(0o644)
        with patch.dict(os.environ, env), patch.object(b, "fetch") as fetch:
            self.assertEqual(b.scan(), "scheduled_after_scan_error")
            fetch.assert_not_called()
        with patch.dict(os.environ, {"NAVIDROME_USER": ""}):
            self.assertEqual(b.scan(), "scheduled")

    def test_ytdlp_argv_is_not_shell(self):
        r = b.validate(request())
        def fake_run(argv, work, env):
            self.assertEqual(argv[0], "yt-dlp")
            self.assertEqual(argv[-2:], ["--", r["tracks"][0]["url"]])
            self.assertIn("--ignore-config", argv)
            self.assertIn("--write-thumbnail", argv)
            self.assertIn("thumbnail:" + str(work / "thumb.%(ext)s"), argv)
            self.assertEqual(argv[argv.index("--user-agent") + 1], f"music-butler/{b.VERSION}")
            (work / "source.flac").write_bytes(b"fixture")
        # The default agent is asserted, so ignore any DOWNLOAD_USER_AGENT in the tester's shell.
        with patch.dict(os.environ), patch.object(b, "run", side_effect=fake_run):
            os.environ.pop("DOWNLOAD_USER_AGENT", None)
            _, provider = b.acquire(r["tracks"][0], r, self.root, {})
            self.assertEqual(provider, "yt-dlp")

    def test_real_ytdlp_parser_accepts_flags(self):
        import yt_dlp
        r = b.validate({**request(), "tracks": [
            {"title": "YT", "url": "https://www.youtube.com/watch?v=fixture1234"}]})
        work = self.root / "work"
        work.mkdir()
        def fake_run(argv, cwd, env):
            yt_dlp.parse_options(argv[1:])
            self.assertNotIn("--user-agent", argv)  # YouTube keeps yt-dlp's default.
            self.assertEqual(argv[-1], r["tracks"][0]["url"])
            (cwd / "source.flac").write_bytes(b"test fixture")
        with patch.object(b, "run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            _, provider = b.acquire(r["tracks"][0], r, work, {})
        self.assertEqual(provider, "yt-dlp")

    def test_optional_features_never_swallow_cancellation(self):
        secret = self.root / "password"
        secret.write_text("test-only")
        secret.chmod(0o600)
        env = {"LRCLIB_USER_AGENT": "offline-test", "NAVIDROME_URL": "http://navidrome:4533",
               "NAVIDROME_USER": "test", "NAVIDROME_PASSWORD_FILE": str(secret)}
        with patch.dict(os.environ, env), patch.object(b, "fetch", side_effect=b.Failure("interrupted", 143)):
            for operation in [lambda: b.scan(),
                              lambda: b.artwork({"artwork_url": "https://archive.org/test"}, self.root, {}),
                              lambda: b.Lyrics(self.root).get(request(), request()["tracks"][0], 2, self.root / "a.mp3")]:
                with self.assertRaises(b.Failure) as cm:
                    operation()
                self.assertEqual(cm.exception.code, 143)
            with patch.object(b, "run", side_effect=b.Failure("interrupted", 143)) as run:
                with self.assertRaises(b.Failure):
                    b.acquire(request()["tracks"][0], request(), self.root, {})
                self.assertEqual(run.call_count, 1)  # Cancellation is never retried.


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required for real encoder tests")
class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Tiny generated fixtures fit in container tmpfs; capacity policy has a dedicated test.
        from types import SimpleNamespace
        disk = patch.object(b.shutil, "disk_usage", return_value=SimpleNamespace(free=4 * 1024 ** 3))
        disk.start()
        self.addCleanup(disk.stop)
        self.env = patch.dict(os.environ, {"LIBRARY_ROOT": str(self.root), "LRCLIB_USER_AGENT": "offline-test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sine = self.root / "fixture.flac"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(self.sine)], check=True)

    def acquire(self, track, request, work, env):
        self.assertEqual(list((self.root / "music").iterdir()), [])
        self.assertNotIn("NAVIDROME_PASSWORD_FILE", env)
        path = work / "source.flac"
        shutil.copyfile(self.sine, path)
        return path, "mock-generated-sine"

    def test_real_mp3_flac_tags_atomic_album(self):
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        for fmt, quality in [("mp3", "192"), ("flac", "8")]:
            r = request()
            r.update(kind="album", format=fmt, quality=quality)
            r["tracks"].append({**r["tracks"][0], "title": "../../$(touch marker)", "url": "https://archive.org/download/test/two.wav"})
            with patch.object(b, "acquire", side_effect=self.acquire), patch.object(b, "fetch", return_value=b'{"syncedLyrics":"[00:01.00]Test tone"}'), patch.object(b, "scan", return_value="scheduled"), contextlib.redirect_stdout(io.StringIO()) as output:
                b.ingest(b.validate(r))
            final = list((self.root / "music").iterdir())
            self.assertEqual(len(final), 1)
            files = sorted(final[0].glob("*." + fmt))
            self.assertEqual(len(files), 2)
            self.assertEqual(len(list(final[0].glob("*.lrc"))), 2)
            audio = MP3(files[0]) if fmt == "mp3" else FLAC(files[0])
            self.assertEqual(str(audio["TIT2"]) if fmt == "mp3" else audio["title"][0], "Test Tone")
            receipt = json.loads((final[0] / "provenance.json").read_text())
            self.assertEqual(receipt["tracks"][0]["sha256"], hashlib.sha256(files[0].read_bytes()).hexdigest())
            self.assertEqual(list((self.root / ".staging").iterdir()), [])
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["event"], "complete")
            shutil.rmtree(final[0])

    def test_album_failure_rolls_back(self):
        r = request()
        r["kind"] = "album"
        r["tracks"].append({**r["tracks"][0], "url": "https://archive.org/download/test/two.wav"})
        count = 0
        def fail_second(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise b.Failure("simulated_provider_failure", 69)
            return self.acquire(*args)
        with patch.object(b, "acquire", side_effect=fail_second), patch.object(b.Lyrics, "get", return_value="unavailable"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(b.Failure):
                b.ingest(b.validate(r))
        self.assertEqual(list((self.root / "music").iterdir()), [])
        self.assertEqual(list((self.root / ".staging").iterdir()), [])
        self.assertEqual(json.loads(next((self.root / "logs").glob("*.json")).read_text())["status"], "failed")

    def test_busy_and_symlink_refusal(self):
        with (self.root / ".ingest.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(b.Failure) as cm:
                b.ingest(b.validate(request()))
            self.assertEqual(cm.exception.code, 75)
        (self.root / "music").rmdir()
        (self.root / "music").symlink_to(self.root)
        with self.assertRaisesRegex(b.Failure, "symlink"):
            b.ingest(b.validate(request()))

    def test_collision_safe_repeat(self):
        def fixture(*args):
            path = args[2] / "source.flac"
            shutil.copyfile(self.sine, path)
            return path, "mock"
        with patch.object(b, "acquire", side_effect=fixture), patch.object(b.Lyrics, "get", return_value="unavailable"), patch.object(b, "scan", return_value="scheduled"), contextlib.redirect_stdout(io.StringIO()):
            b.ingest(b.validate(request()))
            b.ingest(b.validate(request()))
        self.assertEqual(len(list((self.root / "music").iterdir())), 2)

    def test_low_space_refuses_before_acquisition(self):
        from types import SimpleNamespace
        with patch.object(b.shutil, "disk_usage", return_value=SimpleNamespace(free=0)), patch.object(b, "acquire") as acquire:
            with self.assertRaisesRegex(b.Failure, "insufficient_free_space"):
                b.ingest(b.validate(request()))
            acquire.assert_not_called()

    def test_post_publish_failure_requires_reconciliation(self):
        with patch.object(b, "acquire", side_effect=self.acquire), patch.object(b.Lyrics, "get", return_value="disabled"), patch.object(b, "scan", side_effect=OSError("simulated")), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(OSError):
                b.ingest(b.validate(request()))
        self.assertEqual(len(list((self.root / "music").iterdir())), 1)
        self.assertIn("published_reconcile_before_retry", output.getvalue())
        receipt = json.loads(next((self.root / "logs").glob("*.json")).read_text())
        self.assertEqual(receipt["status"], "published_reconcile_required")

    def test_source_thumbnail_becomes_square_cover(self):
        work = self.root / "thumb-work"
        work.mkdir()
        # A 16:9 "YouTube" thumbnail: 320x180 with a red centre square and blue bars.
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=320x180",
                        "-vf", "drawbox=x=70:y=0:w=180:h=180:color=red:t=fill", "-frames:v", "1",
                        str(work / "thumb.png")], check=True)
        cover = b.source_thumbnail(work, dict(os.environ))
        self.assertTrue(cover.startswith(b"\xff\xd8"))
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "json", str(work / "thumb-cover.jpg")],
                               check=True, capture_output=True, text=True)
        stream = json.loads(probe.stdout)["streams"][0]
        self.assertEqual((stream["width"], stream["height"]), (180, 180))
        empty = self.root / "no-thumbnail"
        empty.mkdir()
        self.assertIsNone(b.source_thumbnail(empty, dict(os.environ)))

    def test_ingest_uses_thumbnail_when_no_artwork_url(self):
        from mutagen.mp3 import MP3
        def with_thumbnail(track, request, work, env):
            path, provider = self.acquire(track, request, work, env)
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x36",
                            "-frames:v", "1", str(work / "thumb.jpg")], check=True)
            return path, provider
        with patch.object(b, "acquire", side_effect=with_thumbnail), patch.object(b.Lyrics, "get", return_value="unavailable"), \
                patch.object(b, "scan", return_value="scheduled"), contextlib.redirect_stdout(io.StringIO()):
            result = b.ingest(b.validate(request()))
        self.assertEqual(result["tracks"][0]["artwork"], "source_thumbnail")
        self.assertEqual(result["tracks"][0]["title"], "Test Tone")
        audio = next(Path(result["path"]).glob("*.mp3"))
        self.assertEqual(len(MP3(audio).tags.getall("APIC")), 1)

    def test_real_artwork_encoding(self):
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        ppm = b"P6\n2 2\n255\n" + b"\xff\x00\x00" * 4
        with patch.object(b, "fetch", return_value=ppm) as fetch:
            cover = b.artwork({"artwork_url": "https://archive.org/test.ppm"}, self.root, dict(os.environ))
            self.assertTrue(fetch.call_args.kwargs["headers"]["User-Agent"].startswith("music-butler/"))
        self.assertTrue(cover.startswith(b"\xff\xd8"))
        for fmt in ("mp3", "flac"):
            r = b.validate({**request(), "format": fmt, "quality": "192" if fmt == "mp3" else "5"})
            dest = self.root / ("artwork." + fmt)
            b.encode(self.sine, dest, r, self.root, dict(os.environ))
            b.tag_audio(dest, r, r["tracks"][0], 1, 1, cover)
            if fmt == "mp3":
                self.assertEqual(MP3(dest).tags.getall("APIC")[0].data, cover)
            else:
                self.assertEqual(FLAC(dest).pictures[0].data, cover)


if __name__ == "__main__":
    unittest.main()
