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


# The GOOG scenario set (bull/base/bear price_targets) build_contract reads for
# the O10b EV-uncertainty band. Kept as a fixture so the band math is pinned to
# the same numbers the real bundle carries.
_GOOG_SCENARIOS = [
    {"name": "bull", "price_target": 436.0, "prob": 0.3},
    {"name": "base", "price_target": 365.0, "prob": 0.5},
    {"name": "bear", "price_target": 294.0, "prob": 0.2},
]


def _composite_with_scenarios(scenarios=None, **kw):
    """A composite stub carrying the ev.scenarios the band reads (defaults to the
    real GOOG set)."""
    comp = _composite(**kw)
    comp["ev"]["scenarios"] = _GOOG_SCENARIOS if scenarios is None else scenarios
    return comp


def _snapshot(*, days_to_event=1, last=351.37):
    return {
        "meta": {"ticker": "GOOG", "as_of_utc": "2026-07-21T17:19:25Z"},
        "price": {"last": last},
        "events": {"days_to_event": days_to_event},
    }


def _goog_docs(**overrides):
    """The full GOOG-shaped docs dict (the capital-blocked live case).

    Uses the scenario-carrying composite so the O10b EV-uncertainty band is
    exercised on the real GOOG numbers.
    """
    docs = {
        "module_composite": _composite_with_scenarios(),
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
# 4b. O10b: EV-uncertainty band (PROVISIONAL v1.1.0).
# --------------------------------------------------------------------------- #

class TestEvBandMath(unittest.TestCase):
    """The band math + k selection + guards (compute_ev_band is pure)."""

    def test_goog_band_exact_values(self):
        # r_bull = 436/351.37 - 1 ; r_bear = 294/351.37 - 1 ; spread ; k=0.15
        # (softened 2026-07-21 from 0.25).
        band = dc.compute_ev_band(
            last=351.37, scenarios=_GOOG_SCENARIOS,
            ev_at_current=0.059, confidence_level="LOW")
        self.assertAlmostEqual(band["ev_uncertainty_k"], 0.15)
        self.assertEqual(band["ev_uncertainty_confidence_level"], "LOW")
        # spread ≈ 0.40414 ; halfwidth = 0.15 * 0.40414 ≈ 0.06062.
        self.assertAlmostEqual(band["ev_uncertainty_halfwidth"], 0.06062, places=5)
        self.assertAlmostEqual(band["ev_band"][0], -0.00162, places=5)
        self.assertAlmostEqual(band["ev_band"][1], 0.11962, places=5)

    def test_k_selection_table(self):
        for level, k in (("LOW", 0.15), ("MEDIUM", 0.10), ("HIGH", 0.05)):
            band = dc.compute_ev_band(
                last=100.0, scenarios=[{"price_target": 120.0},
                                       {"price_target": 80.0}],
                ev_at_current=0.05, confidence_level=level)
            self.assertAlmostEqual(band["ev_uncertainty_k"], k, msg=level)
            # spread = 0.4 -> halfwidth = k * 0.4.
            self.assertAlmostEqual(band["ev_uncertainty_halfwidth"], k * 0.4,
                                   places=6, msg=level)

    def test_unrecognized_confidence_falls_back_to_low_k(self):
        # Absent / unknown level -> the conservative (widest) LOW k.
        for level in (None, "UNKNOWN", "medium"):  # case-sensitive: 'medium' != MEDIUM
            band = dc.compute_ev_band(
                last=100.0, scenarios=[{"price_target": 120.0},
                                       {"price_target": 80.0}],
                ev_at_current=0.05, confidence_level=level)
            self.assertAlmostEqual(band["ev_uncertainty_k"], 0.15, msg=str(level))
            self.assertEqual(band["ev_uncertainty_confidence_level"], "LOW",
                             msg=str(level))

    def test_guard_single_scenario_yields_none_band(self):
        band = dc.compute_ev_band(
            last=100.0, scenarios=[{"price_target": 120.0}],
            ev_at_current=0.05, confidence_level="LOW")
        self.assertIsNone(band["ev_band"])
        self.assertIsNone(band["ev_uncertainty_halfwidth"])
        self.assertIsNone(band["ev_uncertainty_k"])
        self.assertIsNone(band["ev_uncertainty_confidence_level"])

    def test_guard_nonpositive_last_yields_none_band(self):
        for bad_last in (0, -10.0, None):
            band = dc.compute_ev_band(
                last=bad_last, scenarios=_GOOG_SCENARIOS,
                ev_at_current=0.05, confidence_level="LOW")
            self.assertIsNone(band["ev_band"], msg=str(bad_last))

    def test_guard_absent_ev_yields_none_band(self):
        band = dc.compute_ev_band(
            last=100.0, scenarios=_GOOG_SCENARIOS,
            ev_at_current=None, confidence_level="LOW")
        self.assertIsNone(band["ev_band"])


class TestEvRobustVsHurdle(unittest.TestCase):
    """ev_robust_vs_hurdle: same verdict at both ends True, straddle False."""

    def test_robust_true_when_both_ends_clear(self):
        # both > hurdle 0.12.
        self.assertIs(dc.ev_robust_vs_hurdle([0.13, 0.20], 0.12), True)

    def test_robust_true_when_both_ends_fail(self):
        # both < hurdle 0.12 (GOOG: -0.042 and 0.160 straddles; but both-below case).
        self.assertIs(dc.ev_robust_vs_hurdle([-0.05, 0.10], 0.12), True)

    def test_robust_false_when_band_straddles(self):
        # low < hurdle, high >= hurdle -> verdicts flip -> not robust.
        self.assertIs(dc.ev_robust_vs_hurdle([-0.04203, 0.16003], 0.12), False)

    def test_robust_boundary_low_equals_hurdle_is_robust(self):
        # ev_low == hurdle counts as "clears" (>=) at both ends -> robust.
        self.assertIs(dc.ev_robust_vs_hurdle([0.12, 0.20], 0.12), True)

    def test_none_when_band_or_hurdle_absent(self):
        self.assertIsNone(dc.ev_robust_vs_hurdle(None, 0.12))
        self.assertIsNone(dc.ev_robust_vs_hurdle([0.1, 0.2], None))
        self.assertIsNone(dc.ev_robust_vs_hurdle([0.1], 0.12))


class TestEvNotRobustBlocker(unittest.TestCase):
    """The EV_NOT_ROBUST_UNDER_UNCERTAINTY blocker trigger boundary."""

    def test_blocker_fires_when_point_passes_but_low_fails(self):
        # point 0.13 >= hurdle 0.12 AND band low 0.05 < 0.12 -> blocker.
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.13,
            days_to_event=None, composite_confidence_level="HIGH",
            valuation_conflict=False, ev_band=[0.05, 0.21])
        self.assertIn("EV_NOT_ROBUST_UNDER_UNCERTAINTY", blockers)

    def test_blocker_absent_when_point_below_hurdle(self):
        # point 0.059 < hurdle -> EV_BELOW_HURDLE covers; NOT_ROBUST not added.
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.059,
            days_to_event=None, composite_confidence_level="HIGH",
            valuation_conflict=False, ev_band=[-0.042, 0.160])
        self.assertNotIn("EV_NOT_ROBUST_UNDER_UNCERTAINTY", blockers)
        self.assertIn("EV_BELOW_HURDLE", blockers)  # the covering blocker

    def test_blocker_absent_when_robust_pass(self):
        # point 0.20 >= hurdle AND band low 0.15 >= hurdle -> robust, no blocker.
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.20,
            days_to_event=None, composite_confidence_level="HIGH",
            valuation_conflict=False, ev_band=[0.15, 0.25])
        self.assertNotIn("EV_NOT_ROBUST_UNDER_UNCERTAINTY", blockers)

    def test_blocker_absent_when_no_band(self):
        blockers = dc.compute_blockers(
            total_return_hurdle=0.12, ev_at_current=0.13,
            days_to_event=None, composite_confidence_level="HIGH",
            valuation_conflict=False, ev_band=None)
        self.assertNotIn("EV_NOT_ROBUST_UNDER_UNCERTAINTY", blockers)


