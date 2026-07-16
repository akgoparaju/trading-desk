"""Tests for scripts/trade_plan.py -- the trade-plan decision skill.

WHY: this is the L3 skill that turns the composite into an EXECUTABLE plan. Unlike
the evidence modules (which score the snapshot) and the composite (which rolls the
module scores into a call), trade-plan consumes the module outputs -- the composite's
EV block, the technical ladder, the risk downside_map, and the newest snapshot's
price/events/sentiment/options fields -- and mints a mechanical entry ladder, exits,
a both-leg invalidation, Kelly-arithmetic sizing, a hedge spec, and a preliminary
EXPRESSION decision (stock vs options). The expression decision table is a decision
of record (expression-v1.0.0): a catalyst in sight selects options for leverage; the
profile only implements. Its arithmetic (EV-at-level, Kelly sizing, required-multiple)
IS delegated to scripts.ev_kelly -- these tests pin the values ev_kelly returns for
the fixture and assert the plan reproduces them.

Pass 2 (--synthesize) re-reads the plan + module_options.json and folds the
options-strategy module's chosen structures into the expression.

stdlib-only; unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import trade_plan as tp
from scripts import ev_kelly


SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "trade_plan.py")

# Pinned fixture scenario set: targets 150/120/80, probs .25/.5/.25, last 100.
#   ev_at(100) = .25*(1.5-1) + .5*(1.2-1) + .25*(0.8-1) = .125 + .10 - .05 = 0.175
_SCENARIOS = [
    {"name": "bull", "prob": 0.25, "price_target": 150.0},
    {"name": "base", "prob": 0.50, "price_target": 120.0},
    {"name": "bear", "prob": 0.25, "price_target": 80.0},
]
_LAST = 100.0

# The fixture composite EV block (balanced hurdle 0.12). ev_at_current 0.175
# clears the 0.12 hurdle, so the ev>=hurdle branch is used only when we force it.
_HURDLE = 0.12
_EV_BREAKEVEN = 117.5 / 1.12   # = sum(p*t)/(1+hurdle)


# --------------------------------------------------------------------------- #
# Fixture bundle builders.
# --------------------------------------------------------------------------- #

def _composite_doc(ev_at_current=0.05, profile="balanced"):
    """A module_composite.json with the pinned EV block + scenarios.

    Default ev_at_current 0.05 is BELOW the balanced hurdle 0.12, so the default
    fixture exercises the confluence entry branch (entry_1 = the 95 confluence). The
    ev>=hurdle sized-down branch is exercised explicitly by overriding to 0.20.
    """
    return {
        "skill": "composite-score",
        "rubric_version": "1.0.0",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "profile": profile,
        "score": 59.9,
        "grade": "C",
        "action": "Hold/Trim",
        "ev": {
            "scenarios": _SCENARIOS,
            "scenario_reasoning": "HBM demand asymmetric",
            "ev_at_current": ev_at_current,
            "hurdle_total": _HURDLE,
            "horizon_years_convention": 1.5,
            "ev_breakeven_entry": round(_EV_BREAKEVEN, 4),
            "ev_at_levels": [],
        },
    }


def _technical_doc():
    """A module_technical.json with a ladder: proven supports 95/90/82 below last
    100, resistance 112 above."""
    ladder = [
        {"level": 82.0, "type": "swing_low", "basis": "ohlcv", "pct_from_last": -0.18},
        {"level": 90.0, "type": "ma200", "basis": "ohlcv", "pct_from_last": -0.10},
        {"level": 95.0, "type": "swing_low", "basis": "ohlcv", "pct_from_last": -0.05},
        {"level": 112.0, "type": "swing_high", "basis": "ohlcv", "pct_from_last": 0.12},
    ]
    return {
        "skill": "technical-analysis",
        "rubric_version": "1.0.0",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "score": 70,
        "ladder": ladder,
    }


def _risk_doc():
    """A module_risk.json with a NEAREST-FIRST downside_map incl. a valuation_floor
    row at 94.0 (confluent with the 95 swing_low)."""
    downside_map = [
        {"level": 95.0, "type": "swing_low", "basis": "ohlcv", "pct_from_last": -0.05},
        {"level": 94.0, "type": "valuation_floor", "basis": "valuation",
         "method": "pe_5yr_median x eps_ntm", "pct_from_last": -0.06},
        {"level": 90.0, "type": "ma200", "basis": "ohlcv", "pct_from_last": -0.10},
        {"level": 82.0, "type": "swing_low", "basis": "ohlcv", "pct_from_last": -0.18},
    ]
    return {
        "skill": "risk-analytics",
        "rubric_version": "1.0.0",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "score": 40,
        "tables": {"downside_map": downside_map},
    }


def _snapshot_doc(earnings_date="2026-07-30", iv_pctile=20.0, iv_minus_rv=None,
                  eps_ntm=5.5, last=_LAST):
    """A snapshot stub: last 100, earnings 14d out (2026-07-16 -> 2026-07-30),
    iv_pctile 20, eps_ntm 5.5. options may be None (iv_minus_rv null-safe)."""
    options = None
    if iv_minus_rv is not None:
        options = {"iv_minus_rv20": iv_minus_rv}
    ne = {"date": earnings_date, "time": "post-market", "consensus_eps": 1.9} \
        if earnings_date else None
    return {
        "meta": {"ticker": "MU", "as_of_utc": "2026-07-16T00:00:00Z"},
        "price": {"last": last},
        "events": {"next_earnings": ne},
        "sentiment": {"iv_pctile_1yr": iv_pctile},
        "options": options,
        "fundamentals": {"eps_ntm_consensus": eps_ntm},
    }


def _write_bundle(dir_, composite=None, technical=None, risk=None, snapshot=None):
    """Write the four module inputs into a bundle dir. None -> omit that file."""
    if composite is not None:
        with open(os.path.join(dir_, "module_composite.json"), "w") as fh:
            json.dump(composite, fh)
    if technical is not None:
        with open(os.path.join(dir_, "module_technical.json"), "w") as fh:
            json.dump(technical, fh)
    if risk is not None:
        with open(os.path.join(dir_, "module_risk.json"), "w") as fh:
            json.dump(risk, fh)
    if snapshot is not None:
        with open(os.path.join(dir_, "snapshot_MU_2026-07-16.json"), "w") as fh:
            json.dump(snapshot, fh)


def _full_bundle(dir_, **snap_over):
    _write_bundle(dir_,
                  composite=_composite_doc(),
                  technical=_technical_doc(),
                  risk=_risk_doc(),
                  snapshot=_snapshot_doc(**snap_over))


# --------------------------------------------------------------------------- #
# days_to_catalyst / binary-event helpers.
# --------------------------------------------------------------------------- #

class TestDaysToCatalyst(unittest.TestCase):
    def test_days_within_range(self):
        # as_of 2026-07-16, earnings 2026-07-30 -> 14 days.
        self.assertEqual(tp.days_to_catalyst("2026-07-16", "2026-07-30"), 14)

    def test_days_none_when_no_earnings(self):
        self.assertIsNone(tp.days_to_catalyst("2026-07-16", None))

    def test_days_none_when_unparseable(self):
        self.assertIsNone(tp.days_to_catalyst("2026-07-16", "not-a-date"))

    def test_binary_event_within_30d_true(self):
        self.assertTrue(tp.binary_event_within_30d(14))

    def test_binary_event_within_30d_false_far(self):
        self.assertFalse(tp.binary_event_within_30d(75))

    def test_binary_event_within_30d_false_none(self):
        self.assertFalse(tp.binary_event_within_30d(None))


# --------------------------------------------------------------------------- #
# Entry ladder: confluence, EV-at-level, spacing, ev>=hurdle branch.
# --------------------------------------------------------------------------- #

class TestEntryLadder(unittest.TestCase):
    def setUp(self):
        self.ev = _composite_doc()["ev"]
        self.ladder = _technical_doc()["ladder"]
        self.downside = _risk_doc()["tables"]["downside_map"]

    def test_confluence_95_over_94_true(self):
        # valuation anchor 94.0, candidate support 95.0 -> 95/94-1 = 1.06% <= 3%.
        anchors = tp.valuation_anchors(self.ev, self.downside)
        self.assertIn(94.0, anchors)
        conf, anchor = tp.confluence_for(95.0, anchors)
        self.assertTrue(conf)
        self.assertEqual(anchor, 94.0)

    def test_entry_1_is_confluent_support_95(self):
        entries = tp.build_entries(_LAST, self.ladder, self.ev, self.downside)
        self.assertEqual(entries[0]["level"], 95.0)
        self.assertTrue(entries[0]["confluence"])
        self.assertEqual(entries[0]["confluence_anchor"], 94.0)
        self.assertFalse(entries[0].get("sized_down", False))

    def test_entry_1_ev_at_level_exact(self):
        # ev_at(scen, 95) = .25*(150/95-1) + .5*(120/95-1) + .25*(80/95-1).
        entries = tp.build_entries(_LAST, self.ladder, self.ev, self.downside)
        expect = round(ev_kelly.ev_at(_SCENARIOS, 95.0), 4)
        self.assertAlmostEqual(entries[0]["ev_at_level"], expect, places=4)

    def test_entries_spacing_at_least_3pct(self):
        # entry_1 95 -> next must be <= 95*0.97 = 92.15 (90 qualifies, 94 does not).
        entries = tp.build_entries(_LAST, self.ladder, self.ev, self.downside)
        levels = [e["level"] for e in entries]
        self.assertEqual(len(levels), len(set(levels)))
        for i in range(len(levels) - 1):
            self.assertLessEqual(levels[i + 1], levels[i] * 0.97 + 1e-9)

    def test_entries_max_three(self):
        entries = tp.build_entries(_LAST, self.ladder, self.ev, self.downside)
        self.assertLessEqual(len(entries), 3)

    def test_entry_2_is_90_ma200(self):
        # after 95, 90 is the next proven support >=3% below (94 too close).
        entries = tp.build_entries(_LAST, self.ladder, self.ev, self.downside)
        self.assertEqual(entries[1]["level"], 90.0)

    def test_ev_clears_hurdle_branch_sizes_down_at_current(self):
        # force ev_at_current 0.20 >= hurdle 0.12 -> entry_1 = last, sized_down.
        ev = _composite_doc(ev_at_current=0.20)["ev"]
        entries = tp.build_entries(_LAST, self.ladder, ev, self.downside)
        self.assertEqual(entries[0]["level"], _LAST)
        self.assertTrue(entries[0]["sized_down"])
        expect = round(ev_kelly.ev_at(_SCENARIOS, _LAST), 4)
        self.assertAlmostEqual(entries[0]["ev_at_level"], expect, places=4)

    def test_ev_below_hurdle_does_not_size_down(self):
        # ev_at_current 0.05 < hurdle 0.12 -> entry_1 is a confluence, not current.
        ev = _composite_doc(ev_at_current=0.05)["ev"]
        entries = tp.build_entries(_LAST, self.ladder, ev, self.downside)
        self.assertEqual(entries[0]["level"], 95.0)


# --------------------------------------------------------------------------- #
# Exits: profit_take (nearest resistance) + bull_target (required_multiple).
# --------------------------------------------------------------------------- #

class TestExits(unittest.TestCase):
    def test_profit_take_nearest_resistance(self):
        exits = tp.build_exits(_LAST, _technical_doc()["ladder"], _SCENARIOS, 5.5)
        self.assertEqual(exits["profit_take"]["level"], 112.0)
        self.assertEqual(exits["profit_take"]["type"], "swing_high")

    def test_bull_target_required_multiple(self):
        # max scenario target = 150; required_multiple = 150 / 5.5.
        exits = tp.build_exits(_LAST, _technical_doc()["ladder"], _SCENARIOS, 5.5)
        self.assertEqual(exits["bull_target"]["level"], 150.0)
        self.assertAlmostEqual(exits["bull_target"]["required_multiple"],
                               150.0 / 5.5, places=4)

    def test_bull_target_required_multiple_null_safe(self):
        # eps_ntm None -> required_multiple None, no crash.
        exits = tp.build_exits(_LAST, _technical_doc()["ladder"], _SCENARIOS, None)
        self.assertEqual(exits["bull_target"]["level"], 150.0)
        self.assertIsNone(exits["bull_target"]["required_multiple"])


# --------------------------------------------------------------------------- #
# Invalidation: BOTH legs mandatory.
# --------------------------------------------------------------------------- #

class TestInvalidation(unittest.TestCase):
    def test_technical_leg_below_entry_2(self):
        # entries 95, 90 -> first proven support strictly below entry_2 (90) = 82.
        entries = tp.build_entries(_LAST, _technical_doc()["ladder"],
                                   _composite_doc()["ev"],
                                   _risk_doc()["tables"]["downside_map"])
        inval = tp.build_invalidation(entries, _technical_doc()["ladder"],
                                      "GM stalls", "below 35%", "thesis pillar")
        self.assertEqual(inval["technical_leg"]["level"], 82.0)
        self.assertEqual(inval["technical_leg"]["condition"], "weekly close below")

    def test_fundamental_leg_from_flags(self):
        entries = tp.build_entries(_LAST, _technical_doc()["ladder"],
                                   _composite_doc()["ev"],
                                   _risk_doc()["tables"]["downside_map"])
        inval = tp.build_invalidation(entries, _technical_doc()["ladder"],
                                      "HBM rev growth", "< 20% 2 quarters",
                                      "core thesis pillar")
        fl = inval["fundamental_leg"]
        self.assertEqual(fl["metric"], "HBM rev growth")
        self.assertEqual(fl["threshold"], "< 20% 2 quarters")
        self.assertEqual(fl["justification"], "core thesis pillar")

    def test_technical_leg_below_entry_1_when_single_entry(self):
        # single entry -> technical leg below entry_1.
        single = [{"level": 95.0, "type": "swing_low"}]
        inval = tp.build_invalidation(single, _technical_doc()["ladder"],
                                      "m", "t", "j")
        # first proven support strictly below 95 = 90.
        self.assertEqual(inval["technical_leg"]["level"], 90.0)


# --------------------------------------------------------------------------- #
# Sizing: full Kelly arithmetic via ev_kelly.
# --------------------------------------------------------------------------- #

class TestSizing(unittest.TestCase):
    def test_sizing_matches_ev_kelly(self):
        # entry_1 level 95, balanced, binary30d True (earnings 14d).
        k = ev_kelly.kelly(_SCENARIOS, 95.0)
        s = ev_kelly.size_recommendation(k["f_star"], "balanced", True)
        sizing = tp.build_sizing(_SCENARIOS, 95.0, "balanced", True)
        # Stored values pass through _clean (4-dp stable-JSON convention), so
        # compare at 4 places -- the project-wide precision for module JSON.
        self.assertAlmostEqual(sizing["f_star"], k["f_star"], places=4)
        self.assertAlmostEqual(sizing["recommended_pct"], s["recommended_pct"],
                               places=4)
        self.assertAlmostEqual(sizing["cap_pct"], s["cap_pct"], places=4)

    def test_sizing_arithmetic_string_present(self):
        sizing = tp.build_sizing(_SCENARIOS, 95.0, "balanced", True)
        self.assertIn("f*", sizing["arithmetic"])
        self.assertIn("binary", sizing["arithmetic"].lower())


# --------------------------------------------------------------------------- #
# Hedge: fires on BOTH clauses independently, and not otherwise.
# --------------------------------------------------------------------------- #

class TestHedge(unittest.TestCase):
    def setUp(self):
        self.downside = _risk_doc()["tables"]["downside_map"]

    def test_hedge_iv_pctile_alone(self):
        # iv_pctile 20 <= 25, binary30d False, size below 5% -> hedge fires (iv clause).
        hedge = tp.build_hedge(binary30d=False, recommended_pct=0.02,
                               iv_pctile=20.0, downside_map=self.downside)
        self.assertTrue(hedge["required"])
        self.assertIn("iv", hedge["trigger"].lower())

    def test_hedge_binary_and_size_alone(self):
        # iv_pctile 60 (>25), binary30d True + size 0.06 >= 0.05 -> hedge fires.
        hedge = tp.build_hedge(binary30d=True, recommended_pct=0.06,
                               iv_pctile=60.0, downside_map=self.downside)
        self.assertTrue(hedge["required"])
        self.assertIn("binary", hedge["trigger"].lower())

    def test_hedge_not_fired(self):
        # iv_pctile 60, binary30d False -> neither clause -> not required.
        hedge = tp.build_hedge(binary30d=False, recommended_pct=0.06,
                               iv_pctile=60.0, downside_map=self.downside)
        self.assertFalse(hedge["required"])

    def test_hedge_not_fired_binary_but_size_too_small(self):
        # binary30d True but size 0.04 < 0.05, iv_pctile 60 -> not required.
        hedge = tp.build_hedge(binary30d=True, recommended_pct=0.04,
                               iv_pctile=60.0, downside_map=self.downside)
        self.assertFalse(hedge["required"])

    def test_hedge_iv_pctile_none_no_iv_clause(self):
        # iv_pctile None must NOT fire the iv clause (null-safe).
        hedge = tp.build_hedge(binary30d=False, recommended_pct=0.02,
                               iv_pctile=None, downside_map=self.downside)
        self.assertFalse(hedge["required"])

    def test_hedge_spec_fields_when_required(self):
        hedge = tp.build_hedge(binary30d=False, recommended_pct=0.02,
                               iv_pctile=20.0, downside_map=self.downside)
        self.assertEqual(hedge["structure"], "put spread or collar")
        # strikes_from = first two downside_map rows (levels).
        self.assertEqual(hedge["strikes_from"], [95.0, 94.0])
        self.assertEqual(hedge["premium_cap_pct"], 0.015)
        self.assertIn("monthly", hedge["expiry_rule"])


# --------------------------------------------------------------------------- #
# Don't-chase.
# --------------------------------------------------------------------------- #

class TestDontChase(unittest.TestCase):
    def test_dont_chase_5pct_above_top_entry(self):
        dc = tp.build_dont_chase(95.0)
        self.assertAlmostEqual(dc["above"], 95.0 * 1.05, places=6)
        self.assertIn("5%", dc["convention"])


# --------------------------------------------------------------------------- #
# Expression decision table (expression-v1.0.0).
# --------------------------------------------------------------------------- #

class TestExpression(unittest.TestCase):
    def test_rule1_catalyst_selector_longterm_mentions_options_kicker(self):
        # days 14 <= 60, catalyst in thesis yes -> selector "catalyst".
        exp = tp.decide_expression(days_to_catalyst=14, catalyst_in_thesis=True,
                                   profile="long-term", iv_minus_rv=None)
        self.assertEqual(exp["selector_fired"], "catalyst")
        self.assertEqual(exp["rule_version"], "expression-v1.0.0")
        self.assertIn("kicker", exp["mode_per_profile"]["long-term"].lower())
        self.assertIn("option", exp["mode_per_profile"]["long-term"].lower())
        self.assertEqual(exp["recommended_for_profile"],
                         exp["mode_per_profile"]["long-term"])

    def test_rule1_all_profiles_options_tilted(self):
        exp = tp.decide_expression(days_to_catalyst=14, catalyst_in_thesis=True,
                                   profile="trader", iv_minus_rv=None)
        self.assertEqual(exp["selector_fired"], "catalyst")
        self.assertIn("spread", exp["mode_per_profile"]["trader"].lower())
        self.assertIn("stock", exp["mode_per_profile"]["balanced"].lower())

    def test_rule2_default_when_catalyst_far(self):
        # days 75 > 60 -> profile-default.
        exp = tp.decide_expression(days_to_catalyst=75, catalyst_in_thesis=True,
                                   profile="balanced", iv_minus_rv=None)
        self.assertEqual(exp["selector_fired"], "profile-default")
        self.assertIn("mixed", exp["mode_per_profile"]["balanced"].lower())

    def test_rule2_default_when_not_in_thesis(self):
        # in-thesis no + days 14 -> profile-default (selector needs BOTH).
        exp = tp.decide_expression(days_to_catalyst=14, catalyst_in_thesis=False,
                                   profile="balanced", iv_minus_rv=None)
        self.assertEqual(exp["selector_fired"], "profile-default")

    def test_default_when_days_none(self):
        exp = tp.decide_expression(days_to_catalyst=None, catalyst_in_thesis=True,
                                   profile="trader", iv_minus_rv=None)
        self.assertEqual(exp["selector_fired"], "profile-default")

    def test_modulator_iv_rich_selling(self):
        # iv_minus_rv +0.06 >= +0.05 -> premium-selling modulator.
        exp = tp.decide_expression(days_to_catalyst=75, catalyst_in_thesis=False,
                                   profile="balanced", iv_minus_rv=0.06)
        joined = " ".join(exp["modulators"]).lower()
        self.assertIn("selling", joined)
        self.assertIn("rich", joined)

    def test_modulator_iv_cheap_long_premium(self):
        exp = tp.decide_expression(days_to_catalyst=75, catalyst_in_thesis=False,
                                   profile="balanced", iv_minus_rv=-0.06)
        joined = " ".join(exp["modulators"]).lower()
        self.assertIn("cheap", joined)
        self.assertIn("long-premium", joined)

    def test_modulator_defined_risk_into_event(self):
        # days 14 <= 30 -> defined-risk-only modulator appended.
        exp = tp.decide_expression(days_to_catalyst=14, catalyst_in_thesis=True,
                                   profile="trader", iv_minus_rv=None)
        joined = " ".join(exp["modulators"]).lower()
        self.assertIn("defined-risk", joined)

    def test_modulator_order_selling_then_defined_risk(self):
        # iv rich AND <=30d -> selling modulator first, defined-risk second.
        exp = tp.decide_expression(days_to_catalyst=14, catalyst_in_thesis=True,
                                   profile="trader", iv_minus_rv=0.06)
        mods = [m.lower() for m in exp["modulators"]]
        self.assertTrue(any("selling" in m for m in mods))
        self.assertTrue(any("defined-risk" in m for m in mods))
        sell_idx = next(i for i, m in enumerate(mods) if "selling" in m)
        dr_idx = next(i for i, m in enumerate(mods) if "defined-risk" in m)
        self.assertLess(sell_idx, dr_idx)

    def test_no_modulators_when_none_apply(self):
        exp = tp.decide_expression(days_to_catalyst=75, catalyst_in_thesis=False,
                                   profile="balanced", iv_minus_rv=0.0)
        self.assertEqual(exp["modulators"], [])

    def test_days_to_catalyst_carried(self):
        exp = tp.decide_expression(days_to_catalyst=14, catalyst_in_thesis=True,
                                   profile="trader", iv_minus_rv=None)
        self.assertEqual(exp["days_to_catalyst"], 14)


# --------------------------------------------------------------------------- #
# Pass 1 CLI end-to-end.
# --------------------------------------------------------------------------- #

def _base_fund_flags():
    return [
        "--catalyst-in-thesis", "yes",
        "--catalyst-in-thesis-justification", "HBM ramp is the asymmetric driver",
        "--fund-invalidation-metric", "HBM revenue growth",
        "--fund-invalidation-threshold", "< 20% for 2 consecutive quarters",
        "--fund-invalidation-justification", "core thesis pillar",
    ]


class TestStockPlanCLI(unittest.TestCase):
    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        _full_bundle(self.dir)

    def _run(self, extra=None, base=True):
        cmd = [sys.executable, SCRIPT, "--stock-plan", "--bundle", self.dir]
        if base:
            cmd += _base_fund_flags()
        if extra:
            cmd += extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def _read(self):
        with open(os.path.join(self.dir, "module_tradeplan.json")) as fh:
            return json.load(fh)

    def test_cli_exit0_writes_module(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        doc = self._read()
        self.assertEqual(doc["skill"], "trade-plan")
        self.assertEqual(doc["rubric_version"], "1.0.0")
        self.assertEqual(doc["ticker"], "MU")
        self.assertEqual(doc["as_of"], "2026-07-16")
        self.assertEqual(doc["profile"], "balanced")
        self.assertIsNone(doc["event_playbook"])
        self.assertIsNone(doc["signal"])

    def test_cli_entry_1_confluence(self):
        self._run()
        doc = self._read()
        entries = doc["stock_plan"]["entries"]
        self.assertEqual(entries[0]["level"], 95.0)
        self.assertTrue(entries[0]["confluence"])

    def test_cli_invalidation_both_legs(self):
        self._run()
        doc = self._read()
        inval = doc["stock_plan"]["invalidation"]
        self.assertIn("technical_leg", inval)
        self.assertIn("fundamental_leg", inval)
        self.assertEqual(inval["fundamental_leg"]["metric"], "HBM revenue growth")

    def test_cli_hedge_fires_iv_and_binary(self):
        # iv_pctile 20 (iv clause) fires; binary30d True; default profile balanced.
        self._run()
        doc = self._read()
        self.assertTrue(doc["stock_plan"]["hedge"]["required"])

    def test_cli_dont_chase(self):
        self._run()
        doc = self._read()
        top = doc["stock_plan"]["entries"][0]["level"]
        self.assertAlmostEqual(doc["stock_plan"]["dont_chase"]["above"],
                               top * 1.05, places=6)

    def test_cli_expression_catalyst_selector(self):
        self._run()
        doc = self._read()
        exp = doc["expression"]
        self.assertEqual(exp["selector_fired"], "catalyst")
        self.assertEqual(exp["rule_version"], "expression-v1.0.0")

    def test_cli_profile_longterm_options_kicker(self):
        self._run(extra=["--profile", "long-term"])
        doc = self._read()
        self.assertIn("kicker",
                      doc["expression"]["mode_per_profile"]["long-term"].lower())

    def test_cli_expression_default_when_earnings_far(self):
        # rebuild snapshot with earnings 75 days out.
        _write_bundle(self.dir, snapshot=_snapshot_doc(earnings_date="2026-09-29"))
        self._run()
        doc = self._read()
        self.assertEqual(doc["expression"]["selector_fired"], "profile-default")

    def test_cli_expression_default_when_not_in_thesis(self):
        proc = self._run(base=False, extra=[
            "--catalyst-in-thesis", "no",
            "--catalyst-in-thesis-justification", "thesis is structural, not event",
            "--fund-invalidation-metric", "HBM revenue growth",
            "--fund-invalidation-threshold", "< 20% 2q",
            "--fund-invalidation-justification", "pillar",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        self.assertEqual(doc["expression"]["selector_fired"], "profile-default")

    def test_cli_modulator_iv_rich(self):
        _write_bundle(self.dir, snapshot=_snapshot_doc(iv_minus_rv=0.06))
        self._run()
        doc = self._read()
        joined = " ".join(doc["expression"]["modulators"]).lower()
        self.assertIn("selling", joined)

    def test_cli_modulator_defined_risk_into_event(self):
        # earnings 14d out -> <=30d modulator present.
        self._run()
        doc = self._read()
        joined = " ".join(doc["expression"]["modulators"]).lower()
        self.assertIn("defined-risk", joined)

    def test_cli_ev_clears_hurdle_sizes_down(self):
        # composite with ev_at_current 0.20 >= hurdle 0.12.
        _write_bundle(self.dir, composite=_composite_doc(ev_at_current=0.20))
        self._run()
        doc = self._read()
        self.assertEqual(doc["stock_plan"]["entries"][0]["level"], _LAST)
        self.assertTrue(doc["stock_plan"]["entries"][0]["sized_down"])

    def test_cli_bull_target_required_multiple(self):
        self._run()
        doc = self._read()
        bt = doc["stock_plan"]["exits"]["bull_target"]
        self.assertAlmostEqual(bt["required_multiple"], 150.0 / 5.5, places=4)

    def test_cli_missing_composite_exit2(self):
        os.remove(os.path.join(self.dir, "module_composite.json"))
        proc = self._run()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("composite", proc.stderr.lower())

    def test_cli_missing_catalyst_in_thesis_exit2(self):
        proc = self._run(base=False, extra=[
            "--fund-invalidation-metric", "m",
            "--fund-invalidation-threshold", "t",
            "--fund-invalidation-justification", "j",
        ])
        self.assertEqual(proc.returncode, 2)

    def test_cli_missing_fund_invalidation_metric_exit2(self):
        proc = self._run(base=False, extra=[
            "--catalyst-in-thesis", "yes",
            "--catalyst-in-thesis-justification", "j",
            "--fund-invalidation-threshold", "t",
            "--fund-invalidation-justification", "j",
        ])
        self.assertEqual(proc.returncode, 2)

    def test_cli_default_profile_from_composite(self):
        # composite profile is balanced; no --profile flag -> balanced.
        self._run()
        doc = self._read()
        self.assertEqual(doc["profile"], "balanced")

    def test_cli_profile_flag_overrides_composite(self):
        _write_bundle(self.dir, composite=_composite_doc(profile="trader"))
        self._run(extra=["--profile", "long-term"])
        doc = self._read()
        self.assertEqual(doc["profile"], "long-term")

    def test_cli_determinism(self):
        p1 = self._run()
        with open(os.path.join(self.dir, "module_tradeplan.json")) as fh:
            a = fh.read()
        p2 = self._run()
        with open(os.path.join(self.dir, "module_tradeplan.json")) as fh:
            b = fh.read()
        self.assertEqual(p1.returncode, 0)
        self.assertEqual(p2.returncode, 0)
        self.assertEqual(a, b)


# --------------------------------------------------------------------------- #
# Pass 2: --synthesize.
# --------------------------------------------------------------------------- #

def _options_doc(structures=None):
    """A stub module_options.json with recommended structures carrying strikes."""
    if structures is None:
        structures = [
            {"name": "bull call spread", "strikes": [100.0, 115.0],
             "expiry": "2026-09-18"},
            {"name": "cash-secured put", "strikes": [95.0], "expiry": "2026-08-21"},
        ]
    return {
        "skill": "options-strategy",
        "ticker": "MU",
        "recommended_structures": structures,
        "hedge": {"name": "put spread", "strikes": [95.0, 90.0],
                  "expiry": "2026-08-21"},
    }


class TestSynthesizeCLI(unittest.TestCase):
    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        _full_bundle(self.dir)
        # run pass 1 first to produce module_tradeplan.json.
        cmd = [sys.executable, SCRIPT, "--stock-plan", "--bundle", self.dir]
        cmd += _base_fund_flags()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _write_options(self, doc=None):
        with open(os.path.join(self.dir, "module_options.json"), "w") as fh:
            json.dump(doc if doc is not None else _options_doc(), fh)

    def _synth(self):
        cmd = [sys.executable, SCRIPT, "--synthesize", "--bundle", self.dir]
        return subprocess.run(cmd, capture_output=True, text=True)

    def _read(self):
        with open(os.path.join(self.dir, "module_tradeplan.json")) as fh:
            return json.load(fh)

    def test_synthesize_missing_options_exit2(self):
        proc = self._synth()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("options", proc.stderr.lower())

    def test_synthesize_happy_path(self):
        self._write_options()
        proc = self._synth()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        exp = doc["expression"]
        self.assertTrue(exp["synthesized"])
        self.assertTrue(len(exp["structures_selected"]) >= 1)
        # hedge required (iv_pctile 20) -> hedge_structure folded in.
        self.assertIsNotNone(exp["hedge_structure"])

    def test_synthesize_preserves_stock_plan(self):
        self._write_options()
        self._synth()
        doc = self._read()
        # entries unchanged from pass 1.
        self.assertEqual(doc["stock_plan"]["entries"][0]["level"], 95.0)


if __name__ == "__main__":
    unittest.main()
