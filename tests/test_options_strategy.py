"""Tests for scripts/options_strategy.py -- the options-strategy skill (L3).

WHY: this is the L3 skill that turns a direction + a REAL options chain into concrete
defined-risk structures with SHOWN arithmetic and HONEST probabilities. Unlike the
evidence modules (which score the snapshot) and the composite/trade-plan (which decide
the call and mint the stock plan), options-strategy selects option STRUCTURES off the
chosen expiry of the actual chain -- real strikes only, economics minted from chain
marks, PoP as a labeled delta approximation, and a battery of MECHANICAL honesty gates.

THE CENTRAL LESSON THIS MODULE ENCODES (MU 2026-07-15 prototype): IV LEVEL alone never
selects a strategy. IV-vs-REALIZED is the primary gate -- a 96% IV that sits 14 pts
BELOW 110-116% realized is CHEAP, not rich, and a naive "sell premium" call would have
been wrong. These tests pin that gate and every downstream honesty warning it drives.

The chain is loaded ONLY via scripts.chain.load_contracts (never into LLM context).
These tests build a rich synthetic chain fixture (~50 contracts across two expiries,
puts/calls strikes 70-130 around spot 100 with plausible monotone deltas, marks, and
mixed OI including an illiquid strike and a wide-spread strike) so the delta-targeted
strike picks and the per-structure economics are EXACT by construction.

stdlib-only; unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import options_strategy as opts


SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "options_strategy.py")

_SPOT = 100.0
_NEAR_EXP = "2026-08-21"   # ~45-DTE, monthlyish (3rd-Friday: day 21, a Friday)
_FAR_EXP = "2026-09-25"    # ~75-DTE, monthlyish (day 25 -> not 15-21; used as far leg)


# --------------------------------------------------------------------------- #
# Synthetic chain fixture.
# --------------------------------------------------------------------------- #
#
# Deltas are constructed monotone and symmetric so the delta-targeted picks land on
# KNOWN strikes:
#   PUTS  (|delta| grows as strike rises toward/through spot):
#       strike 105 |Δ|~0.58, 100 ~0.50, 95 ~0.42, 90 ~0.30 (<- short-put -0.30Δ target),
#       85 ~0.22, 80 ~0.15, 75 ~0.10, 70 ~0.06
#   CALLS (delta grows as strike falls toward/through spot):
#       strike 95 ~0.58 (<- long-call +0.55Δ target), 100 ~0.50, 105 ~0.42,
#       110 ~0.30 (<- short-call +0.30Δ target), 115 ~0.22, 120 ~0.15, 125 ~0.10, 130 ~0.06
#
# So by construction at _NEAR_EXP:
#   short put  = 90  (|Δ| 0.30)         long put = 85 (next lower, 5-wide)
#   short call = 110 (Δ 0.30)           long call (+0.55Δ) = 95
#   condor put side short 85 (|Δ| 0.22) ... call side short 115 (Δ 0.22) [0.25-0.30 band edge]

def _put(strike, delta_abs, mark, oi=500, bid=None, ask=None, iv=0.55,
         expiration=_NEAR_EXP, volume=200):
    c = {"expiration": expiration, "type": "put", "strike": float(strike),
         "delta": -abs(delta_abs), "mark": float(mark), "iv": iv, "oi": oi,
         "volume": volume}
    if bid is not None:
        c["bid"] = bid
    if ask is not None:
        c["ask"] = ask
    return c


def _call(strike, delta, mark, oi=500, bid=None, ask=None, iv=0.55,
          expiration=_NEAR_EXP, volume=200):
    c = {"expiration": expiration, "type": "call", "strike": float(strike),
         "delta": abs(delta), "mark": float(mark), "iv": iv, "oi": oi,
         "volume": volume}
    if bid is not None:
        c["bid"] = bid
    if ask is not None:
        c["ask"] = ask
    return c


def _near_puts():
    # (strike, |delta|, mark). Marks monotone decreasing as strike falls (OTM cheaper).
    rows = [
        (105, 0.58, 8.50),
        (100, 0.50, 5.80),
        (95, 0.42, 3.90),
        (90, 0.30, 2.40),   # <- short put -0.30 delta target
        (85, 0.22, 1.50),   # <- long put (5-wide below 90); also condor put short (0.22)
        (80, 0.15, 0.90),
        (75, 0.10, 0.55),
        (70, 0.06, 0.30),
    ]
    return [_put(s, d, m) for s, d, m in rows]


def _near_calls():
    rows = [
        (95, 0.58, 7.90),   # <- long call +0.55 delta target
        (100, 0.50, 5.40),
        (105, 0.42, 3.60),
        (110, 0.30, 2.20),  # <- short call +0.30 delta target
        (115, 0.22, 1.35),  # <- condor call short (0.22)
        (120, 0.15, 0.80),
        (125, 0.10, 0.48),
        (130, 0.06, 0.26),
    ]
    return [_call(s, d, m) for s, d, m in rows]


def _far_puts():
    rows = [(105, 0.58, 10.5), (100, 0.50, 7.5), (95, 0.42, 5.2),
            (90, 0.30, 3.3), (85, 0.22, 2.1), (80, 0.15, 1.3)]
    return [_put(s, d, m, expiration=_FAR_EXP) for s, d, m in rows]


def _far_calls():
    rows = [(95, 0.58, 9.8), (100, 0.50, 7.0), (105, 0.42, 4.9),
            (110, 0.30, 3.1), (115, 0.22, 1.9), (120, 0.15, 1.2)]
    return [_call(s, d, m, expiration=_FAR_EXP) for s, d, m in rows]


def _chain():
    """Full synthetic chain: near + far expiries, all liquid by default."""
    return _near_puts() + _near_calls() + _far_puts() + _far_calls()


def _chain_with_illiquid():
    """Chain where the long-put strike (85) is ILLIQUID (oi 5) -> a leg failure."""
    chain = _chain()
    for c in chain:
        if c["expiration"] == _NEAR_EXP and c["type"] == "put" and c["strike"] == 85.0:
            c["oi"] = 5
    return chain


def _chain_with_wide_spread():
    """Chain where the short-put strike (90) has a WIDE bid/ask -> a leg failure."""
    chain = _chain()
    for c in chain:
        if c["expiration"] == _NEAR_EXP and c["type"] == "put" and c["strike"] == 90.0:
            c["bid"] = 1.00
            c["ask"] = 4.00   # spread 3.00 >> max(0.10, 0.10*mark=0.24)
    return chain


# --------------------------------------------------------------------------- #
# Snapshot fixture.
# --------------------------------------------------------------------------- #

def _expected_moves(near_one_sigma=8.0):
    """expected_moves passthrough. near one_sigma controls the condor honesty check.

    Default 8.0 => 1σ band [92, 108]; condor shorts 85/115 sit OUTSIDE it (safe).
    Override with a LARGE one_sigma (e.g. 20) to force the condor-inside-1σ warning.
    """
    return [
        {"expiry": _NEAR_EXP, "straddle": near_one_sigma / 0.85 * 1.0,
         "straddle_pct": (near_one_sigma / 0.85) / _SPOT,
         "one_sigma": near_one_sigma, "one_sigma_pct": near_one_sigma / _SPOT,
         "range_low": _SPOT - near_one_sigma, "range_high": _SPOT + near_one_sigma},
        {"expiry": _FAR_EXP, "straddle": 14.0, "straddle_pct": 0.14,
         "one_sigma": 11.9, "one_sigma_pct": 0.119,
         "range_low": 88.1, "range_high": 111.9},
    ]


def _snapshot(iv_minus_rv=-0.10, iv30=0.96, rv20=1.06, iv_pctile=88.0,
              earnings_date="2026-09-10", ex_div=None,
              near_one_sigma=8.0, chain_file="chain_MU.json",
              atm_iv_by_expiry=None):
    """Snapshot stub with an options block pointing at the chain file.

    Defaults encode the MU prototype: iv30 0.96 LOOKS rich but iv_minus_rv -0.10
    (14 pts below realized ~1.06) makes it CHEAP vs realized -> the primary gate.
    """
    if atm_iv_by_expiry is None:
        atm_iv_by_expiry = [
            {"expiry": _NEAR_EXP, "atm_iv": 0.96},
            {"expiry": _FAR_EXP, "atm_iv": 0.90},
        ]
    ne = {"date": earnings_date, "time": "post-market", "consensus_eps": 1.9} \
        if earnings_date else None
    dividends = {"ex_date": ex_div, "amount": None, "yield_pct": None}
    return {
        "meta": {"ticker": "MU", "as_of_utc": "2026-07-16T00:00:00Z"},
        "price": {"last": _SPOT},
        "events": {"next_earnings": ne, "dividends": dividends},
        "sentiment": {
            "iv30": iv30,
            "iv_pctile_1yr": iv_pctile,
            "put_call_ratio_full_chain": 1.29,
            "put_call_ratio_full_chain_volume": 0.93,
            "put_call_ratio_realtime": 0.90,
        },
        "options": {
            "chain_file_path": chain_file,
            "atm_iv_by_expiry": atm_iv_by_expiry,
            "expected_moves": _expected_moves(near_one_sigma),
            "max_pain_by_expiry": [
                {"expiry": _NEAR_EXP, "max_pain": 100.0},
                {"expiry": _FAR_EXP, "max_pain": 100.0},
            ],
            "oi_walls": {
                "call_wall": {"strike": 120.0, "oi": 5000},
                "put_wall": {"strike": 90.0, "oi": 4000},
                "near_money_clusters": [],
            },
            "skew_25d_30d": 0.04,
            "rv20_for_iv_comparison": rv20,
            "iv_minus_rv20": iv_minus_rv,
        },
    }


def _composite_doc(grade="B", profile="balanced"):
    action = {"A": "Buy/Add", "B": "Hold/Accumulate-on-weakness",
              "C": "Hold/Trim", "D": "Reduce/Avoid"}[grade]
    return {
        "skill": "composite-score", "rubric_version": "1.0.0", "ticker": "MU",
        "as_of": "2026-07-16", "profile": profile, "score": 65, "grade": grade,
        "action": action,
        "ev": {"scenarios": [], "ev_at_current": 0.1, "hurdle_total": 0.12,
               "ev_breakeven_entry": 95.0, "ev_at_levels": []},
    }


def _tradeplan_doc(entry_1=90.0, hedge_required=True, days_to_catalyst=56,
                   hedge_strikes=None, premium_cap=0.015):
    """A module_tradeplan.json stub with entry_1 + hedge spec for pipeline alignment."""
    if hedge_strikes is None:
        hedge_strikes = [90.0, 85.0]
    return {
        "skill": "trade-plan", "rubric_version": "1.0.0", "ticker": "MU",
        "as_of": "2026-07-16", "profile": "balanced",
        "stock_plan": {
            "entries": [
                {"level": entry_1, "type": "put_wall", "condition": "resting limit"},
                {"level": 82.0, "type": "swing_low", "condition": "resting limit"},
            ],
            "hedge": {
                "required": hedge_required,
                "trigger": "binary event within 30d",
                "structure": "put spread or collar",
                "strikes_from": hedge_strikes,
                "expiry_rule": "first monthly expiry after the event",
                "premium_cap_pct": premium_cap,
            },
        },
        "expression": {
            "rule_version": "expression-v1.0.0",
            "days_to_catalyst": days_to_catalyst,
        },
    }


def _write_bundle(dir_, chain=None, snapshot=None, composite=None, tradeplan=None,
                  chain_file="chain_MU.json"):
    if chain is not None:
        with open(os.path.join(dir_, chain_file), "w") as fh:
            json.dump({"data": chain}, fh)
    if snapshot is not None:
        with open(os.path.join(dir_, "snapshot_MU_2026-07-16.json"), "w") as fh:
            json.dump(snapshot, fh)
    if composite is not None:
        with open(os.path.join(dir_, "module_composite.json"), "w") as fh:
            json.dump(composite, fh)
    if tradeplan is not None:
        with open(os.path.join(dir_, "module_tradeplan.json"), "w") as fh:
            json.dump(tradeplan, fh)


# --------------------------------------------------------------------------- #
# Vol dashboard + primary gate (IV-vs-realized).
# --------------------------------------------------------------------------- #

class TestVolDashboard(unittest.TestCase):
    def test_verdict_cheap_vs_realized(self):
        # iv_minus_rv -0.10 <= -0.03 -> cheap (the MU lesson: rich-LOOKING but cheap).
        vd = opts.vol_verdict(-0.10)
        self.assertEqual(vd, "cheap_vs_realized")

    def test_verdict_fair(self):
        self.assertEqual(opts.vol_verdict(0.0), "fair")
        self.assertEqual(opts.vol_verdict(-0.02), "fair")
        self.assertEqual(opts.vol_verdict(0.02), "fair")

    def test_verdict_rich_vs_realized(self):
        self.assertEqual(opts.vol_verdict(0.05), "rich_vs_realized")
        self.assertEqual(opts.vol_verdict(0.03), "rich_vs_realized")

    def test_verdict_unknown_when_none(self):
        self.assertEqual(opts.vol_verdict(None), "unknown")

    def test_build_dashboard_carries_fields(self):
        snap = _snapshot()
        vd = opts.build_vol_dashboard(snap)
        self.assertEqual(vd["verdict"], "cheap_vs_realized")
        self.assertEqual(vd["iv30"], 0.96)
        self.assertEqual(vd["rv20"], 1.06)
        self.assertEqual(vd["diff"], -0.10)
        self.assertEqual(vd["iv_pctile_1yr"], 88.0)
        # atm_iv_by_expiry passthrough present.
        self.assertTrue(vd["atm_iv_by_expiry"])


# --------------------------------------------------------------------------- #
# Term structure classification.
# --------------------------------------------------------------------------- #

class TestTermStructure(unittest.TestCase):
    def test_backwardation_front_over_back(self):
        rows = [{"expiry": _NEAR_EXP, "atm_iv": 0.96},
                {"expiry": _FAR_EXP, "atm_iv": 0.90}]  # front - back = 0.06 > 0.02
        self.assertEqual(opts.term_structure(rows), "backwardation")

    def test_contango_back_over_front(self):
        rows = [{"expiry": _NEAR_EXP, "atm_iv": 0.85},
                {"expiry": _FAR_EXP, "atm_iv": 0.92}]  # back - front = 0.07 > 0.02
        self.assertEqual(opts.term_structure(rows), "contango")

    def test_flat_within_band(self):
        rows = [{"expiry": _NEAR_EXP, "atm_iv": 0.90},
                {"expiry": _FAR_EXP, "atm_iv": 0.905}]
        self.assertEqual(opts.term_structure(rows), "flat")

    def test_flat_when_single_expiry(self):
        self.assertEqual(opts.term_structure([{"expiry": _NEAR_EXP, "atm_iv": 0.9}]),
                         "flat")

    def test_tenor_window_excludes_0dte_stub_and_leap(self):
        # Gate-3 MU case: a 0-DTE stub (low IV) + a 2-year LEAP (high IV) made the
        # curve read "contango" while the tradeable window was backwarded.
        rows = [{"expiry": "2026-07-16", "atm_iv": 0.127},   # 0 DTE stub
                {"expiry": "2026-08-21", "atm_iv": 0.98},    # front (36d)
                {"expiry": "2026-10-16", "atm_iv": 0.927},   # back (92d)
                {"expiry": "2028-12-15", "atm_iv": 0.859}]   # LEAP, out of window
        self.assertEqual(opts.term_structure(rows, as_of="2026-07-16"),
                         "backwardation")
        # as_of arrives as a full ISO timestamp in real snapshots (meta.as_of_utc)
        # — the window must still engage (Gate-3 fix regression).
        self.assertEqual(opts.term_structure(rows, as_of="2026-07-16T18:38:07Z"),
                         "backwardation")
        # without as_of the unfiltered legacy behavior remains (documented).
        self.assertEqual(opts.term_structure(rows), "contango")


# --------------------------------------------------------------------------- #
# Monthly-ish + expiry selection.
# --------------------------------------------------------------------------- #

class TestExpirySelection(unittest.TestCase):
    def test_is_monthlyish_third_friday(self):
        # 2026-08-21 is a Friday, day 21 in [15,21] -> monthlyish.
        self.assertTrue(opts.is_monthlyish("2026-08-21"))

    def test_is_monthlyish_false_non_friday(self):
        # 2026-09-25 is a Friday but day 25 not in [15,21] -> not monthlyish.
        self.assertFalse(opts.is_monthlyish("2026-09-25"))

    def test_is_monthlyish_false_weekly(self):
        # 2026-08-07 (a Friday, day 7) -> weekly, not monthlyish.
        self.assertFalse(opts.is_monthlyish("2026-08-07"))

    def test_select_expiry_catalyst_after_date(self):
        # pipeline w/ catalyst <=60d: first monthlyish expiry AFTER the catalyst.
        # catalyst 2026-08-15 -> near (08-21) is monthlyish AND after -> chosen.
        chosen = opts.select_expiry(
            [_NEAR_EXP, _FAR_EXP], as_of="2026-07-16",
            catalyst_date="2026-08-15", days_to_catalyst=30)
        self.assertEqual(chosen, _NEAR_EXP)

    def test_select_expiry_no_catalyst_nearest_45(self):
        # no catalyst -> nearest to 45 DTE within [30,90]; 08-21 (~36d) vs 09-25 (~71d).
        chosen = opts.select_expiry(
            [_NEAR_EXP, _FAR_EXP], as_of="2026-07-16",
            catalyst_date=None, days_to_catalyst=None)
        self.assertEqual(chosen, _NEAR_EXP)

    def test_select_expiry_monthly_beats_closer_weekly(self):
        # Gate-3 MU case: weekly 2026-08-28 sits 43 DTE (|43-45|=2) vs monthly
        # 2026-08-21 at 36 DTE (|36-45|=9). Monthlyish-in-window wins regardless
        # of raw DTE distance — weeklies are a fallback pool, not a peer.
        chosen = opts.select_expiry(
            ["2026-08-28", "2026-08-21"], as_of="2026-07-16",
            catalyst_date=None, days_to_catalyst=None)
        self.assertEqual(chosen, "2026-08-21")

    def test_select_expiry_weekly_fallback_when_no_monthly_in_window(self):
        # only weeklies in [30,90] -> the weekly pool is used.
        chosen = opts.select_expiry(
            ["2026-08-28", "2026-12-18"], as_of="2026-07-16",
            catalyst_date=None, days_to_catalyst=None)
        self.assertEqual(chosen, "2026-08-28")


# --------------------------------------------------------------------------- #
# Delta-targeted strike selection off the REAL chain.
# --------------------------------------------------------------------------- #

class TestStrikeSelection(unittest.TestCase):
    def setUp(self):
        self.chain = _chain()

    def test_short_put_is_30_delta_strike(self):
        # among puts at near expiry, |delta| closest to 0.30 -> strike 90.
        c = opts.pick_by_delta(self.chain, _NEAR_EXP, "put", 0.30)
        self.assertEqual(c["strike"], 90.0)

    def test_short_call_is_30_delta_strike(self):
        c = opts.pick_by_delta(self.chain, _NEAR_EXP, "call", 0.30)
        self.assertEqual(c["strike"], 110.0)

    def test_long_call_is_55_delta_strike(self):
        # +0.55Δ target -> among calls, |delta - 0.55| min -> strike 95 (0.58).
        c = opts.pick_by_delta(self.chain, _NEAR_EXP, "call", 0.55)
        self.assertEqual(c["strike"], 95.0)

    def test_long_put_one_or_two_strikes_below_short(self):
        # short put 90 -> long put is the next lower listed strike (85), 5-wide.
        lp = opts.pick_long_put_below(self.chain, _NEAR_EXP, short_strike=90.0)
        self.assertEqual(lp["strike"], 85.0)


# --------------------------------------------------------------------------- #
# Per-structure economics: bull put spread (credit) exact.
# --------------------------------------------------------------------------- #

class TestBullPutSpreadEconomics(unittest.TestCase):
    def setUp(self):
        self.chain = _chain()

    def test_bull_put_spread_exact(self):
        # short put 90 (mark 2.40), long put 85 (mark 1.50).
        # credit = 2.40 - 1.50 = 0.90 ; width = 5 ; max_loss = 5 - 0.90 = 4.10
        # breakeven = 90 - 0.90 = 89.10 ; PoP = 1 - |Δ short| = 1 - 0.30 = 0.70
        st = opts.build_bull_put_spread(self.chain, _NEAR_EXP)
        self.assertAlmostEqual(st["net_credit"], 0.90, places=4)
        self.assertAlmostEqual(st["max_profit"], 0.90, places=4)
        self.assertAlmostEqual(st["max_loss"], 4.10, places=4)
        self.assertAlmostEqual(st["breakevens"][0], 89.10, places=4)
        self.assertAlmostEqual(st["pop"], 0.70, places=4)
        self.assertIn("delta", st["pop_method"].lower())
        # arithmetic string round-trips the numbers.
        self.assertIn("0.9", st["arithmetic"])
        # legs record real strikes.
        strikes = sorted(leg["strike"] for leg in st["legs"])
        self.assertEqual(strikes, [85.0, 90.0])

    def test_cash_secured_put_effective_entry(self):
        # CSP short put 90 (mark 2.40): max_loss labeled effective entry 90-2.40=87.60.
        st = opts.build_cash_secured_put(self.chain, _NEAR_EXP)
        self.assertAlmostEqual(st["net_credit"], 2.40, places=4)
        self.assertAlmostEqual(st["breakevens"][0], 87.60, places=4)
        self.assertAlmostEqual(st["pop"], 0.70, places=4)
        self.assertIn("effective entry", st["max_loss_note"].lower())

    def test_long_call_vertical_debit(self):
        # long call 95 (mark 7.90), short call 110 (mark 2.20).
        # debit = 7.90 - 2.20 = 5.70 ; max_loss = debit ; max_profit = width - debit.
        st = opts.build_long_call_vertical(self.chain, _NEAR_EXP)
        self.assertAlmostEqual(st["net_debit"], 5.70, places=4)
        self.assertAlmostEqual(st["max_loss"], 5.70, places=4)
        # width 95->110 = 15 ; max_profit = 15 - 5.70 = 9.30
        self.assertAlmostEqual(st["max_profit"], 9.30, places=4)
        # breakeven = long strike + debit = 95 + 5.70 = 100.70
        self.assertAlmostEqual(st["breakevens"][0], 100.70, places=4)
        # PoP ~ |delta long| = 0.58 (rough), labeled.
        self.assertAlmostEqual(st["pop"], 0.58, places=4)
        self.assertIn("delta", st["pop_method"].lower())


# --------------------------------------------------------------------------- #
# Iron condor + expected-move honesty check.
# --------------------------------------------------------------------------- #

class TestIronCondorHonesty(unittest.TestCase):
    def test_condor_pop_two_shorts(self):
        chain = _chain()
        st = opts.build_iron_condor(chain, _NEAR_EXP, one_sigma=8.0)
        # condor short put 85 (|Δ|0.22) + short call 115 (Δ0.22) -> PoP = 1-(0.22+0.22).
        self.assertAlmostEqual(st["pop"], 1 - (0.22 + 0.22), places=4)

    def test_condor_inside_expected_move_warns(self):
        # zone half-width = (115-85)/2 = 15 ; force one_sigma 20 > 15 -> warning fires.
        chain = _chain()
        st = opts.build_iron_condor(chain, _NEAR_EXP, one_sigma=20.0)
        self.assertIsNotNone(st.get("pop_full_profit_note"))
        joined = " ".join(st.get("warnings", [])).lower()
        self.assertIn("1", joined)  # references the 1-sigma move
        self.assertIn("expected move", joined)

    def test_condor_outside_expected_move_no_warn(self):
        # one_sigma 8 < zone half-width 15 -> no inside-1sigma warning.
        chain = _chain()
        st = opts.build_iron_condor(chain, _NEAR_EXP, one_sigma=8.0)
        self.assertIsNone(st.get("pop_full_profit_note"))


# --------------------------------------------------------------------------- #
# Selection matrix: all 6 direction x vol-verdict branches.
# --------------------------------------------------------------------------- #

class TestSelectionMatrix(unittest.TestCase):
    def _names(self, structures):
        return {s["name"] for s in structures}

    def test_bullish_rich(self):
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bullish", "rich_vs_realized", one_sigma=8.0)
        names = self._names(rec)
        self.assertIn("bull_put_spread", names)
        self.assertIn("cash_secured_put", names)

    def test_bullish_cheap_long_call_vertical(self):
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bullish", "cheap_vs_realized", one_sigma=8.0)
        names = self._names(rec)
        self.assertIn("long_call_vertical", names)

    def test_bearish_rich_bear_call_spread(self):
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bearish", "rich_vs_realized", one_sigma=8.0)
        self.assertIn("bear_call_spread", self._names(rec))

    def test_bearish_cheap_long_put_vertical(self):
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bearish", "cheap_vs_realized", one_sigma=8.0)
        self.assertIn("long_put_vertical", self._names(rec))

    def test_neutral_rich_iron_condor(self):
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "neutral", "rich_vs_realized", one_sigma=8.0)
        self.assertIn("iron_condor", self._names(rec))

    def test_neutral_cheap_declines_no_structure(self):
        # neutral x cheap/fair -> NO premium structure; a declined entry instead.
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "neutral", "cheap_vs_realized", one_sigma=8.0)
        self.assertEqual(rec, [])
        joined = " ".join(d["reason"] for d in declined).lower()
        self.assertIn("stand aside", joined)

    def test_neutral_fair_declines_no_structure(self):
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "neutral", "fair", one_sigma=8.0)
        self.assertEqual(rec, [])


# --------------------------------------------------------------------------- #
# Liquidity gate.
# --------------------------------------------------------------------------- #

class TestLiquidityGate(unittest.TestCase):
    def test_leg_passes_by_oi(self):
        leg = {"strike": 90.0, "oi": 500, "mark": 2.40, "bid": 2.30, "ask": 2.50}
        ok, reason = opts.leg_liquid(leg)
        self.assertTrue(ok)

    def test_leg_fails_low_oi(self):
        leg = {"strike": 85.0, "oi": 5, "mark": 1.50, "bid": 1.45, "ask": 1.55}
        ok, reason = opts.leg_liquid(leg)
        self.assertFalse(ok)
        self.assertIn("oi", reason.lower())

    def test_leg_fails_wide_spread(self):
        leg = {"strike": 90.0, "oi": 500, "mark": 2.40, "bid": 1.00, "ask": 4.00}
        ok, reason = opts.leg_liquid(leg)
        self.assertFalse(ok)
        self.assertIn("spread", reason.lower())

    def test_leg_missing_bid_ask_uses_oi_only(self):
        # no bid/ask -> oi-only gate (500 passes) + a disclosure in reason.
        leg = {"strike": 90.0, "oi": 500, "mark": 2.40}
        ok, reason = opts.leg_liquid(leg)
        self.assertTrue(ok)

    def test_illiquid_leg_moves_structure_to_declined(self):
        chain = _chain_with_illiquid()   # long-put 85 has oi 5.
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bullish", "rich_vs_realized", one_sigma=8.0)
        # bull_put_spread needs the 85 long leg -> declined; CSP (only 90) survives.
        rec_names = {s["name"] for s in rec}
        dec_names = {d["name"] for d in declined}
        self.assertIn("bull_put_spread", dec_names)
        self.assertIn("cash_secured_put", rec_names)

    def test_wide_spread_leg_declined(self):
        chain = _chain_with_wide_spread()   # short-put 90 spread 3.00.
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bullish", "rich_vs_realized", one_sigma=8.0)
        dec_names = {d["name"] for d in declined}
        # both structures use the 90 short put -> both declined.
        self.assertIn("bull_put_spread", dec_names)
        self.assertIn("cash_secured_put", dec_names)


# --------------------------------------------------------------------------- #
# Honesty gates (mechanical warnings).
# --------------------------------------------------------------------------- #

class TestHonestyGates(unittest.TestCase):
    def test_cheap_vs_realized_tags_credit_structures(self):
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bullish", "cheap_vs_realized", one_sigma=8.0)
        # bullish x cheap -> long_call_vertical + bull_put_spread WITH warning.
        bps = next((s for s in rec if s["name"] == "bull_put_spread"), None)
        self.assertIsNotNone(bps)
        joined = " ".join(bps.get("warnings", [])).lower()
        self.assertIn("realized", joined)
        self.assertIn("not being paid", joined)

    def test_earnings_within_30d_excludes_csp(self):
        # days_to_earnings 14 <= 30 -> CSP excluded; other structures carry event warning.
        chain = _chain()
        rec, declined = opts.apply_event_gates(
            *opts.select_structures(chain, _NEAR_EXP, "bullish",
                                    "rich_vs_realized", one_sigma=8.0)[:2],
            days_to_earnings=14, ex_div_in_tenor=False)
        rec_names = {s["name"] for s in rec}
        dec_names = {d["name"] for d in declined}
        self.assertNotIn("cash_secured_put", rec_names)
        self.assertIn("cash_secured_put", dec_names)
        # remaining structures carry the IV-crush / defined-risk warning.
        for s in rec:
            joined = " ".join(s.get("warnings", [])).lower()
            self.assertTrue("iv-crush" in joined or "defined-risk" in joined)

    def test_earnings_far_keeps_csp(self):
        chain = _chain()
        rec, declined = opts.apply_event_gates(
            *opts.select_structures(chain, _NEAR_EXP, "bullish",
                                    "rich_vs_realized", one_sigma=8.0)[:2],
            days_to_earnings=60, ex_div_in_tenor=False)
        rec_names = {s["name"] for s in rec}
        self.assertIn("cash_secured_put", rec_names)

    def test_ex_div_tags_short_call_legs(self):
        chain = _chain()
        rec, declined = opts.apply_event_gates(
            *opts.select_structures(chain, _NEAR_EXP, "bearish",
                                    "rich_vs_realized", one_sigma=8.0)[:2],
            days_to_earnings=60, ex_div_in_tenor=True)
        # bear_call_spread has a short call leg -> early-assignment note.
        bcs = next((s for s in rec if s["name"] == "bear_call_spread"), None)
        self.assertIsNotNone(bcs)
        joined = " ".join(bcs.get("warnings", [])).lower()
        self.assertIn("assignment", joined)


# --------------------------------------------------------------------------- #
# Management rules per structure type.
# --------------------------------------------------------------------------- #

class TestManagementRules(unittest.TestCase):
    def test_credit_spread_management(self):
        chain = _chain()
        st = opts.build_bull_put_spread(chain, _NEAR_EXP)
        mgmt = " ".join(st["management"]).lower()
        self.assertIn("50%", mgmt)
        self.assertIn("21", mgmt)   # 21 DTE time exit

    def test_condor_management(self):
        chain = _chain()
        st = opts.build_iron_condor(chain, _NEAR_EXP, one_sigma=8.0)
        mgmt = " ".join(st["management"]).lower()
        self.assertIn("roll", mgmt)

    def test_debit_vertical_management(self):
        chain = _chain()
        st = opts.build_long_call_vertical(chain, _NEAR_EXP)
        mgmt = " ".join(st["management"]).lower()
        self.assertIn("100%", mgmt)


# --------------------------------------------------------------------------- #
# Hedge construction from tradeplan.
# --------------------------------------------------------------------------- #

class TestHedge(unittest.TestCase):
    def test_hedge_put_spread_from_strikes(self):
        chain = _chain()
        # hedge strikes_from [90, 85] -> long put at nearest listed <= 90 (=90),
        # short put at nearest listed <= 85 (=85). cost = 2.40 - 1.50 = 0.90.
        hedge = opts.build_hedge(chain, _NEAR_EXP, strikes_from=[90.0, 85.0],
                                 spot=_SPOT, premium_cap_pct=0.50)
        self.assertEqual(hedge["type"], "put_spread")
        long_leg = next(l for l in hedge["legs"] if l["side"] == "long")
        short_leg = next(l for l in hedge["legs"] if l["side"] == "short")
        self.assertEqual(long_leg["strike"], 90.0)
        self.assertEqual(short_leg["strike"], 85.0)
        self.assertAlmostEqual(hedge["cost"], 0.90, places=4)

    def test_hedge_nearest_listed_strike_at_or_below(self):
        chain = _chain()
        # level 88 -> nearest listed strike <= 88 is 85.
        hedge = opts.build_hedge(chain, _NEAR_EXP, strikes_from=[88.0, 83.0],
                                 spot=_SPOT, premium_cap_pct=0.50)
        long_leg = next(l for l in hedge["legs"] if l["side"] == "long")
        self.assertEqual(long_leg["strike"], 85.0)

    def test_hedge_premium_cap_breach_emits_collar(self):
        chain = _chain()
        # cost 0.90 / spot 100 = 0.009 ; force cap 0.005 -> breach -> collar alt.
        hedge = opts.build_hedge(chain, _NEAR_EXP, strikes_from=[90.0, 85.0],
                                 spot=_SPOT, premium_cap_pct=0.005)
        self.assertIn("collar_alternative", hedge)
        self.assertIsNotNone(hedge["collar_alternative"])
        # collar adds a short call ~0.20Δ and recomputes net (net < put-spread cost).
        collar = hedge["collar_alternative"]
        self.assertLess(collar["net_cost"], hedge["cost"])

    def test_hedge_within_cap_no_collar(self):
        chain = _chain()
        hedge = opts.build_hedge(chain, _NEAR_EXP, strikes_from=[90.0, 85.0],
                                 spot=_SPOT, premium_cap_pct=0.50)
        self.assertIsNone(hedge.get("collar_alternative"))


# --------------------------------------------------------------------------- #
# CLI end-to-end: pipeline + standalone.
# --------------------------------------------------------------------------- #

class TestCLI(unittest.TestCase):
    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _run(self, extra=None):
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir]
        if extra:
            cmd += extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def _read(self):
        with open(os.path.join(self.dir, "module_options.json")) as fh:
            return json.load(fh)

    def test_standalone_requires_direction(self):
        _write_bundle(self.dir, chain=_chain(), snapshot=_snapshot())
        proc = self._run(extra=["--mode", "standalone"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("direction", proc.stderr.lower())

    def test_standalone_bullish_writes_module(self):
        _write_bundle(self.dir, chain=_chain(), snapshot=_snapshot())
        proc = self._run(extra=["--mode", "standalone", "--direction", "bullish"])
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        doc = self._read()
        self.assertEqual(doc["skill"], "options-strategy")
        self.assertEqual(doc["rubric_version"], "1.1.0")
        self.assertEqual(doc["ticker"], "MU")
        self.assertEqual(doc["direction"], "bullish")
        self.assertEqual(doc["direction_source"], "flag")
        self.assertEqual(doc["vol_dashboard"]["verdict"], "cheap_vs_realized")
        self.assertIsNone(doc["signal"])

    def test_pipeline_direction_from_grade_B_bullish(self):
        _write_bundle(self.dir, chain=_chain(), snapshot=_snapshot(),
                      composite=_composite_doc(grade="B"),
                      tradeplan=_tradeplan_doc())
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        doc = self._read()
        self.assertEqual(doc["direction"], "bullish")
        self.assertIn("grade", doc["direction_source"].lower())

    def test_pipeline_direction_from_grade_D_bearish(self):
        _write_bundle(self.dir, chain=_chain(), snapshot=_snapshot(),
                      composite=_composite_doc(grade="D"),
                      tradeplan=_tradeplan_doc())
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        self.assertEqual(doc["direction"], "bearish")

    def test_pipeline_direction_from_grade_C_neutral(self):
        _write_bundle(self.dir, chain=_chain(), snapshot=_snapshot(),
                      composite=_composite_doc(grade="C"),
                      tradeplan=_tradeplan_doc())
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        self.assertEqual(doc["direction"], "neutral")

    def test_pipeline_missing_composite_exit2(self):
        _write_bundle(self.dir, chain=_chain(), snapshot=_snapshot(),
                      tradeplan=_tradeplan_doc())
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("composite", proc.stderr.lower())

    def test_pipeline_missing_tradeplan_exit2(self):
        _write_bundle(self.dir, chain=_chain(), snapshot=_snapshot(),
                      composite=_composite_doc(grade="B"))
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("tradeplan", proc.stderr.lower())

    def test_missing_chain_exit2(self):
        # snapshot points at a chain file that does not exist.
        _write_bundle(self.dir, snapshot=_snapshot(chain_file="nope.json"))
        proc = self._run(extra=["--mode", "standalone", "--direction", "bullish"])
        self.assertEqual(proc.returncode, 2)

    def test_no_chain_in_snapshot_degrades_disclosed(self):
        # V3 acceptance regression: options block missing entirely (disclosed in
        # meta.missing) must NOT hard-stop — it emits a disclosed empty module so
        # the report stays renderable (only the snapshot QC gate is a full stop).
        snap = _snapshot()
        snap["options"] = None
        snap.setdefault("meta", {}).setdefault("missing", []).append("options_chain")
        _write_bundle(self.dir, snapshot=snap)
        proc = self._run(extra=["--mode", "standalone", "--direction", "bullish"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        self.assertEqual(doc["recommended_structures"], [])
        self.assertEqual(doc["declined"][0]["name"], "all")
        self.assertIn("options analysis unavailable", doc["liquidity_verdict"])
        self.assertTrue(any("no options chain" in w.lower()
                            for w in doc["warnings_global"]))
        self.assertEqual(doc["vol_dashboard"]["verdict"], "unknown")

    def test_pipeline_csp_alignment_to_entry_1(self):
        # tradeplan entry_1 = 90.0 (== a listed put strike, within 2%) -> CSP uses 90.
        _write_bundle(self.dir, chain=_chain(),
                      snapshot=_snapshot(iv_minus_rv=0.05),   # rich -> CSP present
                      composite=_composite_doc(grade="B"),
                      tradeplan=_tradeplan_doc(entry_1=90.0))
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        csp = next((s for s in doc["recommended_structures"]
                    if s["name"] == "cash_secured_put"), None)
        self.assertIsNotNone(csp)
        joined = " ".join(csp.get("warnings", []) + [csp.get("alignment_note", "")]).lower()
        self.assertIn("aligned", joined)

    def test_pipeline_hedge_structure_emitted(self):
        _write_bundle(self.dir, chain=_chain(),
                      snapshot=_snapshot(iv_minus_rv=0.05),
                      composite=_composite_doc(grade="B"),
                      tradeplan=_tradeplan_doc(hedge_required=True))
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        self.assertIsNotNone(doc["hedge_structure"])

    def test_pipeline_no_hedge_when_not_required(self):
        _write_bundle(self.dir, chain=_chain(),
                      snapshot=_snapshot(iv_minus_rv=0.05),
                      composite=_composite_doc(grade="B"),
                      tradeplan=_tradeplan_doc(hedge_required=False))
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        self.assertIsNone(doc["hedge_structure"])

    def test_neutral_cheap_declined_at_cli(self):
        _write_bundle(self.dir, chain=_chain(),
                      snapshot=_snapshot(iv_minus_rv=-0.10),   # cheap
                      composite=_composite_doc(grade="C"),     # neutral
                      tradeplan=_tradeplan_doc())
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        self.assertEqual(doc["recommended_structures"], [])
        self.assertTrue(doc["declined"])

    def test_earnings_within_30d_excludes_csp_at_cli(self):
        # earnings 2026-07-30 -> 14d from as_of 2026-07-16.
        _write_bundle(self.dir, chain=_chain(),
                      snapshot=_snapshot(iv_minus_rv=0.05,
                                         earnings_date="2026-07-30"),
                      composite=_composite_doc(grade="B"),
                      tradeplan=_tradeplan_doc(days_to_catalyst=14))
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        rec_names = {s["name"] for s in doc["recommended_structures"]}
        self.assertNotIn("cash_secured_put", rec_names)

    def test_thin_liquidity_verdict(self):
        # illiquid long put -> bull_put_spread declined ; if CSP also excluded (earnings)
        # < 2 recommended -> thin verdict.
        chain = _chain_with_illiquid()
        _write_bundle(self.dir, chain=chain,
                      snapshot=_snapshot(iv_minus_rv=0.05,
                                         earnings_date="2026-07-30"),
                      composite=_composite_doc(grade="B"),
                      tradeplan=_tradeplan_doc(days_to_catalyst=14))
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        self.assertIn("thin", doc["liquidity_verdict"].lower())
        # Gate-3 ETSY regression: the binary event must be globally visible even
        # when zero structures survive (per-structure warnings vanish with them).
        self.assertTrue(any("BINARY EVENT" in w for w in doc["warnings_global"]),
                        doc["warnings_global"])

    def test_determinism(self):
        _write_bundle(self.dir, chain=_chain(), snapshot=_snapshot())
        p1 = self._run(extra=["--mode", "standalone", "--direction", "bullish"])
        with open(os.path.join(self.dir, "module_options.json")) as fh:
            a = fh.read()
        p2 = self._run(extra=["--mode", "standalone", "--direction", "bullish"])
        with open(os.path.join(self.dir, "module_options.json")) as fh:
            b = fh.read()
        self.assertEqual(p1.returncode, 0)
        self.assertEqual(p2.returncode, 0)
        self.assertEqual(a, b)

    def test_cheap_credit_structure_warning_at_cli(self):
        _write_bundle(self.dir, chain=_chain(),
                      snapshot=_snapshot(iv_minus_rv=-0.10),   # cheap
                      composite=_composite_doc(grade="B"),     # bullish
                      tradeplan=_tradeplan_doc())
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        # bullish x cheap -> long_call_vertical + bull_put_spread(with warning).
        bps = next((s for s in doc["recommended_structures"]
                    if s["name"] == "bull_put_spread"), None)
        self.assertIsNotNone(bps)
        joined = " ".join(bps.get("warnings", [])).lower()
        self.assertIn("realized", joined)

    def test_expected_moves_passthrough(self):
        _write_bundle(self.dir, chain=_chain(), snapshot=_snapshot())
        self._run(extra=["--mode", "standalone", "--direction", "bullish"])
        doc = self._read()
        self.assertTrue(doc["expected_moves"])
        self.assertEqual(doc["flow"]["pc_oi"], 1.29)
        self.assertEqual(doc["flow"]["pc_volume"], 0.93)


# --------------------------------------------------------------------------- #
# Wave 4B: skew_verdict (3 branches) + skew-informed routing.
# --------------------------------------------------------------------------- #

class TestSkewVerdict(unittest.TestCase):
    def test_puts_rich_above_threshold(self):
        # rr_25d = IV(25d put) - IV(25d call). > +0.04 -> puts_rich (downside skew).
        self.assertEqual(opts.skew_verdict(0.22), "puts_rich")
        self.assertEqual(opts.skew_verdict(0.05), "puts_rich")

    def test_calls_rich_below_negative_threshold(self):
        self.assertEqual(opts.skew_verdict(-0.05), "calls_rich")
        self.assertEqual(opts.skew_verdict(-0.22), "calls_rich")

    def test_balanced_within_threshold(self):
        self.assertEqual(opts.skew_verdict(0.0), "balanced")
        self.assertEqual(opts.skew_verdict(0.04), "balanced")   # boundary inclusive
        self.assertEqual(opts.skew_verdict(-0.04), "balanced")

    def test_unknown_when_none(self):
        self.assertEqual(opts.skew_verdict(None), "unknown")

    def test_custom_threshold(self):
        self.assertEqual(opts.skew_verdict(0.05, threshold=0.10), "balanced")
        self.assertEqual(opts.skew_verdict(0.12, threshold=0.10), "puts_rich")


class TestSkewRouting(unittest.TestCase):
    def _names(self, structures):
        return {s["name"] for s in structures}

    def test_puts_rich_cheap_prefers_selling_puts(self):
        # bullish + cheap regime: base matrix leads with long_call_vertical (buy calls).
        # With puts_rich skew, the routing PREFERS SELLING puts even in the cheap regime,
        # so the bull_put_spread / CSP lead and appear.
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bullish", "cheap_vs_realized", one_sigma=8.0,
            skew_verdict_="puts_rich")
        names = self._names(rec)
        self.assertIn("bull_put_spread", names)
        self.assertIn("cash_secured_put", names)
        # the sold-puts structure(s) are ordered ahead of the bought-calls structure.
        rec_order = [s["name"] for s in rec]
        self.assertLess(rec_order.index("bull_put_spread"),
                        rec_order.index("long_call_vertical")
                        if "long_call_vertical" in rec_order else len(rec_order))

    def test_calls_rich_cheap_prefers_selling_calls(self):
        # bearish + cheap: base leads with long_put_vertical (buy puts). calls_rich skew
        # PREFERS SELLING calls (bear_call_spread) first.
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bearish", "cheap_vs_realized", one_sigma=8.0,
            skew_verdict_="calls_rich")
        rec_order = [s["name"] for s in rec]
        self.assertIn("bear_call_spread", rec_order)
        if "long_put_vertical" in rec_order:
            self.assertLess(rec_order.index("bear_call_spread"),
                            rec_order.index("long_put_vertical"))

    def test_condor_widens_cheap_wing_on_puts_rich(self):
        # neutral rich condor with puts_rich: sell the RICH put wing NEARER the money
        # (~0.30Δ) and widen the cheap call wing (short call pushed toward ~0.20Δ). Base
        # condor shorts are the SYMMETRIC 85 put / 115 call (~0.22Δ each); puts_rich
        # routing sells the put nearer the money (85 -> 90) -- the asymmetry the skew asks
        # for (the put wing tightens onto the rich premium, the call wing stays/widens).
        chain = _chain()
        base = opts.build_iron_condor(chain, _NEAR_EXP, 8.0)
        base_sp = next(l for l in base["legs"]
                       if l["side"] == "short" and l["type"] == "put")["strike"]

        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "neutral", "rich_vs_realized", one_sigma=8.0,
            skew_verdict_="puts_rich")
        condor = next((s for s in rec if s["name"] == "iron_condor"), None)
        self.assertIsNotNone(condor)
        short_put = next(l for l in condor["legs"]
                         if l["side"] == "short" and l["type"] == "put")
        short_call = next(l for l in condor["legs"]
                          if l["side"] == "short" and l["type"] == "call")
        # the rich put wing is sold NEARER the money than in the symmetric base condor.
        self.assertGreater(short_put["strike"], base_sp)   # 90 > base 85 (nearer money)
        self.assertEqual(short_put["strike"], 90.0)        # 0.30Δ pick
        # the put wing is now nearer the money than the (unmoved/widened) call wing:
        # asymmetric distances from spot 100 -> the cheap call wing is the wider side.
        self.assertLess(_SPOT - short_put["strike"], short_call["strike"] - _SPOT)


# --------------------------------------------------------------------------- #
# Wave 4B: candidate breadth (matrix expand + expiry/delta fallback + count).
# --------------------------------------------------------------------------- #

class TestCandidateBreadth(unittest.TestCase):
    def test_bearish_rich_tries_at_least_two(self):
        # goal 5a: bearish/rich now also tries a debit-put-vertical fallback -> >= 2 tried.
        chain = _chain()
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bearish", "rich_vs_realized", one_sigma=8.0)
        self.assertGreaterEqual(tried, 2)
        names = {s["name"] for s in rec}
        # both a sell-calls credit and a buy-puts debit are candidates.
        self.assertIn("bear_call_spread", names)
        self.assertIn("long_put_vertical", names)

    def test_candidates_tried_reported_in_module(self):
        _write_bundle(self.dir, chain=_chain(),
                      snapshot=_snapshot(iv_minus_rv=0.05),   # rich
                      composite=_composite_doc(grade="D"),    # bearish
                      tradeplan=_tradeplan_doc())
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        self.assertIn("candidates_tried", doc)
        self.assertGreaterEqual(doc["candidates_tried"], 2)
        # the fixture ivs are all equal -> rr_25d 0.0 -> balanced (skew read is emitted).
        self.assertEqual(doc["skew_verdict"], "balanced")
        self.assertIn("skew_rr_25d", doc)

    def test_expiry_fallback_when_primary_all_illiquid(self):
        # Make EVERY near-expiry put illiquid so bull_put_spread + CSP both fail at the
        # primary expiry; the far expiry is fully liquid -> the fallback fires and a
        # structure is recommended at the NEXT listed expiry.
        chain = _chain()
        for c in chain:
            if c["expiration"] == _NEAR_EXP and c["type"] == "put":
                c["oi"] = 5
        rec, declined, tried = opts.select_structures(
            chain, _NEAR_EXP, "bullish", "rich_vs_realized", one_sigma=8.0,
            all_expiries=[_NEAR_EXP, _FAR_EXP])
        # the fallback disclosure decline is present, and something recovered at _FAR_EXP.
        self.assertTrue(any("fallback" in d.get("name", "") for d in declined))
        self.assertTrue(rec, "expiry fallback should recover a structure at the far expiry")
        self.assertTrue(all(s["expiry"] == _FAR_EXP for s in rec))
        # tried counts BOTH expiries' attempts (breadth visible).
        self.assertGreaterEqual(tried, 4)

    def test_delta_retry_on_illiquid_short(self):
        # the 0.30Δ short put (strike 90) is illiquid; pick_short_by_delta retries the
        # adjacent 0.25Δ pick (strike 85, |Δ|0.22, liquid) instead of the illiquid 90.
        chain = _chain()
        for c in chain:
            if c["expiration"] == _NEAR_EXP and c["type"] == "put" and c["strike"] == 90.0:
                c["oi"] = 5
        short = opts.pick_short_by_delta(chain, _NEAR_EXP, "put", 0.30)
        self.assertEqual(short["strike"], 85.0)   # retried to the liquid adjacent delta

    def test_delta_retry_returns_primary_when_all_illiquid(self):
        # if neither the primary nor the adjacent deltas are liquid, the primary is
        # returned (breadth exhausted, not hidden -- the liquidity gate declines it).
        chain = _chain()
        for c in chain:
            if c["expiration"] == _NEAR_EXP and c["type"] == "put":
                c["oi"] = 5
        short = opts.pick_short_by_delta(chain, _NEAR_EXP, "put", 0.30)
        self.assertEqual(short["strike"], 90.0)   # falls back to the primary pick

    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _run(self, extra=None):
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir]
        if extra:
            cmd += extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def _read(self):
        with open(os.path.join(self.dir, "module_options.json")) as fh:
            return json.load(fh)


# --------------------------------------------------------------------------- #
# Wave 4B: IV-crush simulation (priced via chain.bs_price, gated on crush_ev).
# --------------------------------------------------------------------------- #

def _crush_leg(strike, opt_type, side, mark, iv, expiration=_NEAR_EXP, oi=500):
    """One structure leg + its matching contract (crush sim looks up iv on contracts)."""
    leg = {"side": side, "type": opt_type, "strike": float(strike), "mark": float(mark)}
    contract = {"expiration": expiration, "type": opt_type, "strike": float(strike),
                "delta": 0.5, "mark": float(mark), "iv": iv, "oi": oi}
    return leg, contract


class TestCrushSim(unittest.TestCase):
    def test_bs_price_is_actually_called_repriced_legs_differ(self):
        # A long call bought at RICH pre-earnings iv (0.90) is repriced at the CRUSHED
        # iv (0.90 * 0.62 = 0.558) with less time -> the repriced leg differs from entry.
        from scripts import chain as chain_mod
        entry = chain_mod.bs_price(100.0, 100.0, 36 / 365.0, 0.0, 0.90, "call")
        crushed = chain_mod.bs_price(100.0, 100.0, 31 / 365.0, 0.0,
                                     0.90 * opts.IV_CRUSH_FACTOR, "call")
        self.assertNotAlmostEqual(entry, crushed, places=3)
        self.assertLess(crushed, entry)   # crush + theta lowers the price

    def test_long_premium_event_structure_negative_ev_declined(self):
        # A long call bought rich into the print dies on the crush -> crush_ev < 0 ->
        # the structure is DECLINED with the negative-crush-adjusted-EV reason.
        from scripts import chain as chain_mod
        entry = chain_mod.bs_price(100.0, 100.0, 36 / 365.0, 0.0, 0.90, "call")
        leg, contract = _crush_leg(100.0, "call", "long", entry, 0.90)
        st = {"name": "long_call", "type": "debit", "expiry": _NEAR_EXP, "legs": [leg]}
        rec, declined = opts.apply_crush_gate(
            [st], [], event_in_horizon=True, contracts=[contract], spot=100.0,
            one_sigma=5.0, t_post_years=(36 - 5) / 365.0, r=0.0)
        self.assertEqual(rec, [])   # declined out
        self.assertTrue(any("crush" in d["reason"].lower() for d in declined))
        self.assertLess(st["crush_ev"], 0)
        self.assertFalse(st["survives_crush"])

    def test_short_vol_event_structure_survives_crush(self):
        # A short-vol credit spread PROFITS from the crush (sells rich vol pre-print,
        # buys it back cheaper) -> crush_ev > 0 -> survives_crush True, kept.
        sp_leg, sp_c = _crush_leg(90.0, "put", "short", 2.40, 0.55)
        lp_leg, lp_c = _crush_leg(85.0, "put", "long", 1.50, 0.55)
        st = {"name": "bull_put_spread", "type": "credit_spread", "expiry": _NEAR_EXP,
              "legs": [sp_leg, lp_leg]}
        rec, declined = opts.apply_crush_gate(
            [st], [], event_in_horizon=True, contracts=[sp_c, lp_c], spot=100.0,
            one_sigma=4.0, t_post_years=(36 - 5) / 365.0, r=0.0)
        self.assertEqual(len(rec), 1)
        self.assertTrue(st["survives_crush"])
        self.assertGreater(st["crush_ev"], 0)

    def test_non_event_structure_skips_crush_gate(self):
        # NOT event_in_horizon -> the crush gate does not apply: crush_ev None,
        # survives_crush True, and a note says the gate was not applied.
        sp_leg, sp_c = _crush_leg(90.0, "put", "short", 2.40, 0.55)
        lp_leg, lp_c = _crush_leg(85.0, "put", "long", 1.50, 0.55)
        st = {"name": "bull_put_spread", "type": "credit_spread", "expiry": _NEAR_EXP,
              "legs": [sp_leg, lp_leg]}
        rec, declined = opts.apply_crush_gate(
            [st], [], event_in_horizon=False, contracts=[sp_c, lp_c], spot=100.0,
            one_sigma=4.0, t_post_years=None, r=0.0)
        self.assertEqual(len(rec), 1)
        self.assertIsNone(st["crush_ev"])
        self.assertTrue(st["survives_crush"])
        self.assertIn("not applied", st["crush_note"].lower())

    def test_crush_ev_unpriceable_when_leg_iv_missing(self):
        # a leg whose contract lacks iv cannot be crush-priced -> disclosed, not gated.
        leg = {"side": "long", "type": "call", "strike": 100.0, "mark": 5.0}
        contract = {"expiration": _NEAR_EXP, "type": "call", "strike": 100.0,
                    "mark": 5.0, "oi": 500}   # no iv
        st = {"name": "long_call", "type": "debit", "expiry": _NEAR_EXP, "legs": [leg]}
        rec, declined = opts.apply_crush_gate(
            [st], [], event_in_horizon=True, contracts=[contract], spot=100.0,
            one_sigma=5.0, t_post_years=0.08, r=0.0)
        self.assertEqual(len(rec), 1)         # not gated (unpriceable)
        self.assertIsNone(st["crush_ev"])
        self.assertTrue(st["survives_crush"])

    def test_iv_crush_factor_disclosed_constant(self):
        # the 0.62 factor is a labeled module constant (cited/provisional, falsifiable).
        self.assertAlmostEqual(opts.IV_CRUSH_FACTOR, 0.62, places=4)
        # the scenario probability weights are a symmetric set summing to 1.0.
        self.assertAlmostEqual(sum(opts._CRUSH_SCENARIO_PROBS), 1.0, places=6)
        self.assertEqual(len(opts._CRUSH_SCENARIO_SIGMAS), len(opts._CRUSH_SCENARIO_PROBS))


class TestCrushGateCLI(unittest.TestCase):
    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _run(self, extra=None):
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir]
        if extra:
            cmd += extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def _read(self):
        with open(os.path.join(self.dir, "module_options.json")) as fh:
            return json.load(fh)

    def test_crush_fields_present_on_recommended(self):
        # earnings inside the selected-expiry horizon -> recommended structures carry
        # crush_ev + survives_crush (the crush sim ran through the CLI).
        _write_bundle(self.dir, chain=_chain(),
                      snapshot=_snapshot(iv_minus_rv=0.05,          # rich -> credit
                                         earnings_date="2026-08-10"),  # < near expiry
                      composite=_composite_doc(grade="B"),
                      tradeplan=_tradeplan_doc(days_to_catalyst=25))
        proc = self._run(extra=["--mode", "pipeline"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self._read()
        for s in doc["recommended_structures"]:
            self.assertIn("survives_crush", s)
            self.assertIn("crush_ev", s)


if __name__ == "__main__":
    unittest.main()