class TestGoogBandContract(unittest.TestCase):
    """The full contract's O10b band fields on the GOOG fixture."""

    def test_goog_band_fields_and_no_not_robust_blocker(self):
        c = dc.build_contract(_goog_docs())
        self.assertEqual(c["contract_version"], "2.0.0")
        self.assertAlmostEqual(c["ev_band"][0], -0.00162, places=5)
        self.assertAlmostEqual(c["ev_band"][1], 0.11962, places=5)
        self.assertAlmostEqual(c["ev_uncertainty_halfwidth"], 0.06062, places=5)
        self.assertAlmostEqual(c["ev_uncertainty_k"], 0.15)
        self.assertEqual(c["ev_uncertainty_confidence_level"], "LOW")
        # k softened 0.25->0.15: band [-0.2%, +12.0%] no longer straddles the 12%
        # hurdle (both ends below) -> the below-hurdle verdict is now robust.
        self.assertIs(c["ev_robust_vs_hurdle"], True)
        # GOOG point EV 0.059 < hurdle -> EV_BELOW_HURDLE covers; NOT_ROBUST absent.
        self.assertNotIn("EV_NOT_ROBUST_UNDER_UNCERTAINTY", c["capital_blockers"])
        # The existing four blockers are unchanged.
        self.assertEqual(
            c["capital_blockers"],
            ["EV_BELOW_HURDLE", "EARNINGS_WITHIN_1_DAY",
             "LOW_COMPOSITE_CONFIDENCE", "VALUATION_MODEL_CONFLICT"])
        # provisional_note is carried, versioned, and names the B9 falsifier.
        self.assertIn("decision-contract-v1.1.0 PROVISIONAL", c["provisional_note"])
        self.assertIn("Falsifier (B9)", c["provisional_note"])
        self.assertIn("EV_NOT_ROBUST_UNDER_UNCERTAINTY", c["provisional_note"])

    def test_marginal_point_above_hurdle_blocks_and_ineligible(self):
        # A marginal name: ev_at_current 0.13 (> hurdle 0.12), LOW conf, scenarios
        # giving ev_low < 0.12 -> EV_NOT_ROBUST fires and capital is ineligible.
        comp = _composite_with_scenarios(
            scenarios=[{"price_target": 400.0}, {"price_target": 320.0}],
            ev_at_current=0.13, confidence_level="LOW")
        docs = {
            "module_composite": comp,
            "module_fundamental": _fundamental(conflict=False),
            "snapshot": _snapshot(days_to_event=30, last=351.37),
        }
        c = dc.build_contract(docs)
        self.assertGreaterEqual(c["ev_at_current"], c["total_return_hurdle"])
        self.assertLess(c["ev_band"][0], c["total_return_hurdle"])
        self.assertIn("EV_NOT_ROBUST_UNDER_UNCERTAINTY", c["capital_blockers"])
        self.assertIs(c["capital_eligible"], False)
        # EV_BELOW_HURDLE must NOT be present (point is above hurdle).
        self.assertNotIn("EV_BELOW_HURDLE", c["capital_blockers"])

    def test_band_none_when_scenarios_absent_no_blocker(self):
        # A composite with no ev.scenarios -> band absent, no NOT_ROBUST blocker,
        # and the existing blocker set is unchanged.
        comp = _composite()  # no scenarios key
        docs = {
            "module_composite": comp,
            "module_fundamental": _fundamental(conflict=True),
            "snapshot": _snapshot(),
        }
        c = dc.build_contract(docs)
        self.assertIsNone(c["ev_band"])
        self.assertIsNone(c["ev_uncertainty_k"])
        self.assertIsNone(c["ev_robust_vs_hurdle"])
        self.assertNotIn("EV_NOT_ROBUST_UNDER_UNCERTAINTY", c["capital_blockers"])


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


# --------------------------------------------------------------------------- #
# 6. O19: entry_state field (deterministic, derived from blockers + eligibility).
# --------------------------------------------------------------------------- #

