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

    def test_songs_artists_and_year(self):
        song = {"artist": "HUNTR/X", "title": "Golden", "url": "https://youtu.be/one", "album": "Golden",
                "artists": ["HUNTR/X", "EJAE"], "year": "2025"}
        track = b.parse({"songs": [song]})[0]["tracks"][0]
        self.assertEqual((track["artists"], track["year"], track["song"]), (["HUNTR/X", "EJAE"], 2025, 1))
        for change, reason in [({"artists": "HUNTR/X"}, "invalid_artists"), ({"artists": []}, None),
                               ({"year": 99}, "invalid_year"), ({"year": True}, "invalid_year")]:
            with self.subTest(change=change):
                if reason is None:
                    self.assertNotIn("artists", b.parse({"songs": [{**song, **change}]})[0]["tracks"][0])
                    continue
                with self.assertRaises(b.Failure) as cm:
                    b.parse({"songs": [{**song, **change}]})
                self.assertEqual(str(cm.exception), reason)

    def test_retag_and_remove_requests(self):
        rid = "a" * 32
        self.assertEqual(b.validate_changes({"changes": [{"release": rid, "artist": "HUNTR/X", "year": None}]}),
                         [{"release": rid, "artist": "HUNTR/X", "year": None}])
        for bad, reason in [({"changes": [{"release": rid}]}, "invalid_change_keys"),
                            ({"changes": [{"release": "../x", "album": "A"}]}, "invalid_release_id"),
                            ({"changes": [{"release": rid, "track": 0, "title": "T"}]}, "invalid_track_number"),
                            ({"changes": [{"release": rid, "cover_from": "https://evil.test/x"}]}, "source_url_not_allowed"),
                            ({"changes": [{"release": rid, "cover_from": "https://youtu.be/x",
                                           "artwork_url": "https://archive.org/a.jpg"}]}, "one_cover_source_only"),
                            ({"changes": []}, "invalid_change_count")]:
            with self.subTest(reason=reason), self.assertRaises(b.Failure) as cm:
                b.validate_changes(bad)
            self.assertEqual(str(cm.exception), reason)
        self.assertEqual(b.validate_removal({"releases": [rid, rid]}), [rid])
        for bad in [{"releases": []}, {"releases": ["x"]}, {"ids": [rid]}]:
            with self.subTest(bad=bad), self.assertRaises(b.Failure):
                b.validate_removal(bad)

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

    def test_lyrics_get_with_only_plain_lyrics_falls_back_to_strict_search(self):
        req = request()
        track = req["tracks"][0]
        found = [{"artistName": "Elsewhere", "trackName": track["title"], "duration": 200,
                  "syncedLyrics": "[00:01.00]Wrong artist"},
                 {"artistName": req["artist"], "trackName": track["title"], "duration": 201,
                  "syncedLyrics": "[00:01.00]Right"}]
        def fake_fetch(address, **kwargs):
            if "/api/get?" in address:
                return b'{"plainLyrics": "Plain only", "instrumental": false}'
            return json.dumps(found).encode()
        with patch.dict(os.environ, {"LRCLIB_USER_AGENT": "offline-test"}), patch.object(b.time, "sleep"), \
                patch.object(b, "fetch", side_effect=fake_fetch):
            dest = self.root / "plain.mp3"
            self.assertEqual(b.Lyrics(self.root).get(req, track, 200, dest), "synced")
            self.assertEqual(dest.with_suffix(".lrc").read_text(), "[00:01.00]Right")

    def test_release_year_falls_back_to_the_description(self):
        entry = {"artists": ["Ed Sheeran"], "track": "Bad Habits", "album": "Bad Habits"}
        for description, year in [
                ("Provided to YouTube by X\n\nReleased on: 2021-06-25\n\n℗ 2020 Label", 2021),
                ("Bad Habits · Ed Sheeran\n\n℗ An Asylum release, ℗ 2021 Warner Music UK Limited", 2021),
                ("No dates here", None), (None, None)]:
            with self.subTest(description=description):
                self.assertEqual(b.release_song({**entry, "description": description}).get("year"), year)
        self.assertEqual(b.release_song({**entry, "release_year": 2019, "description": "℗ 2021 X"})["year"], 2019)

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
            {"id": "AAAAAAAAAAA", "ie_key": "Youtube", "title": "Artist - Song (Official Video) [HD]", "channel": "ArtistVEVO",
             "duration": 245.0, "url": "https://rr1---sn.googlevideo.com/signed?sig=SECRET"},
            {"id": "BBBBBBBBBBB", "ie_key": "Youtube", "title": "Song", "channel": "Artist - Topic", "duration": 231},
            {"id": "MMMMMMMMMMM", "ie_key": "Youtube", "title": "Song", "channel": "Artist - Topic"},
            {"id": "CCCCCCCCCCC", "ie_key": "Youtube", "title": "Live now", "channel": "X", "live_status": "is_live"},
            {"id": "DDDDDDDDDDD", "ie_key": "Youtube", "title": "10 hour loop", "channel": "Y", "duration": 36000},
            {"id": "UCchannel-not-a-video", "ie_key": "YoutubeTab", "title": "Artist", "channel": "Artist"}]
        music = [{"id": "MMMMMMMMMMM", "ie_key": "Youtube", "title": "Song", "url": "https://music.youtube.com/watch?v=MMMMMMMMMMM"},
                 {"id": "NNNNNNNNNNN", "ie_key": "Youtube", "title": "No metadata"}]
        full = {"BBBBBBBBBBB": {"id": "BBBBBBBBBBB", "extractor_key": "Youtube", "title": "Song", "channel": "Artist - Topic",
                                "duration": 231, "artist": "Artist", "track": "Song\u200b", "album": "LP",
                                "formats": [{"url": "https://rr1---sn.googlevideo.com/signed?sig=SECRET"}]},
                "MMMMMMMMMMM": {"id": "MMMMMMMMMMM", "extractor_key": "Youtube", "title": "Song", "channel": "Artist - Topic",
                                "duration": 230, "artists": ["Artist", "Guest"], "track": "Song", "album": "LP",
                                "release_year": 2020},
                "NNNNNNNNNNN": {"id": "NNNNNNNNNNN", "extractor_key": "Youtube", "title": "No metadata", "duration": 200}}
        calls = []
        def fake_capture(argv, cwd, env, **kwargs):
            calls.append(argv)
            self.assertNotIn("NAVIDROME_PASSWORD_FILE", env)
            self.assertEqual(argv[:3], ["yt-dlp", "--ignore-config", "--no-plugin-dirs"])
            if argv[-1].startswith("https://music.youtube.com/search?"):
                self.assertEqual(argv[-1], "https://music.youtube.com/search?q=Artist%20Song#songs")
                return music
            if argv[-1].startswith("ytsearch"):
                self.assertEqual(argv[-2:], ["--", "ytsearch3:Artist Song"])
                return flat
            if argv[-1] == "https://youtu.be/broken":
                raise b.Failure("media_tool_failed", 69)
            vid = argv[-1][-11:]
            return [full[vid]]
        with patch.object(b, "capture", side_effect=fake_capture), contextlib.redirect_stdout(io.StringIO()) as output:
            b.lookup({"search": ["Artist Song"], "urls": ["https://youtu.be/BBBBBBBBBBB", "https://youtu.be/broken"]})
        line = output.getvalue().strip()
        self.assertNotIn("SECRET", line)
        event = json.loads(line)
        results = event["search"][0]["results"]
        # YouTube Music first (only results with release metadata), then new videos.
        self.assertEqual([r["url"][-11:] for r in results], ["MMMMMMMMMMM", "AAAAAAAAAAA", "BBBBBBBBBBB"])
        self.assertEqual(results[0], {"title": "Song", "channel": "Artist - Topic", "duration": 230,
                                      "url": "https://www.youtube.com/watch?v=MMMMMMMMMMM", "names": "release",
                                      "song": {"artist": "Artist", "title": "Song", "artists": ["Artist", "Guest"],
                                               "album": "LP", "year": 2020}, "source": "youtube_music"})
        self.assertEqual(results[1]["names"], "guessed")
        self.assertEqual(results[1]["song"], {"artist": "Artist", "title": "Song"})
        self.assertEqual(results[2]["song"], {"artist": "Artist", "title": "Song", "album": "LP"})
        self.assertEqual(event["urls"][0]["song"]["title"], "Song")
        self.assertEqual(event["urls"][1], {"url": "https://youtu.be/broken", "error": "lookup_failed"})
        # music search, 2 music details, video search, 1 Topic enrichment, 2 urls
        self.assertEqual(len(calls), 7)

    def test_guess_song_cleans_video_titles(self):
        for title, song in [
                ("“Golden” Official Lyric Video | KPop Demon Hunters | Sony Animation", {"title": "Golden"}),
                ("Djo - End Of Beginning (Official Audio)", {"artist": "Djo", "title": "End Of Beginning"}),
                ("Daft Punk - Get Lucky (Official Audio) ft. Pharrell Williams, Nile Rodgers",
                 {"artist": "Daft Punk", "title": "Get Lucky (feat. Pharrell Williams, Nile Rodgers)"}),
                ("Dj Matrix, Paps'n'Skar - Fanno Bam - GABRY PONTE REMIX (feat. Vise)",
                 {"artist": "Dj Matrix, Paps'n'Skar", "title": "Fanno Bam (GABRY PONTE REMIX) (feat. Vise)"}),
                ("Queen – Bohemian Rhapsody (Official Video Remastered)", {"artist": "Queen", "title": "Bohemian Rhapsody"}),
                ("Artist - Song (Live Video) [HD]", {"artist": "Artist", "title": "Song (Live Video)"}),
                ("Artist - Track (Remastered 2011)", {"artist": "Artist", "title": "Track (Remastered 2011)"}),
                ("", None)]:
            with self.subTest(title=title):
                self.assertEqual(b.guess_song(title), song)

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
        self.assertEqual(len(argvs), 3)  # YouTube Music search, video search, url
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
            # LRCLIB often credits every performer; the main artist must be one of them.
            for credit, expected in [("Test Artist, Guest & Other", "synced"), ("Test Artist feat. Guest", "synced"),
                                     ("Not Test Artist", "unavailable"), ("Test Artistry", "unavailable")]:
                with self.subTest(credit=credit), patch.object(
                        b, "fetch", side_effect=[not_found, json.dumps([hit(artistName=credit)]).encode()]):
                    self.assertEqual(b.Lyrics(self.root).get(request(), track, 180.4, dest), expected)

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

    def test_listener_account_and_playlist_updates(self):
        for name, user in (("admin", "admin-user"), ("listener", "tempo-user")):
            (self.root / name).write_text("pw-" + name)
            (self.root / name).chmod(0o600)
        env = {"NAVIDROME_URL": "http://navidrome:4533", "NAVIDROME_USER": "admin-user",
               "NAVIDROME_PASSWORD_FILE": str(self.root / "admin"), "NAVIDROME_LISTENER_USER": "tempo-user",
               "NAVIDROME_LISTENER_PASSWORD_FILE": str(self.root / "listener")}
        with patch.dict(os.environ, env), patch.object(b, "fetch", return_value=b'{"subsonic-response":{"status":"ok"}}') as fetch:
            b.subsonic("ping", account="NAVIDROME_LISTENER")
            body = urllib.parse.parse_qs(fetch.call_args.kwargs["data"].decode())
            self.assertEqual(body["u"], ["tempo-user"])
            self.assertEqual(body["t"][0], hashlib.md5(("pw-listener" + body["s"][0]).encode()).hexdigest())
        with patch.dict(os.environ, {"NAVIDROME_LISTENER_USER": ""}):
            self.assertIsNone(b.subsonic("ping", account="NAVIDROME_LISTENER"))
        old = self.root / "Discoveries to rate.nsp"
        old.write_text('{"old": true}')
        b.ensure_playlists(self.root)
        self.assertEqual(json.loads(old.read_text())["all"][1], {"inTheLast": {"dateadded": b.STALE_DAYS}})
        self.assertTrue((self.root / "Discover Weekly.nsp").is_file())

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


