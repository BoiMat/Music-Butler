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

    def test_write_request_tool_creates_file(self):
        args = {"kind": "song", "artist": "A", "album": "B", "format": "mp3", "quality": "",
                "junk": 1, "tracks": [{"title": "T", "url": "https://archive.org/download/x/t.flac",
                                       "artwork_url": None}]}
        result = agent.call_tool("butler_write_request", json.dumps(args))
        rid = result.split("id: ")[1].split()[0]
        self.assertRegex(rid, r"^[0-9a-f]{32}$")
        written = json.loads((Path(self.tmp.name) / f"approved-{rid}.json").read_text(encoding="utf-8"))
        # Empty/unknown keys a small model adds are dropped; the importer still validates the rest.
        self.assertEqual(written, {"kind": "song", "artist": "A", "album": "B", "format": "mp3",
                                   "tracks": [{"title": "T", "url": "https://archive.org/download/x/t.flac"}]})

    def test_bad_arguments_never_raise(self):
        with patch.object(agent, "run_butler") as run:
            for name, args in [("butler_validate", {}), ("butler_import", {"id": "../etc"}),
                               ("butler_import", {"id": 5}), ("butler_validate", "not json"),
                               ("butler_backup", {"drive": ["x"]}), ("butler_status", [1])]:
                with self.subTest(name=name, args=args):
                    self.assertTrue(agent.call_tool(name, args).startswith("error:"))
            run.assert_not_called()
            self.assertTrue(agent.call_tool("rm_rf", {}).startswith("Unknown tool"))

    def test_import_has_no_agent_timeout(self):
        rid = "0" * 32
        with patch.object(agent, "run_butler", return_value=(0, "ok")) as run:
            agent.call_tool("butler_import", {"id": rid})
            run.assert_called_once_with(["import", rid], None)
            agent.call_tool("butler_validate", {"id": rid})
            run.assert_called_with(["validate", rid], agent.TIMEOUT)


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
