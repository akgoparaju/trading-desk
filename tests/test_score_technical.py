"""Tests for scripts/score_technical.py -- the technical-analysis evidence skill.

WHY: this is the FIRST scored evidence module, so its arithmetic IS the rubric of
record (rubric v1.0.0). Every scoring branch is pinned to a hand-computed value
here; if the code and these numbers ever diverge, the rubric has silently changed
and that must surface as a test failure, not a shifted report. Tests exercise the
pure scoring functions directly (exact values per branch) plus one end-to-end CLI
run against a real snapshot bundle fabricated the same way test_levels.py does.

The scoring functions take an explicit ``ladder`` and ``last`` so structure/volume
branches can be pinned without reconstructing a full price series for every case.

stdlib-only; unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import score_technical as st


# --------------------------------------------------------------------------- #
# Helpers: minimal technicals blocks and hand-built ladders.
# --------------------------------------------------------------------------- #

def _tech(**over):
    """A fully-populated technicals block; override individual fields per test.

    Defaults describe a TRENDING regime (adx well above the choppy threshold,
    Weinstein stage-2 advancing), so the v1.1.0 momentum guard is INACTIVE by
    default -- baseline momentum/trend tests keep their v1.0.0 values unless a test
    explicitly sets adx14 < 20 or stage == 4. A/D slope + upvol default to the
    accumulation side so the volume factor is fully populated.
    """
    base = {
        "ma50": 100.0,
        "ma200": 90.0,
        "ma50_slope_20d": 0.5,
        "ma200_slope_20d": 0.3,
        "rsi14": 55.0,
        "macd": 1.0,
        "macd_signal": 0.5,
        "vol_20d_vs_90d": 1.2,
        "ret_15d": 0.02,
        # Wave 4A v1.1.0 fields.
        "adx14": 30.0,        # trending (>= 20 choppy threshold -> no MACD discount)
        "stage": 2,           # Weinstein advancing (not 4 -> no RSI cap)
        "ad_line_slope": 1.5,  # accumulation
        "upvol_ratio": 0.60,   # up-day volume dominant
    }
    base.update(over)
    return base


def _ladder(entries):
    """Wrap (level, type) pairs into ladder dicts with pct_from_last filled by
    the caller's ``last`` at scoring time (score_structure recomputes pct)."""
    out = []
    for e in entries:
        out.append({"level": float(e[0]), "type": e[1], "basis": "test"})
    return out


# --------------------------------------------------------------------------- #
# 1. Trend structure (max 30)
# --------------------------------------------------------------------------- #

class TestTrend(unittest.TestCase):
    def test_full_marks(self):
        # price>ma50 (+8), ma50>ma200 (+8), price>ma200 (+4),
        # ma50_slope>0 (+5), ma200_slope>0 (+5) = 30
        tech = _tech(ma50=100.0, ma200=90.0, ma50_slope_20d=0.5, ma200_slope_20d=0.3)
        sub = st.score_trend(last=110.0, tech=tech)
        self.assertEqual(sub["points"], 30)
        self.assertEqual(sub["max"], 30)

    def test_zero_marks_downtrend(self):
        # price<ma50, ma50<ma200, price<ma200, both slopes negative -> 0
        tech = _tech(ma50=100.0, ma200=110.0, ma50_slope_20d=-0.5,
                     ma200_slope_20d=-0.3)
        sub = st.score_trend(last=90.0, tech=tech)
        self.assertEqual(sub["points"], 0)

    def test_null_ma50_component_zero_and_named_na(self):
        # ma50 null: price>ma50 and ma50>ma200 cannot be evaluated -> both 0,
        # named "n/a" in arithmetic. price>ma200 (+4) still applies.
        tech = _tech(ma50=None, ma200=90.0, ma50_slope_20d=0.5,
                     ma200_slope_20d=0.3)
        sub = st.score_trend(last=110.0, tech=tech)
        # +4 (price>ma200) +5 (ma50_slope) +5 (ma200_slope) = 14
        self.assertEqual(sub["points"], 14)
        self.assertIn("n/a", sub["arithmetic"])


# --------------------------------------------------------------------------- #
# 2. Momentum (max 25): RSI (15) + MACD (10)
# --------------------------------------------------------------------------- #

class TestRSI(unittest.TestCase):
    def _rsi_points(self, rsi, divergence="none"):
        sub = st.score_momentum(_tech(rsi14=rsi, macd=1.0, macd_signal=0.5),
                                divergence=divergence, justification="j")
        return sub

    def test_rsi_55_full_15(self):
        rsi_pts = st._rsi_component(55.0, "none")
        self.assertEqual(rsi_pts, 15.0)

    def test_rsi_68_band_12(self):
        self.assertEqual(st._rsi_component(68.0, "none"), 12.0)

    def test_rsi_42_lower_band_12(self):
        # 40 <= 42 < 45 -> 12
        self.assertEqual(st._rsi_component(42.0, "none"), 12.0)

    def test_rsi_78_none_is_6(self):
        # rsi>70: max(0, 12 - (78-70)*0.75) = max(0, 12-6) = 6.0
        self.assertEqual(st._rsi_component(78.0, "none"), 6.0)

    def test_rsi_78_bearish_divergence_is_3(self):
        # 6.0 then additional -3 (bearish & rsi>65) -> 3.0
        self.assertEqual(st._rsi_component(78.0, "bearish"), 3.0)

    def test_rsi_25_is_0point75(self):
        # rsi<40: max(0, 12 - (40-25)*0.75) = max(0, 12-11.25) = 0.75
        self.assertEqual(st._rsi_component(25.0, "none"), 0.75)

    def test_rsi_38_bullish_divergence_plus3(self):
        # rsi<40 base = max(0, 12-(40-38)*0.75)=max(0,10.5)=10.5;
        # bullish & rsi<45 -> +3 -> 13.5 (cap 15)
        self.assertEqual(st._rsi_component(38.0, "bullish"), 13.5)

    def test_bullish_divergence_capped_at_15(self):
        # rsi 44 -> 12 (lower band); bullish & rsi<45 -> +3 -> cap 15
        self.assertEqual(st._rsi_component(44.0, "bullish"), 15.0)

    def test_rsi_floor_zero(self):
        # very high rsi drives base below 0 -> floored at 0
        self.assertEqual(st._rsi_component(90.0, "none"), 0.0)

    def test_bearish_divergence_floor_zero(self):
        # base already 0 -> -3 stays floored at 0
        self.assertEqual(st._rsi_component(90.0, "bearish"), 0.0)

    def test_bearish_divergence_requires_rsi_over_65(self):
        # rsi 55 with bearish flag: rsi not >65 -> NO penalty, stays 15
        self.assertEqual(st._rsi_component(55.0, "bearish"), 15.0)

    def test_bullish_divergence_requires_rsi_under_45(self):
        # rsi 55 with bullish flag: rsi not <45 -> no bonus, stays 15
        self.assertEqual(st._rsi_component(55.0, "bullish"), 15.0)


class TestMACD(unittest.TestCase):
    def test_macd_gt_signal_gt0_is_10(self):
        self.assertEqual(st._macd_component(1.0, 0.5), 10.0)

    def test_macd_gt_signal_le0_is_7(self):
        # macd>signal but macd<=0
        self.assertEqual(st._macd_component(-0.5, -1.0), 7.0)

    def test_macd_le_signal_gt0_is_4(self):
        # macd<=signal but macd>0
        self.assertEqual(st._macd_component(0.5, 1.0), 4.0)

    def test_macd_le_signal_le0_is_0(self):
        self.assertEqual(st._macd_component(-1.0, -0.5), 0.0)