def radio_entry(vid, title, channel, duration=200):
    return {"id": vid, "ie_key": "Youtube", "title": title, "channel": channel, "duration": duration}


class Discovery(unittest.TestCase):
    def test_song_key_ignores_versions_credits_and_spelling(self):
        self.assertEqual(b.song_key("Daft Punk", "Get Lucky (Radio Edit - feat. Pharrell Williams and Nile Rodgers)"),
                         b.song_key("Daft Punk", "Get Lucky"))
        self.assertEqual(b.song_key("HUNTR/X, EJAE & AUDREY NUNA", "Golden"), b.song_key("Huntr X", "Golden"))
        self.assertEqual(b.song_key("Shakira, Burna Boy", "Dai Dai - Live"), b.song_key("Shakira", "Dai Dai"))
        self.assertNotEqual(b.song_key("Djo", "End of Beginning"), b.song_key("Djo", "Delete Ya"))

    def test_radio_candidates_keep_songs_and_drop_noise(self):
        entries = [radio_entry("AAAAAAAAAAA", "Takedown", "HUNTR/X - Topic"),
                   radio_entry("BBBBBBBBBBB", "Taylor Swift - Shake It Off (Official Video)", "Taylor Swift"),
                   radio_entry("CCCCCCCCCCC", "Gabriela", "KATSEYE"),
                   radio_entry("DDDDDDDDDDD", "KPop Demon Hunters - Takedown (Fan Edit)", "Nick", 200),
                   radio_entry("EEEEEEEEEEE", "Short clip", "X - Topic", 63),
                   radio_entry("FFFFFFFFFFF", "Sia - Unstoppable (Official Video - Live from the Tour)", "Sia"),
                   radio_entry("GGGGGGGGGGG", "Golden (Cover by Fun Squad)", "The Fun Squad"),
                   radio_entry("HHHHHHHHHHH", "Soda Pop (Sped Up)", "Saja Boys - Topic"),
                   radio_entry("IIIIIIIIIII", "Live Your Life", "T.I. - Topic")]
        got = [c for c in map(b.radio_candidate, entries) if c]
        self.assertEqual([(c["artist"], c["title"], c["names"]) for c in got],
                         [("HUNTR/X", "Takedown", "release"), ("Taylor Swift", "Shake It Off", "guessed"),
                          ("KATSEYE", "Gabriela", "guessed"), ("T.I.", "Live Your Life", "release")])
        self.assertEqual(got[0]["url"], "https://www.youtube.com/watch?v=AAAAAAAAAAA")

    def test_radio_reads_the_songs_radio(self):
        with patch.object(b, "capture", return_value=[radio_entry("AAAAAAAAAAA", "Takedown", "HUNTR/X - Topic")]) as cap:
            self.assertEqual(len(b.radio("9_bTl2vvYQg", "/tmp", {})), 1)
        argv = cap.call_args.args[0]
        self.assertEqual(argv[-2:], ["--", "https://www.youtube.com/watch?v=9_bTl2vvYQg&list=RDAMVM9_bTl2vvYQg"])
        self.assertIn("--flat-playlist", argv)

    def test_real_ytdlp_parser_accepts_radio_flags(self):
        import yt_dlp
        with patch.object(b, "capture", side_effect=lambda argv, cwd, env: yt_dlp.parse_options(argv[1:]) and []):
            b.radio("9_bTl2vvYQg", "/tmp", {})
            b.first_song("Lucio Battisti", "/tmp", {})

    def test_score_ranks_caps_and_skips(self):
        def c(artist, title, vid):
            return {"artist": artist, "title": title, "url": "https://www.youtube.com/watch?v=" + vid,
                    "duration": 200, "names": "release"}
        loved, liked = {"name": "Loved", "weight": 3}, {"name": "Liked", "weight": 1}
        radios = [(loved, [c("A", "One", "AAAAAAAAAAA"), c("A", "Two", "BBBBBBBBBBB"), c("A", "Three", "CCCCCCCCCCC"),
                           c("Known", "Song", "DDDDDDDDDDD"), c("Nope", "X", "EEEEEEEEEEE"), c("Seen", "Y", "FFFFFFFFFFF")]),
                  (liked, [c("B", "Both", "GGGGGGGGGGG"), c("A", "Two (Radio Edit)", "HHHHHHHHHHH")])]
        ranked = b.score(radios, {b.song_key("Known", "Song")}, {"FFFFFFFFFFF"}, {b.norm("Nope")})
        self.assertEqual([(r["artist"], r["title"]) for r in ranked], [("A", "Two"), ("A", "One"), ("B", "Both")])
        titles = {b.norm("Both"): {b.norm("B.")}, b.norm("One"): {b.norm("Someone else")}}
        self.assertEqual([r["title"] for r in b.score(radios, set(), set(), set(), titles)], ["Two", "One", "Song", "X", "Y"])
        self.assertEqual(ranked[0]["because"], ["Loved", "Liked"])
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])

    def test_pick_seeds_moves_from_user_seeds_to_listening(self):
        users = [{"name": f"user{n}", "weight": 2} for n in range(3)]
        weak = [{"name": f"lib{n}", "weight": 1} for n in range(20)]
        chosen = b.pick_seeds(weak, users, b.random.Random(1))
        self.assertEqual(len(chosen), b.MAX_RADIO_SEEDS)
        self.assertEqual({s["name"] for s in chosen[:3]}, {"user0", "user1", "user2"})
        strong = [{"name": f"star{n}", "weight": 3} for n in range(b.MAX_RADIO_SEEDS)]
        chosen = b.pick_seeds(strong + weak, users, b.random.Random(1))
        self.assertTrue(all(s["name"].startswith("star") for s in chosen[:b.TOP_SEEDS]))

    def test_parse_playlist_exportify_and_other_csvs(self):
        exportify = ("﻿Track URI,Track Name,Album Name,Artist Name(s),Release Date,Duration (ms)\n"
                     "spotify:track:1,Emozioni,Emozioni,Lucio Battisti,1970-01-01,245000\n"
                     "spotify:track:2,\"Get Lucky (feat. Pharrell Williams)\",Random Access Memories,"
                     "\"Daft Punk;Pharrell Williams\",2013,369000\n"
                     "spotify:track:3,,,,,\n").encode()
        rows = b.parse_playlist(exportify)
        self.assertEqual(rows[0], {"artist": "Lucio Battisti", "title": "Emozioni", "search": "Lucio Battisti Emozioni",
                                   "album": "Emozioni", "year": 1970, "duration": 245})
        self.assertEqual((rows[1]["artists"], rows[1]["search"]), (["Daft Punk", "Pharrell Williams"], "Daft Punk Get Lucky"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(b.parse_playlist(b"Title,Artist\nSong,Someone, Else\n")[0]["artist"], "Someone")
        for bad in (b"Name\nx\n", b"Track Name,Artist Name(s)\n", b"\xff\xfe"):
            with self.subTest(bad=bad), self.assertRaises((b.Failure, UnicodeError)):
                b.parse_playlist(bad)

    def test_interleave_spreads_playlists(self):
        self.assertEqual(b.interleave(["a1", "a2", "a3", "a4"], ["b1", "b2"]), ["a1", "b1", "a2", "a3", "b2", "a4"])
        self.assertEqual(b.interleave([], ["b1"]), ["b1"])
        self.assertEqual(b.interleave(["a1"], []), ["a1"])

    def test_validate_seeds(self):
        seeds = b.validate_seeds({"artists": ["Lucio Battisti", {"name": "Mina", "note": "older songs"}],
                                  "songs": [{"artist": "Djo", "title": "End of Beginning"}], "notes": "70s Italian pop"})
        self.assertEqual(seeds["artists"], [{"name": "Lucio Battisti"}, {"name": "Mina", "note": "older songs"}])
        for bad in [{}, {"extra": 1}, {"artists": [{"name": "x", "url": "y"}]}, {"songs": [{"artist": "x"}]},
                    {"block_artists": "x"}, {"artists": ["x\ny"]}, {"clear_notes": "yes"}]:
            with self.subTest(bad=bad), self.assertRaises(b.Failure):
                b.validate_seeds(bad)

    def test_track_stats_and_stale(self):
        releases = [{"id": "a" * 32, "artist": "A", "album": "One", "origin": "discovery", "added": "2026-08-01T00:00:00+00:00",
                     "tracks": [{"n": 1, "title": "One", "duration": 200}]},
                    {"id": "b" * 32, "artist": "B", "album": "Two", "origin": "discovery", "added": "2026-08-01T00:00:00+00:00",
                     "tracks": [{"n": 1, "title": "Two", "duration": 180}]},
                    {"id": "c" * 32, "artist": "C", "album": "Three", "origin": "discovery", "added": "2026-09-25T00:00:00+00:00",
                     "tracks": [{"n": 1, "title": "Three", "duration": 180}]},
                    {"id": "d" * 32, "artist": "D", "album": "Four", "origin": "request", "added": "2026-01-01T00:00:00+00:00",
                     "tracks": [{"n": 1, "title": "Four", "duration": 180}]}]
        songs = [{"title": "Two", "album": "Two", "duration": 181, "playCount": 3},
                 {"title": "One", "album": "One", "duration": 200, "starred": None},
                 {"title": "Four", "album": "Four", "duration": 180, "userRating": 5},
                 {"title": "Unknown", "album": "X", "playCount": 9}]
        stats = b.track_stats(releases, songs)
        self.assertEqual(stats[("b" * 32, 1)], {"plays": 3, "starred": False, "rating": 0})
        self.assertEqual(stats[("d" * 32, 1)]["rating"], 5)
        today = b.datetime.datetime(2026, 9, 30, tzinfo=b.datetime.timezone.utc)
        self.assertEqual([r["id"] for r in b.stale_releases(releases, stats, today)], ["a" * 32])
        seeds = b.library_seeds([{**r, "tracks": [{**r["tracks"][0], "url": f"https://youtu.be/{r['id'][0] * 11}"}]}
                                 for r in releases], stats)
        self.assertEqual([(s["name"], s["weight"]) for s in seeds], [("B - Two", 2), ("D - Four", 4)])
        # 1 star removes any song at once, however new; 2 stars stays.
        stats[("c" * 32, 1)] = {"plays": 4, "starred": False, "rating": 1}
        stats[("d" * 32, 1)] = {"plays": 0, "starred": False, "rating": 1}
        self.assertEqual([r["id"] for r in b.stale_releases(releases, stats, today)], ["a" * 32, "c" * 32, "d" * 32])
        stats[("c" * 32, 1)]["rating"] = stats[("d" * 32, 1)]["rating"] = 2
        self.assertEqual([r["id"] for r in b.stale_releases(releases, stats, today)], ["a" * 32])
        # An old discovery played once but not liked or rated goes; twice stays.
        stats[("a" * 32, 1)] = {"plays": 1, "starred": False, "rating": 0}
        self.assertEqual([r["id"] for r in b.stale_releases(releases, stats, today)], ["a" * 32])
        stats[("a" * 32, 1)]["plays"] = 2
        self.assertEqual(b.stale_releases(releases, stats, today), [])
        self.assertEqual([b.seed_weight({"rating": r}, "request") for r in (1, 2, 3, 4, 5)], [0, 0, 1, 3, 4])
        self.assertEqual((b.seed_weight({"starred": True}, "discovery"), b.seed_weight({}, "discovery")), (4, 0))

    def test_listening_pages_and_fails_soft(self):
        page = {"status": "ok", "searchResult3": {"song": [{"title": "T", "album": "A", "playCount": 1}]}}
        with patch.object(b, "subsonic", return_value=page) as api:
            self.assertEqual(b.listening([{"id": "a" * 32, "album": "A", "tracks": [{"n": 1, "title": "T"}]}]),
                             {("a" * 32, 1): {"plays": 1, "starred": False, "rating": 0}})
            self.assertEqual(api.call_args.kwargs["account"], "NAVIDROME")
            with patch.dict(os.environ, {"NAVIDROME_LISTENER_USER": "tempo"}):
                b.listening([])
            self.assertEqual(api.call_args.kwargs["account"], "NAVIDROME_LISTENER")
        self.assertEqual(api.call_args.args[:2], ("search3", {"query": "", "songCount": 500, "songOffset": 0,
                                                              "artistCount": 0, "albumCount": 0}))
        with patch.object(b, "subsonic", return_value=None):
            self.assertIsNone(b.listening([]))
        with patch.object(b, "subsonic", side_effect=b.Failure("navidrome_api_error", 69)):
            self.assertIsNone(b.listening([]))
        with patch.object(b, "subsonic", side_effect=b.Failure("interrupted", 143)), self.assertRaises(b.Failure):
            b.listening([])


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

    def add(self, songs, fmt="mp3"):
        with patch.object(b, "acquire", side_effect=lambda t, r, w, e: self.acquire_any(w)), \
                patch.object(b.Lyrics, "get", return_value="unavailable"), patch.object(b, "scan", return_value="scheduled"), \
                contextlib.redirect_stdout(io.StringIO()):
            releases = b.parse({"songs": songs, "format": fmt})
            b.check_library(releases)
            return [b.ingest(r) for r in releases], releases

    def acquire_any(self, work):
        path = work / "source.flac"
        shutil.copyfile(self.sine, path)
        return path, "mock-generated-sine"

    def test_library_names_duplicates_retag_and_remove(self):
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        golden = {"artist": "HUNTR/X", "title": "Golden", "album": "Golden", "year": 2025,
                  "artists": ["HUNTR/X", "EJAE", "AUDREY NUNA"], "url": "https://youtu.be/AAAAAAAAAAA"}
        (first,), _ = self.add([golden])
        mp3 = next(Path(first["path"]).glob("*.mp3"))
        tags = MP3(mp3).tags
        self.assertEqual(tags.version[:2], (2, 4))
        self.assertEqual(str(tags["TPE1"]), "HUNTR/X, EJAE & AUDREY NUNA")
        self.assertEqual(tags["TXXX:ARTISTS"].text, ["HUNTR/X", "EJAE", "AUDREY NUNA"])
        self.assertEqual((str(tags["TPE2"]), str(tags["TDRC"])), ("HUNTR/X", "2025"))
        (lib,) = b.read_library()
        self.assertEqual((lib["artist"], lib["album"], lib["year"], lib["tracks"][0]["url"]),
                         ("HUNTR/X", "Golden", "2025", "https://youtu.be/AAAAAAAAAAA"))
        self.assertTrue(lib["tracks"][0]["cover"] is False and lib["tracks"][0]["lyrics"] is False)

        # Same song again (other upload, other spelling) is refused before any download.
        for dup in [{**golden, "url": "https://youtu.be/BBBBBBBBBBB", "artist": "Huntr X"},
                    {"artist": "Someone", "title": "Other", "url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"}]:
            with self.subTest(dup=dup), self.assertRaises(b.Failure) as cm:
                b.check_library(b.parse({"songs": [{"artist": "Z", "title": "First", "url": "https://youtu.be/CCCCCCCCCCC"}, dup]}))
            self.assertEqual((str(cm.exception), cm.exception.item), ("already_in_library", 2))
        # New song: library spelling is reused; a near miss is only a note.
        releases = b.parse({"songs": [{"artist": "huntr/x", "title": "Takedown", "album": "golden",
                                       "url": "https://youtu.be/DDDDDDDDDDD"},
                                      {"artist": "Huntrix", "title": "Soda Pop", "url": "https://youtu.be/EEEEEEEEEEE"}]})
        b.check_library(releases)
        self.assertEqual((releases[0]["artist"], releases[0]["album"]), ("HUNTR/X", "Golden"))
        self.assertEqual([n["note"] for n in releases[0]["notes"]], ["artist_spelling_from_library", "album_spelling_from_library"])
        self.assertEqual(releases[1]["notes"], [{"note": "similar_artist_in_library", "given": "Huntrix", "existing": "HUNTR/X"}])
        self.assertEqual(releases[1]["artist"], "Huntrix")

        # Retag: a new main artist keeps the featured artists; other tags stay.
        (flac_result,), _ = self.add([{"artist": "Djo", "title": "End Of Beginning (Official Audio)",
                                       "url": "https://youtu.be/FFFFFFFFFFF"}], fmt="flac")
        flac_id = Path(flac_result["path"]).name[-32:]
        with patch.object(b, "scan", return_value="requested"), contextlib.redirect_stdout(io.StringIO()) as out:
            code = b.retag(b.validate_changes({"changes": [
                {"release": lib["id"], "artist": "HUNTRIX"},
                {"release": flac_id, "track": 1, "title": "End of Beginning", "album": "DECIDE", "year": 2022},
                {"release": "f" * 32, "album": "Nope"}]}))
        summary = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(code, 65)
        self.assertEqual([c["change"] for c in summary["changed"]], [1, 2])
        self.assertEqual([(f["change"], f["reason"]) for f in summary["failed"]], [(3, "unknown_release")])
        self.assertEqual(summary["scan"], "requested")
        tags = MP3(mp3).tags
        self.assertEqual((str(tags["TPE2"]), str(tags["TPE1"]), tags["TXXX:ARTISTS"].text),
                         ("HUNTRIX", "HUNTRIX, EJAE & AUDREY NUNA", ["HUNTRIX", "EJAE", "AUDREY NUNA"]))
        self.assertEqual(str(tags["TIT2"]), "Golden")  # untouched
        flac = FLAC(next(Path(flac_result["path"]).glob("*.flac")))
        self.assertEqual((flac["title"], flac["album"], flac["date"], flac["artist"]),
                         (["End of Beginning"], ["DECIDE"], ["2022"], ["Djo"]))
        receipt = json.loads((Path(flac_result["path"]) / "provenance.json").read_text())
        self.assertEqual(receipt["retagged"][0]["change"]["title"], "End of Beginning")
        self.assertEqual([p.name for p in Path(flac_result["path"]).iterdir() if p.name.startswith(".")], [])

        # Remove moves the release to the trash; nothing is deleted.
        with patch.object(b, "scan", return_value="requested"), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(b.remove([lib["id"], "e" * 32]), 65)
        summary = json.loads(out.getvalue())
        self.assertEqual((summary["removed"], summary["failed"][0]["reason"]), ([lib["id"]], "unknown_release"))
        self.assertFalse(Path(first["path"]).exists())
        self.assertTrue((self.root / ".trash" / Path(first["path"]).name / mp3.name).is_file())
        self.assertEqual([r["id"] for r in b.read_library()], [flac_id])

    def test_retag_cover_and_busy_lock(self):
        (result,), _ = self.add([{"artist": "A", "title": "T", "url": "https://youtu.be/AAAAAAAAAAA"}])
        rid = Path(result["path"]).name[-32:]
        def thumbnail(argv, cwd, env, **kwargs):
            self.assertIn("--skip-download", argv)
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=64x36",
                            "-frames:v", "1", str(Path(cwd) / "thumb.jpg")], check=True)
        with patch.object(b, "run", side_effect=thumbnail) as run, patch.object(b, "source_thumbnail", return_value=b"\xff\xd8cover"), \
                patch.object(b, "scan", return_value="requested"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(b.retag([{"release": rid, "cover_from": "https://youtu.be/BBBBBBBBBBB"}]), 0)
        self.assertEqual(run.call_args.args[0][-2:], ["--", "https://youtu.be/BBBBBBBBBBB"])
        self.assertTrue(b.read_library()[0]["tracks"][0]["cover"])
        with open(self.root / ".ingest.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            for call in (lambda: b.retag([{"release": rid, "album": "X"}]), lambda: b.remove([rid])):
                with self.subTest(call=call), self.assertRaisesRegex(b.Failure, "busy_retry_later"):
                    call()

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

    def test_bulk_origin_tags_skips_and_library_fields(self):
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        (mp3_result, flac_result), _ = self.add([
            {"artist": "A", "title": "Found", "url": "https://youtu.be/AAAAAAAAAAA", "origin": "discovery"},
            {"artist": "B", "title": "Liked", "url": "https://youtu.be/BBBBBBBBBBB", "origin": "playlist"}])
        self.assertEqual(str(MP3(next(Path(mp3_result["path"]).glob("*.mp3"))).tags["COMM::eng"]), "music-butler:discovery")
        (req_result,), _ = self.add([{"artist": "C", "title": "Asked", "url": "https://youtu.be/CCCCCCCCCCC"}], fmt="flac")
        self.assertNotIn("comment", FLAC(next(Path(req_result["path"]).glob("*.flac"))))
        self.assertEqual(b.read_tags(next(Path(mp3_result["path"]).glob("*.mp3")))["comment"], "music-butler:discovery")
        lib = {r["artist"]: r for r in b.read_library()}
        self.assertEqual({a: r["origin"] for a, r in lib.items()}, {"A": "discovery", "B": "playlist", "C": "request"})
        self.assertTrue(lib["A"]["added"].startswith("20"))
        with self.assertRaises(b.Failure):
            b.parse({"songs": [{"artist": "A", "title": "x", "url": "https://youtu.be/DDDDDDDDDDD", "origin": "robot"}]})
        # A bulk add skips songs already there and keeps the rest.
        releases = b.parse({"origin": "discovery", "songs": [
            {"artist": "a", "title": "found", "url": "https://youtu.be/EEEEEEEEEEE"},
            {"artist": "New", "title": "Song", "url": "https://youtu.be/FFFFFFFFFFF"}]})
        self.assertEqual(b.check_library(releases), [{"artist": "A", "title": "found", "reason": "already_in_library"}])
        self.assertEqual([r["artist"] for r in releases], ["New"])
        with patch.object(b, "ingest", return_value={"scan": "scheduled", "tracks": [], "path": "/p"}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            b.import_all(releases, [{"artist": "A", "title": "found", "reason": "already_in_library"}])
        self.assertEqual(json.loads(out.getvalue())["skipped"][0]["title"], "found")

    def test_seeds_queue_discover_and_forget(self):
        (liked, found), _ = self.add([
            {"artist": "Known", "title": "Liked", "url": "https://youtu.be/AAAAAAAAAAA"},
            {"artist": "Old", "title": "Found", "url": "https://youtu.be/BBBBBBBBBBB", "origin": "discovery"}])
        found_id = Path(found["path"]).name[-32:]
        receipt = Path(found["path"]) / "provenance.json"
        data = json.loads(receipt.read_text())
        data["created"] = "2026-01-01T00:00:00+00:00"
        receipt.write_text(json.dumps(data))

        def run(fn, *args):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(fn(*args), 0)
            return json.loads(out.getvalue().splitlines()[-1])
        seeds = run(b.merge_seeds, b.validate_seeds({"artists": [{"name": "Mina", "note": "older songs"}, "Blocked"],
                                                     "notes": "Italian classics"}))
        seeds = run(b.merge_seeds, b.validate_seeds({"block_artists": ["Blocked"], "notes": "No trap."}))
        self.assertEqual(([a["name"] for a in seeds["artists"]], seeds["block_artists"]), (["Mina"], ["Blocked"]))
        self.assertEqual(seeds["notes"], "Italian classics\nNo trap.")  # notes add up, never replace
        seeds = run(b.merge_seeds, b.validate_seeds({"notes": "no trap"}))
        self.assertEqual(seeds["notes"], "Italian classics\nNo trap.")
        seeds = run(b.merge_seeds, b.validate_seeds({"clear_notes": True, "notes": "Italian classics"}))
        self.assertEqual(seeds["notes"], "Italian classics")
        csv = b"Track Name,Artist Name(s),Duration (ms)\nLiked,Known,200000\nQueued,Q,180000\nQueued,Q,180000\n"
        self.assertEqual(run(b.queue_playlist, csv), {"event": "queued", "queued": 1, "in_library": 1, "duplicate": 1,
                                                       "removed_before": 0, "queue": 1})

        radio = [radio_entry("CCCCCCCCCCC", "Fresh", "New - Topic"), radio_entry("AAAAAAAAAAA", "Liked", "Known - Topic"),
                 radio_entry("DDDDDDDDDDD", "Queued", "Q - Topic"), radio_entry("EEEEEEEEEEE", "Nope", "Blocked - Topic")]
        def fake_capture(argv, cwd, env):
            if "music.youtube.com/search" in argv[-1]:
                self.assertIn("Mina", urllib.parse.unquote(argv[-1]))
                return [radio_entry("MMMMMMMMMMM", "Grande grande grande", "Mina - Topic")]
            return radio
        songs = [{"title": "Liked", "album": "Liked", "duration": 2, "playCount": 5}]
        with patch.object(b, "capture", side_effect=fake_capture), \
                patch.object(b, "subsonic", return_value={"status": "ok", "searchResult3": {"song": songs}}):
            event = run(b.discover)
        self.assertEqual((event["library_songs"], event["listening"], event["queue_total"]), (2, "ok", 1))
        self.assertEqual(event["queue"], [{"artist": "Q", "title": "Queued", "duration": 180, "search": "Q Queued"}])
        self.assertEqual([(c["artist"], c["title"], c["new_artist"]) for c in event["candidates"]], [("New", "Fresh", True)])
        self.assertEqual(set(event["candidates"][0]["because"]), {"Known - Liked", "Mina"})
        self.assertEqual(event["taste"]["artists"], [{"name": "Mina", "note": "older songs"}])
        self.assertEqual([s["id"] for s in event["stale"]], [found_id])
        state = self.root / "discovery"
        self.assertEqual(json.loads((state / "seeds.json").read_text())["artists"][0]["video"], "MMMMMMMMMMM")
        self.assertEqual(json.loads((state / "queue.json").read_text())[0]["offered"], 1)
        for name in b.PLAYLISTS:
            self.assertEqual(json.loads((self.root / "music" / name).read_text())["all"][0],
                             {"contains": {"comment": "music-butler:discovery"}})

        # Removed songs are remembered and never offered again; no Navidrome means no pruning.
        with patch.object(b, "scan", return_value="requested"), contextlib.redirect_stdout(io.StringIO()):
            b.remove([found_id])
        history = json.loads((state / "history.json").read_text())
        self.assertEqual(history[b.song_key("Old", "Found")]["status"], "removed")
        radio.append(radio_entry("BBBBBBBBBBB", "Found", "Old - Topic"))
        with patch.object(b, "capture", side_effect=fake_capture), patch.object(b, "subsonic", return_value=None):
            event = run(b.discover)
        self.assertEqual([c["title"] for c in event["candidates"]], ["Fresh"])
        self.assertEqual((event["listening"], event["stale"]), ("unavailable", []))


class DownloadFailures(unittest.TestCase):
    def test_tool_error_keeps_last_error_line_without_urls(self):
        script = ("echo 'WARNING: slow https://example.com/w' >&2; "
                  "echo 'ERROR: [youtube] abc: HTTP Error 403 https://rr1.googlevideo.com/videoplayback?sig=SECRET' >&2; exit 1")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(b.Failure) as caught:
            b.run(["sh", "-c", script], tmp, {"PATH": os.environ["PATH"]})
        self.assertEqual(str(caught.exception), "media_tool_failed")
        self.assertEqual(caught.exception.detail, "ERROR: [youtube] abc: HTTP Error 403 <url>")

    def test_acquire_retries_a_failed_download_once(self):
        track = {"title": "Song", "url": "https://youtu.be/AAAAAAAAAAA"}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "source.webm.part").write_bytes(b"partial")
            outcomes = [b.Failure("media_tool_failed", 69, detail="ERROR: 403"), (work / "source.flac", "yt-dlp")]
            def fake_download(*args):
                result = outcomes.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
            with patch.object(b, "download", side_effect=fake_download), patch.object(b.time, "sleep") as sleep, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(b.acquire(track, {}, work, {}), (work / "source.flac", "yt-dlp"))
            sleep.assert_called_once_with(b.DOWNLOAD_RETRY_PAUSE)
            self.assertFalse((work / "source.webm.part").exists())
            warning = json.loads(output.getvalue())
            self.assertEqual((warning["reason"], warning["detail"]), ("download_retry", "ERROR: 403"))

    def test_acquire_gives_up_after_the_retry_and_skips_other_failures(self):
        track = {"title": "Song", "url": "https://youtu.be/AAAAAAAAAAA"}
        with tempfile.TemporaryDirectory() as tmp:
            for failure, calls in ((b.Failure("media_tool_failed", 69), 2), (b.Failure("no_audio_output", 69), 1)):
                with patch.object(b, "download", side_effect=failure) as download, patch.object(b.time, "sleep"), \
                        contextlib.redirect_stdout(io.StringIO()), self.assertRaises(b.Failure):
                    b.acquire(track, {}, Path(tmp), {})
                self.assertEqual(download.call_count, calls)

    def test_failed_release_reports_tool_detail(self):
        releases = b.parse({"songs": [{"artist": "A", "title": "S1", "url": "https://youtu.be/s1"}]})
        failure = b.Failure("media_tool_failed", 69, detail="ERROR: [youtube] s1: Sign in to confirm")
        with patch.object(b, "ingest", side_effect=failure), contextlib.redirect_stdout(io.StringIO()) as output:
            b.import_all(releases)
        summary = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(summary["failed"][0]["detail"], "ERROR: [youtube] s1: Sign in to confirm")


class LibraryPages(unittest.TestCase):
    def page(self, n, releases):
        with patch.object(b, "read_library", return_value=releases), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = b.library_page(n)
        return code, json.loads(output.getvalue())

    def test_pages_split_the_library_and_reject_missing_pages(self):
        releases = [{"id": f"{i:032x}"} for i in range(45)]
        code, first = self.page(1, releases)
        self.assertEqual((code, first["page"], first["pages"], first["releases_total"]), (0, 1, 3, 45))
        self.assertEqual([r["id"] for r in first["releases"]], [r["id"] for r in releases[:20]])
        self.assertEqual(self.page(3, releases)[1]["releases"], releases[40:])
        code, error = self.page(4, releases)
        self.assertEqual((code, error["reason"], error["pages"]), (64, "no_such_page", 3))

    def test_empty_library_has_one_empty_page(self):
        self.assertEqual(self.page(1, [])[1]["releases"], [])


class MissingLyrics(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.env = patch.dict(os.environ, {"LIBRARY_ROOT": str(self.root), "LRCLIB_USER_AGENT": "offline-test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def release(self, name, rid, files, tracks):
        folder = self.root / "music" / f"{name}--{rid}"
        folder.mkdir(parents=True)
        for f in files:
            (folder / f).write_bytes(b"")
        (folder / "provenance.json").write_text(json.dumps({"request": {"tracks": []}, "tracks": tracks}))
        return folder

    def test_retries_tracks_without_lyrics_and_records_the_result(self):
        a = self.release("A--One", "a" * 32, ["001-x.mp3"], [{"file": "001-x.mp3", "lyrics": "lookup_failed"}])
        c = self.release("C--Three", "c" * 32, ["001-z.mp3"], [{"file": "001-z.mp3", "lyrics": "unavailable"}])
        done = self.release("B--Two", "b" * 32, ["001-y.mp3", "001-y.lrc"], [{"file": "001-y.mp3", "lyrics": "synced"}])
        tags = {"001-x.mp3": "Song A", "001-z.mp3": "Song C"}
        def fake_get(lyrics, request, track, duration, dest):
            self.assertEqual((request["artist"], duration), ("Main", 200))
            if track["title"] == "Song A":
                dest.with_suffix(".lrc").write_text("[00:01.00] la")
                return "synced"
            return "unavailable"
        with patch.object(b, "read_tags", side_effect=lambda p: {"title": tags[p.name], "albumartist": "Main",
                                                                 "artist": "Main & Guest", "album": None, "duration": 200}), \
                patch.object(b.Lyrics, "get", autospec=True, side_effect=fake_get) as get, \
                patch.object(b, "scan", return_value="requested"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(b.missing_lyrics(), 0)
        self.assertEqual(get.call_count, 2)  # the release that has lyrics is skipped
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["found"], [{"release": "a" * 32, "artist": "Main", "title": "Song A"}])
        self.assertEqual([m["lyrics"] for m in summary["missing"]], ["unavailable"])
        self.assertEqual(summary["scan"], "requested")
        self.assertEqual(json.loads((a / "provenance.json").read_text())["tracks"][0]["lyrics"], "synced")
        self.assertEqual(json.loads((c / "provenance.json").read_text())["tracks"][0]["lyrics"], "unavailable")
        self.assertTrue((done / "001-y.lrc").exists())

    def test_rate_limit_stops_the_run(self):
        for n in "ab":
            self.release(f"{n}--x", n * 32, ["001-s.mp3"], [])
        with patch.object(b, "read_tags", return_value={"title": "S", "artist": "A", "duration": 100}), \
                patch.object(b.Lyrics, "get", return_value="rate_limited") as get, \
                patch.object(b, "scan") as scan, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(b.missing_lyrics(), 69)
        self.assertEqual(get.call_count, 1)
        scan.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["not_attempted"], [{"release": "b" * 32, "file": "001-s.mp3"}])

    def test_disabled_without_user_agent(self):
        with patch.dict(os.environ, {"LRCLIB_USER_AGENT": ""}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(b.missing_lyrics(), 64)
        self.assertEqual(json.loads(output.getvalue())["reason"], "lyrics_disabled")

    def test_lrclib_call_retries_dropped_connections_and_503_but_not_answers(self):
        lyrics = b.Lyrics(self.root)
        replies = [urllib.error.URLError("reset"), b'{"ok": 1}']
        def fake_fetch(*args, **kwargs):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        with patch.object(b, "fetch", side_effect=fake_fetch), patch.object(b.time, "sleep"):
            self.assertEqual(lyrics.call("get", {}), {"ok": 1})
        busy = urllib.error.HTTPError("u", 503, "busy", {}, None)
        replies.extend([busy, busy, b'{"ok": 2}'])
        with patch.object(b, "fetch", side_effect=fake_fetch), patch.object(b.time, "sleep") as sleep:
            self.assertEqual(lyrics.call("get", {}), {"ok": 2})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.5, *b.LYRICS_RETRY_PAUSES])
        with patch.object(b, "fetch", side_effect=busy) as fetch, patch.object(b.time, "sleep"), \
                self.assertRaises(urllib.error.HTTPError):
            lyrics.call("get", {})
        self.assertEqual(fetch.call_count, 3)
        not_found = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with patch.object(b, "fetch", side_effect=not_found) as fetch, patch.object(b.time, "sleep"), \
                self.assertRaises(urllib.error.HTTPError):
            lyrics.call("get", {})
        self.assertEqual(fetch.call_count, 1)


class RefreshPlaylists(unittest.TestCase):
    def test_waits_for_the_scan_then_opens_each_smart_playlist(self):
        calls = []
        scanning = iter([True, False])
        def fake_subsonic(endpoint, params=None, **kwargs):
            calls.append((endpoint, (params or {}).get("id")))
            if endpoint == "getScanStatus":
                return {"scanStatus": {"scanning": next(scanning)}}
            if endpoint == "getPlaylists":
                return {"playlists": {"playlist": [{"id": "1", "name": "Discover Weekly"},
                                                   {"id": "2", "name": "Mine"},
                                                   {"id": "3", "name": "Discoveries to rate"}]}}
            return {"playlist": {"entry": [{}] * int(params["id"])}}
        with patch.object(b, "scan", return_value="requested"), patch.object(b, "subsonic", side_effect=fake_subsonic), \
                patch.object(b.time, "sleep"), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(b.refresh_playlists(), 0)
        self.assertEqual(calls, [("getScanStatus", None), ("getScanStatus", None), ("getPlaylists", None),
                                 ("getPlaylist", "1"), ("getPlaylist", "3")])
        summary = json.loads(output.getvalue())
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["refreshed"], [{"name": "Discover Weekly", "songs": 1},
                                                {"name": "Discoveries to rate", "songs": 3}])

    def test_without_admin_account_nothing_happens(self):
        with patch.object(b, "scan", return_value="scheduled"), patch.object(b, "subsonic") as subsonic, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(b.refresh_playlists(), 0)
        subsonic.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["reason"], "admin_account_not_configured")


if __name__ == "__main__":
    unittest.main()