class TestEntryStateDerivation(unittest.TestCase):
    """_derive_entry_state: four-state disclosure, deterministic precedence."""

    def test_earnings_blocker_yields_wait_for_event(self):
        # EARNINGS_WITHIN_1_DAY takes priority over everything.
        state = dc._derive_entry_state(
            ["EV_BELOW_HURDLE", "EARNINGS_WITHIN_1_DAY"], False)
        self.assertEqual(state, "WAIT_FOR_EVENT")

    def test_earnings_only_blocker_yields_wait_for_event(self):
        state = dc._derive_entry_state(["EARNINGS_WITHIN_1_DAY"], False)
        self.assertEqual(state, "WAIT_FOR_EVENT")

    def test_ev_below_hurdle_without_earnings_yields_watch_zone(self):
        # EV_BELOW_HURDLE (no earnings blocker) -> WATCH_ZONE
        state = dc._derive_entry_state(["EV_BELOW_HURDLE"], False)
        self.assertEqual(state, "WATCH_ZONE")

    def test_ev_not_robust_alone_yields_no_entry(self):
        # EV_NOT_ROBUST but neither EARNINGS nor EV_BELOW_HURDLE
        # -> capital_eligible=False -> NO_ENTRY_AT_CURRENT
        state = dc._derive_entry_state(["EV_NOT_ROBUST_UNDER_UNCERTAINTY"], False)
        self.assertEqual(state, "NO_ENTRY_AT_CURRENT")

    def test_eligible_yields_hurdle_clearing_entry(self):
        # No blockers, capital_eligible True -> HURDLE_CLEARING_ENTRY
        state = dc._derive_entry_state([], True)
        self.assertEqual(state, "HURDLE_CLEARING_ENTRY")

    def test_low_confidence_only_ineligible_yields_no_entry(self):
        # LOW_COMPOSITE_CONFIDENCE only -> ineligible, neither earnings nor EV
        state = dc._derive_entry_state(["LOW_COMPOSITE_CONFIDENCE"], False)
        self.assertEqual(state, "NO_ENTRY_AT_CURRENT")


class TestEntryStateInContract(unittest.TestCase):
    """entry_state on the full contract (build_contract integration)."""

    def test_goog_contract_entry_state_wait_for_event(self):
        # GOOG: EARNINGS_WITHIN_1_DAY in blockers -> WAIT_FOR_EVENT
        c = dc.build_contract(_goog_docs())
        self.assertIn("EARNINGS_WITHIN_1_DAY", c["capital_blockers"])
        self.assertEqual(c["entry_state"], "WAIT_FOR_EVENT")

    def test_eligible_contract_entry_state_hurdle_clearing(self):
        # Clean bundle: no blockers, capital_eligible True
        docs = {
            "module_composite": _composite_with_scenarios(
                ev_at_current=0.20, confidence_level="HIGH", grade="A"),
            "module_fundamental": _fundamental(conflict=False),
            "snapshot": _snapshot(days_to_event=30),
        }
        c = dc.build_contract(docs)
        self.assertIs(c["capital_eligible"], True)
        self.assertEqual(c["entry_state"], "HURDLE_CLEARING_ENTRY")

    def test_ev_below_hurdle_no_earnings_entry_state_watch_zone(self):
        # EV below hurdle, no earnings within 1d -> WATCH_ZONE
        docs = {
            "module_composite": _composite_with_scenarios(
                ev_at_current=0.05, confidence_level="HIGH"),
            "module_fundamental": _fundamental(conflict=False),
            "snapshot": _snapshot(days_to_event=30),
        }
        c = dc.build_contract(docs)
        self.assertIn("EV_BELOW_HURDLE", c["capital_blockers"])
        self.assertNotIn("EARNINGS_WITHIN_1_DAY", c["capital_blockers"])
        self.assertEqual(c["entry_state"], "WATCH_ZONE")

    def test_entry_state_field_present_in_contract_output(self):
        # entry_state must be a key in the contract dict.
        c = dc.build_contract(_goog_docs())
        self.assertIn("entry_state", c)


# =========================================================================== #
# 7. contract_version 2.0.0 -- the ADDED consolidated sections (FR-1/2/5/6).
#
# These fixtures mirror the REAL GOOG 2026-07-22 bundle leaves (see the ground-truth
# bundle in the P0 spec). They are kept SEPARATE from the minimal 1.1.0 fixtures
# above so the original 57 tests stay untouched. The docs dict here carries the full
# module family + coverage leaves that load_docs supplies to build_contract.
# =========================================================================== #

# Real GOOG stock_plan (module_tradeplan.stock_plan) -- verbatim shapes.
_GOOG_SIZING = {
    "arithmetic": "f* 65.7% at entry 334.69; capped to 4.0%",
    "binary_event_within_30d": True,
    "cap_pct": 0.04,
    "entry_level": 334.69,
    "f_star": 0.6571,
    "half": 0.3285,
    "headline": "f* 65.7% at entry 334.69; capped to 4.0% (4.0% cap)",
    "profile": "balanced",
    "quarter": 0.1643,
    "rationale": "half the balanced cap (4.0%) due to a binary event within 30d.",
    "recommended_pct": 0.04,
}

_GOOG_ENTRIES = [
    {"basis": "swing_low, confluent with valuation anchor 332.232",
     "condition": "resting limit at 334.69", "confluence": True,
     "confluence_anchor": 332.2321, "ev_at_level": 0.1118, "level": 334.69,
     "type": "swing_low"},
    {"basis": "swing_low", "condition": "resting limit at 321.743",
     "confluence": False, "confluence_anchor": None, "ev_at_level": 0.1565,
     "level": 321.7431, "type": "swing_low"},
]

_GOOG_EXITS = {
    "bull_target": {"comps_high": 436, "dcf_bull": 197.41, "level": 436,
                    "required_multiple": 30.5918, "scenario_raw": 436,
                    "triangulated": True},
    "profit_take": {"level": 350, "type": "oi_cluster"},
}

_GOOG_DONT_CHASE = {"above": 351.4245, "convention": "5% above top entry"}

_GOOG_RISK_UNITS = {
    "arithmetic": "entry_ref=334.69; binding=event_gap 17.2735/sh",
    "binding_leg": "event_gap", "binding_loss_per_share": 17.2735,
    "entry_ref": 334.69, "loss_per_share_event_gap": 17.2735,
    "loss_per_share_stress": None, "loss_per_share_technical": 12.9469,
    "risk_budget_usd": 1000, "shares_per_risk_unit": 57.8921,
}

