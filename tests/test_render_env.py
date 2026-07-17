"""Tests for scripts/render_env.py -- the render-venv bootstrap CLI.

WHY: chart/PDF rendering needs matplotlib + reportlab, which the plugin does NOT
require for its core (md) pipeline. render_env.py owns a single, idempotent venv
so those heavy deps live in exactly one place and the skills can degrade
gracefully (exit 3 -> md-only + disclosure) when it is absent or cannot be built.

These tests cover the cheap, deterministic surface WITHOUT paying the ~30s venv
build in CI:
  - env-dir resolution honours $CLAUDE_PLUGIN_DATA, else falls back to
    ~/.claude/trading-desk-data/render-venv;
  - ``--check`` against a non-existent venv dir prints MISSING + a one-line fix
    and exits 3;
  - ``--check`` against a venv that already has the deps prints READY + the
    python path and exits 0 (simulated with a fake venv layout).
The real venv CREATION is exercised only when TD_TEST_BOOTSTRAP=1 (opt-in, ~30s).

stdlib-only; unittest.
"""

import os
import subprocess
import sys
import tempfile
import unittest

from scripts import render_env as renv


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDER_ENV = os.path.join(_REPO_ROOT, "scripts", "render_env.py")


def _run(args, env=None):
    full = dict(os.environ)
    if env:
        full.update(env)
    proc = subprocess.run([sys.executable, RENDER_ENV] + args,
                          capture_output=True, text=True, env=full)
    return proc.returncode, proc.stdout, proc.stderr


class TestEnvDirResolution(unittest.TestCase):
    """resolve_env_dir prefers $CLAUDE_PLUGIN_DATA, else the ~/.claude fallback."""

    def test_uses_plugin_data_when_set(self):
        d = renv.resolve_env_dir({"CLAUDE_PLUGIN_DATA": "/data/plug"})
        self.assertEqual(d, os.path.join("/data/plug", "render-venv"))

    def test_falls_back_to_home_claude(self):
        d = renv.resolve_env_dir({"HOME": "/home/tester"})
        self.assertEqual(
            d,
            os.path.join("/home/tester", ".claude", "trading-desk-data",
                         "render-venv"))

    def test_plugin_data_beats_home(self):
        d = renv.resolve_env_dir(
            {"CLAUDE_PLUGIN_DATA": "/data/plug", "HOME": "/home/tester"})
        self.assertEqual(d, os.path.join("/data/plug", "render-venv"))

    def test_venv_python_path(self):
        # The python inside a venv dir is <dir>/bin/python (posix layout).
        p = renv.venv_python("/some/render-venv")
        self.assertEqual(p, os.path.join("/some/render-venv", "bin", "python"))


class TestCheckMissing(unittest.TestCase):
    """--check on an absent venv -> exit 3 + MISSING with a one-line fix."""

    def test_check_missing_exit3(self):
        with tempfile.TemporaryDirectory() as d:
            env_dir = os.path.join(d, "render-venv")  # never created
            rc, out, err = _run(
                ["--check"], env={"CLAUDE_PLUGIN_DATA": d})
            self.assertEqual(rc, 3, out + err)
            self.assertIn("MISSING", out)
            # a one-line, actionable fix must be present.
            self.assertIn("render_env.py", out)

    def test_check_missing_does_not_build(self):
        # --check must NOT create the venv (it only reports); the dir stays absent.
        with tempfile.TemporaryDirectory() as d:
            _run(["--check"], env={"CLAUDE_PLUGIN_DATA": d})
            self.assertFalse(os.path.isdir(os.path.join(d, "render-venv")))


class TestCheckReadyFakeVenv(unittest.TestCase):
    """--check on a venv that reports the deps present -> exit 0 + READY + path.

    We fabricate a minimal venv layout with a fake python that exits 0 for the
    dependency-probe so we exercise the READY branch without a real build.
    """

    def _fake_venv(self, root, probe_ok=True):
        env_dir = os.path.join(root, "render-venv")
        bindir = os.path.join(env_dir, "bin")
        os.makedirs(bindir)
        py = os.path.join(bindir, "python")
        # A shell shim that always exits with the requested code, so the
        # dependency probe (`python -c "import matplotlib, reportlab"`) either
        # "succeeds" (0) or "fails" (1) deterministically.
        code = 0 if probe_ok else 1
        with open(py, "w") as fh:
            fh.write("#!/bin/sh\nexit %d\n" % code)
        os.chmod(py, 0o755)
        return env_dir, py

    def test_ready_prints_path_exit0(self):
        with tempfile.TemporaryDirectory() as d:
            env_dir, py = self._fake_venv(d, probe_ok=True)
            rc, out, err = _run(["--check"], env={"CLAUDE_PLUGIN_DATA": d})
            self.assertEqual(rc, 0, out + err)
            self.assertIn("READY", out)
            self.assertIn(py, out)

    def test_present_but_deps_missing_is_missing(self):
        # venv exists but the import probe fails -> treated as MISSING (exit 3).
        with tempfile.TemporaryDirectory() as d:
            env_dir, py = self._fake_venv(d, probe_ok=False)
            rc, out, err = _run(["--check"], env={"CLAUDE_PLUGIN_DATA": d})
            self.assertEqual(rc, 3, out + err)
            self.assertIn("MISSING", out)


@unittest.skipUnless(os.environ.get("TD_TEST_BOOTSTRAP") == "1",
                     "set TD_TEST_BOOTSTRAP=1 to run the ~30s real venv build")
class TestRealBootstrap(unittest.TestCase):
    """Opt-in: actually build the venv and install the deps (slow)."""

    def test_bootstrap_then_check_ready(self):
        with tempfile.TemporaryDirectory() as d:
            # First bare run creates + installs.
            rc, out, err = _run([], env={"CLAUDE_PLUGIN_DATA": d})
            self.assertEqual(rc, 0, out + err)
            # Second --check sees the ready venv.
            rc2, out2, err2 = _run(["--check"], env={"CLAUDE_PLUGIN_DATA": d})
            self.assertEqual(rc2, 0, out2 + err2)
            self.assertIn("READY", out2)


if __name__ == "__main__":
    unittest.main()
