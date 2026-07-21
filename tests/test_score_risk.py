"""Tests for scripts/score_risk.py -- the risk-analytics evidence skill.

WHY: like technical-analysis, this module's arithmetic IS the rubric of record
(risk rubric v1.0.0). Every scoring branch is pinned to a hand-computed value; if
the code and these numbers ever diverge, the rubric has silently changed and that
must surface as a test failure. Higher score = BETTER risk-reward conditions.

Tests exercise the pure scoring functions directly (exact values per branch), the
downside-map / vol-profile table builders, and one end-to-end CLI run against a
real snapshot bundle fabricated exactly the way test_score_technical.py does. The
scoring functions take already-parsed inputs (a technicals-style block, a ladder,
etc.) so branches pin without reconstructing a full price series.

stdlib-only; unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import score_risk as sr


# --------------------------------------------------------------------------- #
# Helpers: minimal snapshot sub-blocks and hand-built ladders.
# --------------------------------------------------------------------------- #

def _tech(**over):
    """A fully-populated risk-relevant technicals block; override per test."""
    base = {
        "rv30_vs_10yr_pctile": 25.0,
        "max_dd_10yr": -0.30,
        "dd_episodes_20pct_10yr": 3,
        "dd_episodes_30pct_10yr": 1,
        "dist_from_ath_pct": -0.20,
    }
    base.update(over)
    return base


def _ladder(entries):
    """Wrap (level, type) pairs into ladder dicts (basis is cosmetic here)."""
    out = []
    for e in entries:
        out.append({"level": float(e[0]), "type": e[1], "basis": "test"})
    return out


# --------------------------------------------------------------------------- #
# 1. Volatility state (max 25): rv pctile (20) + beta (5)
# --------------------------------------------------------------------------- #

class TestVolatilityState(unittest.TestCase):
    def test_pctile_25_is_20(self):
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=25.0), beta=None)
        self.assertEqual(sub["inputs"]["pctile_points"], 20)

    def test_pctile_45_is_14(self):
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=45.0), beta=None)
        self.assertEqual(sub["inputs"]["pctile_points"], 14)

    def test_pctile_70_is_8(self):
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=70.0), beta=None)
        self.assertEqual(sub["inputs"]["pctile_points"], 8)

    def test_pctile_85_is_3(self):
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=85.0), beta=None)
        self.assertEqual(sub["inputs"]["pctile_points"], 3)

    def test_pctile_null_is_0_na(self):
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=None), beta=1.0)
        self.assertEqual(sub["inputs"]["pctile_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])

    def test_beta_1_0_is_plus5(self):
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=None), beta=1.0)
        self.assertEqual(sub["inputs"]["beta_points"], 5)

    def test_beta_1_5_is_plus3(self):
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=None), beta=1.5)
        self.assertEqual(sub["inputs"]["beta_points"], 3)

    def test_beta_2_1_is_plus0(self):
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=None), beta=2.1)
        self.assertEqual(sub["inputs"]["beta_points"], 0)

    def test_beta_null_is_0(self):
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=25.0), beta=None)
        self.assertEqual(sub["inputs"]["beta_points"], 0)

    def test_both_null_not_evaluable(self):
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=None), beta=None)
        self.assertFalse(sub["evaluable"])


# --------------------------------------------------------------------------- #
# 1b. Short-history confidence gating (REAL-WORLD BUG: a beta of 3.61 computed
# from 100 unadjusted days fed the score as a plain number). A beta needs
# >=150 return-days for a stable estimate; an rv30 regime percentile needs
# >=500 (~2yr) ohlcv rows to be a percentile at all. Below threshold the
# component scores 0 with an explicit "n/a" arithmetic disclosure. When the
# gating input is ABSENT (None) the gate does not trip -- the pure-function
# branch tests that never pass n_days/rows keep their existing behavior.
# --------------------------------------------------------------------------- #

class TestShortHistoryGating(unittest.TestCase):
    def test_beta_ndays_99_gates_component_to_0(self):
        # 99 return-days < 150 -> beta component 0 regardless of the beta value.
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=None), beta=1.0,
                                  beta_n_days=99, ohlcv_rows=2520)
        self.assertEqual(sub["inputs"]["beta_points"], 0)
        self.assertIn("beta n/a", sub["arithmetic"])
        self.assertIn("99", sub["arithmetic"])
        self.assertIn("150", sub["arithmetic"])

    def test_beta_ndays_200_bands_normally(self):
        # 200 >= 150 -> normal banding; beta 1.0 -> +5.
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=None), beta=1.0,
                                  beta_n_days=200, ohlcv_rows=2520)
        self.assertEqual(sub["inputs"]["beta_points"], 5)

    def test_ohlcv_rows_100_gates_percentile_to_0(self):
        # 100 rows < 500 -> percentile component 0 with "n/a".
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=25.0), beta=None,
                                  beta_n_days=250, ohlcv_rows=100)
        self.assertEqual(sub["inputs"]["pctile_points"], 0)
        self.assertIn("rv30 percentile n/a", sub["arithmetic"])
        self.assertIn("100", sub["arithmetic"])
        self.assertIn("500", sub["arithmetic"])

    def test_ohlcv_rows_600_bands_normally(self):
        # 600 >= 500 -> normal banding; pctile 25 -> 20.
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=25.0), beta=None,
                                  beta_n_days=250, ohlcv_rows=600)
        self.assertEqual(sub["inputs"]["pctile_points"], 20)

    def test_gate_at_exact_threshold_passes(self):
        # boundaries are inclusive: 150 return-days and 500 rows both pass.
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=25.0), beta=1.0,
                                  beta_n_days=150, ohlcv_rows=500)
        self.assertEqual(sub["inputs"]["beta_points"], 5)
        self.assertEqual(sub["inputs"]["pctile_points"], 20)

    def test_absent_gating_inputs_do_not_gate(self):
        # No n_days/rows passed (the branch-test call shape) -> no gating.
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=25.0), beta=1.0)
        self.assertEqual(sub["inputs"]["beta_points"], 5)
        self.assertEqual(sub["inputs"]["pctile_points"], 20)

    def test_gated_beta_still_evaluable_via_other_component(self):
        # A gated beta with a valid pctile still leaves the dimension evaluable.
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=25.0), beta=1.0,
                                  beta_n_days=99, ohlcv_rows=2520)
        self.assertTrue(sub["evaluable"])
        self.assertEqual(sub["inputs"]["pctile_points"], 20)
        self.assertEqual(sub["inputs"]["beta_points"], 0)

    def test_both_gated_and_no_other_inputs_not_evaluable(self):
        # pctile null + gated beta -> nothing evaluable in the dimension.
        sub = sr.score_volatility(_tech(rv30_vs_10yr_pctile=None), beta=1.0,
                                  beta_n_days=99, ohlcv_rows=100)
        self.assertFalse(sub["evaluable"])


# --------------------------------------------------------------------------- #
# 2. Drawdown profile (max 25): max_dd (12) + episodes (8) + spread proxy (5)
# --------------------------------------------------------------------------- #

class TestDrawdownProfile(unittest.TestCase):
    def test_maxdd_shallow_is_12(self):
        # -0.30 >= -0.35 -> 12
        sub = sr.score_drawdown(_tech(max_dd_10yr=-0.30))
        self.assertEqual(sub["inputs"]["maxdd_points"], 12)

    def test_maxdd_mid_is_8(self):
        # -0.45 in [-0.50, -0.35) -> 8
        sub = sr.score_drawdown(_tech(max_dd_10yr=-0.45))
        self.assertEqual(sub["inputs"]["maxdd_points"], 8)

    def test_maxdd_deep_is_4(self):
        # -0.55 in [-0.65, -0.50) -> 4
        sub = sr.score_drawdown(_tech(max_dd_10yr=-0.55))
        self.assertEqual(sub["inputs"]["maxdd_points"], 4)

    def test_maxdd_extreme_is_0(self):
        # -0.70 < -0.65 -> 0
        sub = sr.score_drawdown(_tech(max_dd_10yr=-0.70))
        self.assertEqual(sub["inputs"]["maxdd_points"], 0)

    def test_episodes_1_is_8(self):
        sub = sr.score_drawdown(_tech(dd_episodes_30pct_10yr=1))
        self.assertEqual(sub["inputs"]["episodes_points"], 8)

    def test_episodes_3_is_5(self):
        sub = sr.score_drawdown(_tech(dd_episodes_30pct_10yr=3))
        self.assertEqual(sub["inputs"]["episodes_points"], 5)

    def test_episodes_5_is_2(self):
        sub = sr.score_drawdown(_tech(dd_episodes_30pct_10yr=5))
        self.assertEqual(sub["inputs"]["episodes_points"], 2)

    def test_spread_2_is_5(self):
        # (dd20 - dd30) = 2 -> <= 2 -> 5
        sub = sr.score_drawdown(_tech(dd_episodes_20pct_10yr=3,
                                      dd_episodes_30pct_10yr=1))
        self.assertEqual(sub["inputs"]["spread_points"], 5)

    def test_spread_4_is_2(self):
        # (dd20 - dd30) = 4 -> else -> 2
        sub = sr.score_drawdown(_tech(dd_episodes_20pct_10yr=5,
                                      dd_episodes_30pct_10yr=1))
        self.assertEqual(sub["inputs"]["spread_points"], 2)

    def test_spread_method_label(self):
        sub = sr.score_drawdown(_tech())
        self.assertIn("episode_spread_proxy", sub["arithmetic"])

    def test_maxdd_null_is_0_na(self):
        sub = sr.score_drawdown(_tech(max_dd_10yr=None))
        self.assertEqual(sub["inputs"]["maxdd_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])

    def test_all_null_not_evaluable(self):
        sub = sr.score_drawdown(_tech(max_dd_10yr=None,
                                      dd_episodes_20pct_10yr=None,
                                      dd_episodes_30pct_10yr=None))
        self.assertFalse(sub["evaluable"])


# --------------------------------------------------------------------------- #
# 3. Margin of safety (max 30): dist_from_ath (12) + asymmetry (18)
# --------------------------------------------------------------------------- #

class TestMarginOfSafety(unittest.TestCase):
    def test_dist_ath_deep_is_12(self):
        # -0.20 <= -0.15 -> 12
        ladder = _ladder([(96.0, "ma50"), (110.0, "swing_high")])
        sub = sr.score_margin(_tech(dist_from_ath_pct=-0.20), ladder, last=100.0)
        self.assertEqual(sub["inputs"]["dist_ath_points"], 12)

    def test_dist_ath_mid_is_7(self):
        # -0.08 in (-0.15, -0.05] -> 7
        ladder = _ladder([(96.0, "ma50"), (110.0, "swing_high")])
        sub = sr.score_margin(_tech(dist_from_ath_pct=-0.08), ladder, last=100.0)
        self.assertEqual(sub["inputs"]["dist_ath_points"], 7)

    def test_dist_ath_shallow_is_3(self):
        # -0.02 > -0.05 -> 3
        ladder = _ladder([(96.0, "ma50"), (110.0, "swing_high")])
        sub = sr.score_margin(_tech(dist_from_ath_pct=-0.02), ladder, last=100.0)
        self.assertEqual(sub["inputs"]["dist_ath_points"], 3)

    def test_dist_ath_null_is_0_na(self):
        ladder = _ladder([(96.0, "ma50"), (110.0, "swing_high")])
        sub = sr.score_margin(_tech(dist_from_ath_pct=None), ladder, last=100.0)
        self.assertEqual(sub["inputs"]["dist_ath_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])

    def test_asymmetry_ratio_0point4_is_18(self):
        # proven support at 96 -> d_support 4%; resistance at 110 -> d_resist 10%
        # ratio 0.04/0.10 = 0.4 <= 0.5 -> 18
        ladder = _ladder([(96.0, "ma50"), (110.0, "swing_high")])
        sub = sr.score_margin(_tech(), ladder, last=100.0)
        self.assertEqual(sub["inputs"]["asymmetry_points"], 18)

    def test_asymmetry_ratio_2point0_is_6(self):
        # support at 90 -> d_support 10%; resistance at 105 -> d_resist 5%
        # ratio 0.10/0.05 = 2.0 in (1.0, 2.0] -> 6
        ladder = _ladder([(90.0, "ma50"), (105.0, "swing_high")])
        sub = sr.score_margin(_tech(), ladder, last=100.0)
        self.assertEqual(sub["inputs"]["asymmetry_points"], 6)

    def test_asymmetry_ratio_mid_is_12(self):
        # support at 96 -> 4%; resistance at 105 -> 5%; ratio 0.8 in (0.5,1.0] -> 12
        ladder = _ladder([(96.0, "ma50"), (105.0, "swing_high")])
        sub = sr.score_margin(_tech(), ladder, last=100.0)
        self.assertEqual(sub["inputs"]["asymmetry_points"], 12)

    def test_asymmetry_ratio_high_is_2(self):
        # support at 85 -> 15%; resistance at 105 -> 5%; ratio 3.0 > 2.0 -> 2
        ladder = _ladder([(85.0, "ma50"), (105.0, "swing_high")])
        sub = sr.score_margin(_tech(), ladder, last=100.0)
        self.assertEqual(sub["inputs"]["asymmetry_points"], 2)

    def test_blue_sky_convention_resist_15pct(self):
        # no resistance above -> d_resist = 0.15 (labeled). support at 96 -> 4%.
        # ratio 0.04/0.15 = 0.2667 <= 0.5 -> 18
        ladder = _ladder([(96.0, "ma50"), (95.0, "swing_low")])
        sub = sr.score_margin(_tech(), ladder, last=100.0)
        self.assertEqual(sub["inputs"]["asymmetry_points"], 18)
        self.assertIn("blue_sky_convention_15pct", sub["arithmetic"])

    def test_no_proven_support_is_2(self):
        # only a round_number below (not proven) -> asymmetry 2, "no proven floor"
        ladder = _ladder([(96.0, "round_number"), (110.0, "swing_high")])
        sub = sr.score_margin(_tech(), ladder, last=100.0)
        self.assertEqual(sub["inputs"]["asymmetry_points"], 2)
        self.assertIn("no proven floor", sub["arithmetic"])


# --------------------------------------------------------------------------- #
# 4. Liquidity & solvency (max 20): ADV (10) + net-cash ratio (10)
# --------------------------------------------------------------------------- #

class TestLiquiditySolvency(unittest.TestCase):
    def test_adv_mega_is_10(self):
        sub = sr.score_liquidity(adv=600e6, net=None, mktcap=None)
        self.assertEqual(sub["inputs"]["adv_points"], 10)

    def test_adv_large_is_7(self):
        sub = sr.score_liquidity(adv=100e6, net=None, mktcap=None)
        self.assertEqual(sub["inputs"]["adv_points"], 7)

    def test_adv_mid_is_4(self):
        sub = sr.score_liquidity(adv=20e6, net=None, mktcap=None)
        self.assertEqual(sub["inputs"]["adv_points"], 4)

    def test_adv_thin_is_1(self):
        sub = sr.score_liquidity(adv=5e6, net=None, mktcap=None)
        self.assertEqual(sub["inputs"]["adv_points"], 1)

    def test_adv_null_is_0(self):
        sub = sr.score_liquidity(adv=None, net=1.0, mktcap=100.0)
        self.assertEqual(sub["inputs"]["adv_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])

    def test_net_ratio_positive_is_10(self):
        # net/mktcap = 10/100 = 0.10 > 0.05 -> 10
        sub = sr.score_liquidity(adv=None, net=10.0, mktcap=100.0)
        self.assertEqual(sub["inputs"]["net_ratio_points"], 10)

    def test_net_ratio_thin_positive_is_7(self):
        # 3/100 = 0.03 in [0, 0.05] -> 7
        sub = sr.score_liquidity(adv=None, net=3.0, mktcap=100.0)
        self.assertEqual(sub["inputs"]["net_ratio_points"], 7)

    def test_net_ratio_small_negative_is_4(self):
        # -5/100 = -0.05 in [-0.10, 0) -> 4
        sub = sr.score_liquidity(adv=None, net=-5.0, mktcap=100.0)
        self.assertEqual(sub["inputs"]["net_ratio_points"], 4)

    def test_net_ratio_large_negative_is_1(self):
        # -20/100 = -0.20 < -0.10 -> 1
        sub = sr.score_liquidity(adv=None, net=-20.0, mktcap=100.0)
        self.assertEqual(sub["inputs"]["net_ratio_points"], 1)

    def test_net_ratio_null_is_0(self):
        sub = sr.score_liquidity(adv=600e6, net=None, mktcap=100.0)
        self.assertEqual(sub["inputs"]["net_ratio_points"], 0)

    def test_net_ratio_null_mktcap_is_0(self):
        sub = sr.score_liquidity(adv=600e6, net=10.0, mktcap=None)
        self.assertEqual(sub["inputs"]["net_ratio_points"], 0)

    def test_both_null_not_evaluable(self):
        sub = sr.score_liquidity(adv=None, net=None, mktcap=None)
        self.assertFalse(sub["evaluable"])


# --------------------------------------------------------------------------- #
# Downside map table
# --------------------------------------------------------------------------- #

class TestDownsideMap(unittest.TestCase):
    def test_only_below_last_included(self):
        ladder = _ladder([(90.0, "swing_low"), (96.0, "ma50"),
                          (110.0, "swing_high")])
        rows = sr.build_downside_map(ladder, last=100.0, val_floor=None,
                                     stress_pct=None, top_risk=None)
        levels = [r["level"] for r in rows]
        self.assertIn(90.0, levels)
        self.assertIn(96.0, levels)
        self.assertNotIn(110.0, levels)

    def test_valuation_floor_row_arithmetic(self):
        # pe_5yr_median 12 x eps_ntm 6 = 72, inserted in sorted position below last
        ladder = _ladder([(90.0, "swing_low"), (96.0, "ma50")])
        val_floor = sr.valuation_floor(pe_5yr_median=12.0, eps_ntm=6.0)
        self.assertEqual(val_floor["level"], 72.0)
        self.assertEqual(val_floor["type"], "valuation_floor")
        self.assertEqual(val_floor["method"], "pe_5yr_median x eps_ntm")
        rows = sr.build_downside_map(ladder, last=100.0, val_floor=val_floor,
                                     stress_pct=None, top_risk=None)
        vfs = [r for r in rows if r["type"] == "valuation_floor"]
        self.assertEqual(len(vfs), 1)
        self.assertEqual(vfs[0]["level"], 72.0)
        # NEAREST-FIRST (descending): first row is the first support hit; 72 is last
        levels = [r["level"] for r in rows]
        self.assertEqual(levels, sorted(levels, reverse=True))
        self.assertEqual(levels[0], 96.0)
        self.assertEqual(levels[-1], 72.0)

    def test_valuation_floor_none_when_inputs_missing(self):
        self.assertIsNone(sr.valuation_floor(pe_5yr_median=None, eps_ntm=6.0))
        self.assertIsNone(sr.valuation_floor(pe_5yr_median=12.0, eps_ntm=None))

    def test_valuation_floor_not_suspect_when_healthy(self):
        # Healthy inputs (pe_fwd/pe_5yr_median in band, floor a sane % of last):
        # no suspect flag at all (backward-compatible shape).
        vf = sr.valuation_floor(pe_5yr_median=12.0, eps_ntm=6.0,
                                last=100.0, pe_fwd=10.0)
        self.assertEqual(vf["level"], 72.0)
        self.assertNotIn("suspect", vf)

    def test_valuation_floor_suspect_when_floor_collapses_vs_last(self):
        # Real-MU shape: median collapses (1.82) so floor 1.82*74 ~= 134 on a
        # ~850 stock -> floor/last < 0.25 -> suspect (approx_current_eps breakdown).
        vf = sr.valuation_floor(pe_5yr_median=1.82, eps_ntm=74.0, last=853.2)
        self.assertTrue(vf.get("suspect"))
        self.assertEqual(vf["suspect_reason"],
                         "approx_current_eps method breakdown")
        # the level is still emitted (not dropped) for downside-map continuity.
        self.assertIsNotNone(vf["level"])

    def test_valuation_floor_suspect_when_pe_ratio_out_of_band(self):
        # pe_fwd/pe_5yr_median outside [0.2, 5.0] mirrors score_fundamental's
        # sanity band -> suspect even if floor/last is unremarkable.
        vf = sr.valuation_floor(pe_5yr_median=1.0, eps_ntm=50.0,
                                last=80.0, pe_fwd=12.0)  # ratio 12 > 5
        self.assertTrue(vf.get("suspect"))
        self.assertEqual(vf["suspect_reason"],
                         "approx_current_eps method breakdown")

    def test_suspect_row_carried_into_downside_map(self):
        # build_downside_map propagates the suspect flag + reason onto the row so
        # DISPLAY consumers can gray/omit it (fix 3).
        ladder = _ladder([(90.0, "swing_low")])
        vf = sr.valuation_floor(pe_5yr_median=1.82, eps_ntm=20.0, last=200.0)
        self.assertTrue(vf.get("suspect"))
        rows = sr.build_downside_map(ladder, last=200.0, val_floor=vf,
                                     stress_pct=None, top_risk=None)
        vfr = [r for r in rows if r["type"] == "valuation_floor"][0]
        self.assertTrue(vfr.get("suspect"))
        self.assertEqual(vfr["suspect_reason"],
                         "approx_current_eps method breakdown")
        # a normal ladder row carries no suspect flag.
        normal = [r for r in rows if r["type"] == "swing_low"][0]
        self.assertNotIn("suspect", normal)

    def test_stress_row_arithmetic(self):
        # last 100 x (1 + -0.30) = 70
        ladder = _ladder([(90.0, "swing_low"), (96.0, "ma50")])
        rows = sr.build_downside_map(ladder, last=100.0, val_floor=None,
                                     stress_pct=-0.30, top_risk="HBM miss")
        srows = [r for r in rows if r["type"] == "stress_scenario"]
        self.assertEqual(len(srows), 1)
        self.assertEqual(srows[0]["level"], 70.0)
        self.assertEqual(srows[0]["risk"], "HBM miss")


# --------------------------------------------------------------------------- #
# Anchored downside floor (spec A2): with --anchors the valuation floor in the
# downside map uses dcf_bear (labeled "dcf_bear (coverage anchors)"), REPLACING
# the pe-median-derived floor entirely. The suspect-flag machinery is for
# snapshot mode only; an anchored floor is a validated fundamentals-derived
# level, never "suspect". validate_anchors is a LOCAL copy mirroring
# score_fundamental's (same required keys + positivity), so a malformed anchors
# file exits 2 the same way. Nearest-first ordering logic is unchanged -- the
# dcf_bear floor interleaves among the ladder / stress levels by its own level.
# --------------------------------------------------------------------------- #

def _anchors(**over):
    """A valid valuation_anchors dict; override per test (mirror of the
    score_fundamental fixture so the contract stays identical)."""
    base = {
        "dcf_base": 120.0, "dcf_bear": 95.0, "dcf_bull": 150.0,
        "comps_low": 100.0, "comps_high": 140.0,
    }
    base.update(over)
    return base


class TestValidateAnchors(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(sr.validate_anchors(_anchors()), [])

    def test_not_a_dict(self):
        issues = sr.validate_anchors(["x"])
        self.assertTrue(any("not a JSON object" in i for i in issues))

    def test_missing_each_required(self):
        for key in ("dcf_base", "dcf_bear", "dcf_bull", "comps_low",
                    "comps_high"):
            a = _anchors()
            del a[key]
            issues = sr.validate_anchors(a)
            self.assertTrue(any(key in i for i in issues), key)

    def test_nonpositive_rejected(self):
        issues = sr.validate_anchors(_anchors(dcf_bear=-5.0))
        self.assertTrue(any("dcf_bear" in i and "positive" in i for i in issues))

    def test_nonnumeric_rejected(self):
        issues = sr.validate_anchors(_anchors(dcf_bear="cheap"))
        self.assertTrue(any("dcf_bear" in i and "numeric" in i for i in issues))

    def test_current_pb_optional(self):
        self.assertEqual(sr.validate_anchors(_anchors(current_pb=2.5)), [])

    def test_current_pb_bad(self):
        issues = sr.validate_anchors(_anchors(current_pb=-1.0))
        self.assertTrue(any("current_pb" in i for i in issues))


class TestAnchoredValuationFloor(unittest.TestCase):
    def test_anchored_floor_is_dcf_bear(self):
        # dcf_bear 95.0 -> floor level 95.0, labeled basis "dcf_bear (coverage
        # anchors)"; the pe-median inputs are IGNORED in anchored mode.
        vf = sr.valuation_floor(pe_5yr_median=12.0, eps_ntm=6.0,
                                last=100.0, pe_fwd=10.0, anchors=_anchors())
        self.assertEqual(vf["level"], 95.0)
        self.assertEqual(vf["type"], "valuation_floor")
        self.assertEqual(vf["basis"], "dcf_bear (coverage anchors)")
        self.assertEqual(vf["method"], "dcf_bear")

    def test_anchored_floor_never_suspect(self):
        # An anchored floor is a validated fundamentals-derived level; the
        # snapshot-mode suspect machinery does NOT apply even if pe inputs are
        # garbage (real-MU shape) or the floor is far below last.
        vf = sr.valuation_floor(pe_5yr_median=1.82, eps_ntm=74.0, last=853.2,
                                pe_fwd=12.0, anchors=_anchors(dcf_bear=95.0))
        self.assertEqual(vf["level"], 95.0)
        self.assertNotIn("suspect", vf)

    def test_anchored_floor_ignores_pe_median_even_when_pe_missing(self):
        # With anchors, the floor computes from dcf_bear regardless of whether
        # the pe-median snapshot inputs are present (anchored mode replaces the
        # pe path entirely -- a missing pe_5yr_median no longer yields None).
        vf = sr.valuation_floor(pe_5yr_median=None, eps_ntm=None,
                                anchors=_anchors(dcf_bear=95.0))
        self.assertIsNotNone(vf)
        self.assertEqual(vf["level"], 95.0)
        self.assertEqual(vf["method"], "dcf_bear")

    def test_snapshot_mode_unchanged_when_no_anchors(self):
        # anchors=None (or omitted) -> byte-identical to the pre-existing
        # pe-median floor path.
        vf_omitted = sr.valuation_floor(pe_5yr_median=12.0, eps_ntm=6.0)
        vf_none = sr.valuation_floor(pe_5yr_median=12.0, eps_ntm=6.0,
                                     anchors=None)
        self.assertEqual(vf_omitted, vf_none)
        self.assertEqual(vf_none["level"], 72.0)
        self.assertEqual(vf_none["method"], "pe_5yr_median x eps_ntm")

    def test_anchored_floor_interleaves_nearest_first(self):
        # dcf_bear 95 sits between the 96 ladder support (nearer) and the 90
        # ladder support (farther); nearest-first (descending) ordering is
        # unchanged -- 96 first, then 95, then 90.
        ladder = _ladder([(90.0, "swing_low"), (96.0, "ma50")])
        vf = sr.valuation_floor(pe_5yr_median=12.0, eps_ntm=6.0, last=100.0,
                                pe_fwd=10.0, anchors=_anchors(dcf_bear=95.0))
        rows = sr.build_downside_map(ladder, last=100.0, val_floor=vf,
                                     stress_pct=None, top_risk=None)
        levels = [r["level"] for r in rows]
        self.assertEqual(levels, sorted(levels, reverse=True))
        self.assertEqual(levels, [96.0, 95.0, 90.0])
        vfr = [r for r in rows if r["type"] == "valuation_floor"][0]
        self.assertEqual(vfr["level"], 95.0)
        self.assertEqual(vfr["basis"], "dcf_bear (coverage anchors)")


# --------------------------------------------------------------------------- #
# vol_profile table (verbatim passthrough)
# --------------------------------------------------------------------------- #

class TestVolProfile(unittest.TestCase):
    def test_verbatim_fields(self):
        tech = {
            "rv20_ann": 0.40, "rv30_ann": 0.42, "rv30_vs_10yr_pctile": 25.0,
            "max_dd_10yr": -0.30, "dd_episodes_20pct_10yr": 3,
            "dd_episodes_30pct_10yr": 1,
        }
        bench = {"beta": 1.10, "corr": 0.75, "beta_n_days": 250}
        vp = sr.build_vol_profile(tech, bench)
        self.assertEqual(vp["rv20_ann"], 0.40)
        self.assertEqual(vp["rv30_ann"], 0.42)
        self.assertEqual(vp["beta"], 1.10)
        self.assertEqual(vp["corr"], 0.75)
        self.assertEqual(vp["beta_n_days"], 250)
        self.assertEqual(vp["max_dd_10yr"], -0.30)


# --------------------------------------------------------------------------- #
# Renormalization (a whole dimension has zero evaluable inputs)
# --------------------------------------------------------------------------- #

class TestRenormalization(unittest.TestCase):
    def test_liquidity_dimension_null_renormalizes(self):
        # adv, net, mktcap ALL null -> liquidity dimension excluded (max 20).
        # Score rescaled 0-100 over remaining max (100-20=80).
        tech = _tech()
        ladder = _ladder([(96.0, "ma50"), (110.0, "swing_high")])
        result = sr.score(tech=tech, beta=1.0, ladder=ladder, last=100.0,
                          adv=None, net=None, mktcap=None)
        self.assertTrue(result["renormalized"])
        maxes = sum(s["max"] for s in result["subscores"])
        self.assertEqual(maxes, 80)
        self.assertLessEqual(result["score"], 100)
        self.assertGreaterEqual(result["score"], 0)

    def test_no_renormalization_when_all_dimensions_have_inputs(self):
        tech = _tech()
        ladder = _ladder([(96.0, "ma50"), (110.0, "swing_high")])
        result = sr.score(tech=tech, beta=1.0, ladder=ladder, last=100.0,
                          adv=600e6, net=10.0, mktcap=100.0)
        self.assertFalse(result["renormalized"])
        maxes = sum(s["max"] for s in result["subscores"])
        self.assertEqual(maxes, 100)


# --------------------------------------------------------------------------- #
# INPUT_FIELDS declaration (Task 13 cross-skill disjointness will import this)
# --------------------------------------------------------------------------- #

class TestInputFields(unittest.TestCase):
    def test_input_fields_exact(self):
        self.assertEqual(sr.INPUT_FIELDS, {
            "technicals.rv30_vs_10yr_pctile", "benchmark.beta",
            "technicals.max_dd_10yr", "technicals.dd_episodes_20pct_10yr",
            "technicals.dd_episodes_30pct_10yr", "technicals.dist_from_ath_pct",
            "price.adv_dollar_3m", "fundamentals.net_cash_defined.net",
            "price.mktcap_computed",
            # confidence-gating inputs (short-history bug): the beta component is
            # gated on the return-day count, the rv-percentile on the ohlcv rows.
            "benchmark.beta_n_days", "technicals.ohlcv_rows",
        })

    def test_shared_reference_fields_not_listed(self):
        self.assertNotIn("price.last", sr.INPUT_FIELDS)


# --------------------------------------------------------------------------- #
# CLI end-to-end (real bundle, reuses test_build_snapshot fixtures)
# --------------------------------------------------------------------------- #

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "score_risk.py")
TECH_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "score_technical.py")


class TestCLI(unittest.TestCase):
    def setUp(self):
        import shutil
        import tests.test_build_snapshot as tb
        self.tb = tb
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        tb.BundleBuilder(self.dir).build_full()
        proc = tb._run_build(self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _run_tech(self):
        cmd = [sys.executable, TECH_SCRIPT, "--bundle", self.dir]
        return subprocess.run(cmd, capture_output=True, text=True)

    def _run(self, extra=None):
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir]
        if extra:
            cmd += extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_missing_module_technical_errors(self):
        # No module_technical.json yet -> hard error exit 2.
        proc = self._run()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("technical-analysis", proc.stderr)

    def test_cli_exit0_writes_module_json(self):
        self.assertEqual(self._run_tech().returncode, 0)
        proc = self._run()
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = os.path.join(self.dir, "module_risk.json")
        self.assertTrue(os.path.exists(out))
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["skill"], "risk-analytics")
        self.assertEqual(doc["rubric_version"], "1.0.0")
        self.assertEqual(doc["ticker"], "MU")
        self.assertIn("as_of", doc)
        self.assertIsInstance(doc["score"], (int, float))
        self.assertGreaterEqual(doc["score"], 0)
        self.assertLessEqual(doc["score"], 100)
        self.assertIsInstance(doc["subscores"], list)
        self.assertEqual(len(doc["subscores"]), 4)
        self.assertIn("downside_map", doc["tables"])
        self.assertIn("vol_profile", doc["tables"])
        self.assertIsNone(doc["signal"])
        # confidence-v1.0.0: well-formed block; depth MEDIUM at rubric 1.0.0.
        conf = doc["confidence"]
        self.assertEqual(set(conf),
                         {"level", "source", "depth", "staleness", "rule",
                          "version"})
        self.assertIn(conf["level"], ("LOW", "MEDIUM", "HIGH"))
        self.assertEqual(conf["version"], "1.0.0")
        self.assertIn(conf["depth"]["level"], ("MEDIUM", "HIGH"))
        for s in doc["subscores"]:
            self.assertIn("arithmetic", s)
            self.assertIn("inputs", s)
            self.assertIn("name", s)

    def test_stress_requires_top_risk(self):
        self.assertEqual(self._run_tech().returncode, 0)
        proc = self._run(extra=["--stress-pct", "-0.30"])
        self.assertEqual(proc.returncode, 2)

    def test_stress_with_top_risk_ok(self):
        self.assertEqual(self._run_tech().returncode, 0)
        proc = self._run(extra=["--stress-pct", "-0.30",
                                "--top-risk", "HBM demand air-pocket"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_risk.json")
        with open(out) as fh:
            doc = json.load(fh)
        dm = doc["tables"]["downside_map"]
        stress = [r for r in dm if r["type"] == "stress_scenario"]
        self.assertEqual(len(stress), 1)
        self.assertEqual(stress[0]["risk"], "HBM demand air-pocket")
        self.assertEqual(doc["flags"]["stress_pct"], -0.30)
        self.assertEqual(doc["flags"]["top_risk"], "HBM demand air-pocket")

    def test_custom_out_path(self):
        self.assertEqual(self._run_tech().returncode, 0)
        out = os.path.join(self.dir, "custom_risk.json")
        proc = self._run(extra=["--out", out])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

    # -- anchored downside floor CLI ---------------------------------------

    def _write_anchors(self, obj):
        path = os.path.join(self.dir, "valuation_anchors.json")
        with open(path, "w") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh)
        return path

    def test_cli_snapshot_mode_records_pe_median_floor_mode(self):
        # No --anchors -> downside_floor_mode "pe_median" (snapshot mode).
        self.assertEqual(self._run_tech().returncode, 0)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_risk.json")
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["downside_floor_mode"], "pe_median")

    def test_cli_anchors_switch_floor_to_dcf_bear(self):
        # A valid anchors file switches the downside floor to dcf_bear: the
        # module records downside_floor_mode "dcf_bear", and the floor row (a
        # dcf_bear 95 below the ~90-start last MU fixture) carries the label.
        self.assertEqual(self._run_tech().returncode, 0)
        path = self._write_anchors({
            "dcf_base": 120.0, "dcf_bear": 95.0, "dcf_bull": 150.0,
            "comps_low": 100.0, "comps_high": 140.0,
            "assumptions": {"wacc": 0.10}, "citations": {"dcf": "C1"},
            "as_of": "2026-07-15"})
        proc = self._run(extra=["--anchors", path])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_risk.json")
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["downside_floor_mode"], "dcf_bear")
        dm = doc["tables"]["downside_map"]
        vfs = [r for r in dm if r["type"] == "valuation_floor"]
        # The floor is present only when dcf_bear < last (MU fixture last ~90);
        # when present it must be the dcf_bear level with the anchored basis.
        for r in vfs:
            self.assertEqual(r["level"], 95.0)
            self.assertEqual(r["basis"], "dcf_bear (coverage anchors)")
            self.assertNotIn("suspect", r)

    def test_cli_malformed_anchors_exit2(self):
        # missing dcf_bear -> validate_anchors fails -> exit 2 naming the issue.
        self.assertEqual(self._run_tech().returncode, 0)
        path = self._write_anchors({"dcf_base": 120.0, "dcf_bull": 150.0,
                                    "comps_low": 100.0, "comps_high": 140.0})
        proc = self._run(extra=["--anchors", path])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("dcf_bear", proc.stderr)

    def test_cli_anchors_bad_json_exit2(self):
        self.assertEqual(self._run_tech().returncode, 0)
        path = self._write_anchors("{not json")
        proc = self._run(extra=["--anchors", path])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not valid JSON", proc.stderr)

    def test_determinism(self):
        self.assertEqual(self._run_tech().returncode, 0)
        out1 = os.path.join(self.dir, "run1.json")
        out2 = os.path.join(self.dir, "run2.json")
        p1 = self._run(extra=["--out", out1])
        p2 = self._run(extra=["--out", out2])
        self.assertEqual(p1.returncode, 0, p1.stderr)
        self.assertEqual(p2.returncode, 0, p2.stderr)
        with open(out1) as fh:
            a = fh.read()
        with open(out2) as fh:
            b = fh.read()
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