_GOOG_TECHNICAL_LEG_NO_OPERATOR = {
    "condition": "weekly close below", "level": 321.7431,
}
_GOOG_FUNDAMENTAL_LEG = {
    "justification": "Thesis rests on the Search annuity's durability funding Cloud.",
    "metric": "Search revenue growth + Cloud growth/margin",
    "threshold": "Search revenue turns negative YoY, or Cloud decel <30% w/ margin.",
}

_GOOG_EXPRESSION = {
    "catalyst_in_thesis": False,
    "days_to_catalyst": 0,
    "executable": False,
    "mode_per_profile": {"balanced": "mixed: stock core + CSP at entry_1"},
    "recommended_for_profile": "stock: buy the entry ladder from 334.69, sized 4.0%",
    "recommended_for_profile_options_tilted": "mixed: stock core + CSP",
    "rule_version": "expression-v1.0.0",
    "selector_fired": "profile-default",
    "structures_selected": [],
}


def _goog_tradeplan(*, technical_leg=None):
    """A full module_tradeplan stub carrying the stock_plan + expression the added
    sections read. ``technical_leg`` defaults to the on-disk shape WITHOUT the FR-6
    operator (the trade_plan author adds it going forward)."""
    if technical_leg is None:
        technical_leg = dict(_GOOG_TECHNICAL_LEG_NO_OPERATOR)
    return {
        "skill": "trade-plan",
        "rubric_version": "1.1.0",
        "expression": dict(_GOOG_EXPRESSION),
        "stock_plan": {
            "sizing": dict(_GOOG_SIZING),
            "entries": [dict(e) for e in _GOOG_ENTRIES],
            "exits": {k: dict(v) for k, v in _GOOG_EXITS.items()},
            "dont_chase": dict(_GOOG_DONT_CHASE),
            "risk_units": dict(_GOOG_RISK_UNITS),
            "invalidation": {
                "technical_leg": dict(technical_leg),
                "fundamental_leg": dict(_GOOG_FUNDAMENTAL_LEG),
            },
        },
    }


def _goog_options():
    return {
        "skill": "options-strategy",
        "rubric_version": "1.1.0",
        "liquidity_verdict": "thin -- declining to force structures",
        "declined": [
            {"name": "bull_put_spread", "reason": "liquidity: leg 330 spread 0.9"},
            {"name": "cash_secured_put", "reason": "liquidity: leg 330 spread 0.9"},
        ],
    }


def _goog_full_composite():
    """A composite carrying the verbatim sub-objects the added sections read
    (score/grade/action/sensitivity/confidence/dimensions/ev/flags), on the real
    GOOG numbers. as_of matches the earnings date so days_out==0 by construction."""
    comp = _composite_with_scenarios(
        ev_at_current=0.0748, hurdle_total=0.12, horizon_years=1.5,
        ev_breakeven=332.2321, grade="B", confidence_level="MEDIUM",
        profile="balanced", score=62.25)
    comp["as_of"] = "2026-07-22"
    comp["rubric_version"] = "1.1.0"
    comp["action"] = "Hold/Accumulate-on-weakness"
    comp["confidence"] = {
        "level": "MEDIUM", "rule": "min over evidence dimensions",
        "version": "1.0.0", "why": "MEDIUM -- weekend print"}
    comp["sensitivity"] = {
        "balanced": {"grade": "B", "score": 62.25},
        "long-term": {"grade": "C", "score": 59.1},
        "trader": {"grade": "B", "score": 66.95},
        "weight_set": "standard v1"}
    comp["dimensions"] = [
        {"name": "technical", "score": 69, "weight": 0.25,
         "weight_renormalized": 0.25, "contribution": 17.25,
         "source": "module_technical.json",
         "confidence": {"level": "MEDIUM", "rule": "min(source, depth, staleness)",
                        "source": {"level": "HIGH", "why": "AV premium"},
                        "depth": {"level": "HIGH", "why": "regime-conditional"},
                        "staleness": {"level": "MEDIUM", "why": "weekend print"},
                        "version": "1.0.0"}},
    ]
    comp["flags"] = {
        "variant": "some",
        "variant_justification": "Differentiated on the DCF-vs-comps split.",
        "catalyst_clarity": "clear",
        "catalyst_clarity_justification": "Q2 2026 print reports TODAY (0 days out).",
        "invalidation": "both-legs",
        "invalidation_justification": "Search negative YoY or Cloud decel; stop MA200.",
        "base_rate_check": {"flagged": False, "n_history": 7},
    }
    return comp


def _goog_snapshot_full(*, as_of_utc="2026-07-22T17:02:32Z",
                        earnings_date="2026-07-22", ex_date="2026-06-08",
                        days_to_event=0, last=346.19):
    """A full snapshot carrying meta + events (next_earnings/dividends) the added
    sections + catalyst assembly read. Mirrors the real GOOG 2026-07-22 leaves."""
    return {
        "meta": {
            "ticker": "GOOG",
            "as_of_utc": as_of_utc,
            "latest_trading_day": "2026-07-21",
            "data_mode": "alpha_vantage",
            "missing": [],
        },
        "price": {"last": last},
        "events": {
            "next_earnings": {"date": earnings_date, "time": "post-market",
                              "consensus_eps": 2.86},
            "days_to_event": days_to_event,
            "implied_move": 0.05161038735954245,
            "dividends": {"per_share": 0.84, "ex_date": ex_date,
                          "pay_date": "2026-06-15"},
            "catalysts": [],
        },
    }


_GOOG_VALUATION_ANCHORS = {
    "dcf_base": 145.47, "dcf_bear": 78.20, "dcf_bull": 197.41,
    "comps_low": 294.0, "comps_high": 436.0, "current_pb": 8.82,
    "assumptions": {"wacc": 0.1066, "terminal_g": 0.030},
    "citations": {"dcf": "coverage/valuation.md §DCF",
                  "comps": "coverage/valuation.md §Comps"},
    "as_of": "2026-07-21",
}

