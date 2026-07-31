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


# --------------------------------------------------------------------------- #
# word_cap: ONE-SHOT trim instruction (token-efficiency work).
#
# WHY THESE EXIST: word_cap was the single biggest driver of the report-authoring
# loop. Measured across several real refreshes, the author trimmed a few words and
# re-ran the WHOLE QC per shave, converging on the cap a few words at a time
# (e.g. 2209 -> 2157 -> ... -> 2095), with most QC runs in the phase failing here.
# Each cycle is an API call re-reading the agent's accumulated context.
#
# The failure detail must therefore be ACTIONABLE IN ONE EDIT: total, per-page
# breakdown, the exact cut with margin, and which section is largest. The cap
# itself and the authoring process are deliberately unchanged.
# --------------------------------------------------------------------------- #

def _pages(p1, p2, p3):
    """A minimal report with the three page headers and N filler words each."""
    return ("# T\n\n"
            "## Page 1 — Decision\n" + ("w " * p1) + "\n"
            "## Page 2 — Evidence\n" + ("w " * p2) + "\n"
            "## Page 3 — Context & Protocol\n" + ("w " * p3) + "\n")


def test_word_cap_pass_reports_the_per_page_breakdown():
    r = rq.check_word_cap(_pages(600, 700, 500))
    assert r["passed"] is True
    # +3 per page: _page_sections splits on "## Page ", so "1 — Decision" is in the body
    assert "p1 603" in r["detail"] and "p2 703" in r["detail"] and "p3 505" in r["detail"]


def test_word_cap_fail_states_the_exact_cut_with_margin():
    r = rq.check_word_cap(_pages(900, 1400, 661))
    assert r["passed"] is False
    total = 903 + 1403 + 666
    target = rq._WORD_CAP - rq._WORD_TRIM_MARGIN
    # the instruction must be a single actionable number, not just the total
    assert f"CUT >= {total - target} words" in r["detail"]
    assert f"~{target}" in r["detail"]


def test_word_cap_fail_names_the_largest_section():
    r = rq.check_word_cap(_pages(300, 1900, 300))
    assert "p2 is the largest section" in r["detail"]
    r = rq.check_word_cap(_pages(1900, 300, 300))
    assert "p1 is the largest section" in r["detail"]


def test_word_cap_trim_target_is_below_the_cap_not_equal_to_it():
    """The measured tail was 2103 -> 2101 -> 2100: three cycles for three words.
    The instruction must aim under the cap so one edit ends it."""
    r = rq.check_word_cap(_pages(800, 800, 800))
    total = 801 * 3
    cut = int(r["detail"].split("CUT >= ")[1].split(" words")[0])
    assert total - cut < rq._WORD_CAP
    assert rq._WORD_TRIM_MARGIN > 0


def test_word_cap_enforced_threshold_is_unchanged_by_the_margin():
    """The margin changes only the trim ADVICE. A report between target and cap
    must still PASS -- otherwise this silently tightened the cap."""
    cap = rq._WORD_CAP
    # land between (cap - margin) and cap
    n = cap - rq._WORD_TRIM_MARGIN + 10
    per = n // 3
    r = rq.check_word_cap(_pages(per, per, n - 2 * per - 3))
    assert r["passed"] is True


def test_word_cap_skips_when_no_page_sections():
    r = rq.check_word_cap("no page headers at all")
    assert r["passed"] is None
    assert "SKIP" in r["detail"]


# --------------------------------------------------------------------------- #
# word_cap: WHAT IS COUNTED (1.2.2).
#
# WHY THESE EXIST: the cap was enforced over the RAW page text, which includes
# every script-minted pipe-table row and the mandated ### Data Integrity footer.
# Measured on four production bundles the ZERO-PROSE skeleton alone ran
# 2,816-3,935 words, so 2100 was unsatisfiable by prose editing -- and the one
# thing that did move the number was deleting scripted content. In production an
# author reached PASS by cutting 78% of the mandated footer. Counting only the
# text an author can actually influence is what makes the cap a prose budget
# again; these tests pin exactly what is in and out of the count.
# --------------------------------------------------------------------------- #

