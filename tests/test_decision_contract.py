"""Tests for scripts/decision_contract.py -- the canonical decision contract (G4a).

WHY: the decision contract is the deterministic capital-status object built from
the already-scored bundle. It exists so page-1 capital language can be rendered
from data rather than narrated by the LLM. Every field is sourced from a real
bundle leaf and every blocker is an EXPLICIT rule. These tests pin:

  1. The blocker rules (each in isolation + combined), so a code/rule drift fails.
  2. The action mapping precedence table.
  3. The full contract against a GOOG-shaped fixture (the live capital-blocked case:
     EV<hurdle + earnings-in-1-day + LOW confidence + valuation conflict ->
     capital_eligible False, action_unowned WAIT_FOR_EVENT).
  4. Graceful degradation when inputs are absent (blocker omitted, never guessed).

stdlib-only; unittest.
"""

import json
import os
import tempfile
import unittest

from scripts import decision_contract as dc


# --------------------------------------------------------------------------- #
# Fixtures: minimal bundle docs mirroring the real GOOG shapes.
# --------------------------------------------------------------------------- #

def _composite(*, ev_at_current=0.059, hurdle_total=0.12, horizon_years=1.5,
               ev_breakeven=332.2321, grade="B", confidence_level="LOW",
               profile="balanced", score=65.4294):
    """A module_composite stub carrying only the fields build_contract reads."""
    return {
        "profile": profile,
        "grade": grade,
        "score": score,
        "as_of": "2026-07-21",
        "confidence": {"level": confidence_level, "version": "1.0.0"},
        "ev": {
            "ev_at_current": ev_at_current,
            "hurdle_total": hurdle_total,
            "horizon_years_convention": horizon_years,
            "ev_breakeven_entry": ev_breakeven,
        },
    }


def _fundamental(*, conflict=True):
    """A module_fundamental stub whose valuation subscore reflects (or not) the
    widened/haircut band. conflict=True -> anchors with disagreement > 0.25 AND a
    WIDEN in the arithmetic (mirrors the real GOOG shape)."""
    if conflict:
        anchors = {"dcf_base": 145.47, "comps_low": 294.0, "comps_high": 436.0}
        arithmetic = ("disagreement |dcf_base 145.47 - comps_mid 365| / mid "
                      "255.235 = 0.8601 > 0.25 -> WIDEN band to [78.2,436]")
    else:
        # dcf_base close to comps_mid -> disagreement <= 0.25, no widen.
        anchors = {"dcf_base": 340.0, "comps_low": 300.0, "comps_high": 400.0}
        arithmetic = "disagreement 0.03 <= 0.25 -> no widen; band held"
    return {
        "subscores": [
            {"name": "quality", "points": 48, "max": 50, "arithmetic": "..."},
            {"name": "valuation", "points": 18.2, "max": 50,
             "arithmetic": arithmetic,
             "inputs": {"anchors": anchors}},
        ],
    }


def _snapshot(*, days_to_event=1, last=351.37):
    return {
        "meta": {"ticker": "GOOG", "as_of_utc": "2026-07-21T17:19:25Z"},
        "price": {"last": last},
        "events": {"days_to_event": days_to_event},
    }


def _goog_docs(**overrides):
    """The full GOOG-shaped docs dict (the capital-blocked live case)."""
    docs = {
        "module_composite": _composite(),
        "module_fundamental": _fundamental(conflict=True),
        "module_tradeplan": {"skill": "trade-plan"},
        "snapshot": _snapshot(),
    }
    docs.update(overrides)
    return docs


# --------------------------------------------------------------------------- #
# 1. Blocker rules (unit table).
# --------------------------------------------------------------------------- #

