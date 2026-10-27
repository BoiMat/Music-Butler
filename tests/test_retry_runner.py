"""scripts/retry.sh: the background retry of songs YouTube's bot check held back."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def summary(added=(), failed=(), deferred=()):
    out = {"event": "summary", "ok": not failed and not deferred, "added": list(added), "failed": list(failed),
           "not_attempted": []}
    if deferred:
        out.update(deferred=list(deferred), retry="automatic")
    return json.dumps(out)


def release(title):
    return {"artist": "A", "album": title, "kind": "song", "titles": [title]}


class RetryRunner(unittest.TestCase):
    def setUp(self):
        # A copy of the script in its own checkout, so the real requests/ is never touched.
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        (self.home / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "retry.sh", self.home / "scripts" / "retry.sh")
        (self.home / "requests").mkdir()
        self.library = self.home / "library"
        (self.library / "discovery").mkdir(parents=True)
        self.sent = self.home / "sent.txt"
        notify = self.home / "notify.sh"
        notify.write_text(f'#!/bin/sh\ncat >> "{self.sent}"\necho "----" >> "{self.sent}"\n')
        notify.chmod(0o755)
        self.env = dict(os.environ, MUSIC_LIBRARY_PATH=str(self.library), PLAYLIST_NOTIFY=str(notify),
                        RETRY_DELAYS="0 0 0")

    def defer(self, n):
        (self.library / "discovery" / "deferred.json").write_text(
            json.dumps({"songs": [{"artist": "A", "title": f"S{i}"} for i in range(n)]}))

    def importer(self, *steps):
        """A fake `--retry`: each call prints the next summary and leaves `left` songs deferred."""
        (self.home / "steps.json").write_text(json.dumps([list(s) for s in steps]))
        fake = self.home / "importer.py"
        fake.write_text(f"""import json
path = {str(self.home / "steps.json")!r}
steps = json.load(open(path))
line, left = steps.pop(0)
json.dump(steps, open(path, "w"))
json.dump({{"songs": [{{"title": "x"}}] * left}}, open({str(self.library / "discovery" / "deferred.json")!r}, "w"))
print("noise line")
print(line)
""")
        return f"python3 {fake}"

    def script(self, *args, **env):
        return subprocess.run([str(self.home / "scripts" / "retry.sh"), *args], env={**self.env, **env},
                              text=True, capture_output=True, timeout=60)

    def test_tries_until_nothing_is_deferred_and_reports_each_try(self):
        self.defer(3)
        importer = self.importer((summary(added=[release("S0")], deferred=[release("S1"), release("S2")]), 2),
                                 (summary(added=[release("S1"), release("S2")]), 0))
        run = self.script("run", RETRY_IMPORTER=importer)
        self.assertEqual(run.returncode, 0, run.stderr)
        reports = self.sent.read_text().split("----\n")[:-1]
        self.assertEqual(len(reports), 2)
        self.assertIn("Added 1 song YouTube had blocked: A - S0.", reports[0])
        self.assertIn("2 songs wait for another try later.", reports[0])
        self.assertEqual(reports[1].strip(), "Added 2 songs YouTube had blocked: A - S1; A - S2.")
        self.assertFalse((self.home / "requests" / ".retry.pid").exists())

    def test_gives_up_after_the_last_delay(self):
        self.defer(1)
        blocked = summary(deferred=[release("S0")])
        run = self.script("run", RETRY_IMPORTER=self.importer((blocked, 1), (blocked, 1)), RETRY_DELAYS="0 0")
        self.assertEqual(run.returncode, 0, run.stderr)
        reports = self.sent.read_text().split("----\n")[:-1]
        self.assertEqual(len(reports), 2)
        self.assertIn("1 song waits for the next add or scheduled run.", reports[1])

    def test_start_does_nothing_without_deferred_songs(self):
        run = self.script("start", RETRY_IMPORTER="false")
        self.assertEqual(run.returncode, 0, run.stderr)
        time.sleep(0.5)
        self.assertFalse((self.home / "requests" / "retry.log").exists())
        self.assertFalse(self.sent.exists())

    def test_start_runs_in_the_background(self):
        self.defer(1)
        started = time.time()
        run = self.script("start", RETRY_IMPORTER=self.importer((summary(added=[release("S0")]), 0)),
                          RETRY_DELAYS="1")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertLess(time.time() - started, 1)  # returned before the delay
        for _ in range(100):
            if self.sent.exists():
                break
            time.sleep(0.1)
        self.assertIn("Added 1 song ", self.sent.read_text())


if __name__ == "__main__":
    unittest.main()