def _page_with(body):
    """A one-page report whose Page-1 body is ``body`` verbatim."""
    return "# T\n\n## Page 1 — Decision\n" + body + "\n"


def test_word_cap_excludes_pipe_table_rows():
    prose = "w " * 100
    table = ("| Level | Type | Basis |\n"
             "| --- | --- | --- |\n"
             "| 82 | swing_low | ohlcv |\n"
             "| 90 | ma200 | ohlcv |\n")
    with_table = rq.check_word_cap(_page_with(prose + "\n" + table))
    without = rq.check_word_cap(_page_with(prose))
    assert with_table["detail"] == without["detail"]
    # 100 filler + the 3 words of "1 — Decision" consumed into the body
    assert "p1 103" in with_table["detail"]


def test_word_cap_excludes_indented_table_rows():
    # A row indented under a list item is still a table row.
    r = rq.check_word_cap(_page_with("w w\n   | a | b | c | d | e |\n"))
    assert "p1 5" in r["detail"]   # "1 — Decision" (3) + "w w" (2)


def test_word_cap_excludes_the_data_integrity_section():
    prose = "w " * 50
    footer = ("### Data Integrity\n\n"
              "- API tier notes:\n"
              "  - a very long governance disclosure that must not cost the "
              "author a single word of budget\n"
              "- Plugin version: 1.2.2\n\n"
              "_Not financial advice._\n")
    with_footer = rq.check_word_cap(_page_with(prose + "\n" + footer))
    without = rq.check_word_cap(_page_with(prose))
    assert with_footer["detail"] == without["detail"]


def test_word_cap_data_integrity_exclusion_ends_at_the_next_same_level_heading():
    body = ("### Data Integrity\n" + ("skipped " * 30)
            + "\n### Monitoring Protocol\n" + ("counted " * 7) + "\n")
    r = rq.check_word_cap(_page_with(body))
    # 3 ("1 — Decision") + 3 ("### Monitoring Protocol") + 7 counted
    assert "p1 13" in r["detail"]


def test_word_cap_still_counts_prose_and_headings():
    r = rq.check_word_cap(_page_with("### Technical\n\nreal prose words here\n"))
    assert "p1 9" in r["detail"]   # 3 + "### Technical" (2) + 4 prose words


def test_word_cap_fail_message_states_what_is_excluded():
    r = rq.check_word_cap(_pages(900, 900, 900))
    assert r["passed"] is False
    assert "counting prose only" in r["detail"]
    assert "Data Integrity" in r["detail"]
    assert "table rows" in r["detail"]


def test_word_cap_pass_message_states_what_is_excluded():
    r = rq.check_word_cap(_pages(10, 10, 10))
    assert r["passed"] is True
    assert "counting prose only" in r["detail"]


def test_word_cap_cut_and_largest_section_are_computed_on_filtered_text():
    """The one-shot instruction must point at the PROSE, not at a fat table."""
    fat_table = "| a b c d e f g h |\n" * 400
    body1 = ("w " * 200) + "\n" + fat_table
    body2 = "w " * 2100
    text = ("# T\n\n## Page 1 — Decision\n" + body1
            + "\n## Page 2 — Evidence\n" + body2
            + "\n## Page 3 — Context & Protocol\nw\n")
    r = rq.check_word_cap(text)
    assert r["passed"] is False
    assert "p2 is the largest section" in r["detail"]
    counted = int(r["detail"].split(" words across")[0])
    cut = int(r["detail"].split("CUT >= ")[1].split(" words")[0])
    assert counted - cut == rq._WORD_CAP - rq._WORD_TRIM_MARGIN


def test_word_cap_and_margin_constants_are_unchanged():
    assert rq._WORD_CAP == 2100
    assert rq._WORD_TRIM_MARGIN == 40


# --------------------------------------------------------------------------- #
# footer_completeness (1.2.2, BLOCKING).
#
# WHY THIS EXISTS: footer_integrity asserts three STRINGS -- as_of, the rubric
# versions, and the disclaimer. It says nothing about meta.api_tier_notes, which
# carry the substantive disclosure (which broker served the data, what was left
# null and why). A production report deleted 78% of the mandated footer and
# footer_integrity still passed. This gate makes a dropped note a FAILURE that
# names the note.
# --------------------------------------------------------------------------- #