_GOOG_COVERAGE_MANIFEST = {
    "depth_mode": "full",
    "skills_invoked": [
        {"skill": "equity-research:initiating-coverage", "args_summary": "Tasks 1-3"},
    ],
    "data_endpoints": ["SEC EDGAR 10-K", "Alpha Vantage MCP"],
    "artifacts": ["research.md", "model.md", "valuation.md"],
    "generated_utc": "2026-07-21T17:42:46Z",
}


def _goog_full_docs(**overrides):
    """The full 2.0.0 docs dict as load_docs would supply it for the real bundle."""
    docs = {
        "module_composite": _goog_full_composite(),
        "module_fundamental": _fundamental(conflict=True),
        "module_tradeplan": _goog_tradeplan(),
        "module_options": _goog_options(),
        "module_technical": {"skill": "technical-analysis", "rubric_version": "1.2.0"},
        "module_sentiment": {"skill": "sentiment-positioning", "rubric_version": "1.1.0"},
        "module_risk": {"skill": "risk-analytics", "rubric_version": "1.1.0"},
        "module_context": {"skill": "company-context", "version": "0.4.0"},
        "snapshot": _goog_snapshot_full(),
        "valuation_anchors": dict(_GOOG_VALUATION_ANCHORS),
        "coverage_manifest": dict(_GOOG_COVERAGE_MANIFEST),
    }
    docs.update(overrides)
    return docs


class TestContractVersion200(unittest.TestCase):
    """The version bump itself + the 1.1.0 fields still present (additive)."""

    def test_contract_version_is_200(self):
        self.assertEqual(dc.CONTRACT_VERSION, "2.0.0")
        c = dc.build_contract(_goog_full_docs())
        self.assertEqual(c["contract_version"], "2.0.0")

    def test_all_1_1_0_fields_still_present(self):
        # 2.0.0 is purely additive: every 1.1.0 top-level field the consumer consumes
        # must still be present and unchanged in shape.
        c = dc.build_contract(_goog_full_docs())
        for key in ("skill", "contract_version", "ticker", "as_of", "profile",
                    "horizon_months", "scenario_horizon_months",
                    "annual_return_hurdle", "total_return_hurdle", "ev_at_current",
                    "hurdle_clearing_price", "grade", "score", "ev_band",
                    "ev_uncertainty_halfwidth", "ev_uncertainty_k",
                    "ev_uncertainty_confidence_level", "ev_robust_vs_hurdle",
                    "provisional_note", "capital_blockers", "capital_eligible",
                    "action_unowned", "action_owned", "entry_state"):
            self.assertIn(key, c, key)


class TestRunUtcAndMeta(unittest.TestCase):
    """run_utc / latest_trading_day / data_mode / missing from snapshot.meta."""

    def test_run_utc_and_latest_trading_day(self):
        c = dc.build_contract(_goog_full_docs())
        self.assertEqual(c["run_utc"], "2026-07-22T17:02:32Z")
        self.assertEqual(c["latest_trading_day"], "2026-07-21")

    def test_data_mode_and_missing_present(self):
        c = dc.build_contract(_goog_full_docs())
        self.assertEqual(c["data_mode"], "alpha_vantage")
        self.assertEqual(c["missing"], [])

    def test_degraded_omitted_when_absent(self):
        # The real bundle carries no 'degraded' leaf -> the key is omitted.
        c = dc.build_contract(_goog_full_docs())
        self.assertNotIn("degraded", c)

    def test_data_mode_falls_back_to_primary_source(self):
        docs = _goog_full_docs()
        docs["snapshot"]["meta"].pop("data_mode")
        docs["snapshot"]["meta"]["primary_source"] = "cached"
        c = dc.build_contract(docs)
        self.assertEqual(c["data_mode"], "cached")


class TestVerbatimComposite(unittest.TestCase):
    """composite / dimensions / ev / flags copied verbatim from module_composite."""

    def test_composite_block_verbatim(self):
        comp = _goog_full_composite()
        c = dc.build_contract(_goog_full_docs(module_composite=comp))
        self.assertEqual(c["composite"]["score"], comp["score"])
        self.assertEqual(c["composite"]["grade"], comp["grade"])
        self.assertEqual(c["composite"]["action"], comp["action"])
        self.assertEqual(c["composite"]["sensitivity"], comp["sensitivity"])
        self.assertEqual(c["composite"]["confidence"], comp["confidence"])

    def test_dimensions_verbatim(self):
        comp = _goog_full_composite()
        c = dc.build_contract(_goog_full_docs(module_composite=comp))
        self.assertEqual(c["dimensions"], comp["dimensions"])
        # nested confidence sub-object carried through unchanged.
        self.assertEqual(c["dimensions"][0]["confidence"]["source"]["why"],
                         "AV premium")

    def test_ev_verbatim(self):
        comp = _goog_full_composite()
        c = dc.build_contract(_goog_full_docs(module_composite=comp))
        self.assertEqual(c["ev"], comp["ev"])
        self.assertEqual(c["ev"]["scenarios"], comp["ev"]["scenarios"])

    def test_flags_verbatim(self):
        comp = _goog_full_composite()
        c = dc.build_contract(_goog_full_docs(module_composite=comp))
        self.assertEqual(c["flags"], comp["flags"])


