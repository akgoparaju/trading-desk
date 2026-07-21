"""Tests for report_qc.check_judgment_flag_citations (B29 — Wave 4C).

WHY: The judgment_flag_citations check is a REPORT-TIME referential-integrity
gate that verifies every non-default judgment-flag justification string
(technical divergence, sentiment rating_actions/inst_flow/insider_baseline,
risk top_risk, composite variant/catalyst_clarity) satisfies:
(composite `invalidation` is EXEMPT — it cites trade-plan levels, not context
findings — matching score_composite's own exemption; see the exemption test.)

  1. GROUNDING: the justification contains >= 1 C<n> token (cites a context
     finding).  Zero tokens -> FAIL.
  2. REFERENTIAL INTEGRITY: every cited C<n> exists in module_context.findings[].
     An orphan C-ID (not in the registry) -> FAIL.

When module_context.json is ABSENT (the compressed floor) the check auto-passes
with no registry to validate against.

The check is WIRED INTO the blocking list in run_report_qc (full reports) so a
failure makes the gate exit 1.  It is waivable with
  --waive "judgment_flag_citations:reason"
exactly like the other gate checks.

These tests exercise check_judgment_flag_citations directly (unit level) and
via the full run_report_qc path.  Fixtures are minimal bundle directories
built by _mk_bundle from tests/test_report_renderer.py (the shared MU-shaped
bundle) extended with a module_context.json fixture and per-test module JSON
overrides.

stdlib-only; unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import report_qc as rq

# Reuse the report-layer bundle builder (identical snapshot/module shapes).
from tests.test_report_renderer import (
    _mk_bundle, _technical_doc, _sentiment_doc, _risk_doc, _composite_doc,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QC = os.path.join(_REPO_ROOT, "scripts", "report_qc.py")


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #

def _minimal_context(findings=None):
    """A minimal valid module_context.json with two findings (C1, C2)."""
    if findings is None:
        findings = [
            {"id": "C1", "claim": "HBM3E design wins with lead vendor.",
             "source": "coverage/research.md §Competition"},
            {"id": "C2", "claim": "DRAM pricing recovering off trough.",
             "source": "coverage/model.md §Pricing"},
        ]
    return {
        "skill": "company-context",
        "version": "1.0.0",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "mode": "coverage_distilled",
        "business": {"what_they_sell": "Memory chips (DRAM, NAND)."},
        "competitive": {"position": "Third-largest DRAM maker (C1).",
                        "moat_evidence": ["HBM3E design wins (C1)"],
                        "competitors": ["Samsung"]},
        "live_tape": [
            {"date": "2026-07-15", "event": "Analyst upgrade",
             "why_it_matters": "Supports bull case (C2)."},
        ],
        "cases": {
            "bull": {"narrative": "HBM ramp (C1) re-rates stock.",
                     "conditions": ["HBM attach accelerates"]},
            "base": {"narrative": "In-line ramp (C2).",
                     "conditions": ["Ramp on schedule"]},
            "bear": {"narrative": "Oversupply hits (C1).",
                     "conditions": ["DRAM oversupply resumes"]},
        },
        "risks": [{"risk": "DRAM oversupply", "why": "Commodity cycle.",
                   "anchor": "coverage/research.md §Cycle"}],
        "findings": findings,
        "qc": None,
    }


def _write_context(bundle_dir, module=None):
    """Write module_context.json into bundle_dir. Returns path."""
    if module is None:
        module = _minimal_context()
    path = os.path.join(bundle_dir, "module_context.json")
    with open(path, "w") as fh:
        json.dump(module, fh)
    return path


def _write_module(bundle_dir, filename, doc):
    """Overwrite a module JSON in the bundle."""
    with open(os.path.join(bundle_dir, filename), "w") as fh:
        json.dump(doc, fh)


# --------------------------------------------------------------------------- #
# "Clean" module builders: all judgment flags at neutral defaults, no C-IDs
# needed.  Used to neutralize modules that aren't being tested so the check
# focuses on the module under test.
# --------------------------------------------------------------------------- #

def _clean_technical():
    """module_technical.json with divergence at default 'none'."""
    doc = _technical_doc()
    doc["flags"] = {"divergence": "none", "divergence_justification": None}
    return doc


def _clean_sentiment():
    """module_sentiment.json with all sentiment flags at neutral defaults."""
    doc = _sentiment_doc()
    doc["flags"] = {}
    return doc


def _clean_risk():
    """module_risk.json with no stress scenario (top_risk null, stress_pct null)."""
    doc = _risk_doc()
    doc["flags"] = {"top_risk": None, "stress_pct": None}
    return doc


def _clean_composite():
    """module_composite.json with all conviction flags at their defaults."""
    doc = _composite_doc()
    doc["flags"] = {
        "variant": "none", "variant_justification": None,
        "catalyst_clarity": "vague", "catalyst_clarity_justification": None,
        "invalidation": "none", "invalidation_justification": None,
        "base_rate_check": doc["flags"].get("base_rate_check"),
    }
    return doc


def _mk_clean_bundle(d):
    """Write a full bundle with all judgment flags at neutral defaults.

    This is the baseline for tests that add a single non-default module and
    want the check to be sensitive only to that module.
    """
    _mk_bundle(d)
    _write_module(d, "module_technical.json", _clean_technical())
    _write_module(d, "module_sentiment.json", _clean_sentiment())
    _write_module(d, "module_risk.json", _clean_risk())
    _write_module(d, "module_composite.json", _clean_composite())


# --------------------------------------------------------------------------- #
# Per-module non-default flag builders
# --------------------------------------------------------------------------- #

def _technical_with_divergence(justification):
    """module_technical.json with a non-default divergence flag."""
    doc = _technical_doc()
    doc["flags"] = {
        "divergence": "bearish",
        "divergence_justification": justification,
    }
    return doc


def _sentiment_with_rating_actions(rating_actions, justification):
    """module_sentiment.json with a non-default rating_actions flag."""
    doc = _sentiment_doc()
    doc["flags"] = {
        "rating_actions": rating_actions,
        "rating_actions_justification": justification,
    }
    return doc


def _sentiment_with_inst_flow(inst_flow, justification):
    doc = _sentiment_doc()
    doc["flags"] = {
        "inst_flow": inst_flow,
        "inst_flow_justification": justification,
    }
    return doc


def _risk_with_top_risk(top_risk, stress_pct=-0.30):
    """module_risk.json with a stress scenario (top_risk + stress_pct)."""
    doc = _risk_doc()
    doc["flags"] = {"top_risk": top_risk, "stress_pct": stress_pct}
    return doc


def _composite_with_variant(variant_just):
    """module_composite.json with non-default variant; other flags at defaults."""
    doc = _composite_doc()
    doc["flags"] = {
        "variant": "some",
        "variant_justification": variant_just,
        "catalyst_clarity": "vague",
        "catalyst_clarity_justification": None,
        "invalidation": "none",
        "invalidation_justification": None,
        "base_rate_check": doc["flags"].get("base_rate_check"),
    }
    return doc


# --------------------------------------------------------------------------- #
# 1. Context present + technical divergence citing a valid C-ID -> passes
# --------------------------------------------------------------------------- #

class TestInvalidationExempt(unittest.TestCase):
    def test_invalidation_without_cid_passes(self):
        """Code-review fix (4C): composite `invalidation` is exempt from the C-ID
        gate (its legs cite trade-plan levels + fundamental metrics, not context
        findings — matching score_composite, which does NOT require a C-ID in
        --invalidation-justification). A non-default invalidation whose
        justification cites NO C-ID must still PASS (else a valid composite the
        scorer accepted would fail the report gate)."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            comp = _composite_doc()
            comp["flags"] = {
                "variant": "none", "variant_justification": None,
                "catalyst_clarity": "vague", "catalyst_clarity_justification": None,
                "invalidation": "both-legs",
                "invalidation_justification": "weekly close below 165 support; "
                                              "gross margin below 30%",  # no C-ID
                "base_rate_check": comp["flags"].get("base_rate_check"),
            }
            _write_module(d, "module_composite.json", comp)
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])