_NOTES = [
    "All market data fetched via the governed broker mcp__kurama__av_market_data.",
    "iv_history NOT built and iv_pctile_1yr left null: known broker limitation.",
    "pc_ratio_realtime ABSENT: no realtime P/C endpoint on this tier.",
]


def _notes_docs(notes):
    return {"snapshot": {"meta": {"api_tier_notes": notes}}}


def _footer_report(notes):
    body = "\n".join(f"  - {n}" for n in notes)
    return ("# T\n\n## Page 3 — Context & Protocol\n\n### Data Integrity\n\n"
            "- API tier notes:\n" + body + "\n")


def test_footer_completeness_passes_when_every_note_is_present():
    r = rq.check_footer_completeness(_footer_report(_NOTES), _notes_docs(_NOTES))
    assert r["passed"] is True
    assert "all 3" in r["detail"]


def test_footer_completeness_fails_and_names_the_deleted_note():
    report = _footer_report(_NOTES[:2])          # third note deleted
    r = rq.check_footer_completeness(report, _notes_docs(_NOTES))
    assert r["passed"] is False
    assert "1 of 3" in r["detail"]
    assert "pc_ratio_realtime ABSENT" in r["detail"]
    # the notes that ARE present must not be named
    assert "iv_history NOT built" not in r["detail"]


def test_footer_completeness_names_every_missing_note():
    r = rq.check_footer_completeness(_footer_report([]), _notes_docs(_NOTES))
    assert r["passed"] is False
    assert "3 of 3" in r["detail"]
    for note in _NOTES:
        assert note[:40] in r["detail"]


def test_footer_completeness_truncates_a_long_note_in_the_message():
    long_note = "X" * 300
    r = rq.check_footer_completeness("no footer here", _notes_docs([long_note]))
    assert r["passed"] is False
    assert "X" * 80 + "..." in r["detail"]
    assert "X" * 100 not in r["detail"]


def test_footer_completeness_matches_under_whitespace_normalization():
    note = "a note that\n   spans  lines"
    report = "### Data Integrity\n- API tier notes:\n  - a note that spans lines\n"
    r = rq.check_footer_completeness(report, _notes_docs([note]))
    assert r["passed"] is True


def test_footer_completeness_rejects_a_paraphrase():
    r = rq.check_footer_completeness(
        "### Data Integrity\n- API tier notes:\n  - governed broker used\n",
        _notes_docs([_NOTES[0]]))
    assert r["passed"] is False


def test_footer_completeness_passes_on_empty_or_absent_list():
    for docs in (_notes_docs([]), _notes_docs(None), {"snapshot": {"meta": {}}},
                 {"snapshot": {}}, {}):
        r = rq.check_footer_completeness("anything", docs)
        assert r["passed"] is True, docs
        assert "nothing to disclose" in r["detail"]


def test_footer_completeness_is_in_the_full_and_delta_check_sets():
    """It is BLOCKING, and it runs wherever footer_integrity runs."""
    import inspect
    src = inspect.getsource(rq.run_report_qc)
    full, delta = src.count("check_footer_integrity"), src.count(
        "check_footer_completeness")
    assert full == delta == 2


# --------------------------------------------------------------------------- #
# number_provenance: FORMATTED-VARIANT matching (1.2.2).
#
# The renderer now humanizes what it mints ("$1.43B", "41.7"), so the matcher has
# to accept a formatted rendering of a bundle value -- but only at the precision
# the token actually DISPLAYS, or the gate would degrade into "any number that is
# roughly right". Accept AND reject cases are pinned together for that reason.
# --------------------------------------------------------------------------- #

_MKTCAP = 1427214735.0          # the real AMSC market cap


def _prov(text, **price):
    return rq.check_number_provenance(text, {"snapshot": {"price": price}})


def test_provenance_accepts_a_humanized_market_cap():
    assert _prov("Mktcap $1.43B", mktcap=_MKTCAP)["passed"] is True