class TestBlockerRules(unittest.TestCase):
    def test_ev_below_hurdle_fires_when_ev_lt_hurdle(self):
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.059,
            days_to_event=None, composite_confidence_level="HIGH",
            valuation_conflict=False)
        self.assertIn("EV_BELOW_HURDLE", blockers)

    def test_ev_below_hurdle_absent_when_ev_ge_hurdle(self):
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.12,
            days_to_event=None, composite_confidence_level="HIGH",
            valuation_conflict=False)
        self.assertNotIn("EV_BELOW_HURDLE", blockers)

    def test_ev_below_hurdle_omitted_when_ev_none(self):
        # Missing input -> rule skipped, never fabricated.
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=None,
            days_to_event=None, composite_confidence_level="HIGH",
            valuation_conflict=False)
        self.assertNotIn("EV_BELOW_HURDLE", blockers)

    def test_earnings_within_1_day_fires_at_1(self):
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.5,
            days_to_event=1, composite_confidence_level="HIGH",
            valuation_conflict=False)
        self.assertIn("EARNINGS_WITHIN_1_DAY", blockers)

    def test_earnings_within_1_day_fires_at_0(self):
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.5,
            days_to_event=0, composite_confidence_level="HIGH",
            valuation_conflict=False)
        self.assertIn("EARNINGS_WITHIN_1_DAY", blockers)

    def test_earnings_within_1_day_absent_at_2(self):
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.5,
            days_to_event=2, composite_confidence_level="HIGH",
            valuation_conflict=False)
        self.assertNotIn("EARNINGS_WITHIN_1_DAY", blockers)

    def test_earnings_within_1_day_omitted_when_none(self):
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.5,
            days_to_event=None, composite_confidence_level="HIGH",
            valuation_conflict=False)
        self.assertNotIn("EARNINGS_WITHIN_1_DAY", blockers)

    def test_low_confidence_fires_on_low(self):
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.5,
            days_to_event=None, composite_confidence_level="LOW",
            valuation_conflict=False)
        self.assertIn("LOW_COMPOSITE_CONFIDENCE", blockers)

    def test_low_confidence_absent_on_medium_high(self):
        for lvl in ("MEDIUM", "HIGH", None):
            blockers = dc.compute_blockers(
                total_return_hurdle=0.12, ev_at_current=0.5,
                days_to_event=None, composite_confidence_level=lvl,
                valuation_conflict=False)
            self.assertNotIn("LOW_COMPOSITE_CONFIDENCE", blockers, lvl)

    def test_valuation_conflict_fires_on_true(self):
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.5,
            days_to_event=None, composite_confidence_level="HIGH",
            valuation_conflict=True)
        self.assertIn("VALUATION_MODEL_CONFLICT", blockers)

    def test_valuation_conflict_omitted_on_none(self):
        # None (inputs absent) -> omit, never guess.
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.5,
            days_to_event=None, composite_confidence_level="HIGH",
            valuation_conflict=None)
        self.assertNotIn("VALUATION_MODEL_CONFLICT", blockers)

    def test_no_blockers_when_all_clear(self):
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.2,
            days_to_event=10, composite_confidence_level="HIGH",
            valuation_conflict=False)
        self.assertEqual(blockers, [])


# --------------------------------------------------------------------------- #
# 2. Action mapping precedence.
# --------------------------------------------------------------------------- #

class TestActionMapping(unittest.TestCase):
    def test_earnings_wins_precedence(self):
        # Even with EV_BELOW_HURDLE present, earnings takes precedence.
        u, o = dc.map_actions(
            ["EV_BELOW_HURDLE", "EARNINGS_WITHIN_1_DAY"], "B", False)
        self.assertEqual((u, o), ("WAIT_FOR_EVENT", "HOLD_NO_ADD"))

    def test_ev_below_hurdle_when_no_earnings(self):
        u, o = dc.map_actions(
            ["EV_BELOW_HURDLE", "LOW_COMPOSITE_CONFIDENCE"], "B", False)
        self.assertEqual((u, o), ("WAIT_SUB_HURDLE", "HOLD_NO_ADD"))

    def test_accumulate_when_eligible_and_grade_ab(self):
        for g in ("A", "B"):
            u, o = dc.map_actions([], g, True)
            self.assertEqual((u, o), ("ACCUMULATE_ON_WEAKNESS", "HOLD"), g)

    def test_no_entry_when_eligible_but_low_grade(self):
        u, o = dc.map_actions([], "C", True)
        self.assertEqual((u, o), ("NO_ENTRY", "HOLD"))

    def test_no_entry_fallback_when_ineligible_non_ev_non_earnings(self):
        # A blocker set that is neither earnings nor EV -> falls through to else.
        u, o = dc.map_actions(["VALUATION_MODEL_CONFLICT"], "B", False)
        self.assertEqual((u, o), ("NO_ENTRY", "HOLD"))


# --------------------------------------------------------------------------- #
# 3. Full contract on the GOOG fixture (the live capital-blocked case).
# --------------------------------------------------------------------------- #

