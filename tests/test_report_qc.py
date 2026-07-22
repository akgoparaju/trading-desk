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


# =========================================================================== #
# FR-3 decision-contract gates:
#   check_schema_version_presence, check_decision_subset_of_bundle,
#   check_decision_schema.
# =========================================================================== #

from scripts import decision_contract as _dc  # noqa: E402
from scripts._artifact import emit_json as _emit_json, OUTPUT_SCHEMA_VERSION  # noqa: E402

# Dict-shaped sidecar artifacts written into the fixture (beyond the module_*.json
# that _mk_bundle writes). manifest.json is the snapshot-FETCH manifest: it is
# fetch-layer (skill/LLM-authored, never routed through emit_json) and so is
# deliberately left UNSTAMPED here, mirroring the live pipeline — it is EXEMPT from
# the schema_version presence gate, not required. pdf_slots.json is optional
# docket-layer: written AFTER the report gate, absent in md-only mode; when present
# it is stamped via _stamp_slots -> emit_json. scenarios.json is a top-level ARRAY
# input, exempt from the schema_version key.
_UNSTAMPED_FETCH_ARTIFACTS = ("manifest.json",)
_OPTIONAL_DOCKET_ARTIFACTS = ("pdf_slots.json",)


def _mk_decision_bundle(d, *, stamp=True, with_coverage=True,
                        with_pdf_slots=False):
    """Build a full, PASSING FR-3 decision-gates fixture in directory ``d``.

    Writes the MU-shaped module bundle (via _mk_bundle), a derived
    module_decision.json built from the real decision_contract.build_contract
    (so its numeric leaves genuinely trace to the bundle), the fetch-layer
    manifest.json (left UNSTAMPED, as the live pipeline leaves it), and optional
    coverage/*.json. Optionally writes an OPTIONAL docket-layer pdf_slots.json.
    When ``stamp`` is True every OUTPUT-CONTRACT artifact (the 7 scorer modules +
    module_decision.json, plus coverage and any present pdf_slots) is (re)written
    through emit_json so it carries a top-level schema_version; the fetch-layer
    manifest.json is deliberately NOT stamped. Returns the bundle dir.
    """
    _mk_bundle(d)

    # coverage/*.json as a bundle-local subdir (self-contained bundle layout).
    if with_coverage:
        cov = os.path.join(d, "coverage")
        os.makedirs(cov, exist_ok=True)
        _write_json(os.path.join(cov, "valuation_anchors.json"),
                    {"dcf_base": 100.0, "as_of": "2026-07-16"})
        _write_json(os.path.join(cov, "coverage_manifest.json"),
                    {"depth_mode": "standard", "generated_utc": "2026-07-16T00:00:00Z"})

    # Derived decision object from the real builder -> numeric leaves trace.
    docs = _dc.load_docs(d)
    contract = _dc.build_contract(docs)
    _write_json(os.path.join(d, "module_decision.json"), contract)

    # Fetch-layer manifest: present but NEVER stamped (mirrors the live snapshot-
    # fetch manifest, which is not routed through emit_json).
    for name in _UNSTAMPED_FETCH_ARTIFACTS:
        _write_json(os.path.join(d, name), {"generated": True})
    # Optional docket-layer pdf_slots.json: only when requested (absent = md-only
    # mode). When produced, it is stamped through emit_json below.
    if with_pdf_slots:
        for name in _OPTIONAL_DOCKET_ARTIFACTS:
            _write_json(os.path.join(d, name), {"generated": True})
    # scenarios.json: top-level ARRAY input (matches the live shape). Exempt from
    # the schema_version key; the presence gate only requires the file to exist.
    _write_json(os.path.join(d, "scenarios.json"),
                [{"name": "bear", "prob": 0.3, "price_target": 80.0},
                 {"name": "bull", "prob": 0.7, "price_target": 130.0}])

    if stamp:
        _stamp_all_artifacts(d)
    return d


def _write_json(path, doc):
    with open(path, "w") as fh:
        json.dump(doc, fh)


def _bundle_module_files(d):
    """Every present OUTPUT-CONTRACT artifact in the bundle that carries a
    schema_version stamp (paths): every module_*.json and any present optional
    docket-layer artifact (pdf_slots.json). EXCLUDES the snapshot
    (meta.schema_version-stamped), the fetch-layer manifest.json, and the
    coverage/*.json (both are skill-authored inputs never routed through
    emit_json)."""
    out = []
    for name in os.listdir(d):
        if name.startswith("module_") and name.endswith(".json"):
            out.append(os.path.join(d, name))
    for name in _OPTIONAL_DOCKET_ARTIFACTS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            out.append(p)
    return out


def _stamp_all_artifacts(d):
    """Re-emit every output-contract artifact through emit_json so it carries
    schema_version. The snapshot (meta.schema_version), the fetch-layer
    manifest.json, and the transcribed coverage/*.json inputs are deliberately left
    unstamped (they are never routed through emit_json in the live pipeline)."""
    for path in _bundle_module_files(d):
        with open(path) as fh:
            doc = json.load(fh)
        _emit_json(doc, path)