def test_provenance_rejects_a_market_cap_rounded_past_its_displayed_precision():
    r = _prov("Mktcap $1.5B", mktcap=_MKTCAP)
    assert r["passed"] is False
    assert "$1.5B" in r["detail"]


def test_provenance_rejects_a_wrong_two_decimal_market_cap():
    assert _prov("Mktcap $1.44B", mktcap=_MKTCAP)["passed"] is False


def test_provenance_accepts_a_score_at_its_displayed_precision():
    assert _prov("composite 41.7/100", last=41.6894)["passed"] is True


def test_provenance_rejects_a_score_that_is_not_the_rounding():
    r = _prov("composite 41.8/100", last=41.6894)
    assert r["passed"] is False
    assert "41.8" in r["detail"]


def test_provenance_strips_dollar_signs_and_thousands_commas():
    assert _prov("$1,427,214,735 of market cap", mktcap=_MKTCAP)["passed"] is True


def test_provenance_expands_every_magnitude_suffix():
    assert _prov("$25.0K", last=25000.0)["passed"] is True
    assert _prov("$482.5M", last=482512345.0)["passed"] is True
    assert _prov("$61.0B", last=61002432240.0)["passed"] is True
    assert _prov("$1.20T", last=1200000000000.0)["passed"] is True


def test_provenance_suffix_expansion_is_strict():
    assert _prov("$483.0M", last=482512345.0)["passed"] is False
    assert _prov("$1.21T", last=1200000000000.0)["passed"] is False


def test_provenance_unformatted_tokens_behave_exactly_as_before():
    """An OLD-format report (raw numbers) must pass identically."""
    text = ("last 29.45, level 82, ev 0.175, implied 8.5%, "
            "mktcap 1427214735, breakeven 104.9107")
    r = rq.check_number_provenance(text, {"snapshot": {"price": {
        "last": 29.45, "level": 82.0, "ev": 0.175, "implied": 0.085,
        "mktcap": _MKTCAP, "breakeven": 104.9107}}})
    assert r["passed"] is True


def test_provenance_still_orphans_a_fabricated_number():
    r = _prov("a rogue $123.45 in prose", last=29.45)
    assert r["passed"] is False
    assert "$123.45" in r["detail"]


def test_provenance_a_letter_that_is_not_a_magnitude_is_not_a_suffix():
    # "12MB" must not be read as 12 million; it falls back to the bare 12.
    r = _prov("a 12MB payload", last=12.0)
    assert r["passed"] is True


def test_token_parts_splits_mantissa_scale_and_displayed_decimals():
    assert rq._token_parts("$1.43B") == (1.43, 1e9, 2)
    assert rq._token_parts("482.5M") == (482.5, 1e6, 1)
    assert rq._token_parts("8.5%") == (8.5, 1.0, 1)
    assert rq._token_parts("-1,234") == (1234.0, 1.0, 0)
    assert rq._token_parts("$") == (None, None, None)


def test_strip_suffix_preserves_the_percent_marker():
    assert rq._strip_suffix("$1.43B") == "$1.43"
    assert rq._strip_suffix("8.5%") == "8.5%"        # the % marker survives
    assert rq._strip_suffix("1.4B%") == "1.4%"
    assert rq._strip_suffix("41.7") == "41.7"


def test_is_allowed_formatted_never_rejects_what_is_allowed_accepts():
    """The new path is ADDITIVE: it can only widen, never narrow."""
    src = {"a": 41.6894, "b": _MKTCAP, "c": 0.085, "d": 82.0}
    allowed = rq.build_allowed_set(src)
    match = rq.make_precision_matcher(rq.build_raw_values(src))
    for tok in ("41.69", "41.7", "42", "8.5%", "82", "0.09", "999.99", "3"):
        if rq.is_allowed(tok, allowed):
            assert rq.is_allowed_formatted(tok, allowed, match) is True, tok


def test_build_raw_values_is_literal_magnitudes_only():
    """The percent fold lives in the MATCHER, not the raw set -- composing it with
    a magnitude suffix is a 100x hole (see the $14.3M test below)."""
    raw = rq.build_raw_values({"v": 41.6894, "w": -3.0})
    assert raw == {41.6894, 3.0}


