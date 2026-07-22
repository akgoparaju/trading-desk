"""Tests for report_qc G4a semantic assertions.

WHY: number_provenance catches an invented number; these four checks catch a
number that IS in the bundle but is DESCRIBED with a claim that contradicts the
bundle. Each check FAILs (passed=False) only on a genuine contradiction and SKIPs
(passed=None) when its inputs are absent. Each is tested with a passing fixture,
a failing fixture, and (where relevant) a skip fixture.

The final class is the LIVE-DEFECT catch: check_first_positive_ev_label is run
against the ACTUAL shipped GOOG pdf_slots.json (which says "the first positive-EV
entry is 334.69" while ev_at_current is already +0.059) and MUST FAIL. If that
file is not present in the sibling private repo the test SKIPs (the check itself is
still covered by the synthetic fixtures above).

stdlib-only; unittest.
"""

import json
import os
import unittest

from scripts import report_qc as rq


# --------------------------------------------------------------------------- #
# Minimal docs fixtures.
# --------------------------------------------------------------------------- #

def _docs(*, ev_at_current=0.059, ev_breakeven=332.2321, horizon_years=1.5,
          last=351.37):
    return {
        "module_composite": {
            "rubric_version": "1.1.0",
            "confidence": {"version": "1.0.0", "level": "LOW"},
            "ev": {
                "ev_at_current": ev_at_current,
                "ev_breakeven_entry": ev_breakeven,
                "horizon_years_convention": horizon_years,
                "hurdle_total": 0.12,
            },
        },
        "module_tradeplan": {"rubric_version": "1.1.0",
                             "expression": {"rule_version": "expression-v1.0.0"}},
        "module_fundamental": {"rubric_version": "1.2.0"},
        "snapshot": {"meta": {"schema_version": "0.2.1"},
                     "price": {"last": last}},
    }


# --------------------------------------------------------------------------- #
# check_first_positive_ev_label
# --------------------------------------------------------------------------- #

class TestFirstPositiveEvLabel(unittest.TestCase):
    def test_fail_when_phrase_present_and_ev_positive(self):
        res = rq.check_first_positive_ev_label(
            "the first positive-EV entry is 334.69", _docs(ev_at_current=0.059))
        self.assertIs(res["passed"], False, res["detail"])
        self.assertIn("0.059", res["detail"])

    def test_fail_tolerates_space_variant(self):
        res = rq.check_first_positive_ev_label(
            "the first positive EV entry is 334.69", _docs(ev_at_current=0.059))
        self.assertIs(res["passed"], False, res["detail"])

    def test_pass_when_ev_nonpositive(self):
        res = rq.check_first_positive_ev_label(
            "the first positive-EV entry is 334.69", _docs(ev_at_current=-0.02))
        self.assertIs(res["passed"], True, res["detail"])

    def test_skip_when_phrase_absent(self):
        res = rq.check_first_positive_ev_label(
            "accumulate on weakness below the don't-chase line", _docs())
        self.assertIsNone(res["passed"], res["detail"])

    def test_skip_when_ev_absent(self):
        docs = _docs()
        del docs["module_composite"]["ev"]["ev_at_current"]
        res = rq.check_first_positive_ev_label(
            "the first positive-EV entry is 334.69", docs)
        self.assertIsNone(res["passed"], res["detail"])


# --------------------------------------------------------------------------- #
# check_reclaimed_level
# --------------------------------------------------------------------------- #

class TestReclaimedLevel(unittest.TestCase):
    def test_fail_when_reclaimed_above_last(self):
        res = rq.check_reclaimed_level(
            "the reclaimed swing-low at 353 sits just above", _docs(last=351.37))
        self.assertIs(res["passed"], False, res["detail"])
        self.assertIn("353", res["detail"])

    def test_pass_when_reclaimed_below_last(self):
        res = rq.check_reclaimed_level(
            "price reclaimed above 340", _docs(last=351.37))
        self.assertIs(res["passed"], True, res["detail"])

    def test_skip_forward_looking_directional(self):
        # "reclaim toward 367" names a target, not a claim -> skipped.
        res = rq.check_reclaimed_level(
            "a reclaim toward 367 reasserts the uptrend", _docs(last=351.37))
        self.assertIsNone(res["passed"], res["detail"])

    def test_skip_when_no_reclaim_phrase(self):
        res = rq.check_reclaimed_level("holding the 350 shelf", _docs())
        self.assertIsNone(res["passed"], res["detail"])

    def test_skip_when_last_absent(self):
        docs = _docs()
        del docs["snapshot"]["price"]["last"]
        res = rq.check_reclaimed_level("reclaimed 353", docs)
        self.assertIsNone(res["passed"], res["detail"])


# --------------------------------------------------------------------------- #
# check_version_labels
# --------------------------------------------------------------------------- #

