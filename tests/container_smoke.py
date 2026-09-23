#!/usr/bin/env python3
"""Explicit opt-in disposable Docker smoke test: no published ports or media acquisition."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    config = json.loads(run(["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
                            cwd=ROOT, capture_output=True).stdout)
    image = config["services"]["navidrome"]["image"]
    run(["docker", "pull", image])
    # Use actual final tooling image, real encoders and offline mocks, read-only source mount.
    run(["docker", "run", "--rm", "--network", "none", "--read-only", "--user", "1000:1000",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "128",
         "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m,mode=1777", "-e", "HOME=/tmp",
         "-v", str(ROOT) + ":/work:ro", "-w", "/work", "--entrypoint", "python3",
         "music-butler-tools:local", "-m", "unittest", "discover", "-s", "tests", "-v"])
    name = "music-butler-smoke-" + uuid.uuid4().hex[:12]
    storage = ROOT / "storage"
    storage.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=storage) as tmp, tempfile.TemporaryFile() as log:
        tmp = Path(tmp)
        for child in ("data", "music"):
            (tmp / child).mkdir(mode=0o750)
        args = ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
                "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--pids-limit", "128", "--memory", "1g",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m,mode=1777",
                "-v", str(tmp / "data") + ":/data", "-v", str(tmp / "music") + ":/music:ro"]
        for key, value in config["services"]["navidrome"]["environment"].items():
            args += ["-e", f"{key}={value}"]
        proc = subprocess.Popen(args + [image], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                probe = subprocess.run(["docker", "exec", name, "curl", "--fail", "--silent", "--max-time", "2",
                                        "--output", "/dev/null", "http://127.0.0.1:4533/"], capture_output=True)
                if probe.returncode == 0:
                    break
                if proc.poll() is not None:
                    raise RuntimeError("Navidrome exited before HTTP readiness")
                time.sleep(0.25)  # bounded readiness polling, not a blind startup sleep
            else:
                raise RuntimeError("Navidrome HTTP readiness timed out")
            inspect = json.loads(run(["docker", "inspect", name], capture_output=True).stdout)[0]
            assert inspect["HostConfig"]["ReadonlyRootfs"]
            assert not inspect["HostConfig"]["PortBindings"]
            assert inspect["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
            assert not next(v for v in inspect["Mounts"] if v["Destination"] == "/music")["RW"]
            response = run(["docker", "exec", name, "curl", "--silent", "--max-time", "5", "-X", "POST",
                            "--data", "v=1.16.1&c=offline-smoke&f=json", "http://127.0.0.1:4533/rest/startScan.view"],
                           capture_output=True).stdout
            assert json.loads(response)["subsonic-response"]["status"] == "failed"
            print("PASS: Navidrome HTTP ready, non-root/read-only mounts/no ports, unauthenticated startScan rejected.")
            print("NOT TESTED: authenticated scan, real library indexing, provider downloads, Tempo or Hermes gateway.")
        except BaseException:
            log.seek(0)
            print(log.read().decode(errors="replace"))
            raise
        finally:
            subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                proc.kill()
                proc.wait()
    print("PASS: disposable containers and temporary Navidrome storage removed.")


if __name__ == "__main__":
    main()
