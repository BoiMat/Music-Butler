"""scripts/playlist.sh: inbox pickup, the background batch loop and its reports."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def summary(added, remaining, songs=30, in_library=10, failed=(), not_matched=(), stopped=None):
    out = {"event": "summary", "playlist": "mix", "added": list(added), "failed": list(failed),
           "not_matched": list(not_matched), "remaining": remaining, "songs": songs, "in_library": in_library}
    if stopped:
        out["stopped"] = stopped
    return json.dumps(out)


class PlaylistRunner(unittest.TestCase):
    def setUp(self):
        # A copy of the script in its own checkout, so the real requests/ is never touched.
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        (self.home / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "playlist.sh", self.home / "scripts" / "playlist.sh")
        (self.home / "requests").mkdir()
        self.inbox = self.home / "inbox"
        self.inbox.mkdir()
        self.sent = self.home / "sent.txt"
        notify = self.home / "notify.sh"
        notify.write_text(f'#!/bin/sh\ncat >> "{self.sent}"\necho "----" >> "{self.sent}"\n')
        notify.chmod(0o755)
        self.env = dict(os.environ, PLAYLIST_INBOX=str(self.inbox), PLAYLIST_NOTIFY=str(notify),
                        PLAYLIST_BUSY_WAIT="0")

    def script(self, *args, **env):
        return subprocess.run([str(self.home / "scripts" / "playlist.sh"), *args], env={**self.env, **env},
                              text=True, capture_output=True, timeout=60)

    def importer(self, *lines):
        """A fake importer that prints the next summary line on each call."""
        (self.home / "outputs.json").write_text(json.dumps(list(lines)))
        fake = self.home / "importer.py"
        fake.write_text(f"""import json, sys
sys.stdin.read()
path = {str(self.home / "outputs.json")!r}
lines = json.load(open(path))
print("noise line")
print(lines.pop(0))
json.dump(lines, open(path, "w"))
""")
        return f"python3 {fake}"

    def test_inbox_names_new_csvs_and_skips_known_ones(self):
        (self.inbox / "doc_a1b2c3d4e5f6_My Summer Mix!.csv").write_text("Track Name,Artist Name(s)\nA,B\nC,D\n")
        (self.inbox / "Best Hits 2026.csv").write_text("Track Name,Artist Name(s)\nE,F\n")
        (self.inbox / "notes.pdf").write_text("not a playlist")
        out = self.script("inbox").stdout
        self.assertIn("New playlist: my-summer-mix (2 songs)", out)
        self.assertIn("New playlist: best-hits-2026 (1 songs)", out)
        self.assertEqual(self.script("inbox").stdout, "No new playlist in the inbox.\n")
        # Same name, different songs: a new id, the old playlist is kept.
        (self.inbox / "Best Hits 2026.csv").write_text("Track Name,Artist Name(s)\nG,H\n")
        self.assertIn("New playlist: best-hits-2026-2", self.script("inbox").stdout)
        self.assertEqual((self.home / "requests" / "spotify-best-hits-2026.csv").read_text().splitlines()[1], "E,F")

    def test_run_reports_every_batch_and_a_total_at_the_end(self):
        (self.home / "requests" / "spotify-mix.csv").write_text("Track Name,Artist Name(s)\nA,B\n")
        importer = self.importer(
            summary(["A - 1", "A - 2"], 18, failed=[{"song": "B - 3", "reason": "media_tool_failed", "detail": "ERROR: 403"}]),
            summary([], 18, stopped="busy_retry_later"),
            summary(["C - 4"], 0, in_library=13, not_matched=["D - 5"]))
        result = self.script("run", "mix", PLAYLIST_IMPORTER=importer)
        self.assertEqual(result.returncode, 0, result.stderr)
        reports = self.sent.read_text().split("----\n")[:-1]
        self.assertEqual(len(reports), 2)  # the busy wait is silent
        self.assertIn("Playlist mix: +2 imported, 10 of 30 songs in the library, 18 to go.", reports[0])
        self.assertNotIn("A - 1", reports[0])  # imported songs are counted, not listed
        self.assertIn("Failed: B - 3 (ERROR: 403)", reports[0])
        self.assertIn("+1 imported, 13 of 30 songs in the library, import finished.", reports[1])
        self.assertIn("No certain match: D - 5", reports[1])
        self.assertIn("In total 1 failed and 1 had no certain match", reports[1])
        self.assertIn("import finished", (self.home / "requests" / "spotify-mix.log").read_text())
        self.assertEqual(sorted(p.name for p in (self.home / "requests").glob(".playlist-*")), [])

    def test_importer_error_stops_with_a_message(self):
        (self.home / "requests" / "spotify-mix.csv").write_text("Track Name,Artist Name(s)\nA,B\n")
        importer = self.importer(json.dumps({"event": "error", "code": 73, "reason": "insufficient_free_space"}))
        self.script("run", "mix", PLAYLIST_IMPORTER=importer)
        self.assertIn("stopped, the importer said insufficient_free_space (code 73)", self.sent.read_text())

    def test_start_returns_at_once_and_reports_progress_while_running(self):
        (self.home / "requests" / "spotify-mix.csv").write_text("Track Name,Artist Name(s)\nA,B\nC,D\n")
        slow = self.home / "slow.sh"
        slow.write_text(f'#!/bin/sh\ncat > /dev/null\nsleep 2\necho \'{summary(["A - B"], 0, songs=2, in_library=2)}\'\n')
        slow.chmod(0o755)
        started = time.time()
        first = self.script("start", "mix", PLAYLIST_IMPORTER=str(slow))
        self.assertLess(time.time() - started, 2)
        self.assertIn("Started importing playlist mix (2 songs", first.stdout)
        self.assertIn("to the chat", first.stdout)
        time.sleep(0.5)
        self.assertIn("already being imported", self.script("start", "mix", PLAYLIST_IMPORTER=str(slow)).stdout)
        for _ in range(50):
            if self.sent.exists() and "import finished" in self.sent.read_text():
                break
            time.sleep(0.2)
        self.assertIn("2 of 2 songs in the library, import finished.", self.sent.read_text())

    def test_bad_ids_and_missing_files(self):
        self.assertEqual(self.script("start", "../x").returncode, 64)
        self.assertEqual(self.script("start", "nope").returncode, 66)
        self.assertEqual(self.script("inbox", PLAYLIST_INBOX="").returncode, 64)


if __name__ == "__main__":
    unittest.main()
