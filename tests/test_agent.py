"""Offline tests for the Ollama agent's tool layer; no Ollama, Docker or network."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("butler_agent", ROOT / "scripts/agent.py")
assert spec is not None and spec.loader is not None
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(agent, "REQUESTS_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_add_writes_songs_file_and_runs_add_without_timeout(self):
        args = {"format": "", "junk": 1, "songs": [
            {"artist": "A", "title": "T", "url": "https://archive.org/download/x/t.flac",
             "album": "", "artwork_url": None, "genre": "Jazz", "year": 1999},
            {"artist": "B", "title": "U", "url": "https://youtu.be/abc"}]}
        with patch.object(agent, "run_butler", return_value=(0, "")) as run:
            agent.call_tool("butler_add", json.dumps(args))
        command, timeout = run.call_args.args
        self.assertEqual(command[0], "add")
        self.assertIsNone(timeout)
        written = json.loads((Path(self.tmp.name) / f"approved-{command[1]}.json").read_text(encoding="utf-8"))
        # Empty/unknown keys a small model adds are dropped; the importer validates the rest.
        self.assertEqual(written, {"songs": [
            {"artist": "A", "title": "T", "url": "https://archive.org/download/x/t.flac", "year": 1999},
            {"artist": "B", "title": "U", "url": "https://youtu.be/abc"}]})

    def test_single_song_object_is_wrapped(self):
        with patch.object(agent, "run_butler", return_value=(0, "")) as run:
            agent.call_tool("butler_add", {"songs": {"artist": "A", "title": "T", "url": "https://youtu.be/x"}})
        rid = run.call_args.args[0][1]
        written = json.loads((Path(self.tmp.name) / f"approved-{rid}.json").read_text(encoding="utf-8"))
        self.assertEqual(len(written["songs"]), 1)

    def test_lookup_writes_lookup_file_with_bounded_timeout(self):
        line = '{"event": "lookup", "search": [{"query": "a b", "results": []}], "urls": []}'
        with patch.object(agent, "run_butler", return_value=(0, "noise\n" + line)) as run:
            result = agent.call_tool("butler_lookup", {"search": "Daft Punk Get Lucky", "urls": ["", None]})
        command, timeout = run.call_args.args
        self.assertEqual((command[0], timeout), ("lookup", agent.LOOKUP_TIMEOUT))
        self.assertIsNotNone(timeout)
        written = json.loads((Path(self.tmp.name) / f"lookup-{command[1]}.json").read_text(encoding="utf-8"))
        self.assertEqual(written, {"search": ["Daft Punk Get Lucky"]})
        self.assertEqual(json.loads(result.splitlines()[1])["event"], "lookup")

    def test_bad_arguments_never_raise(self):
        with patch.object(agent, "run_butler") as run:
            for name, args in [("butler_add", {}), ("butler_add", {"songs": []}),
                               ("butler_lookup", {}), ("butler_lookup", {"search": [""]}),
                               ("butler_add", "not json"), ("butler_backup", {"drive": ["x"]}),
                               ("butler_status", [1])]:
                with self.subTest(name=name, args=args):
                    self.assertTrue(agent.call_tool(name, args).startswith("error:"))
            run.assert_not_called()
            self.assertTrue(agent.call_tool("rm_rf", {}).startswith("Unknown tool"))

    def test_compact_keeps_only_summary_or_errors(self):
        out = "\n".join([
            '{"event": "valid", "releases": [], "tracks": 2}',
            '{"event": "started", "request_id": "x", "tracks": 1}',
            '{"event": "complete", "path": "/library/music/very/long", "tracks": [{"sha256": "' + "f" * 64 + '"}]}',
            '[stderr]', 'noise',
            '{"event": "summary", "ok": true, "added": [], "failed": [], "not_attempted": []}'])
        result = agent.compact(0, out)
        self.assertEqual(result.splitlines()[0], "exit 0")
        self.assertEqual(json.loads(result.splitlines()[1])["event"], "summary")
        self.assertNotIn("sha256", result)
        errors = agent.compact(65, '{"event": "error", "code": 65, "reason": "source_url_not_allowed", "song": 2}')
        self.assertIn("nothing was done", errors)
        self.assertIn('"song": 2', errors)
        self.assertTrue(agent.compact(64, "invalid request id").endswith("invalid request id"))


@unittest.skipUnless(hasattr(os, "killpg"), "POSIX process groups required")
class StopGroup(unittest.TestCase):
    def test_stop_group_reaches_grandchildren(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "grandchild.pid"
            grandchild = f"import os,time,pathlib; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)"
            parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{grandchild!r}]).wait()"
            proc = subprocess.Popen([sys.executable, "-c", parent], start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                pid = int(pidfile.read_text())
                agent.stop_group(proc)
                self.assertIsNotNone(proc.poll())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("grandchild survived stop_group")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    unittest.main()
