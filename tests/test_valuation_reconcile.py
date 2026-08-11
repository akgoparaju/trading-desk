"""Tests for scripts/valuation_reconcile.py -- the O17 valuation reconciliation.

WHY: this module is the DISCLOSURE-and-GOVERN companion to the DCF-vs-comps split.
It (a) classifies the disagreement into a small state machine reusing the
module_fundamental valuation anchors + the SAME 0.25 edge score_fundamental and
decision_contract use, and (b) solves the reverse-DCF implied terminal growth that
makes the DCF equal the current price. These tests pin:

  1. disagreement / disagreement_state over the state table
     (CONSISTENT / UNRESOLVED_CONFLICT / MODEL_INVALID / None-no-anchors).
  2. reverse_dcf on the real GOOG inputs -> implied_terminal_g ~ 0.0807, and the
     no-finite-solution path (g* >= wacc / needed <= 0) -> None + note.
  3. graceful degradation (absent anchors / absent reverse inputs -> None).
  4. build_reconcile assembly (state emitted even when scenario_drivers absent).

Every value is transcribed (cited) or computed from transcribed inputs; nothing is
invented. stdlib-only; unittest.
"""

import os
import subprocess
import sys
import tempfile
import unittest

from scripts import valuation_reconcile as vr


SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "valuation_reconcile.py")


# --------------------------------------------------------------------------- #
# Fixtures: a module_fundamental with the valuation subscore inputs.anchors, and
# the real GOOG dcf_reverse_inputs.
# --------------------------------------------------------------------------- #

def _fundamental(*, dcf_base=145.47, comps_low=294.0, comps_high=436.0):
    """A module_fundamental stub carrying only the valuation subscore anchors
    disagreement_state reads (mirrors the real GOOG shape)."""
    return {
        "skill": "fundamental",
        "subscores": [
            {"name": "quality", "points": 8, "max": 8},
            {"name": "valuation", "points": 12.75, "max": 17,
             "inputs": {"anchors": {
                 "dcf_base": dcf_base,
                 "comps_low": comps_low,
                 "comps_high": comps_high,
             }}},
        ],
    }


# The real GOOG dcf_reverse_inputs (transcribed from scenario_drivers.json /
# valuation.md), and the snapshot last used to solve the reverse-DCF.
_GOOG_REVERSE = {
    "pv_explicit_fcf_m": 312850,
    "pv_terminal_base_m": 1270900,
    "terminal_g_base": 0.03,
    "wacc": 0.1066,
    "net_cash_m": 49339,
    "diluted_shares_m": 12238,
}
_GOOG_LAST = 351.37

# QC21: the real MU probability_weighted_value_per_share (coverage/scenario_drivers
# .json dcf_reverse_inputs, 2026-08-08 bundle) + the live snapshot price.last used
# to compute the vs-spot pct -- (741.16 - 877.57) / 877.57 = -15.54%.
_MU_PROB_WEIGHTED_FV = 741.16
_MU_LAST = 877.57


# --------------------------------------------------------------------------- #
# disagreement + disagreement_state (the state table).
# --------------------------------------------------------------------------- #

class TestDisagreement(unittest.TestCase):
    def test_goog_disagreement_value(self):
        # AUTHORITATIVE formula (score_fundamental._dcf_band_position): average
        # denominator. comps_mid = (294 + 436)/2 = 365; denom = (145.47 + 365)/2 =
        # 255.235; |145.47 - 365|/255.235 = 0.8601... — matches the 0.8601 printed
        # in module_fundamental's valuation arithmetic (one number, one bundle).
        d = vr.disagreement(_fundamental())
        self.assertAlmostEqual(
            d, abs(145.47 - 365.0) / ((145.47 + 365.0) / 2.0), places=6)
        self.assertAlmostEqual(d, 0.8601, places=4)

    def test_absent_anchors_is_none(self):
        self.assertIsNone(vr.disagreement({"subscores": []}))
        self.assertIsNone(vr.disagreement(None))

    def test_missing_required_anchor_is_none(self):
        f = _fundamental()
        del f["subscores"][1]["inputs"]["anchors"]["dcf_base"]
        self.assertIsNone(vr.disagreement(f))


