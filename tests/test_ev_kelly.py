import unittest
from scripts import ev_kelly as E

SC = [{"name": "bull", "prob": 0.25, "price_target": 150.0},
      {"name": "base", "prob": 0.50, "price_target": 120.0},
      {"name": "bear", "prob": 0.25, "price_target": 80.0}]

class TestEV(unittest.TestCase):
    def test_ev_at_exact(self):
        # at entry 100: 0.25*0.5 + 0.5*0.2 + 0.25*(-0.2) = 0.175
        self.assertAlmostEqual(E.ev_at(SC, 100.0), 0.175)

    def test_probs_must_sum_to_one(self):
        bad = [dict(SC[0], prob=0.5), dict(SC[1], prob=0.6)]
        with self.assertRaises(ValueError): E.scenario_ev(bad)
        with self.assertRaises(ValueError): E.kelly(bad, 100.0)

    def test_kelly_exact(self):
        k = E.kelly(SC, 100.0)
        # p=0.75; win = (0.25*0.5+0.5*0.2)/0.75 = 0.3; loss = 0.2; b = 1.5
        self.assertAlmostEqual(k["p_win"], 0.75)
        self.assertAlmostEqual(k["b_odds"], 1.5)
        self.assertAlmostEqual(k["f_star"], 0.75 - 0.25 / 1.5)
        self.assertAlmostEqual(k["half"], k["f_star"] / 2)

    def test_kelly_edges(self):
        allwin = [{"name": "a", "prob": 1.0, "price_target": 120.0}]
        self.assertAlmostEqual(E.kelly(allwin, 100.0)["f_star"], 1.0)
        alllose = [{"name": "a", "prob": 1.0, "price_target": 80.0}]
        self.assertAlmostEqual(E.kelly(alllose, 100.0)["f_star"], 0.0)

    def test_size_caps_and_event_notch(self):
        s = E.size_recommendation(0.60, "balanced", False)
        self.assertAlmostEqual(s["recommended_pct"], 0.08)      # half-Kelly 0.30 > cap 0.08
        s2 = E.size_recommendation(0.60, "balanced", True)
        self.assertAlmostEqual(s2["recommended_pct"], 0.04)     # cap/2 binds
        s3 = E.size_recommendation(0.10, "long-term", False)
        self.assertAlmostEqual(s3["recommended_pct"], 0.05)     # half-Kelly binds
        with self.assertRaises(ValueError): E.size_recommendation(0.1, "yolo", False)


# --------------------------------------------------------------------------- #
# QC18: coverage_dcf_scenarios -- the ONE place that reshapes
# module_valuation_reconcile.json's "scenarios" block (keyed bear/base/bull ->
# {"probability", "dcf_value_per_share"}, itself a verbatim copy of
# coverage/scenario_drivers.json) into the canonical ev_kelly scenario list
# [{"name","prob","price_target"}, ...]. Both score_composite's
# scenario_derivation disclosure and report_qc's ev_scenario_agreement gate
# (QC18) read the SAME coverage EV off the SAME conversion.
# --------------------------------------------------------------------------- #

# Real MU (2026-08-08 institutional QC review, N10): coverage's own DCF fan,
# pinned from .../MU/coverage/scenario_drivers.json's "scenarios" block (also
# copied verbatim into the bundle's module_valuation_reconcile.json). Pinned
# as literals rather than read from the archived bundle -- unlike the QC12
# raw-history fixtures in test_build_snapshot.py, this is three scalars, not a
# 6700-row series too large to embed.
_MU_RECONCILE_SCENARIOS = {
    "bear": {"probability": 0.25, "dcf_value_per_share": 261.42},
    "base": {"probability": 0.45, "dcf_value_per_share": 542.16},
    "bull": {"probability": 0.30, "dcf_value_per_share": 1439.44},
}


class TestCoverageDCFScenarios(unittest.TestCase):
    def test_real_mu_shape_maps_to_dcf_prefixed_names(self):
        out = E.coverage_dcf_scenarios(_MU_RECONCILE_SCENARIOS)
        self.assertEqual(len(out), 3)
        by_name = {o["name"]: o for o in out}
        self.assertEqual(set(by_name), {"dcf_bear", "dcf_base", "dcf_bull"})
        self.assertAlmostEqual(by_name["dcf_bear"]["prob"], 0.25)
        self.assertAlmostEqual(by_name["dcf_bear"]["price_target"], 261.42)
        self.assertAlmostEqual(by_name["dcf_base"]["prob"], 0.45)
        self.assertAlmostEqual(by_name["dcf_base"]["price_target"], 542.16)
        self.assertAlmostEqual(by_name["dcf_bull"]["prob"], 0.30)
        self.assertAlmostEqual(by_name["dcf_bull"]["price_target"], 1439.44)

    def test_real_mu_reproduces_the_verified_coverage_ev(self):
        # Verified to 4dp (MU institutional QC review, N10): coverage's own
        # 25/45/30 DCF weighting at last 877.57 -> ev -0.1554 (vs the shipped
        # set's +0.0684 -- the sign-disagreement QC18 exists to catch).
        out = E.coverage_dcf_scenarios(_MU_RECONCILE_SCENARIOS)
        self.assertAlmostEqual(sum(o["prob"] for o in out), 1.0)
        self.assertAlmostEqual(round(E.ev_at(out, 877.57), 4), -0.1554)

    def test_missing_role_is_skipped_not_guessed(self):
        out = E.coverage_dcf_scenarios(
            {"bear": {"probability": 0.5, "dcf_value_per_share": 100.0}})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "dcf_bear")

    def test_non_numeric_role_is_skipped(self):
        out = E.coverage_dcf_scenarios(
            {"bear": {"probability": "high", "dcf_value_per_share": 100.0}})
        self.assertEqual(out, [])

    def test_bool_probability_is_not_numeric(self):
        # isinstance(True, int) is True in Python -- must not slip through.
        out = E.coverage_dcf_scenarios(
            {"bear": {"probability": True, "dcf_value_per_share": 100.0}})
        self.assertEqual(out, [])

    def test_non_dict_role_is_skipped(self):
        out = E.coverage_dcf_scenarios({"bear": "not a dict", "base": None})
        self.assertEqual(out, [])

    def test_none_or_non_dict_input_returns_empty_list(self):
        self.assertEqual(E.coverage_dcf_scenarios(None), [])
        self.assertEqual(E.coverage_dcf_scenarios("nope"), [])
        self.assertEqual(E.coverage_dcf_scenarios([]), [])
        self.assertEqual(E.coverage_dcf_scenarios({}), [])


if __name__ == "__main__": unittest.main()