class TestPassValidCid(unittest.TestCase):
    def test_technical_divergence_valid_cid_passes(self):
        """Context present; divergence justification cites C1 which is in the
        registry -> grounded + resolved -> passes."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_technical.json",
                          _technical_with_divergence(
                              "price higher highs, RSI lower highs into C1 resistance"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])
            self.assertEqual(res["check"], "judgment_flag_citations")

    def test_risk_top_risk_valid_cid_passes(self):
        """top_risk string cites C1 (in registry) + stress_pct set -> passes."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_risk.json",
                          _risk_with_top_risk(
                              "HBM demand air-pocket (C1) into the next print"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_composite_variant_valid_cid_passes(self):
        """variant_justification cites C2 (in registry) -> passes."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_composite.json",
                          _composite_with_variant(
                              "differentiated on GM path vs street (C2)"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_multiple_valid_flags_across_modules_pass(self):
        """Technical divergence + composite variant, both citing valid C-IDs."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_technical.json",
                          _technical_with_divergence("bearish RSI divergence (C1)"))
            _write_module(d, "module_composite.json",
                          _composite_with_variant("differentiated on GM (C2)"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])
            self.assertIn("2 non-default", res["detail"])


# --------------------------------------------------------------------------- #
# 2. Non-default flag justification with ORPHAN C-ID -> fails (referential
#    integrity)
# --------------------------------------------------------------------------- #