class TestDisagreementState(unittest.TestCase):
    def test_goog_is_unresolved_conflict(self):
        self.assertEqual(vr.disagreement_state(_fundamental()),
                         vr.STATE_UNRESOLVED)

    def test_consistent_at_or_below_edge(self):
        # Build anchors so disagreement <= 0.25 (average denominator): dcf_base 320,
        # comps_mid 365 -> |320-365|/((320+365)/2) = 45/342.5 = 0.1314 <= 0.25 ->
        # CONSISTENT. (Well below the edge, so unchanged in intent from the old
        # denominator, which gave 0.1233 — still CONSISTENT.)
        f = _fundamental(dcf_base=320.0)
        self.assertLessEqual(vr.disagreement(f), 0.25)
        self.assertEqual(vr.disagreement_state(f), vr.STATE_CONSISTENT)

    def test_exactly_at_edge_is_consistent(self):
        # disagreement == 0.25 EXACTLY is CONSISTENT (state uses > 0.25 for
        # UNRESOLVED). Rebuilt for the AVERAGE denominator: solving
        # (mid - dcf) / ((dcf + mid)/2) = 0.25 for dcf < mid gives dcf/mid = 7/9, so
        # with comps_mid 365 the exact-edge anchor is 365 * 7/9 = 283.89 (was 273.75
        # under the old comps_mid-only denominator). Still exercises the "== edge ->
        # CONSISTENT" boundary, now under the authoritative formula.
        f = _fundamental(dcf_base=365.0 * 7 / 9)
        self.assertAlmostEqual(vr.disagreement(f), 0.25, places=6)
        self.assertEqual(vr.disagreement_state(f), vr.STATE_CONSISTENT)

    def test_just_above_edge_is_unresolved(self):
        # dcf_base just below the exact-edge anchor (283.89) -> further from mid ->
        # disagreement just over 0.25 -> UNRESOLVED. Anchor lowered to 280.0 (from
        # 270.0) to stay just past the edge under the average denominator:
        # |280-365|/((280+365)/2) = 85/322.5 = 0.2636 > 0.25.
        f = _fundamental(dcf_base=280.0)
        self.assertGreater(vr.disagreement(f), 0.25)
        self.assertEqual(vr.disagreement_state(f), vr.STATE_UNRESOLVED)

    def test_nonpositive_dcf_is_model_invalid(self):
        f = _fundamental(dcf_base=-1.0)
        self.assertEqual(vr.disagreement_state(f), vr.STATE_MODEL_INVALID)

    def test_zero_comps_low_is_model_invalid(self):
        f = _fundamental(comps_low=0.0)
        self.assertEqual(vr.disagreement_state(f), vr.STATE_MODEL_INVALID)

    def test_no_anchors_is_none(self):
        self.assertIsNone(vr.disagreement_state({"subscores": []}))
        self.assertIsNone(vr.disagreement_state(None))

    def test_missing_anchor_is_none(self):
        f = _fundamental()
        del f["subscores"][1]["inputs"]["anchors"]["comps_high"]
        self.assertIsNone(vr.disagreement_state(f))

    def test_edge_matches_decision_contract(self):
        # The 0.25 edge must equal the one decision_contract uses so all three
        # consumers (fundamental / contract / reconcile) classify identically.
        from scripts import decision_contract as dc
        self.assertEqual(vr._DISAGREEMENT_TOL, dc._VALUATION_DISAGREEMENT_TOL)


# --------------------------------------------------------------------------- #
# reverse_dcf.
# --------------------------------------------------------------------------- #