class TestVerbatimTradeplan(unittest.TestCase):
    """sizing / plan / risk_units / invalidation / expression from tradeplan."""

    def test_sizing_verbatim(self):
        c = dc.build_contract(_goog_full_docs())
        self.assertEqual(c["sizing"], _GOOG_SIZING)

    def test_plan_verbatim(self):
        c = dc.build_contract(_goog_full_docs())
        self.assertEqual(c["plan"]["entries"], _GOOG_ENTRIES)
        self.assertEqual(c["plan"]["exits"], _GOOG_EXITS)
        self.assertEqual(c["plan"]["dont_chase"], _GOOG_DONT_CHASE)

    def test_risk_units_verbatim(self):
        c = dc.build_contract(_goog_full_docs())
        self.assertEqual(c["risk_units"], _GOOG_RISK_UNITS)

    def test_invalidation_technical_and_fundamental(self):
        c = dc.build_contract(_goog_full_docs())
        tech = c["invalidation"]["technical"]
        self.assertEqual(tech["condition"], "weekly close below")
        self.assertEqual(tech["level"], 321.7431)
        # FR-6 operator is CARRIED THROUGH; None on the current on-disk bundle
        # (the trade_plan author adds it going forward).
        self.assertIn("operator", tech)
        self.assertIsNone(tech["operator"])
        self.assertEqual(c["invalidation"]["fundamental"], _GOOG_FUNDAMENTAL_LEG)

    def test_invalidation_operator_carried_when_present(self):
        # When the trade_plan author HAS emitted the FR-6 enum, it is carried
        # verbatim (defensive .get resolves to the real value).
        tp = _goog_tradeplan(technical_leg={
            "condition": "weekly close below", "level": 321.7431,
            "operator": "weekly_close_below"})
        c = dc.build_contract(_goog_full_docs(module_tradeplan=tp))
        self.assertEqual(c["invalidation"]["technical"]["operator"],
                         "weekly_close_below")

    def test_expression_verbatim_plus_options_leaves(self):
        c = dc.build_contract(_goog_full_docs())
        expr = c["expression"]
        self.assertIs(expr["executable"], False)
        self.assertEqual(expr["days_to_catalyst"], 0)
        self.assertEqual(expr["rule_version"], "expression-v1.0.0")
        self.assertEqual(expr["structures_selected"], [])
        # options leaves grafted from module_options.
        self.assertEqual(expr["options_liquidity_verdict"],
                         "thin -- declining to force structures")
        self.assertEqual(len(expr["options_declined"]), 2)


class TestVerbatimCoverage(unittest.TestCase):
    """valuation_anchors / coverage / rubric_versions from coverage + modules."""

    def test_valuation_anchors_verbatim(self):
        c = dc.build_contract(_goog_full_docs())
        self.assertEqual(c["valuation_anchors"], _GOOG_VALUATION_ANCHORS)
        self.assertEqual(c["valuation_anchors"]["assumptions"]["wacc"], 0.1066)

    def test_coverage_verbatim(self):
        c = dc.build_contract(_goog_full_docs())
        self.assertEqual(c["coverage"], _GOOG_COVERAGE_MANIFEST)
        self.assertEqual(c["coverage"]["depth_mode"], "full")

    def test_rubric_versions_assembly(self):
        c = dc.build_contract(_goog_full_docs())
        rv = c["rubric_versions"]
        self.assertEqual(rv["technical"], "1.2.0")
        self.assertEqual(rv["sentiment"], "1.1.0")
        self.assertEqual(rv["risk"], "1.1.0")
        self.assertEqual(rv["composite"], "1.1.0")
        self.assertEqual(rv["tradeplan"], "1.1.0")
        self.assertEqual(rv["options"], "1.1.0")
        self.assertEqual(rv["confidence"], "1.0.0")
        # The _fundamental stub carries no rubric_version leaf -> omitted (the
        # omit-if-source-absent rule holds per key).
        self.assertNotIn("fundamental", rv)

    def test_rubric_versions_includes_fundamental_when_present(self):
        docs = _goog_full_docs()
        docs["module_fundamental"] = dict(docs["module_fundamental"])
        docs["module_fundamental"]["rubric_version"] = "1.2.0"
        c = dc.build_contract(docs)
        self.assertEqual(c["rubric_versions"]["fundamental"], "1.2.0")

    def test_rubric_versions_omits_module_without_rubric_version(self):
        # module_context carries no rubric_version -> 'context' key omitted.
        c = dc.build_contract(_goog_full_docs())
        self.assertNotIn("context", c["rubric_versions"])