class TestFailOrphanCid(unittest.TestCase):
    def test_orphan_cid_in_technical_divergence_fails(self):
        """Divergence justification cites C99 which is NOT in findings -> fails."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)  # registry has C1, C2 only
            _write_module(d, "module_technical.json",
                          _technical_with_divergence(
                              "price higher highs, RSI lower highs (C99)"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], False, res["detail"])
            self.assertIn("orphan citation", res["detail"])
            self.assertIn("C99", res["detail"])
            self.assertIn("technical", res["detail"])

    def test_orphan_cid_in_sentiment_rating_actions_fails(self):
        """rating_actions_justification cites C77 not in registry -> fails."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_sentiment.json",
                          _sentiment_with_rating_actions(
                              "positive", "3 upgrades post-print (C77)"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], False, res["detail"])
            self.assertIn("C77", res["detail"])
            self.assertIn("orphan", res["detail"])

    def test_orphan_cid_in_composite_variant_fails(self):
        """variant_justification cites C55 not in registry -> fails."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_composite.json",
                          _composite_with_variant(
                              "differentiated on GM path (C55)"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], False, res["detail"])
            self.assertIn("C55", res["detail"])
            self.assertIn("orphan", res["detail"])

    def test_orphan_cid_in_risk_top_risk_fails(self):
        """top_risk cites C88 not in registry -> fails."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_risk.json",
                          _risk_with_top_risk("HBM air-pocket (C88)"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], False, res["detail"])
            self.assertIn("C88", res["detail"])
            self.assertIn("orphan", res["detail"])


# --------------------------------------------------------------------------- #
# 3. Non-default flag justification with NO C-ID -> fails (grounding)
# --------------------------------------------------------------------------- #

class TestFailNoGrounding(unittest.TestCase):
    def test_no_cid_in_technical_divergence_fails(self):
        """Divergence justification has no C<n> token at all -> ungrounded -> fails."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_technical.json",
                          _technical_with_divergence(
                              "price higher highs, RSI lower highs into resistance"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], False, res["detail"])
            self.assertIn("ungrounded", res["detail"])
            self.assertIn("technical", res["detail"])
            self.assertIn("divergence_justification", res["detail"])

    def test_no_cid_in_risk_top_risk_fails(self):
        """top_risk with stress_pct set but no C<n> in the string -> fails."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_risk.json",
                          _risk_with_top_risk("HBM demand air-pocket"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], False, res["detail"])
            self.assertIn("ungrounded", res["detail"])
            self.assertIn("risk", res["detail"])

    def test_no_cid_in_sentiment_inst_flow_fails(self):
        """inst_flow non-default, justification has no C<n> -> fails."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_sentiment.json",
                          _sentiment_with_inst_flow(
                              "accumulating",
                              "13F net buys last quarter — funds adding"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], False, res["detail"])
            self.assertIn("ungrounded", res["detail"])

    def test_no_cid_in_composite_variant_fails(self):
        """variant non-default, justification has no C<n> -> fails."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_composite.json",
                          _composite_with_variant(
                              "consensus underrates HBM growth trajectory"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], False, res["detail"])
            self.assertIn("ungrounded", res["detail"])


# --------------------------------------------------------------------------- #
# 4. Default/neutral flag with no justification -> passes
#    (no requirement for default values)
# --------------------------------------------------------------------------- #

class TestPassDefaultFlag(unittest.TestCase):
    def test_technical_divergence_none_passes_trivially(self):
        """divergence flag is 'none' (the default) — no justification, no C-ID
        required; check trivially passes."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])
            self.assertIn("trivially passes", res["detail"])

    def test_sentiment_all_defaults_passes(self):
        """All sentiment flags at their neutral defaults -> no items to check."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            # _clean_sentiment() has flags={} (no non-default flags)
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_risk_no_stress_pct_passes(self):
        """top_risk non-null but stress_pct null -> not a stress judgment, passes."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            # Set top_risk but NO stress_pct -> condition not met (checked only when
            # top_risk is non-null AND stress_pct is set)
            doc = _risk_doc()
            doc["flags"] = {"top_risk": "HBM demand air-pocket", "stress_pct": None}
            _write_module(d, "module_risk.json", doc)
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_composite_all_defaults_pass(self):
        """All composite conviction flags at their defaults (none/vague/none)."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])


