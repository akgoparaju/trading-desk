"""Render-venv bootstrap for the PDF docket (matplotlib + reportlab).

WHY THIS MODULE EXISTS: the core plugin pipeline is stdlib-only and must run on a
bare machine; only the *docket* renderers need matplotlib + reportlab. This CLI
owns a single, idempotent virtualenv for those heavy deps so (a) they live in
exactly one place, (b) the ~30s pip build happens at most once, and (c) the
skills can degrade gracefully: ``--check`` returns exit 3 when the venv is
absent/unbuildable, at which point the skills fall back to md-only output with a
disclosure line.

Env dir resolution: ``$CLAUDE_PLUGIN_DATA/render-venv`` if that variable is set,
else ``~/.claude/trading-desk-data/render-venv``.

CLI:
  render_env.py            -> ensure the venv exists with deps (build once), then
                              print READY <python path> (exit 0) or MISSING (3).
  render_env.py --check    -> report status WITHOUT building: READY/MISSING, exit
                              0/3. Skills call this to decide md-only fallback.

stdlib-only; >=3.10 guard; deterministic.
"""

import argparse
import os
import subprocess
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)"
             % sys.version_info[:2])

# Allow direct invocation: ensure the repo root is importable.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# The two heavy deps the docket renderers need.
_DEPS = ["matplotlib", "reportlab"]


def resolve_env_dir(environ=None):
    """Return the render-venv directory path per the resolution contract.

    ``$CLAUDE_PLUGIN_DATA/render-venv`` when the variable is set (non-empty),
    else ``~/.claude/trading-desk-data/render-venv`` (HOME-based). Pure: takes
    an environ mapping so it is trivially testable.
    """
    environ = environ if environ is not None else os.environ
    plugin_data = environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        return os.path.join(plugin_data, "render-venv")
    home = environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".claude", "trading-desk-data", "render-venv")


def venv_python(env_dir):
    """Path to the python interpreter inside ``env_dir`` (posix bin layout)."""
    return os.path.join(env_dir, "bin", "python")


def _deps_present(py):
    """True iff ``py`` can import every dep. Any error -> False (treated missing)."""
    if not os.path.isfile(py):
        return False
    probe = "import " + ", ".join(_DEPS)
    try:
        proc = subprocess.run([py, "-c", probe],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def check(env_dir):
    """Return (ready: bool, python_path: str). Never builds anything."""
    py = venv_python(env_dir)
    return _deps_present(py), py


def ensure(env_dir):
    """Ensure the venv exists with all deps installed; build once if needed.

    Returns (ready, python_path, error_or_None). Idempotent: if the deps are
    already importable this is a fast no-op. A build failure returns ready=False
    with a short reason rather than raising, so callers degrade gracefully.
    """
    py = venv_python(env_dir)
    if _deps_present(py):
        return True, py, None

    # Create the venv if the interpreter is not there yet.
    if not os.path.isfile(py):
        try:
            os.makedirs(os.path.dirname(env_dir), exist_ok=True)
            subprocess.run([sys.executable, "-m", "venv", env_dir],
                           capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            return False, py, "venv creation failed: %s" % exc

    # Install the deps (pip; upgrade pip first for wheel resolution).
    try:
        subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"],
                       capture_output=True, text=True, check=True)
        subprocess.run([py, "-m", "pip", "install", *_DEPS],
                       capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        return False, py, "pip install failed: %s" % exc

    if _deps_present(py):
        return True, py, None
    return False, py, "deps still not importable after install"


def _missing_line(env_dir, reason=None):
    """A single-line MISSING message with an actionable fix."""
    fix = ("run `python3 scripts/render_env.py` once to build the render venv "
           "(~30s, matplotlib+reportlab)")
    base = "MISSING: render venv at %s not ready" % env_dir
    if reason:
        base += " (%s)" % reason
    return base + " -- " + fix


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bootstrap (or check) the docket render venv "
                    "(matplotlib + reportlab).")
    parser.add_argument("--check", action="store_true",
                        help="report READY/MISSING without building (exit 0/3)")
    args = parser.parse_args(argv)

    env_dir = resolve_env_dir()

    if args.check:
        ready, py = check(env_dir)
        if ready:
            print("READY %s" % py)
            return 0
        print(_missing_line(env_dir))
        return 3

    # Non-check: build once if needed.
    ready, py, reason = ensure(env_dir)
    if ready:
        print("READY %s" % py)
        return 0
    print(_missing_line(env_dir, reason))
    return 3


if __name__ == "__main__":
    sys.exit(main())
