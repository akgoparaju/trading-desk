"""SessionStart hook: one-time FSI setup notice (harness-executed, not skimmable).

WHY: plugin installs cannot prompt (no post-install hook exists). The FSI offer
inside the skills only fires when a full analysis runs — a user who just
installed sees nothing. This hook runs at the next session start and shows the
notice ONCE (marker in ${CLAUDE_PLUGIN_DATA}), then stays silent forever.
Silent immediately when FSI is already installed or a workspace has a recorded
fsi_offer choice. Exit 0 with no stdout = say nothing.
"""

import glob
import json
import os
import sys


def fsi_installed(home):
    # FSI plugins live in the marketplace cache: ~/.claude/plugins/cache/<mkt>/<plugin>/...
    pats = (
        os.path.join(home, ".claude", "plugins", "cache", "*", "equity-research", "*"),
        os.path.join(home, ".claude", "plugins", "cache", "*", "equity-research"),
    )
    return any(glob.glob(p) for p in pats)


def offer_recorded(cwd):
    try:
        with open(os.path.join(cwd, "trading_desk_config.json")) as fh:
            cfg = json.load(fh)
        return bool((cfg.get("fsi_offer") or {}).get("asked"))
    except (OSError, ValueError):
        return False


def main():
    home = os.environ.get("HOME", os.path.expanduser("~"))
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        home, ".claude", "trading-desk-data")
    marker = os.path.join(data_dir, "fsi_notice_shown")

    if fsi_installed(home) or offer_recorded(os.getcwd()) or os.path.exists(marker):
        return 0  # silent

    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(marker, "w") as fh:
            fh.write("shown\n")
    except OSError:
        pass  # still show the notice; worst case it shows again next session

    print(json.dumps({
        "systemMessage": (
            "trading-desk: deep fundamental mode uses the optional FSI plugins "
            "(equity-research + financial-analysis), which are not installed — "
            "the built-in compressed pass will be used until then. To install: "
            "/plugin marketplace add anthropics/financial-services , then "
            "/plugin install equity-research and /plugin install financial-analysis. "
            "(One-time notice.)"),
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "trading-desk plugin notice: FSI plugins are NOT installed and no "
                "fsi_offer is recorded. If the user starts any trading-desk "
                "analysis this session, make the FSI install offer per the "
                "full-trade-analysis SKILL Phase 0 and record the choice in "
                "trading_desk_config.json."),
        },
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