class TestGoogFixtureContract(unittest.TestCase):
    def test_goog_all_four_blockers_and_wait_for_event(self):
        c = dc.build_contract(_goog_docs())
        self.assertIn("EV_BELOW_HURDLE", c["capital_blockers"])
        self.assertIn("EARNINGS_WITHIN_1_DAY", c["capital_blockers"])
        self.assertIn("LOW_COMPOSITE_CONFIDENCE", c["capital_blockers"])
        self.assertIn("VALUATION_MODEL_CONFLICT", c["capital_blockers"])
        self.assertIs(c["capital_eligible"], False)
        self.assertEqual(c["action_unowned"], "WAIT_FOR_EVENT")
        self.assertEqual(c["action_owned"], "HOLD_NO_ADD")

    def test_goog_field_derivations(self):
        c = dc.build_contract(_goog_docs())
        self.assertEqual(c["profile"], "balanced")
        self.assertEqual(c["horizon_months"], 18.0)          # 1.5 x 12
        self.assertEqual(c["scenario_horizon_months"], 18.0)  # same source
        self.assertAlmostEqual(c["annual_return_hurdle"], 0.08)  # 0.12 / 1.5
        self.assertEqual(c["total_return_hurdle"], 0.12)
        self.assertEqual(c["ev_at_current"], 0.059)
        self.assertEqual(c["hurdle_clearing_price"], 332.2321)
        self.assertEqual(c["grade"], "B")
        # score is carried on the contract (G4b: the page-1 capital-status block
        # renders "composite {score}/100" from this field).
        self.assertEqual(c["score"], 65.4294)

    def test_valuation_conflict_via_anchor_recompute(self):
        # Anchors path (preferred): dcf_base 145.47 vs comps_mid 365 -> conflict.
        self.assertIs(dc._valuation_conflict(_fundamental(conflict=True)), True)
        self.assertIs(dc._valuation_conflict(_fundamental(conflict=False)), False)

    def test_valuation_conflict_arithmetic_fallback(self):
        # No anchors, but arithmetic carries WIDEN -> fallback fires.
        fund = {"subscores": [
            {"name": "valuation", "points": 12, "max": 50,
             "arithmetic": "disagreement 0.86 > 0.25 -> WIDEN band"}]}
        self.assertIs(dc._valuation_conflict(fund), True)

    def test_valuation_conflict_unknown_when_neither(self):
        # No anchors, no WIDEN token, no arithmetic -> None (omit, don't guess).
        fund = {"subscores": [{"name": "valuation", "points": 30, "max": 50}]}
        self.assertIsNone(dc._valuation_conflict(fund))
        # And a bundle with no valuation subscore at all -> None.
        self.assertIsNone(dc._valuation_conflict({"subscores": []}))


# --------------------------------------------------------------------------- #
# 4. Graceful degradation + eligible path.
# --------------------------------------------------------------------------- #

class TestDegradationAndEligible(unittest.TestCase):
    def test_clean_bundle_is_eligible_and_accumulate(self):
        docs = {
            "module_composite": _composite(
                ev_at_current=0.20, confidence_level="HIGH", grade="A"),
            "module_fundamental": _fundamental(conflict=False),
            "snapshot": _snapshot(days_to_event=30),
        }
        c = dc.build_contract(docs)
        self.assertEqual(c["capital_blockers"], [])
        self.assertIs(c["capital_eligible"], True)
        self.assertEqual(c["action_unowned"], "ACCUMULATE_ON_WEAKNESS")
        self.assertEqual(c["action_owned"], "HOLD")

    def test_absent_snapshot_omits_earnings_blocker(self):
        docs = {
            "module_composite": _composite(confidence_level="HIGH"),
            "module_fundamental": _fundamental(conflict=False),
            "snapshot": None,
        }
        c = dc.build_contract(docs)
        self.assertNotIn("EARNINGS_WITHIN_1_DAY", c["capital_blockers"])

    def test_absent_fundamental_omits_valuation_blocker(self):
        docs = {
            "module_composite": _composite(confidence_level="HIGH",
                                           ev_at_current=0.2),
            "module_fundamental": None,
            "snapshot": _snapshot(days_to_event=30),
        }
        c = dc.build_contract(docs)
        self.assertNotIn("VALUATION_MODEL_CONFLICT", c["capital_blockers"])

    def test_missing_horizon_yields_none_derivations(self):
        comp = _composite()
        del comp["ev"]["horizon_years_convention"]
        docs = {"module_composite": comp, "snapshot": _snapshot()}
        c = dc.build_contract(docs)
        self.assertIsNone(c["horizon_months"])
        self.assertIsNone(c["annual_return_hurdle"])


# --------------------------------------------------------------------------- #
# 5. CLI writes module_decision.json.
# --------------------------------------------------------------------------- #

class TestCli(unittest.TestCase):
    def _write_bundle(self, d):
        with open(os.path.join(d, "snapshot_GOOG_2026-07-21.json"), "w") as fh:
            json.dump(_snapshot(), fh)
        with open(os.path.join(d, "module_composite.json"), "w") as fh:
            json.dump(_composite(), fh)
        with open(os.path.join(d, "module_fundamental.json"), "w") as fh:
            json.dump(_fundamental(conflict=True), fh)
        with open(os.path.join(d, "module_tradeplan.json"), "w") as fh:
            json.dump({"skill": "trade-plan"}, fh)

    def test_cli_writes_decision_module(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_bundle(d)
            rc = dc.main(["--bundle", d])
            self.assertEqual(rc, 0)
            out = os.path.join(d, "module_decision.json")
            self.assertTrue(os.path.isfile(out))
            with open(out) as fh:
                contract = json.load(fh)
            self.assertEqual(contract["skill"], "decision-contract")
            self.assertIs(contract["capital_eligible"], False)
            self.assertEqual(contract["action_unowned"], "WAIT_FOR_EVENT")

    def test_cli_errors_without_composite(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "snapshot_GOOG_2026-07-21.json"), "w") as fh:
                json.dump(_snapshot(), fh)
            rc = dc.main(["--bundle", d])
            self.assertEqual(rc, 2)

    def test_cli_errors_on_missing_bundle(self):
        rc = dc.main(["--bundle", "/nonexistent/path/xyz"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