class TestCatalystsAssembly(unittest.TestCase):
    """FR-2 catalysts[] -- earnings + dividend assembly incl days_out arithmetic."""

    def test_earnings_catalyst_zero_days_out(self):
        # as_of 2026-07-22 == earnings date -> days_out 0.
        c = dc.build_contract(_goog_full_docs())
        earnings = c["catalysts"][0]
        self.assertEqual(earnings["type"], "earnings")
        self.assertEqual(earnings["date_iso"], "2026-07-22")
        self.assertEqual(earnings["days_out"], 0)
        self.assertIs(earnings["in_thesis"], False)  # <- expression.catalyst_in_thesis
        self.assertAlmostEqual(earnings["implied_move_pct"], 0.05161038735954245)
        self.assertEqual(earnings["consensus_eps"], 2.86)

    def test_earnings_days_out_known_gap(self):
        # Build a fixture where as_of and earnings differ by a known N=6.
        comp = _goog_full_composite()
        comp["as_of"] = "2026-07-16"
        snap = _goog_snapshot_full(earnings_date="2026-07-22")
        c = dc.build_contract(_goog_full_docs(module_composite=comp, snapshot=snap))
        earnings = c["catalysts"][0]
        self.assertEqual(earnings["days_out"], 6)  # 2026-07-22 - 2026-07-16

    def test_earnings_days_out_negative_when_past(self):
        comp = _goog_full_composite()
        comp["as_of"] = "2026-07-25"
        snap = _goog_snapshot_full(earnings_date="2026-07-22")
        c = dc.build_contract(_goog_full_docs(module_composite=comp, snapshot=snap))
        self.assertEqual(c["catalysts"][0]["days_out"], -3)

    def test_days_out_uses_as_of_utc_when_composite_as_of_absent(self):
        comp = _goog_full_composite()
        comp.pop("as_of")  # falls back to snapshot.meta.as_of_utc[:10]
        snap = _goog_snapshot_full(as_of_utc="2026-07-16T12:00:00Z",
                                   earnings_date="2026-07-22")
        c = dc.build_contract(_goog_full_docs(module_composite=comp, snapshot=snap))
        self.assertEqual(c["catalysts"][0]["days_out"], 6)

    def test_dividend_catalyst(self):
        c = dc.build_contract(_goog_full_docs())
        dividend = next(x for x in c["catalysts"] if x["type"] == "dividend")
        self.assertEqual(dividend["date_iso"], "2026-06-08")
        self.assertEqual(dividend["label"], "dividend ex-date")
        self.assertIs(dividend["in_thesis"], False)
        self.assertEqual(dividend["per_share"], 0.84)
        # days_out: 2026-06-08 - 2026-07-22 = -44 (past ex-date).
        self.assertEqual(dividend["days_out"], -44)

    def test_earnings_omitted_when_date_absent(self):
        snap = _goog_snapshot_full()
        snap["events"]["next_earnings"].pop("date")
        c = dc.build_contract(_goog_full_docs(snapshot=snap))
        types = [x["type"] for x in c.get("catalysts", [])]
        self.assertNotIn("earnings", types)

    def test_prewellformed_catalyst_normalized(self):
        # A pre-shaped catalyst keeps its label/date_iso/type and gains days_out.
        snap = _goog_snapshot_full()
        snap["events"]["catalysts"] = [
            {"label": "antitrust ruling", "date_iso": "2026-09-01", "type": "legal"}]
        c = dc.build_contract(_goog_full_docs(snapshot=snap))
        legal = next(x for x in c["catalysts"] if x.get("type") == "legal")
        self.assertEqual(legal["label"], "antitrust ruling")
        self.assertEqual(legal["date_iso"], "2026-09-01")
        self.assertIn("days_out", legal)          # normalized -> days_out always present
        self.assertIs(legal["in_thesis"], False)

    def test_narrative_catalysts_normalized_to_schema_shape(self):
        # The bug: snapshot narrative catalysts are {date, event, impact} and were
        # extended VERBATIM, so they lacked the schema-required date_iso/type/days_out
        # (surfaced by NVDA -- the first ticker with a populated events.catalysts).
        snap = _goog_snapshot_full()  # structured earnings 2026-07-22, dividend 2026-06-08
        snap["events"]["catalysts"] = [
            {"date": "2026-08-01", "event": "Analyst day", "impact": "guidance refresh"},
            {"date": "2026-08-01", "event": "Product launch", "impact": "TPU v7"}]
        c = dc.build_contract(_goog_full_docs(snapshot=snap))
        # EVERY catalyst carries the three schema-required keys (the regression guard).
        for cat in c["catalysts"]:
            for req in ("date_iso", "type", "days_out"):
                self.assertIn(req, cat, "catalyst missing %r: %r" % (req, cat))
        narr = [x for x in c["catalysts"] if x.get("date_iso") == "2026-08-01"]
        self.assertEqual(len(narr), 2)            # two distinct same-date narratives both kept
        self.assertEqual(narr[0]["label"], "Analyst day")
        self.assertEqual(narr[0]["type"], "event")           # default type for a narrative
        self.assertEqual(narr[0]["impact"], "guidance refresh")
        self.assertIsInstance(narr[0]["days_out"], int)

    def test_narrative_catalyst_on_structured_date_deduped(self):
        # A narrative catalyst on the structured earnings date is that event twice -> drop.
        snap = _goog_snapshot_full()  # structured earnings 2026-07-22
        snap["events"]["catalysts"] = [
            {"date": "2026-07-22", "event": "earnings-day writeup", "impact": "x"}]
        c = dc.build_contract(_goog_full_docs(snapshot=snap))
        on_earn = [x for x in c["catalysts"] if x["date_iso"] == "2026-07-22"]
        self.assertEqual(len(on_earn), 1)         # only the structured earnings, not the narrative
        self.assertEqual(on_earn[0]["type"], "earnings")

    def test_malformed_narrative_catalyst_skipped(self):
        snap = _goog_snapshot_full()
        snap["events"]["catalysts"] = ["not-a-dict", {"event": "no date"}]
        c = dc.build_contract(_goog_full_docs(snapshot=snap))
        for cat in c["catalysts"]:            # unusable entries skipped, no half-shaped emit
            self.assertIsInstance(cat.get("date_iso"), str)

    def test_catalysts_omitted_when_no_events(self):
        docs = _goog_full_docs()
        docs["snapshot"] = {"meta": {"ticker": "GOOG",
                                     "as_of_utc": "2026-07-22T00:00:00Z"},
                            "price": {"last": 346.19}}
        c = dc.build_contract(docs)
        self.assertNotIn("catalysts", c)


class TestThesisIdentity(unittest.TestCase):
    """FR-5 thesis.id -- deterministic + refresh-stable + graceful omission."""

    def test_thesis_id_deterministic(self):
        c = dc.build_contract(_goog_full_docs())
        self.assertEqual(c["thesis"]["registered_date"], "2026-07-21")
        self.assertEqual(c["thesis"]["id"], "GOOG-2026-07-21")
        self.assertEqual(c["thesis"]["variant"], "some")
        self.assertEqual(c["thesis"]["catalyst_clarity"], "clear")

    def test_thesis_omits_next_review(self):
        c = dc.build_contract(_goog_full_docs())
        self.assertNotIn("next_review", c["thesis"])

    def test_thesis_id_stable_across_refresh(self):
        # Same coverage_manifest -> same id even when composite/as_of change.
        docs1 = _goog_full_docs()
        id1 = dc.build_contract(docs1)["thesis"]["id"]

        comp2 = _goog_full_composite()
        comp2["as_of"] = "2026-08-15"  # refreshed later
        comp2["score"] = 71.0
        comp2["grade"] = "A"
        snap2 = _goog_snapshot_full(as_of_utc="2026-08-15T17:00:00Z")
        docs2 = _goog_full_docs(module_composite=comp2, snapshot=snap2)
        # coverage_manifest carried forward unchanged (built once).
        id2 = dc.build_contract(docs2)["thesis"]["id"]

        self.assertEqual(id1, id2)
        self.assertEqual(id2, "GOOG-2026-07-21")

    def test_thesis_omitted_when_coverage_manifest_absent(self):
        docs = _goog_full_docs(coverage_manifest=None)
        c = dc.build_contract(docs)
        self.assertNotIn("thesis", c)


