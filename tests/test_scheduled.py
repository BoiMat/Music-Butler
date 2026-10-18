"""scripts/scheduled.sh: job prompts, bootstrap completion and report extraction."""
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "scheduled.sh"


class Scheduled(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.library = self.tmp / "library"
        (self.library / "music" / "release").mkdir(parents=True)
        # Records its arguments instead of refreshing Navidrome's playlists.
        self.butler = self.tmp / "butler.sh"
        self.butler.write_text('#!/bin/sh\necho "$@" >> "$(dirname "$0")/butler-calls"\necho noise\n')
        self.butler.chmod(0o755)

    def run_job(self, job, **env):
        # Variables set in the environment, even empty, win over the checkout's .env.
        full = dict(os.environ, MUSIC_LIBRARY_PATH=str(self.library), SCHEDULE_AGENT="command",
                    SCHEDULE_COMMAND="cat", SCHEDULE_PROVIDER="", SCHEDULE_MODEL="",
                    SCHEDULE_MODEL_WEEKLY="", BOOTSTRAP_TARGET="", HERMES_PROFILE="",
                    HERMES_BIN="", HERMES_HOME=str(self.tmp / "hermes"), BUTLER_BIN=str(self.butler))
        full.update(env)
        return subprocess.run([str(SCRIPT), job], env=full, text=True, capture_output=True, timeout=60)

    def test_command_agent_gets_task_and_skill_on_stdin(self):
        result = self.run_job("weekly")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Weekly music discovery", result.stdout)
        self.assertIn("skills/music-discover/SKILL.md in the current folder", result.stdout)

    def test_playlists_are_refreshed_after_the_run_without_touching_the_report(self):
        result = self.run_job("weekly")
        self.assertNotIn("noise", result.stdout)
        self.assertEqual((self.tmp / "butler-calls").read_text(), "playlists\n")

    def test_bootstrap_reports_completion_once_then_stays_silent(self):
        for name in ("a.mp3", "b.flac"):
            (self.library / "music" / "release" / name).touch()
        first = self.run_job("bootstrap", BOOTSTRAP_TARGET="2")
        self.assertIn("bootstrap complete: 2 songs", first.stdout)
        self.assertTrue((self.library / "discovery" / "bootstrap.done").exists())
        self.assertEqual(self.run_job("bootstrap", BOOTSTRAP_TARGET="2").stdout, "")

    def test_bootstrap_continues_while_playlist_songs_are_queued(self):
        (self.library / "music" / "release" / "a.mp3").touch()
        (self.library / "discovery").mkdir()
        (self.library / "discovery" / "queue.json").write_text('[{"artist": "A", "title": "B"}]')
        result = self.run_job("bootstrap", BOOTSTRAP_TARGET="1")
        self.assertIn("has 1 of 1 songs and 1 playlist songs are queued", result.stdout)

    def test_failed_agent_still_reports(self):
        result = self.run_job("audit", SCHEDULE_COMMAND="false")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Music audit run exited with status 1.", result.stdout)

    def fake_hermes(self, state_dir, session="s1", printed="noise"):
        """A hermes that answers `config path` with state_dir and runs a chat as session."""
        state_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(state_dir / "state.db")
        db.execute("create table if not exists messages (id integer primary key, session_id text, role text, content text)")
        db.executemany("insert into messages (session_id, role, content) values (?, ?, ?)", [
            ("s1", "assistant", "working..."), ("s1", "assistant", "Added 15 songs."),
            ("s1", "assistant", ""), ("s2", "assistant", "other run")])
        db.commit()
        fake = self.tmp / "hermes-bin"
        fake.write_text(f'''#!/bin/sh
if [ "$3" = config ]; then echo "{state_dir}/config.yaml"; exit 0; fi
printf "%s\\n" "$*" > "$(dirname "$0")/args"
echo "{printed}"
echo "session_id: {session}"
''')
        fake.chmod(0o755)
        return fake

    def test_report_found_when_hermes_home_is_the_profile_folder(self):
        # Hermes' gateway runs cron scripts with HERMES_HOME=<home>/profiles/<profile>.
        profile_dir = self.tmp / "hermes" / "profiles" / "music-butler"
        fake = self.fake_hermes(profile_dir, printed="")
        result = self.run_job("weekly", SCHEDULE_AGENT="hermes", HERMES_BIN=str(fake), HERMES_HOME=str(profile_dir))
        self.assertEqual(result.stdout, "Added 15 songs.\n", result.stderr)

    def test_hermes_run_without_a_report_is_never_silent(self):
        fake = self.fake_hermes(self.tmp / "hermes" / "profiles" / "music-butler", session="gone", printed="")
        result = self.run_job("audit", SCHEDULE_AGENT="hermes", HERMES_BIN=str(fake))
        self.assertEqual(result.stdout, "Music audit run finished, but its report could not be read (Hermes session gone).\n")

    def test_hermes_report_is_last_assistant_message(self):
        fake = self.fake_hermes(self.tmp / "hermes" / "profiles" / "music-butler")
        result = self.run_job("weekly", SCHEDULE_AGENT="hermes", HERMES_BIN=str(fake),
                              SCHEDULE_PROVIDER="prov", SCHEDULE_MODEL="small", SCHEDULE_MODEL_WEEKLY="big")
        self.assertEqual(result.stdout, "Added 15 songs.\n", result.stderr)
        args = (self.tmp / "args").read_text()
        self.assertIn("-s music-discover --provider prov -m big -q Weekly", args)

    def test_unconfigured_agent_is_an_error(self):
        result = self.run_job("audit", SCHEDULE_AGENT="")
        self.assertEqual(result.returncode, 64)
        self.assertIn("SCHEDULE_AGENT is not set", result.stderr)


if __name__ == "__main__":
    unittest.main()