class TestReverseDcf(unittest.TestCase):
    def test_goog_implied_terminal_g(self):
        out = vr.reverse_dcf(_GOOG_REVERSE, _GOOG_LAST)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["implied_terminal_g"], 0.0807, places=4)
        self.assertEqual(out["g_base"], 0.03)
        self.assertEqual(out["wacc"], 0.1066)
        self.assertAlmostEqual(out["implied_vs_base"], 0.0807 - 0.03, places=4)
        self.assertIsNone(out["note"])

    def test_implied_g_solves_the_terminal_ratio(self):
        # Verify the returned g* actually reproduces the target terminal value:
        # (1+g*)/(wacc-g*) must equal `needed` from the inputs + last. The returned
        # g* is rounded to 4dp; at needed ~41.7 a 4dp round of g introduces a ~0.06
        # error in the reconstructed shape, so compare to places=0 (the roundtrip
        # is precision-limited by the disclosed rounding, not a formula error).
        out = vr.reverse_dcf(_GOOG_REVERSE, _GOOG_LAST)
        g = out["implied_terminal_g"]
        wacc = _GOOG_REVERSE["wacc"]
        target_ev = (_GOOG_LAST * _GOOG_REVERSE["diluted_shares_m"]
                     - _GOOG_REVERSE["net_cash_m"])
        pv_terminal_needed = target_ev - _GOOG_REVERSE["pv_explicit_fcf_m"]
        ratio = pv_terminal_needed / _GOOG_REVERSE["pv_terminal_base_m"]
        base_factor = (1 + _GOOG_REVERSE["terminal_g_base"]) / (
            wacc - _GOOG_REVERSE["terminal_g_base"])
        needed = ratio * base_factor
        self.assertAlmostEqual((1 + g) / (wacc - g), needed, places=0)

    def test_low_price_no_finite_solution(self):
        # The reachable no-finite path: a price so low the target EV falls below
        # the explicit-FCF PV makes needed <= 0 -> no finite economic g, note fires.
        # (For these input shapes g* -> wacc only asymptotically as last -> inf, so
        # needed <= 0 is the economically-realized "market prices FCF above the
        # model path" case; see test_g_star_ge_wacc_guard for the g*>=wacc branch.)
        out = vr.reverse_dcf(_GOOG_REVERSE, 20.0)
        self.assertIsNotNone(out)
        self.assertIsNone(out["implied_terminal_g"])
        self.assertIsNone(out["implied_vs_base"])
        self.assertIn("above the model path", out["note"])
        self.assertEqual(out["g_base"], 0.03)
        self.assertEqual(out["wacc"], 0.1066)

    def test_high_price_asymptotes_below_wacc_still_finite(self):
        # A very high price pushes g* toward WACC but never reaches it (the closed
        # form g* = (needed*wacc-1)/(1+needed) < wacc for all needed>0). So a high
        # price stays FINITE and > g_base — pin that so a future refactor that
        # accidentally makes it None is caught.
        out = vr.reverse_dcf(_GOOG_REVERSE, 5000.0)
        self.assertIsNotNone(out["implied_terminal_g"])
        self.assertLess(out["implied_terminal_g"], _GOOG_REVERSE["wacc"])
        self.assertGreater(out["implied_terminal_g"],
                           _GOOG_REVERSE["terminal_g_base"])

    def test_inverted_model_no_finite_solution(self):
        # A degenerate/inverted model (wacc < g_base) yields a negative base_factor;
        # a normal positive price then drives needed <= 0 -> no finite g, note fires.
        # This exercises the no-finite guard on a pathological model input.
        inverted = dict(_GOOG_REVERSE, wacc=0.02, terminal_g_base=0.03)
        out = vr.reverse_dcf(inverted, _GOOG_LAST)
        self.assertIsNotNone(out)
        self.assertIsNone(out["implied_terminal_g"])
        self.assertIn("above the model path", out["note"])

    def test_absent_inputs_is_none(self):
        self.assertIsNone(vr.reverse_dcf(None, _GOOG_LAST))
        self.assertIsNone(vr.reverse_dcf({}, _GOOG_LAST))

    def test_missing_one_input_is_none(self):
        bad = dict(_GOOG_REVERSE)
        del bad["wacc"]
        self.assertIsNone(vr.reverse_dcf(bad, _GOOG_LAST))

    def test_absent_last_is_none(self):
        self.assertIsNone(vr.reverse_dcf(_GOOG_REVERSE, None))

    def test_wacc_equals_g_base_is_none(self):
        bad = dict(_GOOG_REVERSE, wacc=0.03)  # base_factor undefined
        self.assertIsNone(vr.reverse_dcf(bad, _GOOG_LAST))


# --------------------------------------------------------------------------- #
# build_reconcile assembly + graceful degradation.
# --------------------------------------------------------------------------- #