def test_precision_matcher_folds_percent_forms_only_for_unsuffixed_tokens():
    match = rq.make_precision_matcher(rq.build_raw_values({"frac": 0.085}))
    # unsuffixed: the percent RENDERING of the fraction is admitted
    assert match(8.5, 1.0, 1) is True
    assert match(0.085, 1.0, 3) is True
    # suffixed: the percent fold must NOT compose with the magnitude scaling
    assert match(8.5, 1e6, 1) is False


def test_provenance_suffix_on_a_value_stored_in_its_own_unit_still_traces():
    """Bundle units are not uniform: a field stored in MILLIONS (fcf_fy28_m
    3113.2) may legitimately be cited as "3113.2M". The bare part is the correct
    provenance there, and the old scanner validated exactly that -- dropping the
    bare path for suffixed tokens would orphan 5 real citations across the four
    production bundles ("35.3M", "-13.99B", "1.0B", "$113.1M", "731M")."""
    r = rq.check_number_provenance(
        "free cash flow of $3,113.2M", {"snapshot": {"fcf_fy28_m": 3113.2}})
    assert r["passed"] is True


# --------------------------------------------------------------------------- #
# The percent-fold x magnitude-suffix composition hole (review finding, 1.2.2).
#
# build_raw_values used to fold the x100 / /100 percent renderings into the raw
# set. Composed with a K/M/B/T suffix that is a 100x hole: /100 turns a
# 1,427,214,735 market cap into 14,272,147.35, which "$14.3M" then reproduces at
# its displayed precision. Reproduced end-to-end on the real AMSC bundle; the OLD
# gate orphaned that token. The fold now applies ONLY to unsuffixed tokens.
# --------------------------------------------------------------------------- #

def test_provenance_orphans_a_magnitude_that_is_100x_off():
    r = _prov("backlog of $14.3M", mktcap=_MKTCAP)
    assert r["passed"] is False
    assert "$14.3M" in r["detail"]


def test_provenance_orphans_a_magnitude_that_is_one_hundredth_off():
    r = _prov("a market cap of $142.7T", mktcap=_MKTCAP)
    assert r["passed"] is False


def test_provenance_still_accepts_the_correct_magnitude():
    assert _prov("Mktcap $1.43B", mktcap=_MKTCAP)["passed"] is True


def test_provenance_percent_rendering_of_a_fraction_still_passes():
    """The fold that had to be narrowed is still there for unsuffixed tokens."""
    assert _prov("implied move ±8.5%", implied=0.085)["passed"] is True
    assert _prov("EV of 17.5%", ev=0.175)["passed"] is True


# --------------------------------------------------------------------------- #
# _countable_prose: a NESTED Data Integrity heading must not end the skip early.
#
# A delta report nests the footer builder's own "### Data Integrity" inside the
# page-level "## Data Integrity". Taking the inner (deeper) level let the next
# "###" heading terminate the exclusion, so everything after it was counted.
# --------------------------------------------------------------------------- #

def test_countable_prose_nested_data_integrity_keeps_the_outer_level():
    body = ("## Data Integrity\n"
            "### Data Integrity\n"
            + ("skipped " * 20)
            + "\n### Other Heading\n"
            + ("skipped " * 20) + "\n")
    kept = rq._countable_prose(body)
    assert "skipped" not in kept
    assert "Other Heading" not in kept


def test_countable_prose_same_level_heading_still_ends_the_skip():
    body = ("### Data Integrity\n" + ("skipped " * 20)
            + "\n### Other Heading\ncounted words here\n")
    kept = rq._countable_prose(body)
    assert "skipped" not in kept
    assert "counted words here" in kept


def test_countable_prose_shallower_heading_ends_a_nested_skip():
    body = ("## Data Integrity\n### Data Integrity\n" + ("skipped " * 10)
            + "\n## Page Level Heading\ncounted words here\n")
    kept = rq._countable_prose(body)
    assert "skipped" not in kept
    assert "counted words here" in kept
