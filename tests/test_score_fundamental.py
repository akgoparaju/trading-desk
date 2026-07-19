"""Tests for scripts/score_fundamental.py -- the compressed-pass fundamental scorer.

WHY: this is the ALWAYS-AVAILABLE fundamental path (design spec §8.1 "FSI absent"
branch). When the deep FSI initiation / model reuse is not applied, the composite
still needs a disclosed, snapshot-only fundamental score. Like the other scorers,
this module's arithmetic IS the rubric of record (fundamental rubric v1.1.0,
"compressed_snapshot_pass"): every branch is pinned to a hand-computed value so a
report can never silently drift, and the mode is disclosed at the module top level
so a reader always knows this was the snapshot-only pass, not the deep model.

RUBRIC v1.0.0 -> v1.1.0 (coverage-first spec, Task C1): the Quality dimension is
rebalanced from a five-component 50 to a SIX-component 50 to make room for a
moat/positioning judgment flag scored from cited context findings. The mechanical
component maxima shrink (rev growth 15->12, gm 8->7, om 7->5, roe 10->8, fcf 10->8)
and a new Moat component (max 10) is added. Every quality band test below is
RE-PINNED to the new maxima; each carries an ``old -> new`` comment marking the
deliberate rubric change. Valuation (50) is unchanged. The moat flag mirrors the
score_sentiment judgment-flag convention (flag + REQUIRED justification recorded in
module flags), and when the flag is given the justification MUST cite at least one
context finding ID (regex ``C\\d+``, e.g. "C3").

Tests exercise the pure scoring functions directly (exact value per band), the
roe percent-vs-fraction normalization, the pe_fwd/pe_5yr_median ratio bands with
the pe_median_method label carried into the arithmetic string, per-component null
handling, whole-dimension renormalization, the mode disclosure fields, the moat
judgment flag (wide/narrow/none/omitted + evaluability + justification/citation
validation), determinism, and one end-to-end CLI run against a real snapshot bundle
fabricated exactly the way test_score_sentiment.py does. The scoring functions take
already-parsed sub-blocks so branches pin without a full snapshot.

RUBRIC v1.1.0 -> v1.2.0 (sector-scales batch): the SNAPSHOT-MODE valuation tests
above are UNCHANGED (only the module rubric_version assertions move to "1.2.0" --
the snapshot valuation floor is byte-identical). Quality/moat are untouched. The
NEW anchored-mode tests (TestAnchored*) pin the DCF/comps/own-history/fcf/
justified-band components, the disagreement>25% widen + 0.75 haircut arithmetic,
the peg_display block, PEG's absence from the anchored subscores, and the CLI
exit-2 on a malformed anchors file.

stdlib-only; unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import score_fundamental as sf


# --------------------------------------------------------------------------- #
# Helpers: minimal snapshot sub-blocks.
# --------------------------------------------------------------------------- #

def _fund(**over):
    """A fully-populated fundamentals block; override per test."""
    base = {
        "rev_growth_latest_q": 0.25,
        "gm_ttm": 0.50,
        "om_ttm": 0.25,
        "roe": 0.28,
        "fcf_ttm": 5000.0,
        "rev_ttm": 20000.0,
    }
    base.update(over)
    return base


def _val(**over):
    """A fully-populated valuation block; override per test."""
    base = {
        "pe_fwd": 18.0,
        "pe_5yr_median": 20.0,
        "pe_median_method": "approx_current_eps",
        "peg": 1.2,
        "fcf_yield": 0.04,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Quality dim 1: revenue growth (v1.0.0 max 15 -> v1.1.0 max 12)
# --------------------------------------------------------------------------- #

class TestRevGrowth(unittest.TestCase):
    def test_hyper_growth_is_12(self):
        # 0.25 > 0.20 -> v1.0.0: 15 -> v1.1.0: 12
        sub = sf.score_quality(_fund(rev_growth_latest_q=0.25))
        self.assertEqual(sub["inputs"]["rev_growth_points"], 12)

    def test_strong_growth_is_9(self):
        # 0.15 in (0.10,0.20] -> v1.0.0: 11 -> v1.1.0: 9
        sub = sf.score_quality(_fund(rev_growth_latest_q=0.15))
        self.assertEqual(sub["inputs"]["rev_growth_points"], 9)

    def test_moderate_growth_is_6(self):
        # 0.05 in (0.03,0.10] -> v1.0.0: 8 -> v1.1.0: 6
        sub = sf.score_quality(_fund(rev_growth_latest_q=0.05))
        self.assertEqual(sub["inputs"]["rev_growth_points"], 6)

    def test_slow_growth_is_4(self):
        # 0.02 in [0,0.03] -> v1.0.0: 5 -> v1.1.0: 4
        sub = sf.score_quality(_fund(rev_growth_latest_q=0.02))
        self.assertEqual(sub["inputs"]["rev_growth_points"], 4)

    def test_contraction_is_2(self):
        # -0.05 < 0 -> 2 (unchanged)
        sub = sf.score_quality(_fund(rev_growth_latest_q=-0.05))
        self.assertEqual(sub["inputs"]["rev_growth_points"], 2)

    def test_null_is_0_na(self):
        sub = sf.score_quality(_fund(rev_growth_latest_q=None))
        self.assertEqual(sub["inputs"]["rev_growth_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])

    def test_boundary_020_is_9(self):
        # exactly 0.20 is NOT > 0.20 -> (0.10,0.20] band -> v1.0.0: 11 -> v1.1.0: 9
        sub = sf.score_quality(_fund(rev_growth_latest_q=0.20))
        self.assertEqual(sub["inputs"]["rev_growth_points"], 9)


# --------------------------------------------------------------------------- #
# Quality dim 2: margins (gm v1.0.0 max 8 -> v1.1.0 max 7;
#                         om v1.0.0 max 7 -> v1.1.0 max 5)
# --------------------------------------------------------------------------- #

class TestMargins(unittest.TestCase):
    def test_gm_high_is_7(self):
        # >=0.50 -> v1.0.0: 8 -> v1.1.0: 7
        sub = sf.score_quality(_fund(gm_ttm=0.55))
        self.assertEqual(sub["inputs"]["gm_points"], 7)

    def test_gm_mid_is_5(self):
        # 0.40 in [0.35,0.50) -> v1.0.0: 6 -> v1.1.0: 5
        sub = sf.score_quality(_fund(gm_ttm=0.40))
        self.assertEqual(sub["inputs"]["gm_points"], 5)

    def test_gm_low_is_3(self):
        # 0.25 in [0.20,0.35) -> v1.0.0: 4 -> v1.1.0: 3
        sub = sf.score_quality(_fund(gm_ttm=0.25))
        self.assertEqual(sub["inputs"]["gm_points"], 3)

    def test_gm_thin_is_1(self):
        # 0.10 < 0.20 -> v1.0.0: 2 -> v1.1.0: 1
        sub = sf.score_quality(_fund(gm_ttm=0.10))
        self.assertEqual(sub["inputs"]["gm_points"], 1)

    def test_gm_null_is_0(self):
        sub = sf.score_quality(_fund(gm_ttm=None))
        self.assertEqual(sub["inputs"]["gm_points"], 0)

    def test_gm_boundary_050_is_7(self):
        # >=0.50 -> v1.0.0: 8 -> v1.1.0: 7
        sub = sf.score_quality(_fund(gm_ttm=0.50))
        self.assertEqual(sub["inputs"]["gm_points"], 7)

    def test_om_high_is_5(self):
        # >=0.25 -> v1.0.0: 7 -> v1.1.0: 5
        sub = sf.score_quality(_fund(om_ttm=0.30))
        self.assertEqual(sub["inputs"]["om_points"], 5)

    def test_om_mid_is_4(self):
        # 0.20 in [0.15,0.25) -> v1.0.0: 5 -> v1.1.0: 4
        sub = sf.score_quality(_fund(om_ttm=0.20))
        self.assertEqual(sub["inputs"]["om_points"], 4)

    def test_om_low_is_2(self):
        # 0.10 in [0.05,0.15) -> v1.0.0: 3 -> v1.1.0: 2
        sub = sf.score_quality(_fund(om_ttm=0.10))
        self.assertEqual(sub["inputs"]["om_points"], 2)

    def test_om_thin_is_1(self):
        # 0.02 < 0.05 -> 1 (unchanged)
        sub = sf.score_quality(_fund(om_ttm=0.02))
        self.assertEqual(sub["inputs"]["om_points"], 1)

    def test_om_null_is_0(self):
        sub = sf.score_quality(_fund(om_ttm=None))
        self.assertEqual(sub["inputs"]["om_points"], 0)

    def test_om_boundary_025_is_5(self):
        # >=0.25 -> v1.0.0: 7 -> v1.1.0: 5
        sub = sf.score_quality(_fund(om_ttm=0.25))
        self.assertEqual(sub["inputs"]["om_points"], 5)


# --------------------------------------------------------------------------- #
# Quality dim 3: returns on capital / roe (v1.0.0 max 10 -> v1.1.0 max 8),
#                percent-vs-fraction norm (unchanged)
# --------------------------------------------------------------------------- #

class TestRoe(unittest.TestCase):
    def test_roe_high_fraction_is_8(self):
        # 0.36 >= 0.30 -> v1.0.0: 10 -> v1.1.0: 8
        sub = sf.score_quality(_fund(roe=0.36))
        self.assertEqual(sub["inputs"]["roe_points"], 8)

    def test_roe_high_percent_normalized_is_8(self):
        # 36.0 > 3 -> treated as percent -> 0.36 -> v1.0.0: 10 -> v1.1.0: 8,
        # and labeled in arithmetic
        sub = sf.score_quality(_fund(roe=36.0))
        self.assertEqual(sub["inputs"]["roe_points"], 8)
        self.assertEqual(sub["inputs"]["roe_normalized"], 0.36)
        self.assertIn("percent", sub["arithmetic"])

    def test_roe_percent_and_fraction_agree(self):
        # roe "36.0" (percent) and roe 0.36 (fraction) must score identically
        a = sf.score_quality(_fund(roe=36.0))
        b = sf.score_quality(_fund(roe=0.36))
        self.assertEqual(a["inputs"]["roe_points"], b["inputs"]["roe_points"])

    def test_roe_mid_is_6(self):
        # 0.20 in [0.15,0.30) -> v1.0.0: 7 -> v1.1.0: 6
        sub = sf.score_quality(_fund(roe=0.20))
        self.assertEqual(sub["inputs"]["roe_points"], 6)

    def test_roe_low_is_3(self):
        # 0.08 in [0.05,0.15) -> v1.0.0: 4 -> v1.1.0: 3
        sub = sf.score_quality(_fund(roe=0.08))
        self.assertEqual(sub["inputs"]["roe_points"], 3)

    def test_roe_weak_is_1(self):
        # 0.02 < 0.05 -> 1 (unchanged)
        sub = sf.score_quality(_fund(roe=0.02))
        self.assertEqual(sub["inputs"]["roe_points"], 1)

    def test_roe_null_is_0(self):
        sub = sf.score_quality(_fund(roe=None))
        self.assertEqual(sub["inputs"]["roe_points"], 0)

    def test_roe_boundary_3_not_normalized(self):
        # exactly 3 is NOT > 3, so 3.0 stays a fraction (an implausible 300% roe),
        # >= 0.30 -> v1.0.0: 10 -> v1.1.0: 8. Guards the normalization threshold
        # direction.
        sub = sf.score_quality(_fund(roe=3.0))
        self.assertEqual(sub["inputs"]["roe_normalized"], 3.0)
        self.assertEqual(sub["inputs"]["roe_points"], 8)


# --------------------------------------------------------------------------- #
# Quality dim 4: FCF margin = fcf_ttm / rev_ttm (v1.0.0 max 10 -> v1.1.0 max 8)
# --------------------------------------------------------------------------- #

class TestFcfMargin(unittest.TestCase):
    def test_high_is_8(self):
        # 5000/20000 = 0.25 >= 0.20 -> v1.0.0: 10 -> v1.1.0: 8
        sub = sf.score_quality(_fund(fcf_ttm=5000.0, rev_ttm=20000.0))
        self.assertEqual(sub["inputs"]["fcf_margin_points"], 8)

    def test_mid_is_6(self):
        # 3000/20000 = 0.15 in [0.10,0.20) -> v1.0.0: 7 -> v1.1.0: 6
        sub = sf.score_quality(_fund(fcf_ttm=3000.0, rev_ttm=20000.0))
        self.assertEqual(sub["inputs"]["fcf_margin_points"], 6)

    def test_low_is_3(self):
        # 1000/20000 = 0.05 in [0,0.10) -> v1.0.0: 4 -> v1.1.0: 3
        sub = sf.score_quality(_fund(fcf_ttm=1000.0, rev_ttm=20000.0))
        self.assertEqual(sub["inputs"]["fcf_margin_points"], 3)

    def test_negative_is_1(self):
        # -2000/20000 = -0.10 < 0 -> 1 (unchanged)
        sub = sf.score_quality(_fund(fcf_ttm=-2000.0, rev_ttm=20000.0))
        self.assertEqual(sub["inputs"]["fcf_margin_points"], 1)

    def test_fcf_null_is_0(self):
        sub = sf.score_quality(_fund(fcf_ttm=None, rev_ttm=20000.0))
        self.assertEqual(sub["inputs"]["fcf_margin_points"], 0)

    def test_rev_null_is_0(self):
        sub = sf.score_quality(_fund(fcf_ttm=5000.0, rev_ttm=None))
        self.assertEqual(sub["inputs"]["fcf_margin_points"], 0)

    def test_rev_zero_is_0(self):
        sub = sf.score_quality(_fund(fcf_ttm=5000.0, rev_ttm=0.0))
        self.assertEqual(sub["inputs"]["fcf_margin_points"], 0)

    def test_boundary_020_is_8(self):
        # >=0.20 -> v1.0.0: 10 -> v1.1.0: 8
        sub = sf.score_quality(_fund(fcf_ttm=4000.0, rev_ttm=20000.0))
        self.assertEqual(sub["inputs"]["fcf_margin_points"], 8)


# --------------------------------------------------------------------------- #
# Quality dim 6: moat/positioning judgment flag (NEW in v1.1.0, max 10)
#   wide -> 10 / narrow -> 6 / none -> 2 / OMITTED (None) -> 0 "n/a", not evaluable.
#   Scored from cited context findings; mirrors score_sentiment flag conventions.
# --------------------------------------------------------------------------- #

class TestMoat(unittest.TestCase):
    def test_wide_is_10(self):
        sub = sf.score_quality(_fund(), moat="wide",
                               moat_justification="durable pricing power (C3)")
        self.assertEqual(sub["inputs"]["moat_points"], 10)
        self.assertIn("moat wide", sub["arithmetic"])

    def test_narrow_is_6(self):
        sub = sf.score_quality(_fund(), moat="narrow",
                               moat_justification="some switching costs (C1)")
        self.assertEqual(sub["inputs"]["moat_points"], 6)

    def test_none_is_2(self):
        sub = sf.score_quality(_fund(), moat="none",
                               moat_justification="commoditized, no pricing power (C5)")
        self.assertEqual(sub["inputs"]["moat_points"], 2)

    def test_omitted_is_0_na(self):
        # Flag omitted entirely -> 0 with the "n/a (no context assessment)" string.
        sub = sf.score_quality(_fund(), moat=None, moat_justification=None)
        self.assertEqual(sub["inputs"]["moat_points"], 0)
        self.assertIn("moat: n/a (no context assessment)", sub["arithmetic"])

    def test_omitted_does_not_add_to_evaluable(self):
        # Omitted moat mirrors sentiment inst_flow "unknown": contributes 0 and does
        # NOT count toward evaluable. Here the OTHER quality inputs keep the
        # dimension evaluable, so we verify the moat component itself is n/a-shaped
        # AND that an otherwise-empty quality block with only a present moat flag
        # IS evaluable (present flag always evaluable).
        only_moat = sf.score_quality(
            {"rev_growth_latest_q": None, "gm_ttm": None, "om_ttm": None,
             "roe": None, "fcf_ttm": None, "rev_ttm": None},
            moat="wide", moat_justification="brand + scale (C2)")
        self.assertTrue(only_moat["evaluable"])
        self.assertEqual(only_moat["points"], 10)

    def test_omitted_only_quality_not_evaluable(self):
        # All mechanical inputs null AND moat omitted -> the whole dimension has no
        # evaluable inputs (omitted moat does not count), mirroring sentiment's
        # inst_flow-unknown + null-insider "not evaluable" case.
        sub = sf.score_quality(
            {"rev_growth_latest_q": None, "gm_ttm": None, "om_ttm": None,
             "roe": None, "fcf_ttm": None, "rev_ttm": None},
            moat=None, moat_justification=None)
        self.assertFalse(sub["evaluable"])
        self.assertEqual(sub["points"], 0)

    def test_flag_and_justification_recorded_in_inputs(self):
        sub = sf.score_quality(_fund(), moat="wide",
                               moat_justification="network effects (C4)")
        self.assertEqual(sub["inputs"]["moat"], "wide")
        self.assertEqual(sub["inputs"]["moat_justification"],
                         "network effects (C4)")

    def test_default_call_omits_moat(self):
        # score_quality(_fund()) with no moat kwargs behaves as "omitted".
        sub = sf.score_quality(_fund())
        self.assertEqual(sub["inputs"]["moat_points"], 0)
        self.assertIsNone(sub["inputs"]["moat"])


class TestQualityComposite(unittest.TestCase):
    def test_full_quality_max_50_with_moat(self):
        # v1.1.0: 12 + 7 + 5 + 8 + 8 (mechanical = 40) + 10 (moat wide) = 50
        sub = sf.score_quality(_fund(rev_growth_latest_q=0.25, gm_ttm=0.55,
                                     om_ttm=0.30, roe=0.36,
                                     fcf_ttm=5000.0, rev_ttm=20000.0),
                               moat="wide",
                               moat_justification="durable moat (C1)")
        self.assertEqual(sub["points"], 50)
        self.assertEqual(sub["max"], 50)
        self.assertTrue(sub["evaluable"])

    def test_mechanical_max_without_moat_is_40(self):
        # All mechanical bands maxed but moat OMITTED: 12+7+5+8+8 = 40 (moat +0).
        sub = sf.score_quality(_fund(rev_growth_latest_q=0.25, gm_ttm=0.55,
                                     om_ttm=0.30, roe=0.36,
                                     fcf_ttm=5000.0, rev_ttm=20000.0))
        self.assertEqual(sub["points"], 40)
        self.assertEqual(sub["max"], 50)

    def test_all_null_and_moat_omitted_not_evaluable(self):
        sub = sf.score_quality({"rev_growth_latest_q": None, "gm_ttm": None,
                                "om_ttm": None, "roe": None,
                                "fcf_ttm": None, "rev_ttm": None})
        self.assertFalse(sub["evaluable"])
        self.assertEqual(sub["points"], 0)


# --------------------------------------------------------------------------- #
# Valuation dim 1: multiple vs own history (max 20), method label required
# --------------------------------------------------------------------------- #

class TestMultipleVsHistory(unittest.TestCase):
    def test_discount_is_20(self):
        # 15/20 = 0.75 <= 0.75 -> 20
        sub = sf.score_valuation(_val(pe_fwd=15.0, pe_5yr_median=20.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 20)
        self.assertIn("discount", sub["arithmetic"])

    def test_slight_discount_is_14(self):
        # 18/20 = 0.9 in (0.75,1.0] -> 14
        sub = sf.score_valuation(_val(pe_fwd=18.0, pe_5yr_median=20.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 14)

    def test_slight_premium_is_8(self):
        # 22/20 = 1.1 in (1.0,1.25] -> 8
        sub = sf.score_valuation(_val(pe_fwd=22.0, pe_5yr_median=20.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 8)

    def test_rich_premium_is_3(self):
        # 30/20 = 1.5 > 1.25 -> 3
        sub = sf.score_valuation(_val(pe_fwd=30.0, pe_5yr_median=20.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 3)

    def test_boundary_ratio_1_0_is_14(self):
        # 20/20 = 1.0 in (0.75,1.0] -> 14
        sub = sf.score_valuation(_val(pe_fwd=20.0, pe_5yr_median=20.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 14)

    def test_method_label_present_in_arithmetic(self):
        # the pe_median_method label must be disclosed wherever this scores.
        sub = sf.score_valuation(_val(pe_fwd=15.0, pe_5yr_median=20.0,
                                      pe_median_method="approx_current_eps"))
        self.assertIn("approx_current_eps", sub["arithmetic"])

    def test_pe_fwd_null_is_0_na(self):
        sub = sf.score_valuation(_val(pe_fwd=None, pe_5yr_median=20.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])

    def test_pe_median_null_is_0_na(self):
        sub = sf.score_valuation(_val(pe_fwd=18.0, pe_5yr_median=None))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 0)

    def test_pe_median_zero_is_0_na(self):
        # both must be > 0
        sub = sf.score_valuation(_val(pe_fwd=18.0, pe_5yr_median=0.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 0)

    def test_pe_fwd_nonpositive_is_0_na(self):
        # negative pe_fwd (loss-making fwd) -> component n/a
        sub = sf.score_valuation(_val(pe_fwd=-10.0, pe_5yr_median=20.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 0)

    # -- pe_5yr_median sanity band [0.2, 5.0] (approx_current_eps breakdown) --

    def test_ratio_above_band_is_na(self):
        # real MU regime: pe_fwd 10 / pe_5yr_median 1.82 = 5.4 (> 5.0) -> the
        # approx_current_eps median is garbage; component scored 0 + n/a and the
        # sanity-band arithmetic string is emitted.
        sub = sf.score_valuation(_val(pe_fwd=9.828, pe_5yr_median=1.82))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 0)
        self.assertIn("outside sanity band [0.2,5]", sub["arithmetic"])
        self.assertIn("approx_current_eps method breakdown", sub["arithmetic"])
        self.assertIn("component n/a", sub["arithmetic"])

    def test_ratio_normal_09_bands_normally(self):
        # 18/20 = 0.9 is inside the band -> normal (0.75,1.0] -> 14, no n/a.
        sub = sf.score_valuation(_val(pe_fwd=18.0, pe_5yr_median=20.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 14)
        self.assertNotIn("sanity band", sub["arithmetic"])

    def test_ratio_boundary_50_is_normal(self):
        # exactly 5.0 is INSIDE the band (not > 5.0) -> banded (> 1.25 -> 3).
        sub = sf.score_valuation(_val(pe_fwd=100.0, pe_5yr_median=20.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 3)
        self.assertNotIn("sanity band", sub["arithmetic"])

    def test_ratio_boundary_501_is_na(self):
        # 5.01 > 5.0 -> just outside the band -> n/a.
        sub = sf.score_valuation(_val(pe_fwd=100.2, pe_5yr_median=20.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 0)
        self.assertIn("outside sanity band [0.2,5]", sub["arithmetic"])

    def test_ratio_below_band_is_na(self):
        # 0.19 < 0.2 -> just outside the low edge -> n/a (symmetric guard).
        sub = sf.score_valuation(_val(pe_fwd=1.9, pe_5yr_median=10.0))
        self.assertEqual(sub["inputs"]["pe_ratio_points"], 0)
        self.assertIn("outside sanity band [0.2,5]", sub["arithmetic"])

    def test_out_of_band_pe_renormalizes_dimension(self):
        # When pe is the ONLY valuation input and its ratio is out of band, the
        # component is n/a like a null -> the whole valuation dimension has zero
        # evaluable inputs -> score() EXCLUDES it and renormalizes the fundamental
        # score over the remaining quality max (50). Mirrors the null-valuation
        # renormalization test, proving the sanity gate is treated as a null input.
        result = sf.score(
            _fund(),
            {"pe_fwd": 9.828, "pe_5yr_median": 1.82,
             "pe_median_method": "approx_current_eps",
             "peg": None, "fcf_yield": None})
        self.assertTrue(result["renormalized"])
        maxes = sum(s["max"] for s in result["subscores"])
        self.assertEqual(maxes, 50)


# --------------------------------------------------------------------------- #
# Valuation dim 2: PEG (max 15)
# --------------------------------------------------------------------------- #

class TestPeg(unittest.TestCase):
    def test_cheap_is_15(self):
        # 0.8 in (0,1.0] -> 15
        sub = sf.score_valuation(_val(peg=0.8))
        self.assertEqual(sub["inputs"]["peg_points"], 15)

    def test_fair_is_10(self):
        # 1.5 in (1.0,2.0] -> 10
        sub = sf.score_valuation(_val(peg=1.5))
        self.assertEqual(sub["inputs"]["peg_points"], 10)

    def test_rich_is_5(self):
        # 2.5 in (2.0,3.0] -> 5
        sub = sf.score_valuation(_val(peg=2.5))
        self.assertEqual(sub["inputs"]["peg_points"], 5)

    def test_expensive_is_2(self):
        # 4.0 > 3.0 -> 2
        sub = sf.score_valuation(_val(peg=4.0))
        self.assertEqual(sub["inputs"]["peg_points"], 2)

    def test_null_is_0(self):
        sub = sf.score_valuation(_val(peg=None))
        self.assertEqual(sub["inputs"]["peg_points"], 0)

    def test_nonpositive_is_0(self):
        # negative PEG (negative growth denom) -> 0 "n/a"
        sub = sf.score_valuation(_val(peg=-1.0))
        self.assertEqual(sub["inputs"]["peg_points"], 0)

    def test_boundary_1_0_is_15(self):
        sub = sf.score_valuation(_val(peg=1.0))
        self.assertEqual(sub["inputs"]["peg_points"], 15)


# --------------------------------------------------------------------------- #
# Valuation dim 3: FCF yield (max 15)
# --------------------------------------------------------------------------- #

class TestFcfYield(unittest.TestCase):
    def test_high_is_15(self):
        # 0.06 >= 0.05 -> 15
        sub = sf.score_valuation(_val(fcf_yield=0.06))
        self.assertEqual(sub["inputs"]["fcf_yield_points"], 15)

    def test_good_is_11(self):
        # 0.04 in [0.03,0.05) -> 11
        sub = sf.score_valuation(_val(fcf_yield=0.04))
        self.assertEqual(sub["inputs"]["fcf_yield_points"], 11)

    def test_thin_is_7(self):
        # 0.02 in [0.015,0.03) -> 7
        sub = sf.score_valuation(_val(fcf_yield=0.02))
        self.assertEqual(sub["inputs"]["fcf_yield_points"], 7)

    def test_meager_is_3(self):
        # 0.01 in (0,0.015) -> 3
        sub = sf.score_valuation(_val(fcf_yield=0.01))
        self.assertEqual(sub["inputs"]["fcf_yield_points"], 3)

    def test_nonpositive_is_1(self):
        # -0.02 <= 0 -> 1
        sub = sf.score_valuation(_val(fcf_yield=-0.02))
        self.assertEqual(sub["inputs"]["fcf_yield_points"], 1)

    def test_null_is_0(self):
        sub = sf.score_valuation(_val(fcf_yield=None))
        self.assertEqual(sub["inputs"]["fcf_yield_points"], 0)

    def test_boundary_005_is_15(self):
        sub = sf.score_valuation(_val(fcf_yield=0.05))
        self.assertEqual(sub["inputs"]["fcf_yield_points"], 15)


class TestValuationComposite(unittest.TestCase):
    def test_full_valuation_max_50(self):
        # 20 + 15 + 15 = 50
        sub = sf.score_valuation(_val(pe_fwd=15.0, pe_5yr_median=20.0,
                                      peg=0.8, fcf_yield=0.06))
        self.assertEqual(sub["points"], 50)
        self.assertEqual(sub["max"], 50)
        self.assertTrue(sub["evaluable"])

    def test_all_null_not_evaluable(self):
        sub = sf.score_valuation({"pe_fwd": None, "pe_5yr_median": None,
                                  "pe_median_method": "approx_current_eps",
                                  "peg": None, "fcf_yield": None})
        self.assertFalse(sub["evaluable"])
        self.assertEqual(sub["points"], 0)


# --------------------------------------------------------------------------- #
# Composite scoring + renormalization
# --------------------------------------------------------------------------- #

class TestScore(unittest.TestCase):
    def test_no_renormalization_when_both_dimensions_have_inputs(self):
        result = sf.score(_fund(), _val())
        self.assertFalse(result["renormalized"])
        maxes = sum(s["max"] for s in result["subscores"])
        self.assertEqual(maxes, 100)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_two_subscores(self):
        result = sf.score(_fund(), _val())
        self.assertEqual(len(result["subscores"]), 2)

    def test_valuation_null_renormalizes_over_50(self):
        # valuation dimension entirely null -> excluded, renormalize over 50.
        result = sf.score(_fund(), {"pe_fwd": None, "pe_5yr_median": None,
                                    "pe_median_method": "approx_current_eps",
                                    "peg": None, "fcf_yield": None})
        self.assertTrue(result["renormalized"])
        maxes = sum(s["max"] for s in result["subscores"])
        self.assertEqual(maxes, 50)

    def test_quality_null_renormalizes_over_50(self):
        result = sf.score({"rev_growth_latest_q": None, "gm_ttm": None,
                           "om_ttm": None, "roe": None,
                           "fcf_ttm": None, "rev_ttm": None}, _val())
        self.assertTrue(result["renormalized"])
        maxes = sum(s["max"] for s in result["subscores"])
        self.assertEqual(maxes, 50)

    def test_both_null_score_zero(self):
        result = sf.score({"rev_growth_latest_q": None, "gm_ttm": None,
                           "om_ttm": None, "roe": None,
                           "fcf_ttm": None, "rev_ttm": None},
                          {"pe_fwd": None, "pe_5yr_median": None,
                           "pe_median_method": "approx_current_eps",
                           "peg": None, "fcf_yield": None})
        self.assertEqual(result["score"], 0)

    def test_full_score_is_100(self):
        # v1.1.0: a perfect 100 now REQUIRES a wide-moat flag (mechanical quality
        # caps at 40/50 without it), so the moat flag is threaded through score().
        result = sf.score(
            _fund(rev_growth_latest_q=0.25, gm_ttm=0.55, om_ttm=0.30,
                  roe=0.36, fcf_ttm=5000.0, rev_ttm=20000.0),
            _val(pe_fwd=15.0, pe_5yr_median=20.0, peg=0.8, fcf_yield=0.06),
            moat="wide", moat_justification="durable moat (C1)")
        self.assertEqual(result["score"], 100)

    def test_full_mechanical_without_moat_is_90(self):
        # Same maxed inputs but moat OMITTED: quality 40/50 + valuation 50/50 = 90
        # over the full max 100 (moat omitted still counts toward the quality
        # dimension max because the OTHER quality inputs make the dimension
        # evaluable -- the dimension max stays 50, moat just contributes 0).
        result = sf.score(
            _fund(rev_growth_latest_q=0.25, gm_ttm=0.55, om_ttm=0.30,
                  roe=0.36, fcf_ttm=5000.0, rev_ttm=20000.0),
            _val(pe_fwd=15.0, pe_5yr_median=20.0, peg=0.8, fcf_yield=0.06))
        self.assertFalse(result["renormalized"])
        self.assertEqual(result["score"], 90)


# =========================================================================== #
# ANCHORED-MODE VALUATION (v1.2.0): DCF(17) + comps(13) + own-history(8) +
# fcf(7) + justified-band(5). PEG removed from scoring (peg_display top-level).
# =========================================================================== #

def _anchors(**over):
    """A valid valuation_anchors dict; override per test.

    dcf_base 100, comps [90,110] -> comps_mid 100 -> disagreement 0 (no widen).
    """
    base = {
        "dcf_base": 100.0, "dcf_bear": 70.0, "dcf_bull": 130.0,
        "comps_low": 90.0, "comps_high": 110.0,
    }
    base.update(over)
    return base


class TestValidateAnchors(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(sf.validate_anchors(_anchors()), [])

    def test_not_a_dict(self):
        issues = sf.validate_anchors(["x"])
        self.assertTrue(any("not a JSON object" in i for i in issues))

    def test_missing_each_required(self):
        for key in ("dcf_base", "dcf_bear", "dcf_bull", "comps_low", "comps_high"):
            a = _anchors()
            del a[key]
            issues = sf.validate_anchors(a)
            self.assertTrue(any(key in i for i in issues), key)

    def test_nonpositive_rejected(self):
        issues = sf.validate_anchors(_anchors(dcf_base=-5.0))
        self.assertTrue(any("dcf_base" in i and "positive" in i for i in issues))

    def test_nonnumeric_rejected(self):
        issues = sf.validate_anchors(_anchors(comps_low="cheap"))
        self.assertTrue(any("comps_low" in i and "numeric" in i for i in issues))

    def test_current_pb_optional(self):
        self.assertEqual(sf.validate_anchors(_anchors(current_pb=2.5)), [])

    def test_current_pb_bad(self):
        issues = sf.validate_anchors(_anchors(current_pb=-1.0))
        self.assertTrue(any("current_pb" in i for i in issues))


class TestAnchoredDcfBand(unittest.TestCase):
    # No disagreement (comps_mid == dcf_base == 100); banding on r = last/dcf_base.
    def test_deep_discount_is_17(self):
        pts, _ = sf._dcf_band_position(70.0, _anchors())  # r=0.7 <= 0.8
        self.assertEqual(pts, 17)

    def test_boundary_08_is_17(self):
        pts, _ = sf._dcf_band_position(80.0, _anchors())  # r=0.8 (<=0.8)
        self.assertEqual(pts, 17)

    def test_at_base_is_14(self):
        pts, _ = sf._dcf_band_position(100.0, _anchors())  # r=1.0 in (0.8,1.1]
        self.assertEqual(pts, 14)

    def test_boundary_11_is_14(self):
        pts, _ = sf._dcf_band_position(110.0, _anchors())  # r=1.1
        self.assertEqual(pts, 14)

    def test_modest_premium_is_9(self):
        pts, _ = sf._dcf_band_position(140.0, _anchors())  # r=1.4 in (1.1,1.5]
        self.assertEqual(pts, 9)

    def test_boundary_15_is_9(self):
        pts, _ = sf._dcf_band_position(150.0, _anchors())  # r=1.5
        self.assertEqual(pts, 9)

    def test_rich_premium_is_4(self):
        pts, _ = sf._dcf_band_position(200.0, _anchors())  # r=2.0 in (1.5,2.5]
        self.assertEqual(pts, 4)

    def test_boundary_25_is_4(self):
        pts, _ = sf._dcf_band_position(250.0, _anchors())  # r=2.5
        self.assertEqual(pts, 4)

    def test_far_above_is_1(self):
        pts, _ = sf._dcf_band_position(300.0, _anchors())  # r=3.0 > 2.5
        self.assertEqual(pts, 1)


class TestAnchoredDisagreementWiden(unittest.TestCase):
    # comps [40,60] -> comps_mid 50; disagreement = |100-50|/75 = 0.6667 > 0.25.
    def _wide(self):
        return _anchors(comps_low=40.0, comps_high=60.0)

    def test_haircut_scales_max_to_1275(self):
        # r=0.7 -> base 17, haircut 0.75 -> 12.75.
        pts, s = sf._dcf_band_position(70.0, self._wide())
        self.assertEqual(pts, 12.75)

    def test_haircut_visible_in_arithmetic(self):
        pts, s = sf._dcf_band_position(70.0, self._wide())
        self.assertIn("WIDEN", s)
        self.assertIn("x0.75", s)
        self.assertIn("17 -> 12.75", s)

    def test_widened_effective_band_disclosed(self):
        # effective_band = [min(dcf_bear 70, comps_low 40), max(dcf_bull 130,
        # comps_high 60)] = [40, 130].
        _, s = sf._dcf_band_position(70.0, self._wide())
        self.assertIn("[40,130]", s)

    def test_haircut_on_lower_tier(self):
        # r=1.4 -> base 9 -> 9 * 0.75 = 6.75.
        pts, _ = sf._dcf_band_position(140.0, self._wide())
        self.assertEqual(pts, 6.75)

    def test_disagreement_below_threshold_no_widen(self):
        # comps [90,110] -> comps_mid 100 == dcf_base -> disagreement 0 (<= 0.25)
        # -> no widen, full 17 (the boundary at exactly 0.25 is float-fragile and
        # deliberately not pinned; only the clearly-inside case is asserted).
        pts, s = sf._dcf_band_position(70.0, _anchors())  # r=0.7
        self.assertEqual(pts, 17)
        self.assertIn("no widen", s)

    def test_disagreement_just_above_threshold_widens(self):
        # comps_mid 70 -> disagreement = |100-70|/85 = 0.3529 > 0.25 -> widen.
        a = _anchors(comps_low=60.0, comps_high=80.0)  # mid 70
        pts, s = sf._dcf_band_position(70.0, a)  # r=0.7 -> 17*0.75
        self.assertEqual(pts, 12.75)
        self.assertIn("WIDEN", s)


class TestAnchoredComps(unittest.TestCase):
    # comps [90,110] -> mid 100.
    def test_below_low_is_13(self):
        pts, _ = sf._comps_range_position(80.0, _anchors())
        self.assertEqual(pts, 13)

    def test_lower_half_is_9(self):
        pts, _ = sf._comps_range_position(95.0, _anchors())
        self.assertEqual(pts, 9)

    def test_boundary_mid_is_9(self):
        pts, _ = sf._comps_range_position(100.0, _anchors())  # <= mid
        self.assertEqual(pts, 9)

    def test_upper_half_is_6(self):
        pts, _ = sf._comps_range_position(105.0, _anchors())
        self.assertEqual(pts, 6)

    def test_boundary_high_is_6(self):
        pts, _ = sf._comps_range_position(110.0, _anchors())  # <= high
        self.assertEqual(pts, 6)

    def test_above_high_within_15x_is_3(self):
        pts, _ = sf._comps_range_position(150.0, _anchors())  # <= 1.5*110=165
        self.assertEqual(pts, 3)

    def test_boundary_15x_high_is_3(self):
        pts, _ = sf._comps_range_position(165.0, _anchors())  # == 1.5*110
        self.assertEqual(pts, 3)

    def test_far_above_high_is_1(self):
        pts, _ = sf._comps_range_position(200.0, _anchors())  # > 165
        self.assertEqual(pts, 1)


class TestAnchoredOwnHistory(unittest.TestCase):
    # v1.1 pe_fwd/pe_5yr_median band rescaled from 20 to 8; sanity band [0.2,5].
    def test_discount_is_8(self):
        pts, _, ok = sf._own_history_position(15.0, 20.0, "approx_current_eps")
        self.assertEqual(pts, 8)
        self.assertTrue(ok)

    def test_in_line_is_56(self):
        # 18/20=0.9 in (0.75,1.0] -> 20-tier 14 rescaled to 8: 14*8/20 = 5.6.
        pts, _, ok = sf._own_history_position(18.0, 20.0, "approx_current_eps")
        self.assertEqual(pts, 5.6)

    def test_modest_premium_is_32(self):
        # 22/20=1.1 in (1.0,1.25] -> 8*8/20 = 3.2.
        pts, _, ok = sf._own_history_position(22.0, 20.0, "approx_current_eps")
        self.assertEqual(pts, 3.2)

    def test_rich_premium_is_12(self):
        # 30/20=1.5 > 1.25 -> 3*8/20 = 1.2.
        pts, _, ok = sf._own_history_position(30.0, 20.0, "approx_current_eps")
        self.assertEqual(pts, 1.2)

    def test_method_label_in_arithmetic(self):
        _, s, _ = sf._own_history_position(15.0, 20.0, "approx_current_eps")
        self.assertIn("approx_current_eps", s)

    def test_sanity_band_out_is_na(self):
        # 9.828/1.82 = 5.4 > 5.0 -> component n/a, not evaluable.
        pts, s, ok = sf._own_history_position(9.828, 1.82, "approx_current_eps")
        self.assertEqual(pts, 0)
        self.assertFalse(ok)
        self.assertIn("outside sanity band [0.2,5]", s)

    def test_null_is_na(self):
        pts, s, ok = sf._own_history_position(None, 20.0, "approx_current_eps")
        self.assertEqual(pts, 0)
        self.assertFalse(ok)


class TestAnchoredFcfYield(unittest.TestCase):
    def test_high_is_7(self):
        pts, _, ok = sf._fcf_yield_anchored(0.06)
        self.assertEqual(pts, 7)

    def test_boundary_005_is_7(self):
        pts, _, ok = sf._fcf_yield_anchored(0.05)
        self.assertEqual(pts, 7)

    def test_good_is_5(self):
        pts, _, ok = sf._fcf_yield_anchored(0.04)
        self.assertEqual(pts, 5)

    def test_thin_is_3(self):
        pts, _, ok = sf._fcf_yield_anchored(0.02)
        self.assertEqual(pts, 3)

    def test_meager_is_2(self):
        pts, _, ok = sf._fcf_yield_anchored(0.01)
        self.assertEqual(pts, 2)

    def test_nonpositive_is_1(self):
        pts, _, ok = sf._fcf_yield_anchored(-0.01)
        self.assertEqual(pts, 1)

    def test_null_is_na(self):
        pts, s, ok = sf._fcf_yield_anchored(None)
        self.assertEqual(pts, 0)
        self.assertFalse(ok)


def _scale_pb(**over):
    """A valid justified_pb sector scale w/ metric_source; override per test.

    roe .35 r .12 g .04 -> low 2.7125, mid 3.875, high 5.0375.
    """
    base = {
        "scale": "memory_semis", "version": "2026.1", "effective": "2026-07-01",
        "basis": "x", "formula": "justified_pb",
        "parameters": {"roe_normalized": 0.35, "r": 0.12, "g": 0.04},
        "evidence": ["C1"], "falsifiers": [], "prior": None,
        "metric_source": "anchors:current_pb",
    }
    base.update(over)
    return base


class TestAnchoredJustifiedBand(unittest.TestCase):
    # band [2.7125, 3.875, 5.0375] via the pinned pb scale.
    def test_below_low_is_5(self):
        pts, _, ok = sf._justified_band_position(
            _scale_pb(), {}, {"current_pb": 2.0})
        self.assertEqual(pts, 5)
        self.assertTrue(ok)

    def test_low_to_mid_is_4(self):
        pts, _, ok = sf._justified_band_position(
            _scale_pb(), {}, {"current_pb": 3.0})
        self.assertEqual(pts, 4)

    def test_mid_to_high_is_2(self):
        pts, _, ok = sf._justified_band_position(
            _scale_pb(), {}, {"current_pb": 4.5})
        self.assertEqual(pts, 2)

    def test_above_high_is_1(self):
        pts, _, ok = sf._justified_band_position(
            _scale_pb(), {}, {"current_pb": 6.0})
        self.assertEqual(pts, 1)

    def test_anchors_current_pb_source_resolves(self):
        # the metric_source "anchors:current_pb" resolves from the anchors dict.
        _, s, ok = sf._justified_band_position(
            _scale_pb(), {}, {"current_pb": 2.0})
        self.assertTrue(ok)
        self.assertIn("anchors:current_pb", s)

    def test_dotted_snapshot_source_resolves(self):
        scale = _scale_pb(metric_source="valuation.pb_proxy")
        pts, _, ok = sf._justified_band_position(
            scale, {"valuation": {"pb_proxy": 3.0}}, {})
        self.assertEqual(pts, 4)
        self.assertTrue(ok)

    def test_unresolvable_metric_is_na(self):
        scale = _scale_pb(metric_source="valuation.pb_proxy")
        pts, s, ok = sf._justified_band_position(scale, {"valuation": {}}, {})
        self.assertEqual(pts, 0)
        self.assertFalse(ok)
        self.assertIn("unresolvable", s)

    def test_no_scale_is_na(self):
        pts, s, ok = sf._justified_band_position(None, {}, {})
        self.assertEqual(pts, 0)
        self.assertFalse(ok)
        self.assertIn("no --scale", s)


class TestScoreValuationAnchored(unittest.TestCase):
    def test_all_components_sum_to_50_max(self):
        sub = sf.score_valuation_anchored(
            _val(), _anchors(), 100.0, _scale_pb(), None)
        self.assertEqual(sub["max"], 50)
        self.assertEqual(sub["valuation_mode"], "anchored_v1.2")

    def test_component_points_recorded(self):
        # last=70 (r=0.7 -> DCF 17), comps below low (70<90 -> 13),
        # own-history 18/20=0.9 -> 5.6, fcf 0.04 -> 5, justified pb 2.0 -> 5.
        anchors = _anchors(current_pb=2.0)
        sub = sf.score_valuation_anchored(
            _val(pe_fwd=18.0, pe_5yr_median=20.0, fcf_yield=0.04),
            anchors, 70.0, _scale_pb(), None)
        inp = sub["inputs"]
        self.assertEqual(inp["dcf_band_points"], 17)
        self.assertEqual(inp["comps_range_points"], 13)
        self.assertEqual(inp["own_history_points"], 5.6)
        self.assertEqual(inp["fcf_yield_points"], 5)
        self.assertEqual(inp["justified_band_points"], 5)
        # 17 + 13 + 5.6 + 5 + 5 = 45.6
        self.assertEqual(sub["points"], 45.6)

    def test_peg_not_in_subscore_inputs(self):
        # PEG is removed from anchored scoring entirely.
        sub = sf.score_valuation_anchored(
            _val(peg=1.2), _anchors(), 100.0, None, None)
        self.assertNotIn("peg_points", sub["inputs"])
        self.assertNotIn("peg", sub["inputs"])

    def test_no_scale_justified_band_na(self):
        sub = sf.score_valuation_anchored(_val(), _anchors(), 100.0, None, None)
        self.assertEqual(sub["inputs"]["justified_band_points"], 0)


class TestScoreAnchoredMode(unittest.TestCase):
    def test_score_uses_anchored_when_anchors_given(self):
        result = sf.score(_fund(), _val(), anchors=_anchors(), last=100.0)
        val_sub = next(s for s in result["subscores"] if s["name"] == "valuation")
        self.assertEqual(val_sub["valuation_mode"], "anchored_v1.2")

    def test_score_uses_snapshot_without_anchors(self):
        result = sf.score(_fund(), _val())
        val_sub = next(s for s in result["subscores"] if s["name"] == "valuation")
        self.assertEqual(val_sub["valuation_mode"], "snapshot_v1.1")

    def test_score_snapshot_when_last_missing(self):
        # anchors given but no price -> cannot band DCF/comps -> snapshot floor.
        result = sf.score(_fund(), _val(), anchors=_anchors(), last=None)
        val_sub = next(s for s in result["subscores"] if s["name"] == "valuation")
        self.assertEqual(val_sub["valuation_mode"], "snapshot_v1.1")


class TestBuildModuleAnchored(unittest.TestCase):
    def _snap(self, **over):
        snap = {
            "fundamentals": _fund(), "valuation": _val(),
            "price": {"last": 70.0},
            "meta": {"ticker": "MU", "as_of_utc": "2026-07-15T00:00:00Z"},
        }
        snap.update(over)
        return snap

    def test_peg_display_present_in_anchored(self):
        doc = sf.build_module(self._snap(), anchors=_anchors())
        self.assertIn("peg_display", doc)
        self.assertEqual(doc["peg_display"]["value"], _val()["peg"])
        self.assertIn("display-only", doc["peg_display"]["note"])
        self.assertIn("excluded from scoring", doc["peg_display"]["note"])

    def test_peg_absent_from_anchored_subscores(self):
        doc = sf.build_module(self._snap(), anchors=_anchors())
        val_sub = next(s for s in doc["subscores"] if s["name"] == "valuation")
        self.assertNotIn("peg_points", val_sub["inputs"])

    def test_no_peg_display_in_snapshot_mode(self):
        doc = sf.build_module(self._snap())  # no anchors
        self.assertNotIn("peg_display", doc)
        # snapshot mode still SCORES peg.
        val_sub = next(s for s in doc["subscores"] if s["name"] == "valuation")
        self.assertIn("peg_points", val_sub["inputs"])

    def test_sector_scale_recorded(self):
        doc = sf.build_module(self._snap(), anchors=_anchors(current_pb=2.0),
                              scale=_scale_pb())
        self.assertEqual(doc["sector_scale"], "memory_semis@2026.1")

    def test_sector_scale_null_without_scale(self):
        doc = sf.build_module(self._snap(), anchors=_anchors())
        self.assertIsNone(doc["sector_scale"])

    def test_valuation_mode_on_subscore(self):
        doc = sf.build_module(self._snap(), anchors=_anchors())
        val_sub = next(s for s in doc["subscores"] if s["name"] == "valuation")
        self.assertEqual(val_sub["valuation_mode"], "anchored_v1.2")


# --------------------------------------------------------------------------- #
# INPUT_FIELDS / GUARD_FIELDS declaration
# --------------------------------------------------------------------------- #

class TestInputFields(unittest.TestCase):
    def test_input_fields_exact(self):
        self.assertEqual(sf.INPUT_FIELDS, {
            "fundamentals.rev_growth_latest_q", "fundamentals.gm_ttm",
            "fundamentals.om_ttm", "fundamentals.roe", "fundamentals.fcf_ttm",
            "fundamentals.rev_ttm", "valuation.pe_fwd",
            "valuation.pe_5yr_median", "valuation.peg", "valuation.fcf_yield",
        })

    def test_does_not_score_net_cash(self):
        # solvency (net_cash_defined.net) is OWNED by risk-analytics.
        self.assertNotIn("fundamentals.net_cash_defined.net", sf.INPUT_FIELDS)

    def test_does_not_score_revisions(self):
        # revisions_90d is OWNED by sentiment.
        self.assertNotIn("fundamentals.revisions_90d", sf.INPUT_FIELDS)


# --------------------------------------------------------------------------- #
# Mode disclosure
# --------------------------------------------------------------------------- #

class TestModeDisclosure(unittest.TestCase):
    def test_build_module_has_mode_fields(self):
        snap = {"fundamentals": _fund(), "valuation": _val(),
                "meta": {"ticker": "MU", "as_of_utc": "2026-07-15T00:00:00Z"}}
        doc = sf.build_module(snap)
        self.assertEqual(doc["fundamental_mode"], "compressed_snapshot_pass")
        self.assertIn("snapshot-only", doc["mode_note"])
        self.assertIn("deep FSI", doc["mode_note"])
        self.assertEqual(doc["skill"], "fundamental")
        self.assertEqual(doc["rubric_version"], "1.2.0")

    def test_anchored_run_discloses_anchored_mode(self):
        # ORCL live finding: the static "deep FSI ... not applied" note shipped
        # on a run where initiation HAD run and anchors scored the valuation.
        snap = {"fundamentals": _fund(), "valuation": _val(),
                "price": {"last": 120.0},
                "meta": {"ticker": "ORCL", "as_of_utc": "2026-07-19T00:00:00Z"}}
        doc = sf.build_module(snap, anchors=_anchors())
        self.assertEqual(doc["fundamental_mode"], "coverage_anchored_pass")
        self.assertIn("coverage-anchored", doc["mode_note"])
        self.assertNotIn("not applied", doc["mode_note"])


# --------------------------------------------------------------------------- #
# Moat flag recorded in module flags (mirrors score_sentiment conventions)
# --------------------------------------------------------------------------- #

class TestModuleMoatFlags(unittest.TestCase):
    def test_moat_omitted_flags_none(self):
        snap = {"fundamentals": _fund(), "valuation": _val(),
                "meta": {"ticker": "MU", "as_of_utc": "2026-07-15T00:00:00Z"}}
        doc = sf.build_module(snap)
        self.assertIn("moat", doc["flags"])
        self.assertIsNone(doc["flags"]["moat"])
        self.assertIsNone(doc["flags"]["moat_justification"])

    def test_moat_present_flags_recorded(self):
        snap = {"fundamentals": _fund(), "valuation": _val(),
                "meta": {"ticker": "MU", "as_of_utc": "2026-07-15T00:00:00Z"}}
        doc = sf.build_module(snap, moat="wide",
                              moat_justification="brand + scale (C3)")
        self.assertEqual(doc["flags"]["moat"], "wide")
        self.assertEqual(doc["flags"]["moat_justification"], "brand + scale (C3)")
        # And it flows into the quality subscore.
        qual = next(s for s in doc["subscores"] if s["name"] == "quality")
        self.assertEqual(qual["inputs"]["moat_points"], 10)


# --------------------------------------------------------------------------- #
# CLI end-to-end (real bundle, reuses test_build_snapshot fixtures)
# --------------------------------------------------------------------------- #

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "score_fundamental.py")


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

    def _run(self, extra=None):
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir]
        if extra:
            cmd += extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_cli_exit0_writes_module_json(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = os.path.join(self.dir, "module_fundamental.json")
        self.assertTrue(os.path.exists(out))
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["skill"], "fundamental")
        self.assertEqual(doc["rubric_version"], "1.2.0")
        self.assertEqual(doc["fundamental_mode"], "compressed_snapshot_pass")
        self.assertIn("snapshot-only", doc["mode_note"])
        self.assertEqual(doc["ticker"], "MU")
        self.assertIn("as_of", doc)
        self.assertIsInstance(doc["score"], (int, float))
        self.assertGreaterEqual(doc["score"], 0)
        self.assertLessEqual(doc["score"], 100)
        self.assertIsInstance(doc["subscores"], list)
        self.assertEqual(len(doc["subscores"]), 2)
        self.assertIn("quality", doc["tables"])
        self.assertIn("valuation", doc["tables"])
        # v1.1.0: flags now always carry the moat keys (omitted -> null), mirroring
        # score_sentiment which always records its judgment flags.
        self.assertEqual(doc["flags"],
                         {"moat": None, "moat_justification": None})
        self.assertIsNone(doc["signal"])
        for s in doc["subscores"]:
            self.assertIn("arithmetic", s)
            self.assertIn("inputs", s)
            self.assertIn("name", s)

    def test_cli_omitted_moat_arithmetic_and_evaluable(self):
        # No --moat flag: quality still scores (mechanical inputs present) and the
        # moat component reads "n/a (no context assessment)".
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_fundamental.json")
        with open(out) as fh:
            doc = json.load(fh)
        qual = next(s for s in doc["subscores"] if s["name"] == "quality")
        self.assertIn("moat: n/a (no context assessment)", qual["arithmetic"])
        self.assertEqual(qual["inputs"]["moat_points"], 0)

    def test_cli_moat_wide_scores_and_records_flag(self):
        proc = self._run(extra=["--moat", "wide",
                                "--moat-justification",
                                "durable pricing power per C3"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_fundamental.json")
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["flags"]["moat"], "wide")
        self.assertEqual(doc["flags"]["moat_justification"],
                         "durable pricing power per C3")
        qual = next(s for s in doc["subscores"] if s["name"] == "quality")
        self.assertEqual(qual["inputs"]["moat_points"], 10)

    def test_cli_moat_without_justification_exit2(self):
        proc = self._run(extra=["--moat", "narrow"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("justification", proc.stderr.lower())

    def test_cli_moat_justification_without_citation_exit2(self):
        # A justification that cites no context finding ID (no C\d+) is rejected.
        proc = self._run(extra=["--moat", "wide",
                                "--moat-justification",
                                "strong brand and scale advantages"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cite context finding IDs", proc.stderr)
        self.assertIn("C3", proc.stderr)

    def test_cli_moat_justification_with_citation_ok(self):
        # A single C\d+ token anywhere in the justification satisfies the citation
        # requirement.
        proc = self._run(extra=["--moat", "none",
                                "--moat-justification",
                                "commoditized DRAM, see C7 and C9"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_fundamental.json")
        with open(out) as fh:
            doc = json.load(fh)
        qual = next(s for s in doc["subscores"] if s["name"] == "quality")
        self.assertEqual(qual["inputs"]["moat_points"], 2)

    def _write_context(self, ids=("C1", "C2", "C3")):
        # Minimal context module carrying a findings[] registry for the
        # referential-integrity check (only findings[].id is read here).
        ctx = {"findings": [{"id": i, "claim": "c", "source": "s"} for i in ids]}
        path = os.path.join(self.dir, "module_context.json")
        with open(path, "w") as fh:
            json.dump(ctx, fh)
        return path

    def test_cli_moat_cited_id_resolves_passes(self):
        # A cited C-ID that exists in the context findings[] passes.
        self._write_context(ids=("C1", "C2", "C3"))
        proc = self._run(extra=["--moat", "wide",
                                "--moat-justification", "durable moat (C3)"])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_cli_moat_cited_id_unresolved_exit2(self):
        # C99 is not in the C1..C3 registry -> exit 2, message names it.
        self._write_context(ids=("C1", "C2", "C3"))
        proc = self._run(extra=["--moat", "wide",
                                "--moat-justification", "moat per C99"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("C99 does not exist", proc.stderr)
        self.assertIn("module_context.json", proc.stderr)
        self.assertIn("C1..C3", proc.stderr)

    def test_cli_moat_no_context_module_presence_only_unchanged(self):
        # No module_context.json in the bundle: presence-only behavior, a cited but
        # unverifiable C99 is accepted (the compressed / FSI-absent floor).
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "module_context.json")))
        proc = self._run(extra=["--moat", "narrow",
                                "--moat-justification", "some moat per C99"])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_cli_method_label_in_valuation_arithmetic(self):
        # the fabricated bundle carries pe_median_method="approx_current_eps"
        # and a computable pe_fwd/pe_5yr_median, so the label must be disclosed.
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_fundamental.json")
        with open(out) as fh:
            doc = json.load(fh)
        val_sub = next(s for s in doc["subscores"] if s["name"] == "valuation")
        self.assertIn("approx_current_eps", val_sub["arithmetic"])

    def test_custom_out_path(self):
        out = os.path.join(self.dir, "custom_fundamental.json")
        proc = self._run(extra=["--out", out])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

    # -- anchored mode CLI --------------------------------------------------

    def _write_anchors(self, obj):
        path = os.path.join(self.dir, "valuation_anchors.json")
        with open(path, "w") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh)
        return path

    def test_cli_anchors_switches_to_anchored_mode(self):
        # A valid anchors file switches the valuation dimension to anchored mode,
        # emits peg_display, and drops peg from the subscore.
        anchors = {"dcf_base": 100.0, "dcf_bear": 70.0, "dcf_bull": 130.0,
                   "comps_low": 90.0, "comps_high": 110.0,
                   "assumptions": {"wacc": 0.10, "terminal_g": 0.03},
                   "citations": {"dcf": "C1"}, "as_of": "2026-07-15"}
        path = self._write_anchors(anchors)
        proc = self._run(extra=["--anchors", path])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_fundamental.json")
        with open(out) as fh:
            doc = json.load(fh)
        val_sub = next(s for s in doc["subscores"] if s["name"] == "valuation")
        self.assertEqual(val_sub["valuation_mode"], "anchored_v1.2")
        self.assertIn("peg_display", doc)
        self.assertNotIn("peg_points", val_sub["inputs"])
        self.assertIn("dcf_band_points", val_sub["inputs"])

    def test_cli_anchors_with_scale_records_sector_scale(self):
        anchors = {"dcf_base": 100.0, "dcf_bear": 70.0, "dcf_bull": 130.0,
                   "comps_low": 90.0, "comps_high": 110.0, "current_pb": 2.0}
        apath = self._write_anchors(anchors)
        scale = {"scale": "memory_semis", "version": "2026.1",
                 "effective": "2026-07-01", "basis": "x",
                 "formula": "justified_pb",
                 "parameters": {"roe_normalized": 0.35, "r": 0.12, "g": 0.04},
                 "evidence": ["C1"], "falsifiers": [], "prior": None,
                 "metric_source": "anchors:current_pb"}
        spath = os.path.join(self.dir, "memory_semis.json")
        with open(spath, "w") as fh:
            json.dump(scale, fh)
        proc = self._run(extra=["--anchors", apath, "--scale", spath])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_fundamental.json")
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["sector_scale"], "memory_semis@2026.1")
        val_sub = next(s for s in doc["subscores"] if s["name"] == "valuation")
        # current_pb 2.0 < band low 2.7125 -> justified band 5.
        self.assertEqual(val_sub["inputs"]["justified_band_points"], 5)

    def test_cli_malformed_anchors_exit2(self):
        # missing dcf_base -> validate_anchors fails -> exit 2 naming the issue.
        path = self._write_anchors({"dcf_bear": 70.0, "dcf_bull": 130.0,
                                    "comps_low": 90.0, "comps_high": 110.0})
        proc = self._run(extra=["--anchors", path])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("dcf_base", proc.stderr)

    def test_cli_anchors_bad_json_exit2(self):
        path = self._write_anchors("{not json")
        proc = self._run(extra=["--anchors", path])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not valid JSON", proc.stderr)

    def test_cli_bad_scale_exit2(self):
        anchors = {"dcf_base": 100.0, "dcf_bear": 70.0, "dcf_bull": 130.0,
                   "comps_low": 90.0, "comps_high": 110.0}
        apath = self._write_anchors(anchors)
        # scale missing formula -> load_scale raises -> exit 2.
        bad_scale = {"scale": "x", "version": "1", "effective": "2026-01-01",
                     "basis": "x", "parameters": {}, "evidence": [],
                     "falsifiers": [], "prior": None}
        spath = os.path.join(self.dir, "bad_scale.json")
        with open(spath, "w") as fh:
            json.dump(bad_scale, fh)
        proc = self._run(extra=["--anchors", apath, "--scale", spath])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("formula", proc.stderr)

    def test_missing_bundle_errors(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--bundle",
             os.path.join(self.dir, "nonexistent")],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_determinism(self):
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