class TestBuildReconcile(unittest.TestCase):
    def test_full_bundle(self):
        drivers = {
            "scenarios": {
                "bear": {"eps_fy28": 10.73, "fcf_fy28_m": -313},
                "base": {"eps_fy28": 14.16, "fcf_fy28_m": 41794},
                "bull": {"eps_fy28": 17.18, "fcf_fy28_m": 80734},
            },
            "dcf_reverse_inputs": _GOOG_REVERSE,
            "citations": {"scenarios": "coverage/model.md"},
        }
        doc = vr.build_reconcile(_fundamental(), drivers, _GOOG_LAST)
        self.assertEqual(doc["disagreement_state"], vr.STATE_UNRESOLVED)
        self.assertAlmostEqual(doc["reverse_dcf"]["implied_terminal_g"], 0.0807,
                               places=4)
        self.assertEqual(doc["scenarios"], drivers["scenarios"])
        self.assertEqual(doc["citations"], drivers["citations"])
        self.assertEqual(doc["disagreement_edge"], 0.25)

    def test_absent_scenario_drivers_still_emits_state(self):
        # No scenario_drivers -> reverse_dcf + scenarios None, but the state (from
        # the fundamental anchors) is still emitted.
        doc = vr.build_reconcile(_fundamental(), None, _GOOG_LAST)
        self.assertEqual(doc["disagreement_state"], vr.STATE_UNRESOLVED)
        self.assertIsNone(doc["reverse_dcf"])
        self.assertIsNone(doc["scenarios"])

    def test_absent_fundamental_anchors_state_none(self):
        doc = vr.build_reconcile({"subscores": []}, None, _GOOG_LAST)
        self.assertIsNone(doc["disagreement_state"])

    def test_reconcile_version_is_1_1_0(self):
        # D4 (adversarial review): the artifact gained two keys
        # (probability_weighted_fv / probability_weighted_fv_vs_price_pct)
        # since 1.0.0 -- pin the bump.
        doc = vr.build_reconcile(_fundamental(), None, _GOOG_LAST)
        self.assertEqual(doc["reconcile_version"], "1.1.0")
        self.assertEqual(vr.RECONCILE_VERSION, "1.1.0")


# --------------------------------------------------------------------------- #
# QC21: probability_weighted_fv -- the coverage bear/base/bull DCF blend
# (dcf_reverse_inputs.probability_weighted_value_per_share) surfaced as a REAL
# numeric leaf on module_valuation_reconcile.json, so the report-layer street-
# confrontation row can cite it without a number_provenance whitelist entry.
# --------------------------------------------------------------------------- #

class TestProbabilityWeightedFv(unittest.TestCase):
    def test_present_surfaces_value_and_vs_price_pct(self):
        drivers = {
            "dcf_reverse_inputs": {
                "probability_weighted_value_per_share": _MU_PROB_WEIGHTED_FV,
            },
        }
        doc = vr.build_reconcile(_fundamental(), drivers, _MU_LAST)
        self.assertEqual(doc["probability_weighted_fv"], _MU_PROB_WEIGHTED_FV)
        expected_pct = (_MU_PROB_WEIGHTED_FV - _MU_LAST) / _MU_LAST
        self.assertAlmostEqual(doc["probability_weighted_fv_vs_price_pct"],
                               expected_pct, places=4)
        self.assertAlmostEqual(doc["probability_weighted_fv_vs_price_pct"],
                               -0.1554, places=4)

    def test_absent_scenario_drivers_both_none(self):
        doc = vr.build_reconcile(_fundamental(), None, _MU_LAST)
        self.assertIsNone(doc["probability_weighted_fv"])
        self.assertIsNone(doc["probability_weighted_fv_vs_price_pct"])

    def test_field_missing_from_dcf_reverse_inputs_both_none(self):
        drivers = {"dcf_reverse_inputs": {"wacc": 0.135}}
        doc = vr.build_reconcile(_fundamental(), drivers, _MU_LAST)
        self.assertIsNone(doc["probability_weighted_fv"])
        self.assertIsNone(doc["probability_weighted_fv_vs_price_pct"])

    def test_absent_dcf_reverse_inputs_block_both_none(self):
        drivers = {"scenarios": {}}
        doc = vr.build_reconcile(_fundamental(), drivers, _MU_LAST)
        self.assertIsNone(doc["probability_weighted_fv"])
        self.assertIsNone(doc["probability_weighted_fv_vs_price_pct"])

    def test_absent_last_price_fv_still_present_pct_none(self):
        # AAPL-shape guard: even without a current price, the transcribed FV is
        # still a real leaf (the pct alone is what needs both legs).
        drivers = {
            "dcf_reverse_inputs": {
                "probability_weighted_value_per_share": _MU_PROB_WEIGHTED_FV,
            },
        }
        doc = vr.build_reconcile(_fundamental(), drivers, None)
        self.assertEqual(doc["probability_weighted_fv"], _MU_PROB_WEIGHTED_FV)
        self.assertIsNone(doc["probability_weighted_fv_vs_price_pct"])

    def test_zero_last_price_pct_none_no_div_by_zero(self):
        drivers = {
            "dcf_reverse_inputs": {
                "probability_weighted_value_per_share": _MU_PROB_WEIGHTED_FV,
            },
        }
        doc = vr.build_reconcile(_fundamental(), drivers, 0.0)
        self.assertEqual(doc["probability_weighted_fv"], _MU_PROB_WEIGHTED_FV)
        self.assertIsNone(doc["probability_weighted_fv_vs_price_pct"])

    def test_non_numeric_field_is_none(self):
        drivers = {
            "dcf_reverse_inputs": {
                "probability_weighted_value_per_share": "n/a",
            },
        }
        doc = vr.build_reconcile(_fundamental(), drivers, _MU_LAST)
        self.assertIsNone(doc["probability_weighted_fv"])
        self.assertIsNone(doc["probability_weighted_fv_vs_price_pct"])

    def test_pct_computed_from_rounded_leaf_not_raw_value(self):
        # D2 (adversarial review): probability_weighted_fv is rounded 2dp for
        # display, but probability_weighted_fv_vs_price_pct was computed from
        # the UNROUNDED raw value -- a reader who recomputes the row's own
        # printed numbers (fv - last) / last got a DIFFERENT answer than the
        # bundle carries (measured 52.6% self-inconsistency on sub-$10 names).
        # Trigger: last=1.20, raw FV 0.914999 -> leaf 0.91 -> pct must be
        # (0.91 - 1.20) / 1.20 = -0.241667 (4dp -0.2417), NOT the
        # raw-value answer -0.2375.
        drivers = {
            "dcf_reverse_inputs": {
                "probability_weighted_value_per_share": 0.914999,
            },
        }
        doc = vr.build_reconcile(_fundamental(), drivers, 1.20)
        self.assertEqual(doc["probability_weighted_fv"], 0.91)
        self.assertAlmostEqual(doc["probability_weighted_fv_vs_price_pct"],
                               -0.2417, places=4)
        # Explicitly reject the raw-value (self-inconsistent) answer.
        self.assertNotAlmostEqual(doc["probability_weighted_fv_vs_price_pct"],
                                  -0.2375, places=4)


