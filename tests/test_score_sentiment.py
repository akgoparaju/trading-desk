"""Tests for scripts/score_sentiment.py -- the sentiment-positioning evidence skill.

WHY: like technical-analysis and risk-analytics, this module's arithmetic IS the
rubric of record (sentiment rubric v1.0.0). Every scoring branch is pinned to a
hand-computed value; if the code and these numbers ever diverge, the rubric has
silently changed and that must surface as a test failure, not a shifted report.

Tests exercise the pure scoring functions directly (exact values per branch), the
positioning / momentum table builders and the hedging-cost note, the three
judgment flags (rating-actions, inst-flow, insider-baseline) with their
justification guards, renormalization, and one end-to-end CLI run against a real
snapshot bundle fabricated exactly the way test_score_risk.py does. The scoring
functions take already-parsed sub-blocks so branches pin without a full snapshot.

stdlib-only; unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import score_sentiment as ss


# --------------------------------------------------------------------------- #
# Helpers: minimal snapshot sub-blocks.
# --------------------------------------------------------------------------- #

def _ratings(strong_buy=10, buy=8, hold=5, sell=1, strong_sell=0, n=None):
    if n is None:
        n = strong_buy + buy + hold + sell + strong_sell
    return {"strong_buy": strong_buy, "buy": buy, "hold": hold,
            "sell": sell, "strong_sell": strong_sell, "n": n}


def _sent(**over):
    """A fully-populated sentiment-relevant block; override per test."""
    base = {
        "ratings": _ratings(),
        "pt_vs_price_pct": 0.10,
        "insider_net_90d_usd": 1000.0,
        "short_interest_pct": 5.0,        # PERCENT units
        "put_call_ratio_full_chain": 0.9,
        "iv_pctile_1yr": 50.0,
    }
    base.update(over)
    return base


def _rev(**over):
    base = {"pct": 0.02, "up_30d": 9, "down_30d": 3}
    base.update(over)
    return base


def _tech(**over):
    base = {"rsi14": 55.0, "ret_3m": 0.05, "ret_6m": 0.10, "ret_12m": 0.30}
    base.update(over)
    return base


def _bench(**over):
    base = {"spy_ret_3m": 0.02, "spy_ret_12m": 0.10}
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# 1. Street view (max 25): buy_pct (10) + PT (10) + rating-actions (5)
#    Spec §5.2 cap: pt_vs_price_pct < 0 caps the WHOLE dimension at 10.
# --------------------------------------------------------------------------- #

class TestStreetView(unittest.TestCase):
    def test_buy_pct_high_is_10(self):
        # (10+8)/24 = 0.75 >= 0.70 -> 10
        sub = ss.score_street(_sent(ratings=_ratings()), "neutral", None)
        self.assertEqual(sub["inputs"]["buy_pct_points"], 10)

    def test_buy_pct_mid_is_7(self):
        # (5+5)/20 = 0.50 in [0.50,0.70) -> 7
        sub = ss.score_street(
            _sent(ratings=_ratings(strong_buy=5, buy=5, hold=10, sell=0)),
            "neutral", None)
        self.assertEqual(sub["inputs"]["buy_pct_points"], 7)

    def test_buy_pct_low_is_4(self):
        # (2+2)/10 = 0.40 in [0.30,0.50) -> 4
        sub = ss.score_street(
            _sent(ratings=_ratings(strong_buy=2, buy=2, hold=6, sell=0)),
            "neutral", None)
        self.assertEqual(sub["inputs"]["buy_pct_points"], 4)

    def test_buy_pct_verylow_is_2(self):
        # (1+1)/10 = 0.20 < 0.30 -> 2
        sub = ss.score_street(
            _sent(ratings=_ratings(strong_buy=1, buy=1, hold=8, sell=0)),
            "neutral", None)
        self.assertEqual(sub["inputs"]["buy_pct_points"], 2)

    def test_buy_pct_null_ratings_is_0_na(self):
        sub = ss.score_street(_sent(ratings=None), "neutral", None)
        self.assertEqual(sub["inputs"]["buy_pct_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])

    def test_buy_pct_zero_n_is_0_na(self):
        sub = ss.score_street(_sent(ratings=_ratings(0, 0, 0, 0, 0, n=0)),
                              "neutral", None)
        self.assertEqual(sub["inputs"]["buy_pct_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])

    def test_pt_strong_is_10(self):
        # pt_vs_price_pct 0.20 > 0.15 -> 10
        sub = ss.score_street(_sent(pt_vs_price_pct=0.20), "neutral", None)
        self.assertEqual(sub["inputs"]["pt_points"], 10)

    def test_pt_mid_is_7(self):
        # 0.10 in (0.05,0.15] -> 7
        sub = ss.score_street(_sent(pt_vs_price_pct=0.10), "neutral", None)
        self.assertEqual(sub["inputs"]["pt_points"], 7)

    def test_pt_thin_is_4(self):
        # 0.03 in [0,0.05] -> 4
        sub = ss.score_street(_sent(pt_vs_price_pct=0.03), "neutral", None)
        self.assertEqual(sub["inputs"]["pt_points"], 4)

    def test_pt_negative_is_0(self):
        # -0.05 < 0 -> 0
        sub = ss.score_street(_sent(pt_vs_price_pct=-0.05), "neutral", None)
        self.assertEqual(sub["inputs"]["pt_points"], 0)

    def test_rating_actions_positive_plus5(self):
        sub = ss.score_street(_sent(), "positive", "3 upgrades this week")
        self.assertEqual(sub["inputs"]["rating_actions_points"], 5)

    def test_rating_actions_neutral_plus3(self):
        sub = ss.score_street(_sent(), "neutral", None)
        self.assertEqual(sub["inputs"]["rating_actions_points"], 3)

    def test_rating_actions_negative_plus0(self):
        sub = ss.score_street(_sent(), "negative", "2 downgrades post-print")
        self.assertEqual(sub["inputs"]["rating_actions_points"], 0)

    def test_pt_below_price_caps_dimension_at_10(self):
        # buy_pct 10 + actions 5, but pt < 0 -> whole dimension capped at 10/25.
        sub = ss.score_street(
            _sent(ratings=_ratings(), pt_vs_price_pct=-0.05),
            "positive", "3 upgrades this week")
        self.assertEqual(sub["points"], 10)
        self.assertIn("dimension capped at 10/25", sub["arithmetic"])

    def test_no_cap_when_pt_nonneg(self):
        # buy_pct 10 + pt 10 + actions 5 = 25 (no cap)
        sub = ss.score_street(
            _sent(ratings=_ratings(), pt_vs_price_pct=0.20),
            "positive", "3 upgrades this week")
        self.assertEqual(sub["points"], 25)

    def test_pt_null_not_capped(self):
        # null PT is n/a (0 pts) but NOT the <0 cap trigger.
        sub = ss.score_street(_sent(pt_vs_price_pct=None), "neutral", None)
        self.assertEqual(sub["inputs"]["pt_points"], 0)
        self.assertNotIn("dimension capped", sub["arithmetic"])


# --------------------------------------------------------------------------- #
# 2. Revisions momentum (max 20): band + up/down adjustment
# --------------------------------------------------------------------------- #

class TestRevisions(unittest.TestCase):
    def test_rev_strong_is_20(self):
        # pct 0.05 > 0.03 -> 20 (up==down so no adj)
        sub = ss.score_revisions(_rev(pct=0.05, up_30d=3, down_30d=3))
        self.assertEqual(sub["inputs"]["band_points"], 20)
        self.assertEqual(sub["inputs"]["adjustment_points"], 0)

    def test_rev_good_is_14(self):
        # 0.02 in (0.005,0.03] -> 14
        sub = ss.score_revisions(_rev(pct=0.02, up_30d=3, down_30d=3))
        self.assertEqual(sub["inputs"]["band_points"], 14)

    def test_rev_flat_is_10(self):
        # 0.0 in [-0.005,0.005] -> 10
        sub = ss.score_revisions(_rev(pct=0.0, up_30d=3, down_30d=3))
        self.assertEqual(sub["inputs"]["band_points"], 10)

    def test_rev_soft_is_5(self):
        # -0.01 in [-0.03,-0.005) -> 5
        sub = ss.score_revisions(_rev(pct=-0.01, up_30d=3, down_30d=3))
        self.assertEqual(sub["inputs"]["band_points"], 5)

    def test_rev_weak_is_0(self):
        # -0.05 < -0.03 -> 0
        sub = ss.score_revisions(_rev(pct=-0.05, up_30d=3, down_30d=3))
        self.assertEqual(sub["inputs"]["band_points"], 0)

    def test_rev_null_is_0_na(self):
        sub = ss.score_revisions(_rev(pct=None))
        self.assertEqual(sub["inputs"]["band_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])

    def test_up_gt_down_adds_2_capped(self):
        # band 20 + up(9)>down(3) +2 -> capped at 20
        sub = ss.score_revisions(_rev(pct=0.05, up_30d=9, down_30d=3))
        self.assertEqual(sub["inputs"]["adjustment_points"], 2)
        self.assertEqual(sub["points"], 20)

    def test_up_gt_down_adds_2_below_cap(self):
        # band 14 + up>down +2 -> 16
        sub = ss.score_revisions(_rev(pct=0.02, up_30d=9, down_30d=3))
        self.assertEqual(sub["inputs"]["adjustment_points"], 2)
        self.assertEqual(sub["points"], 16)

    def test_down_gt_up_subtracts_2_floored(self):
        # band 0 + down>up -2 -> floored at 0
        sub = ss.score_revisions(_rev(pct=-0.05, up_30d=1, down_30d=9))
        self.assertEqual(sub["inputs"]["adjustment_points"], -2)
        self.assertEqual(sub["points"], 0)

    def test_tie_no_adjustment(self):
        sub = ss.score_revisions(_rev(pct=0.02, up_30d=5, down_30d=5))
        self.assertEqual(sub["inputs"]["adjustment_points"], 0)

    def test_null_counts_no_adjustment(self):
        sub = ss.score_revisions(_rev(pct=0.02, up_30d=None, down_30d=None))
        self.assertEqual(sub["inputs"]["adjustment_points"], 0)

    def test_all_null_not_evaluable(self):
        sub = ss.score_revisions(None)
        self.assertFalse(sub["evaluable"])


# --------------------------------------------------------------------------- #
# 3. Smart money & insiders (max 20): inst-flow (8) + insider (12)
# --------------------------------------------------------------------------- #

class TestSmartMoney(unittest.TestCase):
    def test_inst_flow_accumulating_is_8(self):
        sub = ss.score_smart_money(_sent(), "accumulating", "13F net buys",
                                   "normal", None)
        self.assertEqual(sub["inputs"]["inst_flow_points"], 8)

    def test_inst_flow_neutral_is_5(self):
        sub = ss.score_smart_money(_sent(), "neutral", "flat 13F",
                                   "normal", None)
        self.assertEqual(sub["inputs"]["inst_flow_points"], 5)

    def test_inst_flow_distributing_is_2(self):
        sub = ss.score_smart_money(_sent(), "distributing", "13F net sells",
                                   "normal", None)
        self.assertEqual(sub["inputs"]["inst_flow_points"], 2)

    def test_inst_flow_unknown_is_0_na(self):
        sub = ss.score_smart_money(_sent(), "unknown", None, "normal", None)
        self.assertEqual(sub["inputs"]["inst_flow_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])
        self.assertIn("13F not assessed", sub["arithmetic"])

    def test_insider_positive_is_12(self):
        # insider_net_90d_usd > 0 -> 12
        sub = ss.score_smart_money(_sent(insider_net_90d_usd=5000.0),
                                   "unknown", None, "normal", None)
        self.assertEqual(sub["inputs"]["insider_points"], 12)

    def test_insider_nonpos_normal_is_8(self):
        # <= 0 with baseline normal -> 8 (routine selling)
        sub = ss.score_smart_money(_sent(insider_net_90d_usd=-3000.0),
                                   "unknown", None, "normal", None)
        self.assertEqual(sub["inputs"]["insider_points"], 8)
        self.assertIn("routine", sub["arithmetic"])

    def test_insider_nonpos_unusual_is_2(self):
        # <= 0 with baseline unusual -> 2
        sub = ss.score_smart_money(_sent(insider_net_90d_usd=-3000.0),
                                   "unknown", None, "unusual",
                                   "cluster of CFO sales at highs")
        self.assertEqual(sub["inputs"]["insider_points"], 2)

    def test_insider_zero_normal_is_8(self):
        # exactly 0 counts as <= 0
        sub = ss.score_smart_money(_sent(insider_net_90d_usd=0.0),
                                   "unknown", None, "normal", None)
        self.assertEqual(sub["inputs"]["insider_points"], 8)

    def test_insider_null_is_0(self):
        sub = ss.score_smart_money(_sent(insider_net_90d_usd=None),
                                   "unknown", None, "normal", None)
        self.assertEqual(sub["inputs"]["insider_points"], 0)

    def test_not_evaluable_when_unknown_and_null_insider(self):
        sub = ss.score_smart_money(_sent(insider_net_90d_usd=None),
                                   "unknown", None, "normal", None)
        self.assertFalse(sub["evaluable"])


# --------------------------------------------------------------------------- #
# 4. Positioning & derivatives (max 20): SI (8) + P/C (6) + IV pctile (6)
#    COMPLACENCY GUARD evaluated first: si<1.5 AND rsi>70 -> 2.
# --------------------------------------------------------------------------- #

class TestPositioning(unittest.TestCase):
    def test_complacency_guard_fires(self):
        # si 1.2 < 1.5 AND rsi 74 > 70 -> 2, labeled complacency guard
        sub = ss.score_positioning(_sent(short_interest_pct=1.2), rsi14=74.0)
        self.assertEqual(sub["inputs"]["si_points"], 2)
        self.assertIn("complacency guard", sub["arithmetic"])

    def test_complacency_guard_not_fired_low_rsi(self):
        # si 1.2 < 1.5 but rsi 55 <= 70 -> normal si<2 band -> 6
        sub = ss.score_positioning(_sent(short_interest_pct=1.2), rsi14=55.0)
        self.assertEqual(sub["inputs"]["si_points"], 6)
        self.assertNotIn("complacency guard", sub["arithmetic"])

    def test_si_low_is_6(self):
        # si 1.8 < 2 (and not complacency: 1.8 >= 1.5) -> 6
        sub = ss.score_positioning(_sent(short_interest_pct=1.8), rsi14=74.0)
        self.assertEqual(sub["inputs"]["si_points"], 6)

    def test_si_moderate_is_8(self):
        # si 5.0 in [2,8] -> 8
        sub = ss.score_positioning(_sent(short_interest_pct=5.0), rsi14=55.0)
        self.assertEqual(sub["inputs"]["si_points"], 8)

    def test_si_elevated_is_5(self):
        # si 12.0 in (8,15] -> 5
        sub = ss.score_positioning(_sent(short_interest_pct=12.0), rsi14=55.0)
        self.assertEqual(sub["inputs"]["si_points"], 5)

    def test_si_high_percent_unit_is_3(self):
        # si 26.23 (PERCENT units) > 15 -> 3
        sub = ss.score_positioning(_sent(short_interest_pct=26.23), rsi14=55.0)
        self.assertEqual(sub["inputs"]["si_points"], 3)

    def test_si_null_is_0(self):
        sub = ss.score_positioning(_sent(short_interest_pct=None), rsi14=55.0)
        self.assertEqual(sub["inputs"]["si_points"], 0)

    def test_pc_balanced_is_6(self):
        # 0.9 in [0.7,1.1] -> 6
        sub = ss.score_positioning(_sent(put_call_ratio_full_chain=0.9),
                                   rsi14=55.0)
        self.assertEqual(sub["inputs"]["pc_points"], 6)

    def test_pc_call_heavy_is_3(self):
        # 0.5 < 0.7 -> 3 (call-heavy froth)
        sub = ss.score_positioning(_sent(put_call_ratio_full_chain=0.5),
                                   rsi14=55.0)
        self.assertEqual(sub["inputs"]["pc_points"], 3)
        self.assertIn("call-heavy froth", sub["arithmetic"])

    def test_pc_hedged_is_4(self):
        # 1.3 > 1.1 -> 4 (hedged/bearish tilt)
        sub = ss.score_positioning(_sent(put_call_ratio_full_chain=1.3),
                                   rsi14=55.0)
        self.assertEqual(sub["inputs"]["pc_points"], 4)
        self.assertIn("hedged", sub["arithmetic"])

    def test_pc_null_is_0(self):
        sub = ss.score_positioning(_sent(put_call_ratio_full_chain=None),
                                   rsi14=55.0)
        self.assertEqual(sub["inputs"]["pc_points"], 0)

    def test_iv_pctile_cheap_is_6_with_note(self):
        # < 25 -> 6, emit hedges-cheap note
        sub = ss.score_positioning(_sent(iv_pctile_1yr=15.0), rsi14=55.0)
        self.assertEqual(sub["inputs"]["iv_points"], 6)
        self.assertIn("hedges cheap", sub["arithmetic"])

    def test_iv_pctile_mid_is_4(self):
        # [25,75] -> 4
        sub = ss.score_positioning(_sent(iv_pctile_1yr=50.0), rsi14=55.0)
        self.assertEqual(sub["inputs"]["iv_points"], 4)

    def test_iv_pctile_rich_is_2(self):
        # > 75 -> 2
        sub = ss.score_positioning(_sent(iv_pctile_1yr=90.0), rsi14=55.0)
        self.assertEqual(sub["inputs"]["iv_points"], 2)

    def test_iv_pctile_null_is_0(self):
        sub = ss.score_positioning(_sent(iv_pctile_1yr=None), rsi14=55.0)
        self.assertEqual(sub["inputs"]["iv_points"], 0)

    def test_all_null_not_evaluable(self):
        sub = ss.score_positioning(
            _sent(short_interest_pct=None, put_call_ratio_full_chain=None,
                  iv_pctile_1yr=None), rsi14=None)
        self.assertFalse(sub["evaluable"])


# --------------------------------------------------------------------------- #
# 5. Price momentum (max 15): rel12 (7) + rel3 (5) + abs6 (3)
# --------------------------------------------------------------------------- #

class TestPriceMomentum(unittest.TestCase):
    def test_rel12_strong_is_7(self):
        # ret_12m 0.30 - spy 0.10 = 0.20 > 0.15 -> 7
        sub = ss.score_momentum(_tech(ret_12m=0.30), _bench(spy_ret_12m=0.10))
        self.assertEqual(sub["inputs"]["rel12_points"], 7)

    def test_rel12_positive_is_5(self):
        # 0.20 - 0.10 = 0.10 in (0,0.15] -> 5
        sub = ss.score_momentum(_tech(ret_12m=0.20), _bench(spy_ret_12m=0.10))
        self.assertEqual(sub["inputs"]["rel12_points"], 5)

    def test_rel12_mild_lag_is_2(self):
        # 0.05 - 0.10 = -0.05 in [-0.15,0] -> 2
        sub = ss.score_momentum(_tech(ret_12m=0.05), _bench(spy_ret_12m=0.10))
        self.assertEqual(sub["inputs"]["rel12_points"], 2)

    def test_rel12_deep_lag_is_0(self):
        # -0.10 - 0.10 = -0.20 < -0.15 -> 0
        sub = ss.score_momentum(_tech(ret_12m=-0.10), _bench(spy_ret_12m=0.10))
        self.assertEqual(sub["inputs"]["rel12_points"], 0)

    def test_rel3_strong_is_5(self):
        # 0.15 - 0.02 = 0.13 > 0.10 -> 5
        sub = ss.score_momentum(_tech(ret_3m=0.15), _bench(spy_ret_3m=0.02))
        self.assertEqual(sub["inputs"]["rel3_points"], 5)

    def test_rel3_positive_is_4(self):
        # 0.07 - 0.02 = 0.05 in (0,0.10] -> 4
        sub = ss.score_momentum(_tech(ret_3m=0.07), _bench(spy_ret_3m=0.02))
        self.assertEqual(sub["inputs"]["rel3_points"], 4)

    def test_rel3_nonpositive_is_1(self):
        # 0.01 - 0.02 = -0.01 <= 0 -> 1
        sub = ss.score_momentum(_tech(ret_3m=0.01), _bench(spy_ret_3m=0.02))
        self.assertEqual(sub["inputs"]["rel3_points"], 1)

    def test_abs6_positive_is_3(self):
        sub = ss.score_momentum(_tech(ret_6m=0.10), _bench())
        self.assertEqual(sub["inputs"]["abs6_points"], 3)

    def test_abs6_nonpositive_is_0(self):
        sub = ss.score_momentum(_tech(ret_6m=-0.05), _bench())
        self.assertEqual(sub["inputs"]["abs6_points"], 0)

    def test_rel12_null_component_0_na(self):
        sub = ss.score_momentum(_tech(ret_12m=None), _bench())
        self.assertEqual(sub["inputs"]["rel12_points"], 0)
        self.assertIn("n/a", sub["arithmetic"])

    def test_all_null_not_evaluable(self):
        sub = ss.score_momentum(
            _tech(ret_3m=None, ret_6m=None, ret_12m=None),
            _bench(spy_ret_3m=None, spy_ret_12m=None))
        self.assertFalse(sub["evaluable"])


# --------------------------------------------------------------------------- #
# Tables: positioning / momentum_vs_spy / hedging_cost_note
# --------------------------------------------------------------------------- #

class TestTables(unittest.TestCase):
    def test_positioning_table_verbatim(self):
        sent = {
            "short_interest_pct": 2.4, "si_trend": "rising",
            "si_as_of": "2026-07-10", "put_call_ratio_full_chain": 0.74,
            "put_call_ratio_realtime": 1.05, "iv30": 0.515,
            "iv_pctile_1yr": 91.7, "implied_move_next_earnings_pct": 0.127,
        }
        tbl = ss.build_positioning_table(sent)
        self.assertEqual(tbl["short_interest_pct"], 2.4)
        self.assertEqual(tbl["si_trend"], "rising")
        self.assertEqual(tbl["put_call_ratio_realtime"], 1.05)
        self.assertEqual(tbl["iv30"], 0.515)
        self.assertEqual(tbl["implied_move_next_earnings_pct"], 0.127)

    def test_momentum_table_rel_computed(self):
        tech = _tech(ret_3m=0.07, ret_6m=0.10, ret_12m=0.30)
        bench = _bench(spy_ret_3m=0.02, spy_ret_12m=0.10)
        tbl = ss.build_momentum_table(tech, bench)
        self.assertAlmostEqual(tbl["rel_3m"], 0.05, places=6)
        self.assertAlmostEqual(tbl["rel_12m"], 0.20, places=6)
        self.assertEqual(tbl["ret_6m"], 0.10)
        self.assertEqual(tbl["spy_ret_3m"], 0.02)

    def test_momentum_table_rel_null_when_missing(self):
        tech = _tech(ret_3m=None)
        bench = _bench()
        tbl = ss.build_momentum_table(tech, bench)
        self.assertIsNone(tbl["rel_3m"])

    def test_hedging_note_set_when_iv_cheap(self):
        note = ss.hedging_cost_note(15.0)
        self.assertIsNotNone(note)
        self.assertIn("options-strategy", note)

    def test_hedging_note_null_when_iv_not_cheap(self):
        self.assertIsNone(ss.hedging_cost_note(50.0))
        self.assertIsNone(ss.hedging_cost_note(None))


# --------------------------------------------------------------------------- #
# Composite scoring + renormalization
# --------------------------------------------------------------------------- #

class TestScore(unittest.TestCase):
    def _full(self):
        return {"sentiment": _sent(), "revisions": _rev(),
                "tech": _tech(), "bench": _bench()}

    def test_no_renormalization_when_all_dimensions_have_inputs(self):
        d = self._full()
        result = ss.score(d["sentiment"], d["revisions"], d["tech"], d["bench"],
                          "neutral", None, "neutral", None, "normal", None)
        self.assertFalse(result["renormalized"])
        maxes = sum(s["max"] for s in result["subscores"])
        self.assertEqual(maxes, 100)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_revisions_null_renormalizes(self):
        # revisions block entirely null -> that dimension (max 20) excluded.
        d = self._full()
        result = ss.score(d["sentiment"], None, d["tech"], d["bench"],
                          "neutral", None, "neutral", None, "normal", None)
        self.assertTrue(result["renormalized"])
        maxes = sum(s["max"] for s in result["subscores"])
        self.assertEqual(maxes, 80)

    def test_score_five_subscores(self):
        d = self._full()
        result = ss.score(d["sentiment"], d["revisions"], d["tech"], d["bench"],
                          "neutral", None, "neutral", None, "normal", None)
        self.assertEqual(len(result["subscores"]), 5)


# --------------------------------------------------------------------------- #
# INPUT_FIELDS / GUARD_FIELDS declaration
# --------------------------------------------------------------------------- #

class TestInputFields(unittest.TestCase):
    def test_input_fields_exact(self):
        self.assertEqual(ss.INPUT_FIELDS, {
            "sentiment.ratings", "sentiment.pt_vs_price_pct",
            "fundamentals.revisions_90d", "sentiment.insider_net_90d_usd",
            "sentiment.short_interest_pct",
            "sentiment.put_call_ratio_full_chain", "sentiment.iv_pctile_1yr",
            "technicals.ret_3m", "technicals.ret_6m", "technicals.ret_12m",
            "benchmark.spy_ret_3m", "benchmark.spy_ret_12m",
        })

    def test_guard_fields_exact(self):
        self.assertEqual(ss.GUARD_FIELDS, {"technicals.rsi14"})

    def test_guard_not_in_scored(self):
        self.assertFalse(ss.GUARD_FIELDS & ss.INPUT_FIELDS)

    def test_shared_reference_fields_not_listed(self):
        self.assertNotIn("price.last", ss.INPUT_FIELDS)


# --------------------------------------------------------------------------- #
# CLI end-to-end (real bundle, reuses test_build_snapshot fixtures)
# --------------------------------------------------------------------------- #

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "score_sentiment.py")


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
        out = os.path.join(self.dir, "module_sentiment.json")
        self.assertTrue(os.path.exists(out))
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["skill"], "sentiment-positioning")
        self.assertEqual(doc["rubric_version"], "1.0.0")
        self.assertEqual(doc["ticker"], "MU")
        self.assertIn("as_of", doc)
        self.assertIsInstance(doc["score"], (int, float))
        self.assertGreaterEqual(doc["score"], 0)
        self.assertLessEqual(doc["score"], 100)
        self.assertIsInstance(doc["subscores"], list)
        self.assertEqual(len(doc["subscores"]), 5)
        self.assertIn("positioning", doc["tables"])
        self.assertIn("momentum_vs_spy", doc["tables"])
        self.assertIn("hedging_cost_note", doc["tables"])
        self.assertIsNone(doc["signal"])
        # confidence-v1.0.0: well-formed block. Sentiment SOURCE is MEDIUM at best
        # (short_interest is a by-design web input), so overall <= MEDIUM.
        conf = doc["confidence"]
        self.assertEqual(set(conf),
                         {"level", "source", "depth", "staleness", "rule",
                          "version"})
        self.assertIn(conf["level"], ("LOW", "MEDIUM"))
        self.assertEqual(conf["version"], "1.0.0")
        self.assertIn(conf["source"]["level"], ("LOW", "MEDIUM"))
        for s in doc["subscores"]:
            self.assertIn("arithmetic", s)
            self.assertIn("inputs", s)
            self.assertIn("name", s)

    def test_rating_actions_without_justification_errors(self):
        proc = self._run(extra=["--rating-actions", "positive"])
        self.assertEqual(proc.returncode, 2)

    def test_rating_actions_with_justification_ok(self):
        proc = self._run(extra=["--rating-actions", "positive",
                                "--rating-actions-justification",
                                "3 upgrades this week"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, "module_sentiment.json")
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["flags"]["rating_actions"], "positive")
        self.assertEqual(doc["flags"]["rating_actions_justification"],
                         "3 upgrades this week")

    def test_inst_flow_without_justification_errors(self):
        proc = self._run(extra=["--inst-flow", "accumulating"])
        self.assertEqual(proc.returncode, 2)

    def test_inst_flow_with_justification_ok(self):
        proc = self._run(extra=["--inst-flow", "accumulating",
                                "--inst-flow-justification",
                                "13F net buys last quarter"])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_insider_baseline_unusual_without_justification_errors(self):
        proc = self._run(extra=["--insider-baseline", "unusual"])
        self.assertEqual(proc.returncode, 2)

    def test_insider_baseline_unusual_with_justification_ok(self):
        proc = self._run(extra=["--insider-baseline", "unusual",
                                "--insider-baseline-justification",
                                "cluster of CFO sales at the highs"])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_custom_out_path(self):
        out = os.path.join(self.dir, "custom_sentiment.json")
        proc = self._run(extra=["--out", out])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

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