class TestMomentumFlags(unittest.TestCase):
    def test_divergence_flag_recorded(self):
        sub = st.score_momentum(_tech(rsi14=78.0), divergence="bearish",
                                 justification="lower highs into resistance")
        self.assertEqual(sub["inputs"]["divergence"], "bearish")

    def test_rsi_null_contributes_zero(self):
        sub = st.score_momentum(_tech(rsi14=None, macd=1.0, macd_signal=0.5),
                                divergence="none", justification="j")
        # rsi n/a (0) + macd 10 = 10
        self.assertEqual(sub["points"], 10.0)
        self.assertIn("n/a", sub["arithmetic"])


# --------------------------------------------------------------------------- #
# 2b. Momentum regime GUARD (v1.1.0): adx14 (choppy) + stage (declining).
#     Band SHAPES unchanged; the guard modulates the earned points. Null
#     adx/stage -> NO modulation (must equal the no-guard baseline byte-for-byte).
# --------------------------------------------------------------------------- #

class TestMomentumRegimeGuard(unittest.TestCase):
    def _mom(self, **over):
        # rsi 55 -> 15, macd 1>0.5>0 -> 10; baseline momentum 25 before any guard.
        tech = _tech(rsi14=55.0, macd=1.0, macd_signal=0.5, **over)
        return st.score_momentum(tech, divergence="none", justification="j")

    def test_null_guard_no_modulation_matches_baseline(self):
        # adx14 + stage BOTH null -> the guard is inactive; momentum == v1.0.0.
        sub = self._mom(adx14=None, stage=None)
        self.assertEqual(sub["points"], 25)  # 15 + 10, unmodulated
        self.assertNotIn("choppy", sub["arithmetic"])
        self.assertNotIn("stage-4 cap", sub["arithmetic"])
        self.assertFalse(sub["inputs"]["regime_choppy"])
        self.assertFalse(sub["inputs"]["regime_declining"])

    def test_choppy_adx_halves_macd_sub(self):
        # adx14 15 < 20 -> MACD sub halved: 10 -> 5; rsi 15 unchanged -> 20.
        sub = self._mom(adx14=15.0, stage=2)
        self.assertEqual(sub["points"], 20)
        self.assertTrue(sub["inputs"]["regime_choppy"])
        self.assertIn("choppy", sub["arithmetic"])

    def test_adx_at_threshold_not_choppy(self):
        # adx14 == 20 is NOT < 20 -> no discount (boundary).
        sub = self._mom(adx14=20.0, stage=2)
        self.assertEqual(sub["points"], 25)
        self.assertFalse(sub["inputs"]["regime_choppy"])

    def test_stage4_caps_rsi_healthy_bonus(self):
        # stage == 4 -> RSI healthy-band 15 capped at 12; macd 10 unchanged -> 22.
        sub = self._mom(adx14=30.0, stage=4)
        self.assertEqual(sub["points"], 22)
        self.assertTrue(sub["inputs"]["regime_declining"])
        self.assertIn("stage-4 cap", sub["arithmetic"])

    def test_stage4_cap_does_not_bite_below_cap(self):
        # rsi 25 -> 0.75 (already below the cap 12) -> cap does not apply; macd 10.
        tech = _tech(rsi14=25.0, macd=1.0, macd_signal=0.5, adx14=30.0, stage=4)
        sub = st.score_momentum(tech, divergence="none", justification="j")
        self.assertEqual(sub["points"], 10.75)
        self.assertNotIn("stage-4 cap", sub["arithmetic"])

    def test_both_guards_compose(self):
        # choppy (macd 10 -> 5) AND stage-4 (rsi 15 -> 12) -> 17.
        sub = self._mom(adx14=15.0, stage=4)
        self.assertEqual(sub["points"], 17)

    def test_non_declining_stage_no_rsi_cap(self):
        # stage 1/2/3 -> no RSI cap.
        for stg in (1, 2, 3):
            sub = self._mom(adx14=30.0, stage=stg)
            self.assertEqual(sub["points"], 25, f"stage {stg}")


# --------------------------------------------------------------------------- #
# 3. Structure & levels (max 25)
# --------------------------------------------------------------------------- #