# --------------------------------------------------------------------------- #
# 5. No module_context.json (compressed floor) -> passes automatically
# --------------------------------------------------------------------------- #

class TestPassNoContext(unittest.TestCase):
    def test_no_context_file_auto_passes(self):
        """module_context.json absent -> compressed floor -> auto-pass."""
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)  # uses default composite with non-default flags, no C-IDs
            # No _write_context call -> no module_context.json in bundle
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])
            self.assertIn("compressed floor", res["detail"])

    def test_non_default_flags_without_context_still_auto_pass(self):
        """Even non-default flags without any C-IDs pass when context is absent."""
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _write_module(d, "module_technical.json",
                          _technical_with_divergence("bearish divergence no cid"))
            _write_module(d, "module_composite.json",
                          _composite_with_variant(
                              "very differentiated view — no cid cited"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])
            self.assertIn("compressed floor", res["detail"])


# --------------------------------------------------------------------------- #
# 6. --waive path: waiving the check records the reason and exits zero
# --------------------------------------------------------------------------- #

class TestWaive(unittest.TestCase):
    def test_waive_ungrounded_divergence_via_helper(self):
        """A failing ungrounded-divergence check is waived via _apply_waivers."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_technical.json",
                          _technical_with_divergence("no cid here"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], False)

            waiver_reasons = rq._parse_waivers(
                ["judgment_flag_citations:pre-context run, C-IDs added next session"])
            results, unwaived = rq._apply_waivers([res], waiver_reasons)
            self.assertEqual(unwaived, 0, "waived check should not count as failure")
            self.assertIn("WAIVED", results[0]["detail"])
            self.assertIn("C-IDs added next session", results[0]["detail"])

    def test_waive_via_cli_exits_zero(self):
        """CLI --waive judgment_flag_citations:reason exits 0 even on failure.

        The report skeleton has unfilled SLOT markers (no prose fill in this
        test), so no_empty_slots also fails — we waive it too so that the
        test isolates the judgment_flag_citations waiver path.
        """
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_technical.json",
                          _technical_with_divergence("no cid here at all"))
            RENDER = os.path.join(_REPO_ROOT, "scripts", "render_report.py")
            subprocess.run([sys.executable, RENDER, "--bundle", d],
                           capture_output=True)
            report = next(
                (os.path.join(d, f) for f in os.listdir(d)
                 if "Trade_Report" in f and f.endswith(".md")),
                None,
            )
            if report is None:
                self.skipTest("render failed; cannot test CLI waive path")
            proc = subprocess.run(
                [sys.executable, QC, "--bundle", d, "--report", report,
                 "--waive", "judgment_flag_citations:pre-context run",
                 "--waive", "no_empty_slots:skeleton only test"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("WAIVED", proc.stdout)
            self.assertIn("judgment_flag_citations", proc.stdout)


# --------------------------------------------------------------------------- #
# 7. Absent module JSON is skipped (no failure on missing module)
# --------------------------------------------------------------------------- #

class TestAbsentModuleSkipped(unittest.TestCase):
    def test_missing_technical_module_is_skipped(self):
        """module_technical.json absent -> skip (don't fail); other modules OK."""
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d, technical=False)
            _write_context(d)
            # Re-write the other modules to all-defaults
            _write_module(d, "module_sentiment.json", _clean_sentiment())
            _write_module(d, "module_risk.json", _clean_risk())
            _write_module(d, "module_composite.json", _clean_composite())
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_missing_composite_module_is_skipped(self):
        """module_composite.json absent -> skip; clean other modules -> passes."""
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d, composite=False)
            _write_context(d)
            _write_module(d, "module_technical.json", _clean_technical())
            _write_module(d, "module_sentiment.json", _clean_sentiment())
            _write_module(d, "module_risk.json", _clean_risk())
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], True, res["detail"])


# --------------------------------------------------------------------------- #
# 8. First failure stops and is reported (deterministic ordering)
# --------------------------------------------------------------------------- #

class TestFirstFailureReported(unittest.TestCase):
    def test_technical_checked_before_composite(self):
        """When both technical and composite are ungrounded, technical is reported."""
        with tempfile.TemporaryDirectory() as d:
            _mk_clean_bundle(d)
            _write_context(d)
            _write_module(d, "module_technical.json",
                          _technical_with_divergence("no cid"))
            _write_module(d, "module_composite.json",
                          _composite_with_variant("also no cid"))
            res = rq.check_judgment_flag_citations(d)
            self.assertIs(res["passed"], False)
            # technical is collected before composite in the implementation
            self.assertIn("technical", res["detail"])


if __name__ == "__main__":
    unittest.main()
