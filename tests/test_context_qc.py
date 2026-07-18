"""Tests for report_qc.py --context -- the company-context provenance + structure gate.

WHY: the company-context module (skill: company-context, v1.0.0) is the coverage-
distilled / web-compressed business + competitive + cases + risks brief that feeds
score_fundamental's --moat justification and grounds composite's conviction. It is
UNSCORED as a dimension; its findings[] list is the citation registry that prose
references inline ("(C3)"). Two invariants are load-bearing and tested here:

  1. NUMBER PROVENANCE OVER PROSE. ``report_qc.py --context`` runs number_provenance
     over EVERY narrative string (business/competitive/live_tape/cases/risks) using
     the SAME allowed-set machinery as the report / pdf-slots gates. A fabricated
     number in any case/risk narrative orphans (exit 1, named). Finding ``claim`` /
     ``source`` strings are the citation registry, not scanned prose (a section name
     or URL may carry digits) -- excluded from the number scan.

  2. STRUCTURE. findings IDs are C<n>, unique, sequential C1..Cn; every finding has
     a non-empty claim + source; at least one C<n> is referenced from cases/
     competitive prose; live_tape dates parse and are <= as_of; mode is one of the
     two legal values.

On PASS the gate stamps {"qc_passed": true, "checked_utc": ...} INTO module.qc and
exits 0; any structural or provenance failure exits 1 and names the offender. The
CLI keeps the exactly-one-of {--report, --pdf-slots, --context} discipline.

The fixture bundle builders are REUSED from tests/test_report_renderer.py (the same
minimal MU-shaped bundle the whole pipeline is tested against), so a context module's
cited numbers are checked against the identical shapes the real snapshot emits.

stdlib-only; unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import report_qc as rq

# Reuse the report-layer fixture bundle builder (identical snapshot/module shapes).
from tests.test_report_renderer import _mk_bundle

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QC = os.path.join(_REPO_ROOT, "scripts", "report_qc.py")

# The fixture bundle's as_of (see _snapshot_doc: meta.as_of_utc 2026-07-16).
_AS_OF = "2026-07-16"


# --------------------------------------------------------------------------- #
# A valid company-context module whose every prose number is a fixture-bundle
# leaf. Legit fixture numbers used below: last 100, entries 95 / 90, swing low 82,
# 52wk 130 / 60, implied move 8.5% (0.085), short interest 3.5%. Dates are bundle
# dates (as_of 2026-07-16, earnings 2026-07-30 is NOT used in live_tape since it is
# future vs as_of; live_tape dates are all <= as_of).
# --------------------------------------------------------------------------- #

def _valid_context():
    return {
        "skill": "company-context",
        "version": "1.0.0",
        "ticker": "MU",
        "as_of": _AS_OF,
        "mode": "coverage_distilled",
        "business": {
            "what_they_sell": "Memory chips (DRAM and NAND) for data center, "
                              "mobile, and PC end markets.",
            "revenue_drivers": [
                "HBM attach into AI accelerators (C1)",
                "DRAM pricing recovery off the trough (C2)",
            ],
            "segments": ["Compute & Networking", "Mobile", "Storage", "Embedded"],
        },
        "competitive": {
            "position": "Number-three DRAM maker behind two larger Asian peers; "
                        "HBM qualification is the differentiator this cycle (C1).",
            "moat_evidence": [
                "Process-node parity closing the historical gap (C3)",
                "HBM3E design wins with the lead accelerator vendor (C1)",
            ],
            "competitors": ["Samsung", "SK Hynix"],
        },
        "live_tape": [
            {"date": "2026-07-15", "event": "Sell-side note lifts HBM TAM estimate",
             "why_it_matters": "Supports the bull revenue driver (C2)."},
            {"date": "2026-07-14", "event": "DRAM spot pricing ticked higher",
             "why_it_matters": "Confirms the pricing-recovery leg."},
            {"date": "2026-07-10", "event": "Peer capex commentary flagged discipline",
             "why_it_matters": "Reduces the oversupply tail risk."},
        ],
        "cases": {
            "bull": {
                "narrative": "HBM ramps into the AI build-out and price holds; the "
                             "stock re-rates toward the 130 prior high (C1).",
                "conditions": ["HBM revenue attach accelerates",
                               "DRAM price stays firm above the trough"],
            },
            "base": {
                "narrative": "In-line ramp; the tape holds the 95 support shelf near "
                             "the last 100 print (C2).",
                "conditions": ["Ramp proceeds roughly on schedule"],
            },
            "bear": {
                "narrative": "Oversupply returns and price cracks; a break of the 82 "
                             "swing low opens the low-60 zone (C3).",
                "conditions": ["DRAM oversupply resumes",
                               "HBM qualification slips"],
            },
        },
        "risks": [
            {"risk": "Cyclical DRAM oversupply", "why": "Memory is commoditized and "
             "capacity-cycle driven.", "anchor": "coverage/research.md §Cycle risk"},
            {"risk": "HBM qualification timing", "why": "A slip cedes the AI socket "
             "to a faster peer.", "anchor": "coverage/research.md §Competition"},
        ],
        "findings": [
            {"id": "C1", "claim": "HBM3E design wins with the lead accelerator vendor.",
             "source": "coverage/research.md §Competition"},
            {"id": "C2", "claim": "DRAM pricing is recovering off the cycle trough.",
             "source": "coverage/model.md §Pricing"},
            {"id": "C3", "claim": "Process-node gap to the leaders is closing.",
             "source": "https://example.com/mu-node-analysis"},
        ],
        "qc": None,
    }


def _write_context(bundle, module):
    path = os.path.join(bundle, "module_context.json")
    with open(path, "w") as fh:
        json.dump(module, fh)
    return path


def _qc_context(bundle, context_path, extra=None):
    proc = subprocess.run(
        [sys.executable, QC, "--context", context_path, "--bundle", bundle]
        + (extra or []),
        capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- #
# Happy path: valid module passes both checks and gets stamped.
# --------------------------------------------------------------------------- #

class TestContextPass(unittest.TestCase):
    def test_valid_context_passes_and_stamps(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            path = _write_context(d, _valid_context())
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("PASS", out)
            self.assertIn("number_provenance", out)
            self.assertIn("context_structure", out)
            # Stamp written INTO module.qc, content preserved.
            with open(path) as fh:
                stamped = json.load(fh)
            self.assertIsInstance(stamped.get("qc"), dict)
            self.assertIs(stamped["qc"].get("qc_passed"), True)
            self.assertIn("checked_utc", stamped["qc"])
            self.assertEqual(len(stamped["findings"]), 3)
            self.assertEqual(stamped["mode"], "coverage_distilled")

    def test_web_compressed_mode_also_valid(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["mode"] = "web_compressed"
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 0, out + err)

    def test_stamp_not_written_scanned_prose_only(self):
        # collect_context_strings excludes findings + meta keys.
        m = _valid_context()
        prose = rq.collect_context_strings(m)
        joined = "\n".join(prose)
        # A finding's claim / source must NOT be in the scanned prose set.
        self.assertNotIn("coverage/model.md", joined)
        self.assertNotIn("Process-node gap to the leaders", joined)
        # But a narrative field IS scanned.
        self.assertIn("re-rates toward the 130 prior high", joined)


# --------------------------------------------------------------------------- #
# Number-provenance failures (fabricated numbers in narrative prose).
# --------------------------------------------------------------------------- #

class TestContextProvenance(unittest.TestCase):
    def test_fabricated_number_in_case_narrative_fails_and_names_it(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["cases"]["bull"]["narrative"] = "Targets a fabricated 4444 handle (C1)."
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("number_provenance", out)
            self.assertIn("4444", out)
            # No stamp on failure.
            with open(path) as fh:
                unstamped = json.load(fh)
            self.assertIsNone(unstamped.get("qc"))

    def test_fabricated_number_in_business_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["business"]["what_they_sell"] = "Sells 7231 distinct SKUs (C1)."
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("7231", out)

    def test_fabricated_number_in_risk_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["risks"][0]["why"] = "Prices could fall 6197 on oversupply."
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("6197", out)

    def test_fabricated_number_in_live_tape_why_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["live_tape"][0]["why_it_matters"] = "Adds 8888 of upside TAM."
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("8888", out)

    def test_number_only_in_finding_claim_does_not_orphan(self):
        # A number living in a finding claim/source is the registry, not prose;
        # it must not orphan the gate (structure check owns findings).
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["findings"][0]["claim"] = "HBM3E wins at 3141 units of capacity."
            m["findings"][0]["source"] = "coverage/research.md §Section 2718"
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 0, out + err)


# --------------------------------------------------------------------------- #
# Structural failures.
# --------------------------------------------------------------------------- #

class TestContextStructure(unittest.TestCase):
    def test_duplicate_finding_id_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["findings"][1]["id"] = "C1"  # dup of findings[0]
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("context_structure", out)
            self.assertIn("duplicate", out.lower())

    def test_finding_without_source_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["findings"][2]["source"] = ""
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("source", out.lower())

    def test_finding_without_claim_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["findings"][0]["claim"] = "   "
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("claim", out.lower())

    def test_non_sequential_finding_ids_fail(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            # C1, C2, C5 -> gap.
            m["findings"][2]["id"] = "C5"
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("sequential", out.lower())

    def test_malformed_finding_id_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["findings"][0]["id"] = "X1"  # not C<n>
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("context_structure", out)

    def test_no_finding_reference_in_prose_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            # Strip every (Cn) reference from cases + competitive prose.
            import re
            for case in m["cases"].values():
                case["narrative"] = re.sub(r"\s*\(C\d+\)", "", case["narrative"])
            m["competitive"]["position"] = re.sub(
                r"\s*\(C\d+\)", "", m["competitive"]["position"])
            m["competitive"]["moat_evidence"] = [
                re.sub(r"\s*\(C\d+\)", "", s)
                for s in m["competitive"]["moat_evidence"]]
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("reference", out.lower())

    def test_empty_findings_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["findings"] = []
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("findings", out.lower())

    def test_bad_live_tape_date_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["live_tape"][0]["date"] = "2026-13-40"  # unparseable
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("context_structure", out)

    def test_live_tape_date_after_as_of_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["live_tape"][0]["date"] = "2026-07-30"  # after as_of 2026-07-16
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("after as_of", out.lower())

    def test_mode_typo_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            m = _valid_context()
            m["mode"] = "coverage_distiled"  # typo
            path = _write_context(d, m)
            rc, out, err = _qc_context(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("mode", out.lower())


# --------------------------------------------------------------------------- #
# CLI discipline: exactly one of {--report, --pdf-slots, --context}.
# --------------------------------------------------------------------------- #

class TestContextCliDiscipline(unittest.TestCase):
    def test_context_plus_pdf_slots_errors(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            cpath = _write_context(d, _valid_context())
            spath = os.path.join(d, "pdf_slots.json")
            with open(spath, "w") as fh:
                json.dump({"thesis_bullets": []}, fh)
            rc, out, err = _qc_context(d, cpath, ["--pdf-slots", spath])
            self.assertEqual(rc, 1, out + err)
            self.assertIn("exactly one", (out + err).lower())

    def test_context_plus_report_errors(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            cpath = _write_context(d, _valid_context())
            rpath = os.path.join(d, "report.md")
            with open(rpath, "w") as fh:
                fh.write("# report\n")
            rc, out, err = _qc_context(d, cpath, ["--report", rpath])
            self.assertEqual(rc, 1, out + err)
            self.assertIn("exactly one", (out + err).lower())

    def test_none_of_the_three_errors(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            proc = subprocess.run(
                [sys.executable, QC, "--bundle", d],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("exactly one", (proc.stdout + proc.stderr).lower())

    def test_missing_context_file_errors(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            rc, out, err = _qc_context(d, os.path.join(d, "nope.json"))
            self.assertEqual(rc, 1, out + err)
            self.assertIn("not found", (out + err).lower())


# --------------------------------------------------------------------------- #
# Unit-level checks on the pure helpers.
# --------------------------------------------------------------------------- #

class TestContextHelpers(unittest.TestCase):
    def test_structure_check_passes_on_valid(self):
        m = _valid_context()
        res = rq.check_context_structure(m, as_of=m["as_of"])
        self.assertIs(res["passed"], True)

    def test_structure_check_sequential_out_of_order_ok(self):
        # C3, C1, C2 (unordered but complete set) is still sequential.
        m = _valid_context()
        m["findings"] = [
            {"id": "C3", "claim": "c", "source": "s"},
            {"id": "C1", "claim": "c", "source": "s"},
            {"id": "C2", "claim": "c", "source": "s"},
        ]
        res = rq.check_context_structure(m, as_of=m["as_of"])
        self.assertIs(res["passed"], True, res["detail"])

    def test_stamp_context_preserves_content(self):
        with tempfile.TemporaryDirectory() as d:
            m = _valid_context()
            path = os.path.join(d, "module_context.json")
            with open(path, "w") as fh:
                json.dump(m, fh)
            rq._stamp_context(path, m)
            with open(path) as fh:
                out = json.load(fh)
            self.assertIs(out["qc"]["qc_passed"], True)
            self.assertEqual(out["ticker"], "MU")
            self.assertEqual(len(out["findings"]), 3)


if __name__ == "__main__":
    unittest.main()
