"""The SessionStart notice must be quiet in every case except exactly one:
FSI absent + no recorded offer + never shown before."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "session_notice.py")


def _run(home, data_dir, cwd):
    env = dict(os.environ, HOME=home, CLAUDE_PLUGIN_DATA=data_dir)
    return subprocess.run([sys.executable, SCRIPT], capture_output=True,
                          text=True, env=env, cwd=cwd)


class TestSessionNotice(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.data = tempfile.mkdtemp()
        self.cwd = tempfile.mkdtemp()
        self.addCleanup(lambda: None)

    def test_first_run_shows_notice_once(self):
        p = _run(self.home, self.data, self.cwd)
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertIn("FSI", out["systemMessage"])
        self.assertIn("additionalContext", out["hookSpecificOutput"])
        # second session: marker written -> silent
        p2 = _run(self.home, self.data, self.cwd)
        self.assertEqual(p2.returncode, 0)
        self.assertEqual(p2.stdout.strip(), "")

    def test_silent_when_fsi_installed(self):
        os.makedirs(os.path.join(self.home, ".claude", "plugins", "cache",
                                 "claude-for-financial-services",
                                 "equity-research", "1.0.0"))
        p = _run(self.home, self.data, self.cwd)
        self.assertEqual(p.stdout.strip(), "")

    def test_silent_when_offer_recorded_in_workspace(self):
        with open(os.path.join(self.cwd, "trading_desk_config.json"), "w") as fh:
            json.dump({"fsi_offer": {"asked": True, "choice": "compressed"}}, fh)
        p = _run(self.home, self.data, self.cwd)
        self.assertEqual(p.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
