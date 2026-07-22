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
        self.assertEqual(c["contract_version"], "1.1.0")
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


if __name__ == "__main__":
    unittest.main()