class TestSchemaVersionPresence(unittest.TestCase):
    def test_md_only_bundle_passes(self):
        """The realistic md-only decision-gate bundle — 7 scorer modules +
        module_decision.json stamped, an UNSTAMPED fetch-layer manifest.json, and
        NO pdf_slots.json — must PASS (this is exactly what the live pipeline
        produces at decision-gate time)."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True, with_pdf_slots=False)
            self.assertFalse(os.path.isfile(os.path.join(d, "pdf_slots.json")))
            self.assertTrue(os.path.isfile(os.path.join(d, "manifest.json")))
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_bundle_with_stamped_pdf_slots_passes(self):
        """A present, emit_json-stamped optional docket-layer pdf_slots.json (as
        _stamp_slots now produces) must PASS the presence gate."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True, with_pdf_slots=True)
            path = os.path.join(d, "pdf_slots.json")
            self.assertTrue(os.path.isfile(path))
            with open(path) as fh:
                self.assertIn("schema_version", json.load(fh))
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_present_pdf_slots_without_schema_version_fails(self):
        """When pdf_slots.json IS present it is checked (optional-when-present); an
        UNSTAMPED present pdf_slots.json must FAIL."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True, with_pdf_slots=True)
            path = os.path.join(d, "pdf_slots.json")
            with open(path) as fh:
                doc = json.load(fh)
            doc.pop("schema_version", None)
            _write_json(path, doc)  # write WITHOUT re-stamping
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], False)
            self.assertIn("pdf_slots.json", res["detail"])

    def test_one_missing_schema_version_fails_and_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            # Strip schema_version from exactly one scorer module.
            path = os.path.join(d, "module_composite.json")
            with open(path) as fh:
                doc = json.load(fh)
            doc.pop("schema_version", None)
            _write_json(path, doc)
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], False)
            self.assertIn("module_composite.json", res["detail"])

    def test_required_scorer_module_absent_fails(self):
        """A REQUIRED output-contract scorer module missing from the bundle FAILs."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            os.remove(os.path.join(d, "module_options.json"))
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], False)
            self.assertIn("module_options.json", res["detail"])

    def test_unstamped_manifest_is_exempt(self):
        """manifest.json is the fetch-layer snapshot manifest — never routed through
        emit_json — so an UNSTAMPED (or absent) manifest.json must NOT be flagged."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            # Present-but-unstamped manifest.json: still passes (it is exempt).
            with open(os.path.join(d, "manifest.json")) as fh:
                self.assertNotIn("schema_version", json.load(fh))
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], True, res["detail"])
            self.assertNotIn("manifest.json", res["detail"])
            # Absent manifest.json: also passes (not required).
            os.remove(os.path.join(d, "manifest.json"))
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_unstamped_coverage_inputs_are_exempt(self):
        """valuation_anchors.json / coverage_manifest.json are transcribed coverage
        INPUTS (skill-authored, never routed through emit_json — the skill pins their
        shape with no schema_version). Present-but-unstamped coverage files must NOT
        be flagged (they are gated structurally by coverage_qc.py, not here)."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True, with_coverage=True)
            cov = os.path.join(d, "coverage")
            for name in ("valuation_anchors.json", "coverage_manifest.json"):
                with open(os.path.join(cov, name)) as fh:
                    self.assertNotIn("schema_version", json.load(fh))
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], True, res["detail"])
            self.assertNotIn("valuation_anchors.json", res["detail"])
            self.assertNotIn("coverage_manifest.json", res["detail"])

    def test_snapshot_is_exempt(self):
        """The snapshot has no top-level schema_version (meta.schema_version is its
        concern) and must NOT be flagged."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], True)
            self.assertNotIn("snapshot", res["detail"])

    def test_scenarios_array_is_exempt_from_key(self):
        """scenarios.json is a top-level ARRAY (score_composite input); it cannot
        hold a schema_version key and must NOT be flagged when present as an array."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            # scenarios.json is an array with no schema_version -> still passes.
            with open(os.path.join(d, "scenarios.json")) as fh:
                self.assertIsInstance(json.load(fh), list)
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_scenarios_file_absent_fails(self):
        """scenarios.json is required to be PRESENT (only its key is array-exempt)."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            os.remove(os.path.join(d, "scenarios.json"))
            res = rq.check_schema_version_presence(d)
            self.assertIs(res["passed"], False)
            self.assertIn("scenarios.json", res["detail"])


class TestStampersEmitSchemaVersion(unittest.TestCase):
    """The in-place stampers now route through emit_json, so the file they write
    gains a top-level schema_version (while preserving the qc / qc_passed content
    and the non-sorted indent=2 formatting)."""

    def test_stamp_slots_adds_schema_version(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pdf_slots.json")
            _write_json(path, {"exec_summary": "...", "b_word": "buy"})
            rq._stamp_slots(path, {"exec_summary": "...", "b_word": "buy"})
            with open(path) as fh:
                doc = json.load(fh)
            self.assertIn("schema_version", doc)
            self.assertEqual(doc["schema_version"], OUTPUT_SCHEMA_VERSION)
            # The stamp keys are preserved and the original content survives.
            self.assertIs(doc["qc_passed"], True)
            self.assertIn("checked_utc", doc)
            self.assertEqual(doc["exec_summary"], "...")

    def test_stamp_context_adds_schema_version_and_qc(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "module_context.json")
            module = {"skill": "company-context", "version": "1.0.0",
                      "ticker": "MU", "findings": []}
            _write_json(path, module)
            rq._stamp_context(path, module)
            with open(path) as fh:
                doc = json.load(fh)
            self.assertIn("schema_version", doc)
            self.assertEqual(doc["schema_version"], OUTPUT_SCHEMA_VERSION)
            # qc attestation object set; original module keys preserved.
            self.assertIs(doc["qc"]["qc_passed"], True)
            self.assertIn("checked_utc", doc["qc"])
            self.assertEqual(doc["skill"], "company-context")
            self.assertEqual(doc["ticker"], "MU")


class TestDecisionSubsetOfBundle(unittest.TestCase):
    def test_traceable_decision_passes(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            res = rq.check_decision_subset_of_bundle(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_fabricated_numeric_leaf_fails(self):
        """A non-derived numeric leaf absent from the bundle orphans -> FAIL."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            path = os.path.join(d, "module_decision.json")
            with open(path) as fh:
                dec = json.load(fh)
            # 'score' is a verbatim bundle leaf; overwrite with a value that appears
            # nowhere in the bundle. 'score' is NOT in the derived allowlist.
            dec["score"] = 12345.6789
            _emit_json(dec, path)
            res = rq.check_decision_subset_of_bundle(d)
            self.assertIs(res["passed"], False)
            self.assertIn("score", res["detail"])
            self.assertIn("12345.6789", res["detail"])

    def test_tweaked_derived_days_out_still_passes(self):
        """A derived leaf (catalysts[].days_out) is allowlisted: changing it to a
        value not in the bundle must NOT fail the ⊆-check."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            path = os.path.join(d, "module_decision.json")
            with open(path) as fh:
                dec = json.load(fh)
            cats = dec.get("catalysts") or []
            if not cats:
                # Inject a synthetic catalyst carrying a derived days_out so the
                # allowlist path is exercised even if the MU fixture has none.
                dec["catalysts"] = [{"date_iso": "2026-09-01", "type": "earnings",
                                     "days_out": -99999}]
            else:
                cats[0]["days_out"] = -99999  # value absent from the bundle
            _emit_json(dec, path)
            res = rq.check_decision_subset_of_bundle(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_tweaked_ev_band_still_passes(self):
        """ev_band[*] is a §3-derived (recomputed) leaf and is allowlisted."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            path = os.path.join(d, "module_decision.json")
            with open(path) as fh:
                dec = json.load(fh)
            dec["ev_band"] = [0.111111, 0.999999]  # off-bundle by construction
            _emit_json(dec, path)
            res = rq.check_decision_subset_of_bundle(d)
            self.assertIs(res["passed"], True, res["detail"])