class TestStructure(unittest.TestCase):
    def test_support_within_5pct_is_12(self):
        # proven support at 97 -> -3% from last 100 -> 12
        ladder = _ladder([(97.0, "ma50"), (110.0, "swing_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["support_points"], 12)

    def test_support_5_to_10pct_is_8(self):
        # proven support at 92 -> -8% -> 8
        ladder = _ladder([(92.0, "ma50"), (110.0, "swing_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["support_points"], 8)

    def test_no_proven_support_is_0(self):
        # only a round_number below (not proven) -> support 0
        ladder = _ladder([(97.0, "round_number"), (110.0, "swing_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["support_points"], 0)

    def test_resistance_5pct_plus_is_8(self):
        # resistance at 110 -> +10% -> 8
        ladder = _ladder([(92.0, "ma50"), (110.0, "swing_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["resistance_points"], 8)

    def test_resistance_2_to_5pct_is_4(self):
        ladder = _ladder([(92.0, "ma50"), (103.0, "swing_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["resistance_points"], 4)

    def test_resistance_under_2pct_is_0(self):
        ladder = _ladder([(92.0, "ma50"), (101.0, "swing_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["resistance_points"], 0)

    def test_blue_sky_resistance_is_8(self):
        # no ladder entry above last -> ATH blue sky -> 8
        ladder = _ladder([(92.0, "ma50"), (95.0, "swing_low")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["resistance_points"], 8)

    def test_confluence_bonus_plus5(self):
        # two ladder entries below last within 2% of each other -> +5
        # 90 and 91.5 are ~1.6% apart, both below 100.
        ladder = _ladder([(90.0, "swing_low"), (91.5, "ma200"),
                          (110.0, "swing_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["confluence_points"], 5)

    def test_no_confluence_when_spread(self):
        # 80 and 92 below last -> >2% apart -> no bonus
        ladder = _ladder([(80.0, "swing_low"), (92.0, "ma200"),
                          (110.0, "swing_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["confluence_points"], 0)

    def test_structure_total_capped_at_25(self):
        # 12 (support) + 8 (resistance) + 5 (confluence) = 25
        ladder = _ladder([(97.0, "ma50"), (96.0, "put_wall"),
                          (110.0, "swing_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertLessEqual(sub["points"], 25)
        self.assertEqual(sub["points"], 25)

    # -- v1.1.0: anchored-VWAP levels register in the existing bands ---------
    def test_vwap_earnings_registers_as_proven_support(self):
        # anchored VWAP (institutional cost basis) below last within 5% -> 12,
        # exactly like an ma/swing proven support. Confirms the structure scorer
        # accepts the vwap type (the ladder mints it in levels.py).
        ladder = _ladder([(97.0, "vwap_earnings"), (110.0, "swing_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["support_points"], 12)

    def test_vwap_52wk_high_registers_as_resistance(self):
        # a VWAP above price registers generically as nearest resistance
        # (nearest_resistance accepts any type). +10% headroom -> 8.
        ladder = _ladder([(92.0, "ma50"), (110.0, "vwap_52wk_high")])
        sub = st.score_structure(ladder, last=100.0)
        self.assertEqual(sub["inputs"]["resistance_points"], 8)

    def test_vwap_types_in_structure_proven_set(self):
        # the structure-proven set = base proven types + anchored-VWAP types.
        self.assertIn("vwap_52wk_high", st._STRUCTURE_PROVEN_SUPPORT)
        self.assertIn("vwap_earnings", st._STRUCTURE_PROVEN_SUPPORT)
        self.assertIn("ma50", st._STRUCTURE_PROVEN_SUPPORT)


# --------------------------------------------------------------------------- #
# 4. Volume & extension (max 20)
# --------------------------------------------------------------------------- #

class TestVolumeExtension(unittest.TestCase):
    """v1.1.0 re-split: extension 10 / vol-regime 5 / A/D 3 / upvol 2 = 20.

    _tech() supplies ad_line_slope + upvol_ratio (added to the fixture) so a
    fully-populated volume factor is present unless a test nulls a sub-component.
    Extension + vol-regime keep their v1.0.0 BAND SHAPES, scaled to the new maxes.
    """

    def test_extension_120_is_scaled(self):
        # last/ma200 = 1.20 -> ext 0.20 -> penalty (0.20-0.12)*100=8 ->
        # 10 - 8*(10/12) = 10 - 6.6667 = 3.3333 (v1.0.0 shape, scaled to /10).
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02)
        sub = st.score_volume(last=120.0, tech=tech)
        self.assertEqual(sub["inputs"]["extension_points"], 3.3333)

    def test_extension_not_extended_full_10(self):
        # last/ma200 = 1.05 -> ext 0.05 < 0.12 -> penalty 0 -> full 10.
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["extension_points"], 10)

    def test_extension_floor_zero(self):
        # last/ma200 = 1.30 -> ext 0.30 -> penalty 18 -> max(0, 10 - 18*10/12) = 0.
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02)
        sub = st.score_volume(last=130.0, tech=tech)
        self.assertEqual(sub["inputs"]["extension_points"], 0)

    def test_volume_ratio_12_is_5(self):
        # 0.8 <= 1.2 <= 1.5 -> full vol-regime 5 (v1.0.0 in-band, scaled).
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["volume_points"], 5)

    def test_volume_ratio_high_is_scaled(self):
        # >1.5 -> 5*(5/8) = 3.125 (v1.0.0 high band scaled).
        tech = _tech(ma200=100.0, vol_20d_vs_90d=2.0, ret_15d=0.02)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["volume_points"], 3.125)

    def test_volume_ratio_low_is_scaled(self):
        # <0.8 -> 4*(5/8) = 2.5 (v1.0.0 low band scaled).
        tech = _tech(ma200=100.0, vol_20d_vs_90d=0.5, ret_15d=0.02)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["volume_points"], 2.5)

    def test_volume_null_is_na_renormalizes(self):
        # vol null -> vol-regime EXCLUDED; the factor is renormalized over the
        # present sub-maxes (NOT zeroed).
        tech = _tech(ma200=100.0, vol_20d_vs_90d=None, ret_15d=0.02)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertIsNone(sub["inputs"]["volume_points"])
        self.assertIn("n/a", sub["arithmetic"])
        self.assertTrue(sub["inputs"]["renormalized"])

    # -- A/D-line slope band (max 3) --------------------------------------
    def test_ad_line_positive_is_3(self):
        tech = _tech(ma200=100.0, ad_line_slope=2.5)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["ad_line_points"], 3)

    def test_ad_line_flat_is_2(self):
        tech = _tech(ma200=100.0, ad_line_slope=0.0)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["ad_line_points"], 2)

    def test_ad_line_negative_is_0(self):
        tech = _tech(ma200=100.0, ad_line_slope=-3.0)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["ad_line_points"], 0)

    # -- up/down volume band (max 2) --------------------------------------
    def test_upvol_over_055_is_2(self):
        tech = _tech(ma200=100.0, upvol_ratio=0.60)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["upvol_points"], 2)

    def test_upvol_mid_band_is_1(self):
        # [0.45, 0.55] inclusive -> 1. 0.55 itself is in the mid band (> 0.55
        # is strict for the top band).
        for uv in (0.45, 0.50, 0.55):
            tech = _tech(ma200=100.0, upvol_ratio=uv)
            sub = st.score_volume(last=105.0, tech=tech)
            self.assertEqual(sub["inputs"]["upvol_points"], 1, uv)

    def test_upvol_under_045_is_0(self):
        tech = _tech(ma200=100.0, upvol_ratio=0.40)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["upvol_points"], 0)

    # -- factor sums to 20 with all four sub-components present ------------
    def test_factor_full_marks_sums_to_20(self):
        # ext 10 (1.05) + vol 5 (1.2) + A/D 3 (>0) + upvol 2 (>0.55) = 20.
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02,
                     ad_line_slope=1.0, upvol_ratio=0.60)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["points"], 20)
        self.assertEqual(sub["max"], 20)
        self.assertFalse(sub["inputs"]["renormalized"])

    # -- renormalization when A/D + upvol are both null -------------------
    def test_ad_upvol_null_renormalizes_to_20(self):
        # A/D + upvol null -> present = ext(10) + vol(5) = 15 max; earned 15 ->
        # renormalized back to the 20 factor max (NOT zeroed).
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02,
                     ad_line_slope=None, upvol_ratio=None)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertTrue(sub["inputs"]["renormalized"])
        self.assertEqual(sub["points"], 20)

    def test_ad_upvol_null_partial_renormalizes(self):
        # ext 3.3333 (@1.20) + vol 5 = 8.3333 over present max 15 ->
        # *20/15 = 11.1111.
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02,
                     ad_line_slope=None, upvol_ratio=None)
        sub = st.score_volume(last=120.0, tech=tech)
        self.assertTrue(sub["inputs"]["renormalized"])
        self.assertEqual(sub["points"], 11.1111)

    def test_vertical_rally_penalty_minus4(self):
        # ret_15d 0.15 > 0.12 -> -4 off the factor total.
        # full 20 -> 16.
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.15,
                     ad_line_slope=1.0, upvol_ratio=0.60)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["points"], 16)
        self.assertEqual(sub["inputs"]["vertical_rally_penalty"], -4)

    def test_dimension_floor_zero(self):
        # push all present components to their floor then apply penalty -> 0.
        # ext 1.30 -> 0; vol null; A/D negative -> 0; upvol <0.45 -> 0;
        # ret_15d 0.15 -> -4 -> floor 0.
        tech = _tech(ma200=100.0, vol_20d_vs_90d=None, ret_15d=0.15,
                     ad_line_slope=-1.0, upvol_ratio=0.30)
        sub = st.score_volume(last=130.0, tech=tech)
        self.assertEqual(sub["points"], 0)


# --------------------------------------------------------------------------- #
# QC20(c) — extension-credit gating: score_volume's consumption of
# technicals.extension_gate (built by build_snapshot.build_extension_gate).
# --------------------------------------------------------------------------- #

class TestExtensionGateCap(unittest.TestCase):
    """extension_pts := min(today, the pre-break reading) when the gate is
    armed AND the cap actually binds (the pre-break reading is lower). No
    change when the gate is absent, not armed, or armed-but-not-binding
    (mirrors MU: armed would never bind because extension was already 0)."""

    _NOT_ARMED = {"armed": False, "qualifying_breaks": [],
                  "earliest_break_date": None, "reference_date": None,
                  "reference_close": None, "reference_ma200": None}

    def _armed_gate(self, ref_close, ref_ma200, break_date="2026-07-31", z=-4.04):
        return {
            "armed": True,
            "qualifying_breaks": [{"date": break_date, "return_1d": -0.0735,
                                   "sigma_1d": 0.0182, "z": z,
                                   "reported_date": "2026-07-30"}],
            "earliest_break_date": break_date,
            "reference_date": "2026-07-30",
            "reference_close": ref_close,
            "reference_ma200": ref_ma200,
        }

    def test_gate_absent_no_change(self):
        # No extension_gate key at all (a pre-QC20c snapshot) -> unaffected.
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02)
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["extension_points"], 10)
        self.assertFalse(sub["inputs"]["extension_gate_armed"])
        self.assertFalse(sub["inputs"]["extension_gate_capped"])
        self.assertNotIn("capped", sub["arithmetic"])

    def test_gate_not_armed_no_change(self):
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02,
                     extension_gate=dict(self._NOT_ARMED))
        sub = st.score_volume(last=105.0, tech=tech)
        self.assertEqual(sub["inputs"]["extension_points"], 10)
        self.assertFalse(sub["inputs"]["extension_gate_armed"])
        self.assertFalse(sub["inputs"]["extension_gate_capped"])

    def test_gate_armed_and_binds_caps_extension(self):
        # today: last/ma200 = 130/100 = +30% -> ext_pts 0 (over the 12% band).
        # reference: 105/100 = +5% -> under 12% -> full 10. 0 < 10 is already
        # the WRONG direction for "capping down" -- use the AAPL-shaped case
        # instead: today's reading is HIGH (near-full, little extension) and the
        # reference (pre-break) reading is LOWER (more extended, pre-crash) so
        # capping actually reduces today's generous post-crash credit.
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02,
                     extension_gate=self._armed_gate(ref_close=120.0, ref_ma200=100.0))
        # today: last/ma200 = 112/100 = +12% -> right at the band edge -> ext_pts 10.
        sub = st.score_volume(last=112.0, tech=tech)
        today_uncapped = 10
        # reference: 120/100 = +20% -> penalty (0.20-0.12)*100=8 -> 10-8*10/12=3.3333.
        self.assertLess(3.3333, today_uncapped)
        self.assertEqual(sub["inputs"]["extension_points"], 3.3333)
        self.assertTrue(sub["inputs"]["extension_gate_armed"])
        self.assertTrue(sub["inputs"]["extension_gate_capped"])
        self.assertIn("extension credit capped at the pre-break value", sub["arithmetic"])
        self.assertIn("2026-07-31", sub["arithmetic"])
        self.assertIn("z=-4.04", sub["arithmetic"])

    def test_gate_armed_but_not_binding_no_change(self):
        # MU-shaped: the gate is armed but the reference reading is HIGHER than
        # (or equal to) today's already-floored reading -> min() is a no-op ->
        # not disclosed as "capped" (silent, matching the spec's MU asymmetry).
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02,
                     extension_gate=self._armed_gate(ref_close=200.0, ref_ma200=100.0))
        # today: last/ma200 = 200/100 = +100% -> floored at 0. reference is the
        # SAME reading -> 0 either way -> the cap does not bind.
        sub = st.score_volume(last=200.0, tech=tech)
        self.assertEqual(sub["inputs"]["extension_points"], 0)
        self.assertTrue(sub["inputs"]["extension_gate_armed"])
        self.assertFalse(sub["inputs"]["extension_gate_capped"])
        self.assertNotIn("capped", sub["arithmetic"])

    def test_gate_armed_missing_reference_ma200_graceful(self):
        # reference_ma200 None (insufficient history at the reference session,
        # e.g. a young ticker) -> the cap cannot be computed -> skipped, no crash.
        gate = self._armed_gate(ref_close=120.0, ref_ma200=None)
        tech = _tech(ma200=100.0, vol_20d_vs_90d=1.2, ret_15d=0.02,
                     extension_gate=gate)
        sub = st.score_volume(last=112.0, tech=tech)
        self.assertEqual(sub["inputs"]["extension_points"], 10)
        self.assertFalse(sub["inputs"]["extension_gate_capped"])

    def test_extension_gate_field_not_in_extension_pts_when_ma200_null(self):
        # extension itself n/a (ma200 null) -> the gate must not be consulted /
        # must not crash.
        tech = _tech(ma200=None, vol_20d_vs_90d=1.2, ret_15d=0.02,
                     extension_gate=self._armed_gate(ref_close=120.0, ref_ma200=100.0))
        sub = st.score_volume(last=112.0, tech=tech)
        self.assertIsNone(sub["inputs"]["extension_points"])


# --------------------------------------------------------------------------- #
# QC20(c) real ground truth: AAPL's extension component 9.978 -> 3.150,
# volume_extension 18.9777 -> 12.1497. MU unaffected (gate never arms, and its
# extension_pts is 0.000 either way). From the 2026-08-09 falsifier
# pre-registration. Guarded -- SKIPPED where the read-only bundle isn't mounted.
# --------------------------------------------------------------------------- #

_QC20C_SCORE_AAPL_RAW = ("/Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/"
                         "stock-analysis/AAPL/2026-08-07-refresh/"
                         "detail_reports_2026-08-07/raw")
_QC20C_SCORE_MU_RAW = ("/Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/"
                       "stock-analysis/MU/2026-08-08/detail_reports_2026-08-08/raw")


@unittest.skipUnless(os.path.isdir(_QC20C_SCORE_AAPL_RAW) and
                     os.path.isdir(_QC20C_SCORE_MU_RAW),
                     "QC20c ground-truth read-only bundles not mounted on this machine")
class TestExtensionGateRealGroundTruthScoring(unittest.TestCase):

    @staticmethod
    def _tech_and_last(raw_dir):
        from scripts import build_snapshot as bs
        daily = bs.load_daily_raw(os.path.join(raw_dir, "daily_adjusted.json"))
        rows = bs.parse_daily_rows(daily)
        earnings = bs.load_raw(os.path.join(raw_dir, "earnings.json"))
        earn_q = bs.load_quarterly_earnings(earnings)
        tech = bs.build_technicals(rows, earn_q=earn_q)
        return tech, rows[-1]["adjusted_close"]

    def test_aapl_extension_capped_to_3_150(self):
        tech, last = self._tech_and_last(_QC20C_SCORE_AAPL_RAW)
        sub = st.score_volume(last=last, tech=tech)
        self.assertTrue(tech["extension_gate"]["armed"])
        self.assertAlmostEqual(sub["inputs"]["extension_points"], 3.1501, places=3)
        self.assertTrue(sub["inputs"]["extension_gate_capped"])
        self.assertIn("2026-07-31", sub["arithmetic"])

    def test_mu_extension_unaffected(self):
        tech, last = self._tech_and_last(_QC20C_SCORE_MU_RAW)
        sub = st.score_volume(last=last, tech=tech)
        self.assertFalse(tech["extension_gate"]["armed"])
        self.assertEqual(sub["inputs"]["extension_points"], 0)
        self.assertFalse(sub["inputs"]["extension_gate_capped"])


# --------------------------------------------------------------------------- #
# QC20(a) — trend_short: a 10/21-day factor (max 10), technical-v1.3.0
# --------------------------------------------------------------------------- #

class TestTrendShort(unittest.TestCase):
    """3/3/2/2 split: last>ma10 (+3), last>ma21 (+3), ma10_slope_5d>0 (+2),
    ma21_slope_5d>0 (+2). Null sub-component -> excluded, renormalize over
    present sub-maxes (v1.1.0 pattern, mirrors score_volume). All four null ->
    evaluable=False, factor drops out of the denominator (v1.2.0 pattern,
    mirrors score_rel_strength)."""

    def _tech(self, **over):
        base = {"ma10": 100.0, "ma21": 95.0,
                "ma10_slope_5d": 0.01, "ma21_slope_5d": 0.02}
        base.update(over)
        return base

    def test_full_marks(self):
        # last>ma10 (+3), last>ma21 (+3), both slopes>0 (+2+2) = 10.
        tech = self._tech(ma10=100.0, ma21=95.0,
                          ma10_slope_5d=0.01, ma21_slope_5d=0.02)
        sub = st.score_trend_short(last=110.0, tech=tech)
        self.assertEqual(sub["points"], 10)
        self.assertEqual(sub["max"], st._TS_MAX)
        self.assertTrue(sub["evaluable"])

    def test_zero_marks(self):
        # last below both MAs, both slopes negative -> 0.
        tech = self._tech(ma10=100.0, ma21=95.0,
                          ma10_slope_5d=-0.01, ma21_slope_5d=-0.02)
        sub = st.score_trend_short(last=80.0, tech=tech)
        self.assertEqual(sub["points"], 0)

    def test_last_ma10_only(self):
        # last>ma10 only -> +3 of 10.
        tech = self._tech(ma10=100.0, ma21=120.0,
                          ma10_slope_5d=-0.01, ma21_slope_5d=-0.02)
        sub = st.score_trend_short(last=105.0, tech=tech)
        self.assertEqual(sub["points"], 3)

    def test_slopes_only(self):
        # both slopes positive, last below both MAs -> +2+2 = 4.
        tech = self._tech(ma10=120.0, ma21=130.0,
                          ma10_slope_5d=0.01, ma21_slope_5d=0.02)
        sub = st.score_trend_short(last=110.0, tech=tech)
        self.assertEqual(sub["points"], 4)

    def test_null_last_renormalizes_over_slopes_only(self):
        # last null -> both level comparisons n/a (max 6 excluded); only the two
        # slope comparisons (max 4) remain present -> renormalize present pts
        # over present max 4, scaled back to _TS_MAX.
        tech = self._tech(ma10_slope_5d=0.01, ma21_slope_5d=0.02)
        sub = st.score_trend_short(last=None, tech=tech)
        self.assertTrue(sub["evaluable"])
        # present: 2 (ma10 slope) + 2 (ma21 slope) = 4 over present max 4 -> full 10.
        self.assertEqual(sub["points"], st._TS_MAX)
        self.assertIn("n/a", sub["arithmetic"])

    def test_null_ma10_renormalizes(self):
        # ma10 null -> last>ma10 excluded (present max 7: last>ma21 3 + slopes 2+2).
        # last>ma21 true (+3), ma10_slope>0 (+2), ma21_slope>0 (+2) = 7 earned of 7
        # present max -> renormalizes to full _TS_MAX.
        tech = self._tech(ma10=None, ma21=95.0,
                          ma10_slope_5d=0.01, ma21_slope_5d=0.02)
        sub = st.score_trend_short(last=110.0, tech=tech)
        self.assertEqual(sub["points"], st._TS_MAX)
        self.assertIn("n/a", sub["arithmetic"])

    def test_null_ma10_slope_partial_renormalizes(self):
        # ma10_slope_5d null -> excluded (present max 8: last>ma10 3 + last>ma21 3
        # + ma21_slope 2). Only last>ma10 (+3) and last>ma21 (+3) earn -> 6/8 * 10
        # = 7.5.
        tech = self._tech(ma10=100.0, ma21=95.0,
                          ma10_slope_5d=None, ma21_slope_5d=-0.01)
        sub = st.score_trend_short(last=110.0, tech=tech)
        self.assertEqual(sub["points"], 7.5)

    def test_all_four_null_not_evaluable(self):
        tech = self._tech(ma10=None, ma21=None,
                          ma10_slope_5d=None, ma21_slope_5d=None)
        sub = st.score_trend_short(last=110.0, tech=tech)
        self.assertFalse(sub["evaluable"])
        self.assertIsNone(sub["points"])

    def test_all_four_null_including_last(self):
        sub = st.score_trend_short(last=None, tech=self._tech(
            ma10=None, ma21=None, ma10_slope_5d=None, ma21_slope_5d=None))
        self.assertFalse(sub["evaluable"])

    def test_provisional_stamp(self):
        sub = st.score_trend_short(last=110.0, tech=self._tech())
        self.assertIn("PROVISIONAL", sub["arithmetic"])

    def test_factor_shape(self):
        sub = st.score_trend_short(last=110.0, tech=self._tech())
        for key in ("name", "points", "max", "arithmetic", "inputs", "evaluable"):
            self.assertIn(key, sub)
        self.assertEqual(sub["name"], "trend_short")
        self.assertEqual(sub["max"], st._TS_MAX)

    def test_boundary_not_strictly_greater_is_zero(self):
        # last == ma10/ma21 (not strictly greater) -> +0; slope == 0 -> +0.
        tech = self._tech(ma10=100.0, ma21=100.0,
                          ma10_slope_5d=0.0, ma21_slope_5d=0.0)
        sub = st.score_trend_short(last=100.0, tech=tech)
        self.assertEqual(sub["points"], 0)


# --------------------------------------------------------------------------- #
# QC20(a) — real ground-truth regression: the exact AAPL/MU archived-bundle
# trend_short values from the 2026-08-09 falsifier pre-registration (AAPL 2/10,
# MU 3/10). Guarded -- SKIPPED where the read-only bundle volume isn't mounted.
# --------------------------------------------------------------------------- #

_QC20_AAPL_RAW = ("/Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/"
                  "stock-analysis/AAPL/2026-08-07-refresh/"
                  "detail_reports_2026-08-07/raw")
_QC20_MU_RAW = ("/Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/"
               "stock-analysis/MU/2026-08-08/detail_reports_2026-08-08/raw")


@unittest.skipUnless(os.path.isdir(_QC20_AAPL_RAW) and os.path.isdir(_QC20_MU_RAW),
                     "QC20 ground-truth read-only bundles not mounted on this machine")
class TestTrendShortRealGroundTruth(unittest.TestCase):

    @staticmethod
    def _tech_and_last(raw_dir):
        import tests.test_build_snapshot as tb
        block = tb.TestField2RealGroundTruthShortMAs._block(raw_dir)
        from scripts import build_snapshot as bs
        daily = bs.load_daily_raw(os.path.join(raw_dir, "daily_adjusted.json"))
        rows = bs.parse_daily_rows(daily)
        last = rows[-1]["adjusted_close"]
        return block, last

    def test_aapl_trend_short_is_2_of_10(self):
        tech, last = self._tech_and_last(_QC20_AAPL_RAW)
        sub = st.score_trend_short(last=last, tech=tech)
        self.assertEqual(sub["points"], 2)

    def test_mu_trend_short_is_3_of_10(self):
        tech, last = self._tech_and_last(_QC20_MU_RAW)
        sub = st.score_trend_short(last=last, tech=tech)
        self.assertEqual(sub["points"], 3)


# --------------------------------------------------------------------------- #
# Track O4 — sector-relative RS factor (score_rel_strength), technical-v1.2.0
# --------------------------------------------------------------------------- #

class TestRelStrength(unittest.TestCase):
    """O4.3: score_rel_strength blends rel_sector_ret_3m/6m and scores a MONOTONE
    band (PROVISIONAL, unratified pending B9). Factor absent when both legs null."""

    def test_rel_strength_bands(self):
        # Full mark at rel >= +0.10.
        self.assertEqual(
            st.score_rel_strength({"rel_sector_ret_3m": 0.15,
                                   "rel_sector_ret_6m": 0.15})["points"],
            st._RS_MAX)
        # Half at rel == 0.0 (strictly between 0 and _RS_MAX).
        mid = st.score_rel_strength({"rel_sector_ret_3m": 0.0,
                                     "rel_sector_ret_6m": 0.0})["points"]
        self.assertTrue(0 < mid < st._RS_MAX)
        # Zero when clearly underperforming (rel < -0.03).
        self.assertEqual(
            st.score_rel_strength({"rel_sector_ret_3m": -0.20,
                                   "rel_sector_ret_6m": -0.20})["points"],
            0)

    def test_rel_strength_monotone_bands(self):
        # Points are non-increasing as rel falls across the four bands.
        full = st.score_rel_strength({"rel_sector_ret_3m": 0.12,
                                      "rel_sector_ret_6m": 0.12})["points"]
        upper = st.score_rel_strength({"rel_sector_ret_3m": 0.05,
                                       "rel_sector_ret_6m": 0.05})["points"]
        half = st.score_rel_strength({"rel_sector_ret_3m": 0.0,
                                      "rel_sector_ret_6m": 0.0})["points"]
        low = st.score_rel_strength({"rel_sector_ret_3m": -0.10,
                                     "rel_sector_ret_6m": -0.10})["points"]
        self.assertGreaterEqual(full, upper)
        self.assertGreaterEqual(upper, half)
        self.assertGreaterEqual(half, low)
        # every band value in [0, _RS_MAX]
        for v in (full, upper, half, low):
            self.assertGreaterEqual(v, 0)
            self.assertLessEqual(v, st._RS_MAX)

    def test_rel_strength_blends_available_leg(self):
        # Only 3m present -> blend uses the present leg; only 6m present likewise.
        only3 = st.score_rel_strength({"rel_sector_ret_3m": 0.15})
        self.assertEqual(only3["points"], st._RS_MAX)
        self.assertTrue(only3["evaluable"])
        only6 = st.score_rel_strength({"rel_sector_ret_6m": 0.15})
        self.assertEqual(only6["points"], st._RS_MAX)
        self.assertTrue(only6["evaluable"])

    def test_rel_strength_absent_when_both_legs_null(self):
        # Both legs absent -> factor NOT evaluable (drops out of present_max).
        sub = st.score_rel_strength({})
        self.assertFalse(sub["evaluable"])
        sub2 = st.score_rel_strength(None)
        self.assertFalse(sub2["evaluable"])

    def test_rel_strength_provisional_stamp(self):
        sub = st.score_rel_strength({"rel_sector_ret_3m": 0.15,
                                     "rel_sector_ret_6m": 0.15})
        self.assertIn("PROVISIONAL", sub["arithmetic"])

    def test_rel_strength_factor_shape(self):
        sub = st.score_rel_strength({"rel_sector_ret_3m": 0.15,
                                     "rel_sector_ret_6m": 0.15})
        for key in ("name", "points", "max", "arithmetic", "inputs", "evaluable"):
            self.assertIn(key, sub)
        self.assertEqual(sub["max"], st._RS_MAX)


class TestTechnicalGracefulIdentity(unittest.TestCase):
    """O4.3 graceful-identity: with benchmark=None (or no benchmark arg, or a
    benchmark lacking the rel_sector fields), the technical score is byte-for-byte
    identical to the pre-O4 (v1.1.0) score -- the sector-RS factor drops out of the
    present_max renormalization entirely."""

    def _fixture(self):
        tech = _tech()
        ladder = _ladder([(97.0, "ma50"), (110.0, "swing_high")])
        return tech, ladder

    def test_score_identical_none_vs_omitted(self):
        tech, ladder = self._fixture()
        omitted = st.score(last=110.0, tech=tech, ladder=ladder,
                           divergence="none", justification=None)
        explicit_none = st.score(last=110.0, tech=tech, ladder=ladder,
                                 divergence="none", justification=None,
                                 benchmark=None)
        self.assertEqual(json.dumps(omitted, sort_keys=True),
                         json.dumps(explicit_none, sort_keys=True))

    def test_score_identical_benchmark_without_rel_fields(self):
        # A benchmark block that has SPY returns but NO rel_sector fields -> the
        # RS factor stays non-evaluable -> identical to the no-benchmark score.
        tech, ladder = self._fixture()
        baseline = st.score(last=110.0, tech=tech, ladder=ladder,
                            divergence="none", justification=None)
        spy_only = st.score(last=110.0, tech=tech, ladder=ladder,
                            divergence="none", justification=None,
                            benchmark={"spy_ret_3m": 0.05, "spy_ret_12m": 0.10})
        self.assertEqual(json.dumps(baseline, sort_keys=True),
                         json.dumps(spy_only, sort_keys=True))
        # sanity: max total still 100 (RS factor excluded from the arithmetic,
        # zeroed in its published row).
        self.assertEqual(sum(s["max"] for s in baseline["subscores"]), 100)
        # QC10: the sector-RS dimension being genuinely missing IS an exclusion
        # and must be disclosed -- renormalized flips True, naming it. Before the
        # fix this asserted assertFalse here, pinning the defect (the one case
        # that genuinely needed disclosure was silent).
        self.assertTrue(baseline["renormalized"])
        self.assertIn("rel_strength_sector", baseline["renormalization_note"])

    def test_score_with_rel_fields_adds_factor(self):
        # When rel_sector fields ARE present, the RS factor is included: the score
        # differs (a fifth scored factor participates) and present_max grows by
        # _RS_MAX.
        tech, ladder = self._fixture()
        baseline = st.score(last=110.0, tech=tech, ladder=ladder,
                            divergence="none", justification=None)
        with_rs = st.score(last=110.0, tech=tech, ladder=ladder,
                          divergence="none", justification=None,
                          benchmark={"rel_sector_ret_3m": 0.15,
                                     "rel_sector_ret_6m": 0.15})
        # the RS factor appears as a subscore (alongside trend_short, always a
        # considered member since v1.3.0) and lifts the denominator.
        self.assertEqual(len(with_rs["subscores"]), 6)
        self.assertEqual(sum(s["max"] for s in with_rs["subscores"]),
                         100 + st._RS_MAX)
        # a full-mark RS on top of a strong tech read is still <= 100.
        self.assertLessEqual(with_rs["score"], 100)
        self.assertGreaterEqual(with_rs["score"], 0)


# --------------------------------------------------------------------------- #
# trend_claim (mechanical)
# --------------------------------------------------------------------------- #

class TestTrendClaim(unittest.TestCase):
    def test_uptrend(self):
        self.assertEqual(st.trend_claim(110.0, _tech(ma50=100.0, ma200=90.0)),
                         "uptrend")

    def test_downtrend(self):
        self.assertEqual(st.trend_claim(80.0, _tech(ma50=90.0, ma200=100.0)),
                         "downtrend")

    def test_sideways(self):
        # last>ma50 but ma50<ma200 -> neither strict chain -> sideways
        self.assertEqual(st.trend_claim(95.0, _tech(ma50=92.0, ma200=100.0)),
                         "sideways")

    def test_sideways_on_null(self):
        self.assertEqual(st.trend_claim(95.0, _tech(ma50=None, ma200=100.0)),
                         "sideways")


# --------------------------------------------------------------------------- #
# Renormalization (a whole dimension has zero evaluable inputs)
# --------------------------------------------------------------------------- #

class TestRenormalization(unittest.TestCase):
    def test_momentum_dimension_null_renormalizes(self):
        # rsi, macd, macd_signal ALL null -> momentum dimension excluded.
        # Score rescaled 0-100 over remaining max (100-25=75).
        tech = _tech(rsi14=None, macd=None, macd_signal=None)
        ladder = _ladder([(97.0, "ma50"), (110.0, "swing_high")])
        result = st.score(last=110.0, tech=tech, ladder=ladder,
                          divergence="none", justification=None)
        self.assertTrue(result["renormalized"])
        # the momentum subscore must be excluded from the raw-max total
        maxes = sum(s["max"] for s in result["subscores"])
        self.assertEqual(maxes, 75)
        self.assertLessEqual(result["score"], 100)
        self.assertGreaterEqual(result["score"], 0)

    def test_no_renormalization_when_all_dimensions_have_inputs(self):
        # QC10: the sector-RS dimension is now always a considered member, so
        # "all dimensions have inputs" must include it -- pass a benchmark with
        # both rel-sector legs to genuinely satisfy the test's own name. Before
        # the fix this test omitted the RS dimension entirely (it wasn't yet a
        # considered member when non-evaluable) and asserted no-renormalization
        # anyway; that omission is exactly the defect QC10 corrects. v1.3.0:
        # trend_short is ALSO now always a considered member -- its four inputs
        # must be supplied too, or this test's own name is no longer true.
        tech = _tech(ma10=95.0, ma21=90.0, ma10_slope_5d=0.5, ma21_slope_5d=0.3)
        ladder = _ladder([(97.0, "ma50"), (110.0, "swing_high")])
        result = st.score(last=110.0, tech=tech, ladder=ladder,
                          divergence="none", justification=None,
                          benchmark={"rel_sector_ret_3m": 0.15,
                                     "rel_sector_ret_6m": 0.15})
        self.assertFalse(result["renormalized"])
        maxes = sum(s["max"] for s in result["subscores"])
        self.assertEqual(maxes, 100 + st._RS_MAX + st._TS_MAX)


# --------------------------------------------------------------------------- #
# QC10: `renormalized` must key on ACTUAL EXCLUSION of a considered dimension,
# not on `raw_max != 100`. The bonus sector-RS dimension (max _RS_MAX) growing
# the denominator to 110 is NOT an exclusion; the bonus dimension being
# genuinely absent (no rel_sector_ret_3m/6m in the benchmark) IS an exclusion
# and must be disclosed by name -- never as an empty list.
# --------------------------------------------------------------------------- #

class TestQC10SectorRSExclusionDisclosure(unittest.TestCase):
    def _fixture(self):
        tech = _tech()
        ladder = _ladder([(97.0, "ma50"), (110.0, "swing_high")])
        return tech, ladder

    def test_mu_shaped_rs_present_nothing_excluded_no_disclosure(self):
        # MU-shaped: benchmark carries rel_sector_ret_3m/_6m -- the sector-RS
        # dimension is evaluable and joins the denominator (100 -> 110). No
        # dimension was excluded, so renormalized must be False and the note
        # None -- membership GROWTH is not exclusion. v1.3.0: trend_short is
        # ALSO now always a considered member -- its own fixture (not the
        # shared ``_fixture()``, which the sibling AAPL-shaped test below
        # deliberately leaves trend_short-absent) supplies all four inputs so
        # "nothing excluded" stays true of both new v1.3.0 factors, denominator
        # 110 -> 120.
        tech, ladder = self._fixture()
        tech = dict(tech, ma10=95.0, ma21=90.0,
                   ma10_slope_5d=0.5, ma21_slope_5d=0.3)
        result = st.score(last=110.0, tech=tech, ladder=ladder,
                          divergence="none", justification=None,
                          benchmark={"rel_sector_ret_3m": 0.15,
                                     "rel_sector_ret_6m": 0.15})
        self.assertFalse(result["renormalized"])
        self.assertIsNone(result["renormalization_note"])
        if result["renormalization_note"]:
            self.assertNotIn("inputs: )", result["renormalization_note"])
        # numeric score/denominator pinned to today's (pre- and post-fix
        # identical) values -- this is a disclosure fix, not a scoring change.
        self.assertEqual(result["score"], 78.7346)
        self.assertEqual(sum(s["max"] for s in result["subscores"]), 120)

    def test_aapl_shaped_rs_absent_is_excluded_and_disclosed(self):
        # AAPL-shaped: benchmark lacks BOTH rel-sector fields -- the sector-RS
        # dimension is genuinely not evaluable. This IS an exclusion and must
        # be disclosed by name; today it is silently dropped instead.
        tech, ladder = self._fixture()
        result = st.score(last=110.0, tech=tech, ladder=ladder,
                          divergence="none", justification=None,
                          benchmark={"spy_ret_3m": 0.05, "spy_ret_12m": 0.10})
        self.assertTrue(result["renormalized"])
        self.assertIsNotNone(result["renormalization_note"])
        self.assertIn("rel_strength_sector", result["renormalization_note"])
        self.assertNotIn("inputs: )", result["renormalization_note"])
        # numeric score/denominator pinned to today's (pre- and post-fix
        # identical) values -- out of 100, RS row excluded/zeroed.
        self.assertEqual(result["score"], 74.4815)
        self.assertEqual(sum(s["max"] for s in result["subscores"]), 100)

    def test_momentum_still_excluded_and_disclosed_when_genuinely_null(self):
        # Regression guard: a genuine exclusion of an always-present dimension
        # (momentum, all inputs null) must still renormalize and be named --
        # this is the path TestRenormalization.test_momentum_dimension_null_
        # renormalizes already covers; pinned again here alongside the sector-
        # RS cases so the three exclusion scenarios read together.
        tech = _tech(rsi14=None, macd=None, macd_signal=None)
        ladder = _ladder([(97.0, "ma50"), (110.0, "swing_high")])
        result = st.score(last=110.0, tech=tech, ladder=ladder,
                          divergence="none", justification=None)
        self.assertTrue(result["renormalized"])
        self.assertIn("momentum", result["renormalization_note"])
        self.assertNotIn("inputs: )", result["renormalization_note"])


# --------------------------------------------------------------------------- #
# CLI end-to-end (real bundle, reuses test_build_snapshot fixtures)
# --------------------------------------------------------------------------- #

SCRIPT = os.path.join(
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

    def _run(self, extra=None):
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir]
        if extra:
            cmd += extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_cli_exit0_writes_module_json(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = os.path.join(self.dir, "module_technical.json")
        self.assertTrue(os.path.exists(out))
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["skill"], "technical-analysis")
        self.assertEqual(doc["rubric_version"], "1.3.0")
        self.assertIn("PROVISIONAL", doc["module_note"])
        self.assertEqual(doc["ticker"], "MU")
        self.assertIn("as_of", doc)
        self.assertIsInstance(doc["score"], (int, float))
        self.assertGreaterEqual(doc["score"], 0)
        self.assertLessEqual(doc["score"], 100)
        self.assertIn(doc["trend_claim"], ("uptrend", "downtrend", "sideways"))
        self.assertIsInstance(doc["subscores"], list)
        # QC10: the sector-RS dimension is now always a considered member (the
        # standard build_full fixture carries no sector_daily_adjusted, so RS is
        # not evaluable here and is published as a zeroed/excluded row). v1.3.0:
        # trend_short is ALSO always a considered member; the fixture's random-
        # walk daily series DOES carry ma10/ma21/slopes (Field 2, already shipped)
        # so trend_short IS evaluable here -- 4 core + trend_short + excluded
        # sector-RS = 6 rows.
        self.assertEqual(len(doc["subscores"]), 6)
        self.assertIsInstance(doc["ladder"], list)
        self.assertIn("divergence", doc["flags"])
        # signal is ALWAYS null in the JSON (the LLM writes it in prose)
        self.assertIsNone(doc["signal"])
        # confidence-v1.0.0: the module carries a well-formed confidence block.
        conf = doc["confidence"]
        self.assertEqual(set(conf),
                         {"level", "source", "depth", "staleness", "rule",
                          "version"})
        self.assertIn(conf["level"], ("LOW", "MEDIUM", "HIGH"))
        self.assertEqual(conf["version"], "1.0.0")
        self.assertEqual(conf["rule"], "min(source, depth, staleness)")
        # v1.1.0: technical DEPTH promotes to HIGH (regime-conditional pass
        # landed) and SOURCE is HIGH on this premium (alpha_vantage) build -- both
        # promotions the E2E gate looks for. The OVERALL badge here is pinned by
        # STALENESS only: the build_snapshot fixture stamps as_of one day past the
        # last row (a deliberately stale/weekend print), so staleness is MEDIUM and
        # overall is MEDIUM. On a genuinely fresh print (latest == as_of) all three
        # axes are HIGH -> overall HIGH; test_confidence pins that end-to-end.
        self.assertEqual(conf["depth"]["level"], "HIGH")
        self.assertEqual(conf["source"]["level"], "HIGH")
        self.assertEqual(conf["staleness"]["level"], "MEDIUM")
        self.assertEqual(conf["level"], "MEDIUM")
        # every subscore carries arithmetic + inputs
        for s in doc["subscores"]:
            self.assertIn("arithmetic", s)
            self.assertIn("inputs", s)
            self.assertIn("name", s)

    def test_divergence_without_justification_errors(self):
        proc = self._run(extra=["--divergence", "bearish"])
        self.assertNotEqual(proc.returncode, 0)

    def test_divergence_with_justification_ok(self):
        proc = self._run(extra=["--divergence", "bearish",
                                "--divergence-justification",
                                "lower highs into 130 resistance"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_technical.json")
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["flags"]["divergence"], "bearish")
        self.assertEqual(doc["flags"]["divergence_justification"],
                         "lower highs into 130 resistance")

    def test_custom_out_path(self):
        out = os.path.join(self.dir, "custom_tech.json")
        proc = self._run(extra=["--out", out])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

    def test_determinism(self):
        # two identical runs -> byte-identical JSON
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


# --------------------------------------------------------------------------- #
# INPUT_FIELDS declaration (Task 13 cross-skill disjointness will import this)
# --------------------------------------------------------------------------- #

class TestInputFields(unittest.TestCase):
    def test_input_fields_exact(self):
        # v1.1.0 adds the two SCORED volume-quality fields (ad_line_slope,
        # upvol_ratio). adx14 + stage are GUARDS, not here (see GUARD_FIELDS).
        # v1.2.0 (Track O4) adds the two SCORED sector-relative RS fields.
        # v1.3.0 (QC20a) adds the four SCORED trend_short inputs; the new
        # extension_gate field (QC20c) is a GUARD/CAP, not here.
        self.assertEqual(st.INPUT_FIELDS, {
            "technicals.ma50", "technicals.ma200",
            "technicals.ma50_slope_20d", "technicals.ma200_slope_20d",
            "technicals.rsi14", "technicals.macd", "technicals.macd_signal",
            "technicals.vol_20d_vs_90d", "technicals.ret_15d",
            "technicals.ad_line_slope", "technicals.upvol_ratio",
            "benchmark.rel_sector_ret_3m", "benchmark.rel_sector_ret_6m",
            "technicals.ma10", "technicals.ma21",
            "technicals.ma10_slope_5d", "technicals.ma21_slope_5d",
        })

    def test_rel_sector_fields_in_input_fields(self):
        # Track O4: the scored sector-RS inputs are declared here (and ONLY here --
        # test_single_mapping asserts they are not in score_sentiment.INPUT_FIELDS).
        self.assertIn("benchmark.rel_sector_ret_3m", st.INPUT_FIELDS)
        self.assertIn("benchmark.rel_sector_ret_6m", st.INPUT_FIELDS)

    def test_guard_fields_exact(self):
        # adx14 + stage MODULATE momentum but earn no points -> guard fields.
        # v1.3.0 (QC20c): extension_gate CAPS volume_extension, earns no points.
        self.assertEqual(st.GUARD_FIELDS, {
            "technicals.adx14", "technicals.stage", "technicals.extension_gate",
        })

    def test_guard_and_input_fields_disjoint(self):
        # a field is scored xor a pure guard -- never both (governance rule,
        # mirrored in test_single_mapping).
        self.assertFalse(st.GUARD_FIELDS & st.INPUT_FIELDS)

    def test_vwap_fields_not_in_input_fields(self):
        # anchored-VWAP fields feed the LADDER, not a scored branch directly.
        self.assertNotIn("technicals.vwap_52wk_high", st.INPUT_FIELDS)
        self.assertNotIn("technicals.vwap_earnings", st.INPUT_FIELDS)

    def test_shared_reference_fields_not_listed(self):
        # price.last and ladder are shared reference infrastructure, NOT scored
        # inputs -> must not appear in INPUT_FIELDS.
        self.assertNotIn("price.last", st.INPUT_FIELDS)


# --------------------------------------------------------------------------- #
# Rubric version + provisional module note (v1.3.0, QC20).
# --------------------------------------------------------------------------- #

class TestRubricAndNote(unittest.TestCase):
    def test_rubric_version_is_130(self):
        # Phase-2 (QC20a + QC20c) bumps technical to v1.3.0.
        self.assertEqual(st.RUBRIC_VERSION, "1.3.0")

    def test_module_note_is_provisional(self):
        self.assertIn("PROVISIONAL", st.MODULE_NOTE)
        self.assertIn("B9", st.MODULE_NOTE)
        self.assertIn("falsifier", st.MODULE_NOTE)

    def test_module_note_discloses_sector_rs(self):
        # The note must disclose the v1.2.0 sector-RS factor as provisional.
        self.assertIn("sector", st.MODULE_NOTE.lower())

    def test_module_note_discloses_trend_short(self):
        self.assertIn("trend_short", st.MODULE_NOTE)

    def test_module_note_discloses_extension_gate(self):
        self.assertIn("extension-credit gate", st.MODULE_NOTE)


if __name__ == "__main__":
    unittest.main()