# --------------------------------------------------------------------------- #
# CLI: writes module_valuation_reconcile.json on a real-shaped bundle.
# --------------------------------------------------------------------------- #

class TestCli(unittest.TestCase):
    def _bundle(self, td, *, with_drivers=True):
        import json
        # snapshot with price.last
        with open(os.path.join(td, "snapshot_GOOG_2026-07-21.json"), "w") as fh:
            json.dump({"meta": {"ticker": "GOOG"}, "price": {"last": _GOOG_LAST}}, fh)
        with open(os.path.join(td, "module_fundamental.json"), "w") as fh:
            json.dump(_fundamental(), fh)
        cov = os.path.join(td, "coverage")
        os.makedirs(cov, exist_ok=True)
        if with_drivers:
            with open(os.path.join(cov, "scenario_drivers.json"), "w") as fh:
                json.dump({
                    "scenarios": {
                        "bear": {"eps_fy28": 10.73, "fcf_fy28_m": -313},
                        "base": {"eps_fy28": 14.16, "fcf_fy28_m": 41794},
                        "bull": {"eps_fy28": 17.18, "fcf_fy28_m": 80734},
                    },
                    "dcf_reverse_inputs": _GOOG_REVERSE,
                    "citations": {"scenarios": "coverage/model.md"},
                }, fh)
        return td

    def test_cli_writes_reconcile(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            self._bundle(td)
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--bundle", td],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = os.path.join(td, "module_valuation_reconcile.json")
            self.assertTrue(os.path.isfile(out))
            doc = json.load(open(out))
            self.assertEqual(doc["disagreement_state"], vr.STATE_UNRESOLVED)
            self.assertAlmostEqual(doc["reverse_dcf"]["implied_terminal_g"],
                                   0.0807, places=4)

    def test_cli_absent_drivers_still_emits_state(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            self._bundle(td, with_drivers=False)
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--bundle", td],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            doc = json.load(open(os.path.join(
                td, "module_valuation_reconcile.json")))
            self.assertEqual(doc["disagreement_state"], vr.STATE_UNRESOLVED)
            self.assertIsNone(doc["reverse_dcf"])

    def test_cli_absent_fundamental_errors(self):
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--bundle", td],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("module_fundamental.json absent", proc.stderr)


if __name__ == "__main__":
    unittest.main()
