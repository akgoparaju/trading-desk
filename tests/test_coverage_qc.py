"""Tests for coverage_qc.py -- the FULL-FSI-depth coverage contract gate (B1/B2).

WHY THIS GATE EXISTS: the user demanded FULL FSI initiation depth three times; the
shipped "proportionate depth" override in full-trade-analysis Phase 0.5 was drift the
user never chose. Project law: "an instruction without a required artifact is a
suggestion." This gate is that required artifact -- it makes depth (a) the DEFAULT,
(b) QC-checkable by script, (c) provenance-recorded via coverage_manifest.json.
Shallow coverage survives ONLY as an explicit per-run user request, disclosed in the
manifest depth_mode ("shallow (user-requested)") and re-checked in --mode shallow.

The depth thresholds are FLOORS, not targets: the FSI initiating-coverage templates
set the real target (Task 1 = 6-8K words / 9 sections; Task 3 comps = 5-10 peers).
The floor exists to make SILENT SHRINKAGE fail loudly, so a coverage run that quietly
degraded below a defensible minimum cannot pass.

Checks mirror report_qc.py house conventions: result dicts {check, passed, detail},
--waive check:reason, PASS/FAIL/WAIVED table, exit 0 (all pass/waived) / 1 otherwise.

stdlib-only; unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import coverage_qc as cq

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CQ = os.path.join(_REPO_ROOT, "scripts", "coverage_qc.py")


# --------------------------------------------------------------------------- #
# Fixture builders: a minimal coverage/ directory that PASSES every full-mode
# check, plus per-check single-mutation breakers. The prose is padded to clear
# the word floors; the numbers in valuation.md are the anchors transcribed from
# valuation_anchors.json (anchors_coherent requires the transcription).
# --------------------------------------------------------------------------- #

# The nine REAL FSI Task-1 research sections (from the cached initiating-coverage
# references/task1-company-research.md §Step 7 structure).
_RESEARCH_SECTIONS = [
    "Company Overview",
    "Company History",
    "Management Team",
    "Products & Services",
    "Customers & Go-to-Market",
    "Industry Overview",
    "Competitive Landscape",
    "Market Opportunity",
    "Risk Assessment",
]

# ~300 words of filler per section, so nine sections clear the full 2500 total floor
# (9 * ~300 = ~2700) AND each clears the 150-word per-section floor.
_FILLER = (
    "The company operates across multiple end markets with a durable competitive "
    "position anchored in scale advantages and process leadership that peers have "
    "struggled to replicate over successive product generations and capital deployment "
    "programs. Management has articulated a coherent capital-allocation framework "
    "balancing reinvestment against shareholder returns, and the historical record "
    "supports the execution narrative through both up-phases and down-phases in the "
    "underlying commodity and demand environment. Revenue drivers span secular and "
    "cyclical components, with the secular attach into accelerated computing offering "
    "a structurally higher-margin mix than the legacy commodity base that dominated "
    "prior periods. Competitive dynamics remain rational among the three principal "
    "suppliers, though the risk of a discipline breakdown persists and is enumerated "
    "in the risk assessment section that follows. Customers concentrate among the "
    "largest cloud and device vendors, creating both concentration risk and a "
    "qualification moat that protects incumbency once a design win is secured through "
    "the lengthy qualification process. The industry structure rewards patient "
    "capital and penalizes latecomers, and the addressable market continues to expand "
    "as new workloads emerge and displace older architectures across the installed "
    "base of deployed systems worldwide. Pricing power ebbs and flows with the supply "
    "and demand balance, but the long-run trajectory of unit demand remains firmly "
    "upward as compute intensity rises across every major end market the company "
    "serves. The management team has demonstrated an ability to navigate the trough "
    "of the cycle without impairing the balance sheet, preserving the optionality to "
    "invest counter-cyclically when competitors are forced to retrench and conserve "
    "cash. That counter-cyclical discipline has historically translated into share "
    "gains coming out of each downturn, compounding the structural advantages the "
    "franchise already enjoys across its core and adjacent product families over time."
)


def _research_md(sections=None, per_section_words=None):
    """A research.md with the given sections (default all nine), each padded."""
    if sections is None:
        sections = _RESEARCH_SECTIONS
    parts = ["# Company Research — MU\n"]
    for name in sections:
        body = _FILLER
        if per_section_words is not None:
            body = " ".join(["word"] * per_section_words)
        parts.append(f"## {name}\n\n{body}\n")
    return "\n".join(parts)


def _model_md(forward_years=("2026E", "2027E", "2028E"),
              latest_hist="2025", statements=("Income Statement",
              "Balance Sheet", "Cash Flow Statement")):
    """A model.md with the three statements + forward-year columns."""
    parts = ["# Financial Model — MU\n",
             f"Latest historical fiscal year: FY{latest_hist}.\n"]
    for st in statements:
        parts.append(f"## {st}\n\nProjected columns: "
                     + ", ".join(forward_years) + ".\n"
                     "Revenue, EBITDA, and net income are projected across the "
                     "forward window off the historical base.\n")
    return "\n".join(parts)


def _valuation_md(comps_rows=5, include_dcf=True, include_scenarios=True,
                  dcf_base="120.00", dcf_bear="95.00", dcf_bull="150.00"):
    """A valuation.md with a DCF section (wacc + terminal growth), a comps table
    with `comps_rows` ticker rows, and bull/base/bear scenario values that
    transcribe the anchors."""
    parts = ["# Valuation — MU\n"]
    if include_dcf:
        # DCF prose transcribes the three anchor figures but deliberately does NOT
        # use the words bull/base/bear — the scenario-value check must key off the
        # Valuation Summary block, not incidental DCF prose.
        parts.append(
            "## DCF Analysis\n\n"
            "WACC of 10.0% and a terminal growth rate of 3.0% discount the "
            f"projected free cash flows to a central estimate of ${dcf_base}, "
            f"bracketed by a downside of ${dcf_bear} and an upside of ${dcf_bull}.\n")
    parts.append("## Comparable Companies\n")
    parts.append("| Ticker | Mkt Cap | EV/EBITDA | P/E |")
    parts.append("| --- | --- | --- | --- |")
    peers = ["SSNLF", "HXSCL", "WDC", "STX", "NVDA", "AMD", "INTC"]
    for i in range(comps_rows):
        t = peers[i % len(peers)]
        parts.append(f"| {t} | 45.0 | 12.5x | 20x |")
    parts.append("")
    if include_scenarios:
        parts.append(
            "## Valuation Summary\n\n"
            f"Bear ${dcf_bear} / Base ${dcf_base} / Bull ${dcf_bull} football "
            "field spans the scenario range.\n")
    return "\n".join(parts)


def _anchors(dcf_base=120.0, dcf_bear=95.0, dcf_bull=150.0,
             comps_low=100.0, comps_high=140.0):
    return {
        "dcf_base": dcf_base, "dcf_bear": dcf_bear, "dcf_bull": dcf_bull,
        "comps_low": comps_low, "comps_high": comps_high, "current_pb": 2.0,
        "assumptions": {"wacc": 0.10, "terminal_g": 0.03},
        "citations": {"dcf": "coverage/valuation.md §DCF",
                      "comps": "coverage/valuation.md §Comps"},
        "as_of": "2026-07-16",
    }


def _manifest(depth_mode="full", subskills=("3-statement-model", "dcf-model",
                                            "comps-analysis")):
    invoked = [{"skill": "equity-research:initiating-coverage",
                "args_summary": "Tasks 1-3, MU"}]
    for s in subskills:
        invoked.append({"skill": f"financial-analysis:{s}",
                        "args_summary": f"MU {s}"})
    return {
        "depth_mode": depth_mode,
        "skills_invoked": invoked,
        "data_endpoints": ["SEC EDGAR 10-K", "company IR", "consensus estimates"],
        "artifacts": ["research.md", "model.md", "valuation.md",
                      "valuation_anchors.json"],
        "generated_utc": "2026-07-16T12:00:00Z",
    }


def _write_coverage(root, *, research=None, model=None, valuation=None,
                    anchors=None, manifest=None, omit=()):
    """Write a coverage/ dir under root. Any of the artifacts can be overridden;
    names in `omit` are skipped entirely (missing-artifact tests)."""
    cov = os.path.join(root, "coverage")
    os.makedirs(cov, exist_ok=True)
    files = {
        "research.md": research if research is not None else _research_md(),
        "model.md": model if model is not None else _model_md(),
        "valuation.md": valuation if valuation is not None else _valuation_md(),
    }
    for name, content in files.items():
        if name in omit:
            continue
        with open(os.path.join(cov, name), "w") as fh:
            fh.write(content)
    if "valuation_anchors.json" not in omit:
        with open(os.path.join(cov, "valuation_anchors.json"), "w") as fh:
            json.dump(anchors if anchors is not None else _anchors(), fh)
    if "coverage_manifest.json" not in omit:
        with open(os.path.join(cov, "coverage_manifest.json"), "w") as fh:
            json.dump(manifest if manifest is not None else _manifest(), fh)
    return cov


def _by_name(results):
    return {r["check"]: r for r in results}


class FullModePasses(unittest.TestCase):
    def test_all_checks_pass_on_a_complete_full_coverage_dir(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)
            results = cq.run_coverage_qc(cov, mode="full")
            failed = [r for r in results if r["passed"] is False]
            self.assertEqual(failed, [], f"unexpected failures: {failed}")
            # every named check is present.
            names = {r["check"] for r in results}
            for expected in ("artifacts_present", "manifest_shape", "fsi_invoked",
                             "subskills_invoked", "research_depth", "model_depth",
                             "valuation_depth", "anchors_coherent"):
                self.assertIn(expected, names)


class ArtifactsPresent(unittest.TestCase):
    def test_missing_research_fails(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, omit=("research.md",))
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["artifacts_present"]
            self.assertIs(r["passed"], False)
            self.assertIn("research.md", r["detail"])

    def test_missing_manifest_fails(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, omit=("coverage_manifest.json",))
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["artifacts_present"]
            self.assertIs(r["passed"], False)
            self.assertIn("coverage_manifest.json", r["detail"])


class ManifestShape(unittest.TestCase):
    def test_missing_depth_mode_fails(self):
        m = _manifest()
        del m["depth_mode"]
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, manifest=m)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["manifest_shape"]
            self.assertIs(r["passed"], False)

    def test_mode_flag_vs_manifest_depth_mode_mismatch_fails(self):
        # manifest says shallow, gate invoked --mode full -> disagreement.
        m = _manifest(depth_mode="shallow (user-requested)")
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, manifest=m)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["manifest_shape"]
            self.assertIs(r["passed"], False)
            self.assertIn("depth_mode", r["detail"])

    def test_malformed_json_fails(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)
            with open(os.path.join(cov, "coverage_manifest.json"), "w") as fh:
                fh.write("{not json")
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["manifest_shape"]
            self.assertIs(r["passed"], False)

    def test_illegal_depth_mode_value_fails(self):
        m = _manifest()
        m["depth_mode"] = "medium"
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, manifest=m)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["manifest_shape"]
            self.assertIs(r["passed"], False)


class FsiInvoked(unittest.TestCase):
    def test_missing_initiating_coverage_fails(self):
        m = _manifest()
        # drop the initiating-coverage entry, keep sub-skills.
        m["skills_invoked"] = [s for s in m["skills_invoked"]
                               if "initiating-coverage" not in s["skill"]]
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, manifest=m)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["fsi_invoked"]
            self.assertIs(r["passed"], False)
            self.assertIn("initiating-coverage", r["detail"])


class SubskillsInvoked(unittest.TestCase):
    def test_fewer_than_two_subskills_fails_in_full_mode(self):
        m = _manifest(subskills=("dcf-model",))  # only one
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, manifest=m)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["subskills_invoked"]
            self.assertIs(r["passed"], False)

    def test_two_distinct_subskills_pass_in_full_mode(self):
        m = _manifest(subskills=("dcf-model", "comps-analysis"))
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, manifest=m)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["subskills_invoked"]
            self.assertIs(r["passed"], True)

    def test_auto_passes_in_shallow_mode(self):
        m = _manifest(depth_mode="shallow (user-requested)", subskills=())
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, manifest=m,
                                  research=_research_md(),
                                  model=_model_md(forward_years=("2026E",)),
                                  valuation=_valuation_md(comps_rows=2))
            # shallow research word floor is lower; use padded default research.
            r = _by_name(cq.run_coverage_qc(cov, mode="shallow"))["subskills_invoked"]
            self.assertIs(r["passed"], True)
            self.assertIn("shallow", r["detail"].lower())


class ResearchDepth(unittest.TestCase):
    def test_missing_section_fails(self):
        secs = _RESEARCH_SECTIONS[:-1]  # drop Risk Assessment
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, research=_research_md(sections=secs))
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["research_depth"]
            self.assertIs(r["passed"], False)
            self.assertIn("Risk Assessment", r["detail"])

    def test_thin_section_below_word_floor_fails(self):
        # all sections present but each only 10 words -> per-section floor fails.
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td,
                                  research=_research_md(per_section_words=10))
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["research_depth"]
            self.assertIs(r["passed"], False)

    def test_per_section_floor_applies_in_shallow_mode(self):
        # The 150-word per-section floor is mode-INDEPENDENT: 100w sections fail
        # even in shallow mode, and the detail names the per-section floor.
        secs_md = _research_md(per_section_words=100)
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, research=secs_md,
                                  manifest=_manifest(
                                      depth_mode="shallow (user-requested)"))
            r = _by_name(cq.run_coverage_qc(cov, mode="shallow"))["research_depth"]
            self.assertIs(r["passed"], False)
            self.assertIn("150", r["detail"])

    def test_shallow_total_floor_is_lower_than_full(self):
        # 9 sections x 170w = 1530w: clears the 150 per-section floor and the
        # shallow 800w total, but sits below the full 2500w total — the same doc
        # passes shallow and fails full on the TOTAL floor.
        secs_md = _research_md(per_section_words=170)
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, research=secs_md,
                                  manifest=_manifest(
                                      depth_mode="shallow (user-requested)"))
            r = _by_name(cq.run_coverage_qc(cov, mode="shallow"))["research_depth"]
            self.assertIs(r["passed"], True, r["detail"])
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, research=secs_md,
                                  manifest=_manifest(depth_mode="full"))
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["research_depth"]
            self.assertIs(r["passed"], False)
            self.assertIn("2500", r["detail"])


class ModelDepth(unittest.TestCase):
    def test_missing_statement_fails(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, model=_model_md(
                statements=("Income Statement", "Balance Sheet")))  # no cash flow
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["model_depth"]
            self.assertIs(r["passed"], False)
            self.assertIn("cash", r["detail"].lower())

    def test_two_forward_years_fails_full(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, model=_model_md(
                forward_years=("2026E", "2027E")))  # only 2 forward
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["model_depth"]
            self.assertIs(r["passed"], False)

    def test_three_forward_years_pass_full(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)  # default 3 forward years
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["model_depth"]
            self.assertIs(r["passed"], True)

    def test_one_forward_year_passes_shallow(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td,
                                  model=_model_md(forward_years=("2026E",)),
                                  manifest=_manifest(
                                      depth_mode="shallow (user-requested)"))
            r = _by_name(cq.run_coverage_qc(cov, mode="shallow"))["model_depth"]
            self.assertIs(r["passed"], True)

    def test_one_forward_year_fails_full(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td,
                                  model=_model_md(forward_years=("2026E",)))
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["model_depth"]
            self.assertIs(r["passed"], False)


class ValuationDepth(unittest.TestCase):
    def test_three_comps_fails_full(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, valuation=_valuation_md(comps_rows=3))
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["valuation_depth"]
            self.assertIs(r["passed"], False)
            self.assertIn("comps", r["detail"].lower())

    def test_four_comps_pass_full(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, valuation=_valuation_md(comps_rows=4))
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["valuation_depth"]
            self.assertIs(r["passed"], True)

    def test_two_comps_pass_shallow(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, valuation=_valuation_md(comps_rows=2),
                                  manifest=_manifest(
                                      depth_mode="shallow (user-requested)"))
            r = _by_name(cq.run_coverage_qc(cov, mode="shallow"))["valuation_depth"]
            self.assertIs(r["passed"], True)

    def test_missing_dcf_terminal_growth_fails(self):
        # WACC named, but NO terminal-growth / terminal-value / perpetuity phrase.
        val = ("# Valuation — MU\n\n## DCF Analysis\n\nWACC 10.0% discounts the "
               "cash flows to $120.00, spanning $95.00 to $150.00.\n"
               "## Comparable Companies\n| Ticker | X |\n| --- | --- |\n"
               "| AAA | 1 |\n| BBB | 1 |\n| CCC | 1 |\n| DDD | 1 |\n"
               "## Summary\nBear $95.00 Base $120.00 Bull $150.00\n")
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, valuation=val)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["valuation_depth"]
            self.assertIs(r["passed"], False)
            self.assertIn("terminal", r["detail"].lower())

    def test_missing_scenarios_fails(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td,
                                  valuation=_valuation_md(include_scenarios=False))
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["valuation_depth"]
            self.assertIs(r["passed"], False)


class AnchorsCoherent(unittest.TestCase):
    def test_valid_anchors_transcribed_pass(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["anchors_coherent"]
            self.assertIs(r["passed"], True)

    def test_missing_required_anchor_key_fails(self):
        a = _anchors()
        del a["dcf_bull"]
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, anchors=a)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["anchors_coherent"]
            self.assertIs(r["passed"], False)
            self.assertIn("dcf_bull", r["detail"])

    def test_negative_anchor_fails(self):
        a = _anchors(dcf_bear=-5.0)
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, anchors=a)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["anchors_coherent"]
            self.assertIs(r["passed"], False)

    def test_anchor_number_absent_from_valuation_md_fails(self):
        # anchors say dcf_base 120; valuation.md transcribes a DIFFERENT base (111).
        val = _valuation_md(dcf_base="111.00")  # bear/bull still match
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, valuation=val)
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["anchors_coherent"]
            self.assertIs(r["passed"], False)
            self.assertIn("valuation.md", r["detail"])

    def test_anchor_transcription_within_tolerance_passes(self):
        # valuation.md prints 120.00; anchor 120.0 -> exact/within 0.5%.
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, valuation=_valuation_md(dcf_base="120.00"))
            r = _by_name(cq.run_coverage_qc(cov, mode="full"))["anchors_coherent"]
            self.assertIs(r["passed"], True)


class Waivers(unittest.TestCase):
    def test_waived_failure_prints_waived_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, valuation=_valuation_md(comps_rows=3))
            proc = subprocess.run(
                [sys.executable, CQ, "--coverage", cov, "--mode", "full",
                 "--waive", "valuation_depth:thin comp set accepted this run"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("WAIVED", proc.stdout)
            self.assertIn("thin comp set accepted", proc.stdout)


class Cli(unittest.TestCase):
    def test_full_pass_exit_zero(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)
            proc = subprocess.run(
                [sys.executable, CQ, "--coverage", cov],  # default --mode full
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("COVERAGE QC: PASS", proc.stdout)
            self.assertIn("PASS", proc.stdout)

    def test_failure_exit_one(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td, omit=("model.md",))
            proc = subprocess.run(
                [sys.executable, CQ, "--coverage", cov, "--mode", "full"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("COVERAGE QC: FAIL", proc.stdout)

    def test_shallow_full_pass_exit_zero(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td,
                                  model=_model_md(forward_years=("2026E",)),
                                  valuation=_valuation_md(comps_rows=2),
                                  manifest=_manifest(
                                      depth_mode="shallow (user-requested)"))
            proc = subprocess.run(
                [sys.executable, CQ, "--coverage", cov, "--mode", "shallow"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("COVERAGE QC: PASS", proc.stdout)

    def test_mode_mismatch_exit_one(self):
        # manifest depth_mode full, invoked --mode shallow -> mismatch fails.
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)  # manifest depth_mode "full"
            proc = subprocess.run(
                [sys.executable, CQ, "--coverage", cov, "--mode", "shallow"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)

    def test_missing_coverage_dir_errors(self):
        proc = subprocess.run(
            [sys.executable, CQ, "--coverage", "/nonexistent/xyz",
             "--mode", "full"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)


# --------------------------------------------------------------------------- #
# O14 — check_adjusted_financials (optional; SKIP when absent)
# --------------------------------------------------------------------------- #

def _valid_adjusted():
    return {
        "core_eps_fwd": 10.85,
        "consensus_eps_fwd": 14.2522,
        "core_roe": 0.318,
        "gaap_roe_ttm": 0.389,
        "one_time_items": [
            {"label": "Q1'26 unrealized equity-securities gain",
             "pre_tax_usd_m": 37700, "period": "2026Q1",
             "source": "coverage/research.md §Q1-2026"},
        ],
        "as_of": "2026-07-21",
        "citations": {"core_eps_fwd": "coverage/model.md §projections_base"},
    }


def _write_adjusted(cov_dir, obj):
    path = os.path.join(cov_dir, "adjusted_financials.json")
    with open(path, "w") as fh:
        if isinstance(obj, str):
            fh.write(obj)
        else:
            json.dump(obj, fh)
    return path


class TestAdjustedFinancialsCheck(unittest.TestCase):
    """check_adjusted_financials: absent -> SKIP; valid -> PASS; malformed -> FAIL."""

    def test_absent_file_returns_skip(self):
        with tempfile.TemporaryDirectory() as td:
            # No adjusted_financials.json present.
            result = cq.check_adjusted_financials(td)
            self.assertIsNone(result["passed"],
                              "absent file should be SKIP (passed=None)")
            self.assertIn("absent", result["detail"].lower())

    def test_valid_adjusted_passes(self):
        with tempfile.TemporaryDirectory() as td:
            _write_adjusted(td, _valid_adjusted())
            result = cq.check_adjusted_financials(td)
            self.assertIs(result["passed"], True, result["detail"])

    def test_missing_core_eps_fwd_fails(self):
        with tempfile.TemporaryDirectory() as td:
            adj = {k: v for k, v in _valid_adjusted().items()
                   if k != "core_eps_fwd"}
            _write_adjusted(td, adj)
            result = cq.check_adjusted_financials(td)
            self.assertIs(result["passed"], False)
            self.assertIn("core_eps_fwd", result["detail"])

    def test_missing_core_roe_fails(self):
        with tempfile.TemporaryDirectory() as td:
            adj = {k: v for k, v in _valid_adjusted().items() if k != "core_roe"}
            _write_adjusted(td, adj)
            result = cq.check_adjusted_financials(td)
            self.assertIs(result["passed"], False)
            self.assertIn("core_roe", result["detail"])

    def test_nonpositive_core_eps_fwd_fails(self):
        with tempfile.TemporaryDirectory() as td:
            adj = {**_valid_adjusted(), "core_eps_fwd": 0.0}
            _write_adjusted(td, adj)
            result = cq.check_adjusted_financials(td)
            self.assertIs(result["passed"], False)
            self.assertIn("positive", result["detail"])

    def test_one_time_item_empty_source_fails(self):
        with tempfile.TemporaryDirectory() as td:
            adj = _valid_adjusted()
            adj["one_time_items"] = [
                {"label": "gain", "pre_tax_usd_m": 1000, "period": "2026Q1",
                 "source": ""}
            ]
            _write_adjusted(td, adj)
            result = cq.check_adjusted_financials(td)
            self.assertIs(result["passed"], False)
            self.assertIn("source", result["detail"])

    def test_one_time_item_empty_label_fails(self):
        with tempfile.TemporaryDirectory() as td:
            adj = _valid_adjusted()
            adj["one_time_items"] = [
                {"label": "", "pre_tax_usd_m": 1000, "period": "2026Q1",
                 "source": "coverage/research.md"}
            ]
            _write_adjusted(td, adj)
            result = cq.check_adjusted_financials(td)
            self.assertIs(result["passed"], False)
            self.assertIn("label", result["detail"])

    def test_empty_citations_fails(self):
        with tempfile.TemporaryDirectory() as td:
            adj = {**_valid_adjusted(), "citations": {}}
            _write_adjusted(td, adj)
            result = cq.check_adjusted_financials(td)
            self.assertIs(result["passed"], False)
            self.assertIn("citations", result["detail"])

    def test_bad_json_fails(self):
        with tempfile.TemporaryDirectory() as td:
            _write_adjusted(td, "{not json")
            result = cq.check_adjusted_financials(td)
            self.assertIs(result["passed"], False)
            self.assertIn("parse", result["detail"])

    def test_absent_does_not_affect_overall_pass(self):
        # A coverage dir without adjusted_financials.json should still PASS
        # overall (SKIP is not a failure).
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)  # no adjusted_financials.json
            results = cq.run_coverage_qc(cov, mode="full")
            failed = [r for r in results if r["passed"] is False]
            self.assertEqual(failed, [], f"unexpected failures: {failed}")
            # The adjusted_financials check is present and is SKIP.
            by_name = {r["check"]: r for r in results}
            self.assertIn("adjusted_financials", by_name)
            self.assertIsNone(by_name["adjusted_financials"]["passed"])

    def test_present_valid_passes_alongside_required_checks(self):
        # A coverage dir WITH a valid adjusted_financials.json passes all checks.
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)
            _write_adjusted(cov, _valid_adjusted())
            results = cq.run_coverage_qc(cov, mode="full")
            failed = [r for r in results if r["passed"] is False]
            self.assertEqual(failed, [], f"unexpected failures: {failed}")
            by_name = {r["check"]: r for r in results}
            self.assertIs(by_name["adjusted_financials"]["passed"], True)

    def test_present_malformed_fails_overall(self):
        # A coverage dir with a malformed adjusted_financials.json fails overall.
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)
            adj = {k: v for k, v in _valid_adjusted().items()
                   if k != "core_eps_fwd"}
            _write_adjusted(cov, adj)
            results = cq.run_coverage_qc(cov, mode="full")
            by_name = {r["check"]: r for r in results}
            self.assertIs(by_name["adjusted_financials"]["passed"], False)

    def test_goog_real_coverage_passes(self):
        # Validate against the real GOOG coverage dir (integration gate).
        goog_cov = (
            "/Users/ankugo/dev/jutsu-trading-desk/trading_desk_GOOG/coverage")
        if not os.path.isdir(goog_cov):
            self.skipTest("GOOG coverage dir not accessible")
        result = cq.check_adjusted_financials(goog_cov)
        self.assertIs(result["passed"], True, result["detail"])


# --------------------------------------------------------------------------- #
# O17 — check_scenario_drivers (optional; SKIP when absent)
# --------------------------------------------------------------------------- #

def _valid_scenario_drivers():
    return {
        "scenarios": {
            "bear": {"eps_fy28": 10.73, "fcf_fy28_m": -313,
                     "rev_growth_path": [0.115, 0.090, 0.070], "op_margin": 0.290},
            "base": {"eps_fy28": 14.16, "fcf_fy28_m": 41794,
                     "rev_growth_path": [0.155, 0.135, 0.120], "op_margin": 0.330},
            "bull": {"eps_fy28": 17.18, "fcf_fy28_m": 80734,
                     "rev_growth_path": [0.185, 0.170, 0.150], "op_margin": 0.360},
        },
        "dcf_reverse_inputs": {
            "pv_explicit_fcf_m": 312850, "pv_terminal_base_m": 1270900,
            "terminal_g_base": 0.03, "wacc": 0.1066, "net_cash_m": 49339,
            "diluted_shares_m": 12238,
        },
        "as_of": "2026-07-21",
        "citations": {"scenarios": "coverage/model.md §scenarios_FY2028E"},
    }


def _write_scenario_drivers(cov_dir, obj):
    path = os.path.join(cov_dir, "scenario_drivers.json")
    with open(path, "w") as fh:
        if isinstance(obj, str):
            fh.write(obj)
        else:
            json.dump(obj, fh)
    return path


class TestScenarioDriversCheck(unittest.TestCase):
    """check_scenario_drivers: absent -> SKIP; valid -> PASS; malformed -> FAIL."""

    def test_absent_file_returns_skip(self):
        with tempfile.TemporaryDirectory() as td:
            result = cq.check_scenario_drivers(td)
            self.assertIsNone(result["passed"],
                              "absent file should be SKIP (passed=None)")
            self.assertIn("absent", result["detail"].lower())

    def test_valid_scenario_drivers_passes(self):
        with tempfile.TemporaryDirectory() as td:
            _write_scenario_drivers(td, _valid_scenario_drivers())
            result = cq.check_scenario_drivers(td)
            self.assertIs(result["passed"], True, result["detail"])

    def test_negative_bear_fcf_still_passes(self):
        # A bear FCF may legitimately be NEGATIVE — no positivity check on it.
        with tempfile.TemporaryDirectory() as td:
            sd = _valid_scenario_drivers()
            sd["scenarios"]["bear"]["fcf_fy28_m"] = -5000
            _write_scenario_drivers(td, sd)
            result = cq.check_scenario_drivers(td)
            self.assertIs(result["passed"], True, result["detail"])

    def test_missing_scenario_tier_fails(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _valid_scenario_drivers()
            del sd["scenarios"]["bull"]
            _write_scenario_drivers(td, sd)
            result = cq.check_scenario_drivers(td)
            self.assertIs(result["passed"], False)
            self.assertIn("bull", result["detail"])

    def test_nonnumeric_eps_fails(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _valid_scenario_drivers()
            sd["scenarios"]["base"]["eps_fy28"] = "n/a"
            _write_scenario_drivers(td, sd)
            result = cq.check_scenario_drivers(td)
            self.assertIs(result["passed"], False)
            self.assertIn("eps_fy28", result["detail"])

    def test_missing_dcf_reverse_inputs_fails(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _valid_scenario_drivers()
            del sd["dcf_reverse_inputs"]
            _write_scenario_drivers(td, sd)
            result = cq.check_scenario_drivers(td)
            self.assertIs(result["passed"], False)
            self.assertIn("dcf_reverse_inputs", result["detail"])

    def test_nonnumeric_dcf_reverse_input_fails(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _valid_scenario_drivers()
            sd["dcf_reverse_inputs"]["wacc"] = "10.66%"
            _write_scenario_drivers(td, sd)
            result = cq.check_scenario_drivers(td)
            self.assertIs(result["passed"], False)
            self.assertIn("wacc", result["detail"])

    def test_empty_citations_fails(self):
        with tempfile.TemporaryDirectory() as td:
            sd = {**_valid_scenario_drivers(), "citations": {}}
            _write_scenario_drivers(td, sd)
            result = cq.check_scenario_drivers(td)
            self.assertIs(result["passed"], False)
            self.assertIn("citations", result["detail"])

    def test_bad_json_fails(self):
        with tempfile.TemporaryDirectory() as td:
            _write_scenario_drivers(td, "{not json")
            result = cq.check_scenario_drivers(td)
            self.assertIs(result["passed"], False)
            self.assertIn("parse", result["detail"])

    def test_absent_does_not_affect_overall_pass(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)  # no scenario_drivers.json
            results = cq.run_coverage_qc(cov, mode="full")
            failed = [r for r in results if r["passed"] is False]
            self.assertEqual(failed, [], f"unexpected failures: {failed}")
            by_name = {r["check"]: r for r in results}
            self.assertIn("scenario_drivers", by_name)
            self.assertIsNone(by_name["scenario_drivers"]["passed"])

    def test_present_valid_passes_alongside_required_checks(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)
            _write_scenario_drivers(cov, _valid_scenario_drivers())
            results = cq.run_coverage_qc(cov, mode="full")
            failed = [r for r in results if r["passed"] is False]
            self.assertEqual(failed, [], f"unexpected failures: {failed}")
            by_name = {r["check"]: r for r in results}
            self.assertIs(by_name["scenario_drivers"]["passed"], True)

    def test_present_malformed_fails_overall(self):
        with tempfile.TemporaryDirectory() as td:
            cov = _write_coverage(td)
            sd = _valid_scenario_drivers()
            del sd["scenarios"]["bear"]
            _write_scenario_drivers(cov, sd)
            results = cq.run_coverage_qc(cov, mode="full")
            by_name = {r["check"]: r for r in results}
            self.assertIs(by_name["scenario_drivers"]["passed"], False)

    def test_goog_real_coverage_passes(self):
        goog_cov = (
            "/Users/ankugo/dev/jutsu-trading-desk/trading_desk_GOOG/coverage")
        if not os.path.isdir(goog_cov):
            self.skipTest("GOOG coverage dir not accessible")
        result = cq.check_scenario_drivers(goog_cov)
        self.assertIs(result["passed"], True, result["detail"])


if __name__ == "__main__":
    unittest.main()