class TestGracefulOmission(unittest.TestCase):
    """Each added section omitted (never fabricated) when its source module is None.

    Preserves the module's 'omit-if-source-absent' rule for the 2.0.0 sections.
    """

    def test_absent_tradeplan_omits_plan_sizing_expression(self):
        docs = _goog_full_docs(module_tradeplan=None)
        c = dc.build_contract(docs)
        for key in ("sizing", "plan", "risk_units", "invalidation"):
            self.assertNotIn(key, c, key)
        # expression still carries the options leaves (from module_options) but no
        # tradeplan leaves -> present with only options keys.
        self.assertIn("options_liquidity_verdict", c.get("expression", {}))
        self.assertNotIn("executable", c.get("expression", {}))
        self.assertEqual(c["rubric_versions"].get("tradeplan"), None)

    def test_absent_options_omits_options_expression_leaves(self):
        docs = _goog_full_docs(module_options=None)
        c = dc.build_contract(docs)
        self.assertNotIn("options_liquidity_verdict", c.get("expression", {}))
        self.assertNotIn("options_declined", c.get("expression", {}))
        self.assertNotIn("options", c["rubric_versions"])

    def test_absent_valuation_anchors_omits_block(self):
        c = dc.build_contract(_goog_full_docs(valuation_anchors=None))
        self.assertNotIn("valuation_anchors", c)

    def test_absent_coverage_manifest_omits_coverage_and_thesis(self):
        c = dc.build_contract(_goog_full_docs(coverage_manifest=None))
        self.assertNotIn("coverage", c)
        self.assertNotIn("thesis", c)

    def test_absent_snapshot_omits_run_utc_and_catalysts(self):
        c = dc.build_contract(_goog_full_docs(snapshot=None))
        self.assertNotIn("run_utc", c)
        self.assertNotIn("catalysts", c)


class TestDerivedFieldAllowlist(unittest.TestCase):
    """The §3 derived-field allowlist items are present in the contract output."""

    def test_derived_fields_present(self):
        c = dc.build_contract(_goog_full_docs())
        # The derived leaves (computed, not verbatim) must all be present.
        for key in ("annual_return_hurdle", "horizon_months",
                    "scenario_horizon_months", "ev_band",
                    "ev_uncertainty_halfwidth", "ev_uncertainty_k",
                    "ev_robust_vs_hurdle", "hurdle_clearing_price",
                    "capital_eligible", "capital_blockers", "action_owned",
                    "action_unowned", "entry_state", "contract_version"):
            self.assertIn(key, c, key)
        # nested derived leaves.
        self.assertEqual(c["catalysts"][0]["days_out"], 0)
        self.assertEqual(c["thesis"]["id"], "GOOG-2026-07-21")
        self.assertEqual(c["thesis"]["registered_date"], "2026-07-21")


# --------------------------------------------------------------------------- #
# 8. Structural validation against docs/decision.schema.json (stdlib-only).
#
# We do NOT add a jsonschema dependency; a minimal, hand-rolled validator checks the
# schema's `required` keys + declared leaf `type`s + the pinned enums. This mirrors
# the stdlib structural check report_qc will use (FR-3).
# --------------------------------------------------------------------------- #

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "docs", "decision.schema.json")

_JSON_TYPE = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "number": (int, float), "integer": int, "null": type(None),
}


def _type_ok(value, type_decl):
    """True if value matches a JSON-Schema `type` (str or list of str)."""
    types = type_decl if isinstance(type_decl, list) else [type_decl]
    for t in types:
        py = _JSON_TYPE.get(t)
        if py is None:
            continue
        # bool is a subclass of int -> guard number/integer against bool.
        if t in ("number", "integer") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def _validate(instance, schema, path="$"):
    """Minimal structural validation: required keys, leaf types, enums, array items.
    Returns a list of error strings (empty == valid)."""
    errors = []
    stype = schema.get("type")
    if stype is not None and not _type_ok(instance, stype):
        errors.append(f"{path}: type {type(instance).__name__} not in {stype}")
        return errors  # type mismatch -> stop descending

    if stype == "object" or (stype is None and isinstance(instance, dict)):
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}: missing required key '{req}'")
        for key, subschema in (schema.get("properties") or {}).items():
            if key in instance:
                errors.extend(_validate(instance[key], subschema, f"{path}.{key}"))

    if stype == "array" and isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(instance):
                errors.extend(_validate(item, item_schema, f"{path}[{i}]"))
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")

    enum = schema.get("enum")
    if enum is not None and instance not in enum:
        errors.append(f"{path}: value {instance!r} not in enum {enum}")

    const = schema.get("const")
    if const is not None and instance != const:
        errors.append(f"{path}: value {instance!r} != const {const!r}")

    return errors


class TestSchemaValidation(unittest.TestCase):
    """The built contract validates against docs/decision.schema.json (structural)."""

    @classmethod
    def setUpClass(cls):
        with open(_SCHEMA_PATH) as fh:
            cls.schema = json.load(fh)

    def test_schema_file_is_valid_json(self):
        self.assertEqual(
            self.schema["properties"]["contract_version"]["const"], "2.0.0")

    def test_full_contract_validates(self):
        c = dc.build_contract(_goog_full_docs())
        errors = _validate(c, self.schema)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_required_capital_block_enforced(self):
        # Dropping a required capital field is caught by the validator.
        c = dc.build_contract(_goog_full_docs())
        del c["capital_eligible"]
        errors = _validate(c, self.schema)
        self.assertTrue(any("capital_eligible" in e for e in errors), errors)

    def test_catalyst_required_leaves_enforced(self):
        c = dc.build_contract(_goog_full_docs())
        del c["catalysts"][0]["days_out"]
        errors = _validate(c, self.schema)
        self.assertTrue(any("days_out" in e for e in errors), errors)

    def test_thesis_id_required_enforced(self):
        c = dc.build_contract(_goog_full_docs())
        del c["thesis"]["id"]
        errors = _validate(c, self.schema)
        self.assertTrue(any("id" in e for e in errors), errors)

    def test_operator_enum_enforced(self):
        # A bad operator value fails the enum pin.
        tp = _goog_tradeplan(technical_leg={
            "condition": "weekly close below", "level": 321.7431,
            "operator": "NOT_A_VALID_OP"})
        c = dc.build_contract(_goog_full_docs(module_tradeplan=tp))
        errors = _validate(c, self.schema)
        self.assertTrue(any("operator" in e for e in errors), errors)

    def test_valid_operator_passes_enum(self):
        tp = _goog_tradeplan(technical_leg={
            "condition": "weekly close below", "level": 321.7431,
            "operator": "weekly_close_below"})
        c = dc.build_contract(_goog_full_docs(module_tradeplan=tp))
        errors = _validate(c, self.schema)
        self.assertEqual(errors, [], "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
