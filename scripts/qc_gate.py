"""QC gate CLI for the trading-desk plugin.

WHY THIS MODULE EXISTS: The snapshot QC gate is the blocking checkpoint between
raw-derived numbers and any trade decision. This CLI loads a snapshot.json, runs
scripts.qc.run_qc over it, writes the verdict back INTO meta.qc IN PLACE, prints
a human-readable check table plus the attestation paragraph, and exits 0 (pass)
or 1 (fail). Waivers supplied on the command line are inserted into
meta.qc.waivers BEFORE running the gate so a known, justified failure does not
block the pipeline while remaining fully disclosed.

stdlib-only. All check logic lives in scripts.qc; this file is thin I/O + format.
"""

import argparse
import json
import os
import sys

# Allow direct invocation (``python3 scripts/qc_gate.py``): ensure the repo root
# is importable so ``from scripts import qc`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import qc


def _status(passed, check_name, waived_names):
    """Map a check result to a display status string."""
    if check_name in waived_names and passed is False:
        return "WAIVED"
    if passed is True:
        return "PASS"
    if passed is False:
        return "FAIL"
    return "SKIP"


def _render_table(results, waived_names):
    """Aligned check | status | detail table as a single string."""
    name_w = max([len(r["check"]) for r in results] + [len("check")])
    header = f"{'check'.ljust(name_w)}  STATUS  detail"
    lines = [header, "-" * len(header)]
    for r in results:
        status = _status(r["passed"], r["check"], waived_names)
        lines.append(f"{r['check'].ljust(name_w)}  {status.ljust(6)}  {r['detail']}")
    return "\n".join(lines)


def _parse_waivers(raw_waivers):
    """Parse repeated --waive "name:reason" strings into waiver dicts."""
    out = []
    for w in raw_waivers or []:
        if ":" in w:
            name, reason = w.split(":", 1)
            name, reason = name.strip(), reason.strip()
        else:
            name, reason = w.strip(), ""
        if name:
            out.append({"check": name, "reason": reason})
    return out


def run_gate(snapshot_path, waivers=None):
    """Load, run QC, write meta.qc back in place, return (verdict_dict, snapshot)."""
    with open(snapshot_path) as fh:
        snapshot = json.load(fh)

    meta = snapshot.setdefault("meta", {})
    qc_meta = meta.setdefault("qc", {})
    existing = qc_meta.get("waivers") or []
    if not isinstance(existing, list):
        existing = []
    # CLI waivers are inserted BEFORE running so they take effect this run.
    all_waivers = existing + _parse_waivers(waivers)
    qc_meta["waivers"] = all_waivers

    verdict = qc.run_qc(snapshot)

    meta["qc"] = {
        "passed": verdict["passed"],
        "checks": verdict["checks"],
        "waivers": all_waivers,
    }

    with open(snapshot_path, "w") as fh:
        json.dump(snapshot, fh, indent=2)

    return verdict, snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the snapshot QC gate and write results back in place.")
    parser.add_argument("snapshot", help="path to snapshot.json")
    parser.add_argument("--waive", action="append", default=[],
                        metavar="check_name:reason",
                        help="waive a named check (repeatable)")
    args = parser.parse_args(argv)

    try:
        verdict, _ = run_gate(args.snapshot, args.waive)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot load snapshot {args.snapshot}: {exc}", file=sys.stderr)
        return 1

    waived_names = {w["check"] for w in _parse_waivers(args.waive)}
    print(_render_table(verdict["checks"], waived_names))
    print()
    print(verdict["attestation"])
    print()
    print("GATE: " + ("PASS" if verdict["passed"] else "FAIL"))
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