class TestDecisionSchema(unittest.TestCase):
    def test_valid_decision_passes(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            res = rq.check_decision_schema(d)
            self.assertIs(res["passed"], True, res["detail"])

    def test_missing_required_key_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            path = os.path.join(d, "module_decision.json")
            with open(path) as fh:
                dec = json.load(fh)
            dec.pop("entry_state", None)  # a required 1.1.0 capital field
            _emit_json(dec, path)
            res = rq.check_decision_schema(d)
            self.assertIs(res["passed"], False)
            self.assertIn("entry_state", res["detail"])

    def test_wrong_contract_version_const_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            path = os.path.join(d, "module_decision.json")
            with open(path) as fh:
                dec = json.load(fh)
            dec["contract_version"] = "1.0.0"  # schema pins const "2.0.0"
            _emit_json(dec, path)
            res = rq.check_decision_schema(d)
            self.assertIs(res["passed"], False)
            self.assertIn("contract_version", res["detail"])

    def test_bad_operator_enum_fails(self):
        """The FR-6 invalidation.technical.operator enum is pinned; an off-enum
        value must fail the schema check."""
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            path = os.path.join(d, "module_decision.json")
            with open(path) as fh:
                dec = json.load(fh)
            inv = dec.setdefault("invalidation", {}).setdefault("technical", {})
            inv["operator"] = "not_a_real_operator"
            _emit_json(dec, path)
            res = rq.check_decision_schema(d)
            self.assertIs(res["passed"], False)
            self.assertIn("operator", res["detail"])


class TestDecisionGatesCLI(unittest.TestCase):
    def test_cli_pass_on_stamped_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=True)
            proc = subprocess.run(
                [sys.executable, QC, "--bundle", d, "--decision-gates"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("DECISION GATES: PASS", proc.stdout)

    def test_cli_fail_on_unstamped_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_decision_bundle(d, stamp=False)  # no schema_version anywhere
            proc = subprocess.run(
                [sys.executable, QC, "--bundle", d, "--decision-gates"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("DECISION GATES: FAIL", proc.stdout)
            self.assertIn("schema_version", proc.stdout)


if __name__ == "__main__":
    unittest.main()
