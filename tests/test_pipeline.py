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