class TestVersionLabels(unittest.TestCase):
    def test_pass_when_all_versions_owned(self):
        # v1.1.0 (composite/tradeplan), v1.2.0 (fundamental), 0.2.1 (schema).
        res = rq.check_version_labels(
            "composite v1.1.0 / fundamental v1.2.0 / schema 0.2.1", _docs())
        self.assertIs(res["passed"], True, res["detail"])

    def test_confidence_version_is_allowed(self):
        # confidence-v1.0.0 is a SEPARATE legitimate artifact version.
        res = rq.check_version_labels("confidence-v1.0.0 badge", _docs())
        self.assertIs(res["passed"], True, res["detail"])

    def test_fail_on_orphan_version(self):
        res = rq.check_version_labels("technical rubric v9.99.99", _docs())
        self.assertIs(res["passed"], False, res["detail"])
        self.assertIn("v9.99.99", res["detail"])

    def test_skip_when_no_version_token(self):
        res = rq.check_version_labels("no versions here", _docs())
        self.assertIsNone(res["passed"], res["detail"])


# --------------------------------------------------------------------------- #
# check_horizon_consistency
# --------------------------------------------------------------------------- #

class TestHorizonConsistency(unittest.TestCase):
    def test_fail_when_12month_but_horizon_not_1(self):
        res = rq.check_horizon_consistency(
            "over the 12-month horizon", _docs(horizon_years=1.5))
        self.assertIs(res["passed"], False, res["detail"])
        self.assertIn("1.5", res["detail"])

    def test_pass_when_12month_and_horizon_1(self):
        res = rq.check_horizon_consistency(
            "over the 12-month horizon", _docs(horizon_years=1.0))
        self.assertIs(res["passed"], True, res["detail"])

    def test_tolerates_space_variant(self):
        res = rq.check_horizon_consistency(
            "a 12 month view", _docs(horizon_years=1.5))
        self.assertIs(res["passed"], False, res["detail"])

    def test_skip_when_no_12month_label(self):
        res = rq.check_horizon_consistency(
            "over the 1.5-year horizon", _docs(horizon_years=1.5))
        self.assertIsNone(res["passed"], res["detail"])

    def test_skip_when_horizon_absent(self):
        docs = _docs()
        del docs["module_composite"]["ev"]["horizon_years_convention"]
        res = rq.check_horizon_consistency("the 12-month horizon", docs)
        self.assertIsNone(res["passed"], res["detail"])


# --------------------------------------------------------------------------- #
# Registration in the gate orchestrators.
# --------------------------------------------------------------------------- #

class TestRegistration(unittest.TestCase):
    def test_pdf_slots_qc_runs_first_positive_ev_check(self):
        # run_pdf_slots_qc must include the first_positive_ev_label result.
        import tempfile
        from tests.test_report_renderer import _mk_bundle
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            slots = {"thesis_bullets": ["neutral prose without the phrase"]}
            results = rq.run_pdf_slots_qc(d, slots)
            names = {r["check"] for r in results}
            self.assertIn("first_positive_ev_label", names)

    def test_report_qc_registers_all_four_semantic_checks(self):
        import tempfile
        from tests.test_report_renderer import (
            _mk_bundle, _render, _find_report, _fill_slots)
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _render(d)
            rep = _find_report(d)
            _fill_slots(rep)
            results = rq.run_report_qc(d, rep)
            names = {r["check"] for r in results}
            for expected in ("first_positive_ev_label", "reclaimed_level",
                             "version_labels", "horizon_consistency"):
                self.assertIn(expected, names, expected)


# --------------------------------------------------------------------------- #
# LIVE-DEFECT CATCH: the actual shipped GOOG pdf_slots.json.
# --------------------------------------------------------------------------- #

_GOOG_BUNDLE = ("/Users/ankugo/dev/jutsu-trading-desk/trading_desk_GOOG/"
                "detail_reports_2026-07-21")


class TestGoogLiveDefect(unittest.TestCase):
    def _load(self, name):
        path = os.path.join(_GOOG_BUNDLE, name)
        if not os.path.isfile(path):
            self.skipTest(f"GOOG bundle file absent: {path}")
        with open(path) as fh:
            return json.load(fh)

    def test_first_positive_ev_fails_on_real_goog_slots(self):
        slots = self._load("pdf_slots.json")
        composite = self._load("module_composite.json")
        docs = {"module_composite": composite}
        slot_text = "\n".join(rq.collect_slot_strings(slots))
        # Sanity: the defect string and the positive EV are both really present.
        self.assertIn("first positive-EV", slot_text)
        self.assertGreater(composite["ev"]["ev_at_current"], 0)
        res = rq.check_first_positive_ev_label(slot_text, docs)
        self.assertIs(res["passed"], False, res["detail"])
        self.assertIn("ev_at_current", res["detail"])


if __name__ == "__main__":
    unittest.main()
