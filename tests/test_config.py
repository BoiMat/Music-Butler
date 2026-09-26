"""Validate the checked-in example with Compose; no Docker daemon/services needed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI not installed")
class ComposeConfig(unittest.TestCase):
    def test_profiles_mounts_security_and_example(self):
        env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
        # macOS CLT python3 exports these compiler paths; they must never become bind mounts.
        env.update(LIBRARY_PATH="/usr/local/lib", CPATH="/usr/local/include")
        cmd = ["docker", "compose", "--env-file", ".env.example", "--profile", "tools", "--profile", "hermes", "config", "--format", "json"]
        result = subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True, check=True)
        services = json.loads(result.stdout)["services"]
        self.assertEqual(set(services), {"navidrome", "downloader", "hermes-exec"})
        nav = services["navidrome"]
        self.assertNotIn("profiles", nav)
        self.assertEqual(nav["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(nav["environment"]["ND_SCANNER_SCHEDULE"], "@every 1m")
        for svc in services.values():
            self.assertEqual(svc["user"], "1000:1000")
            self.assertTrue(svc["read_only"])
            self.assertIn("ALL", svc["cap_drop"])
            self.assertIn("no-new-privileges:true", svc["security_opt"])
            self.assertFalse(svc.get("privileged", False))
            self.assertFalse(any("docker.sock" in v["source"] for v in svc["volumes"]))
            for v in svc["volumes"]:
                self.assertTrue(Path(v["source"]).is_relative_to(ROOT), v["source"])
        mounts = {v["target"]: v for v in nav["volumes"]}
        self.assertTrue(mounts["/music"]["read_only"])
        self.assertNotIn("/library", mounts)
        self.assertEqual(services["downloader"]["profiles"], ["tools"])
        self.assertEqual(services["downloader"]["environment"]["ENABLE_SPOTDL"], "false")
        self.assertEqual(services["hermes-exec"]["profiles"], ["hermes"])
        self.assertNotIn("ports", services["downloader"])


if __name__ == "__main__":
    unittest.main()
