"""Tests for scripts/render_report.py + scripts/report_qc.py -- the report layer.

WHY: this is the FINAL output layer, the 3-page trade decision report. The
architecture kills LLM-number-leakage BY CONSTRUCTION: render_report.py generates
the ENTIRE report skeleton (every table, header, number) from the bundle's module
JSONs -- LLM prose goes ONLY into marked `<!-- SLOT:... -->` slots. report_qc.py
then verifies the FINAL document numerically against the bundle so a report can
never ship with a number that is not in the bundle.

These tests assemble a realistic minimal bundle from module JSONs that MIRROR the
real shapes emitted by score_composite / trade_plan / options_strategy / the four
scorers / build_snapshot, then assert:
  - render exits 0 and writes the file, with all expected SLOT markers present;
  - every scripted table value traces to a module JSON (spot-check 6+ values);
  - a missing required module -> exit 2 naming it;
  - delta mode shows old/new/Δ and structures added/removed;
  - report_qc on the unfilled skeleton FAILS no_empty_slots;
  - a clean prose fill (no numbers) -> exit 0 all checks;
  - a rogue "$123.45" in prose -> number_provenance FAILS naming it;
  - a corrupt composite score -> composite_arithmetic FAILS;
  - removing the fundamental invalidation leg text -> invalidation FAILS;
  - a 2200-word slot -> word_cap FAILS;
  - a waiver flips a failure to waived;
  - the skeleton is deterministic across two renders.

stdlib-only; unittest.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from scripts import render_report as rr
from scripts import report_qc as rq


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDER = os.path.join(_REPO_ROOT, "scripts", "render_report.py")
QC = os.path.join(_REPO_ROOT, "scripts", "report_qc.py")


# --------------------------------------------------------------------------- #
# Realistic minimal module JSONs (mirror the real shapes).
# --------------------------------------------------------------------------- #

_SCENARIOS = [
    {"name": "bull", "prob": 0.25, "price_target": 150.0},
    {"name": "base", "prob": 0.50, "price_target": 120.0},
    {"name": "bear", "prob": 0.25, "price_target": 80.0},
]
_LAST = 100.0
# ev_at(100) = .25*(1.5-1) + .5*(1.2-1) + .25*(0.8-1) = 0.175
_EV_AT_CURRENT = 0.175
_HURDLE = 0.12
_MEAN_TARGET = 0.25 * 150 + 0.50 * 120 + 0.25 * 80  # = 117.5
_EV_BREAKEVEN = round(_MEAN_TARGET / (1 + _HURDLE), 4)  # 117.5/1.12


def _snapshot_doc():
    """A snapshot stub mirroring build_snapshot's shape: meta (sources/qc/missing/
    schema_version), price (52wk/mktcap), events (next_earnings/catalysts),
    sentiment (implied_move), options (chain_file_path)."""
    return {
        "meta": {
            "ticker": "MU",
            "as_of_utc": "2026-07-16T00:00:00Z",
            "schema_version": "0.2.1",
            "missing": ["short_interest"],
            "api_tier_notes": ["premium tier: 75 req/min"],
            "sources": [
                {"field_group": "global_quote",
                 "endpoint_or_url": "GLOBAL_QUOTE",
                 "retrieved_utc": "2026-07-16T00:00:00Z",
                 "covers": ["price"]},
                {"field_group": "daily_adjusted",
                 "endpoint_or_url": "TIME_SERIES_DAILY_ADJUSTED",
                 "retrieved_utc": "2026-07-15T22:00:00Z",
                 "covers": ["technicals"]},
                {"field_group": "options_chain",
                 "endpoint_or_url": "HISTORICAL_OPTIONS",
                 "retrieved_utc": "2026-07-16T00:00:00Z",
                 "covers": ["options", "sentiment"]},
            ],
            "qc": {
                "passed": True,
                "checks": [],
                "attestation": ("QC attestation for MU as of 2026-07-16: "
                                "8 passed / 0 failed / 0 waived / 1 skipped."),
                "waivers": [],
            },
        },
        "price": {
            "last": _LAST,
            "prev_close": 98.0,
            "wk52_high": 130.0,
            "wk52_low": 60.0,
            "mktcap_computed": 111000000000.0,
            "shares_diluted_m": 1110.0,
            "adv_dollar_3m": 2500000000.0,
        },
        "events": {
            "next_earnings": {"date": "2026-07-30", "time": "post-market",
                              "consensus_eps": 1.9},
            "dividends": {"per_share": None, "ex_date": None, "pay_date": None},
            "catalysts": [],
        },
        "sentiment": {
            "iv_pctile_1yr": 20.0,
            "implied_move_next_earnings_pct": 0.085,
            "short_interest_pct": 3.5,
            "put_call_ratio_full_chain": 0.9,
            "iv30": 0.55,
        },
        "options": {
            "chain_file_path": "chain_MU.json",
            "iv_minus_rv20": -0.06,
        },
    }


def _chain_file():
    """A tiny options chain covering the strikes used by module_options."""
    strikes = [80.0, 85.0, 90.0, 95.0, 100.0, 105.0, 110.0]
    contracts = []
    for k in strikes:
        for typ, delta in (("put", -0.30), ("call", 0.30)):
            contracts.append({
                "expiration": "2026-08-21", "type": typ, "strike": k,
                "delta": delta, "mark": 2.0, "bid": 1.9, "ask": 2.1,
                "oi": 500, "volume": 100, "iv": 0.55,
            })
    return {"data": contracts}


def _technical_doc():
    ladder = [
        {"level": 82.0, "type": "swing_low", "basis": "ohlcv", "pct_from_last": -0.18},
        {"level": 90.0, "type": "ma200", "basis": "ohlcv", "pct_from_last": -0.10},
        {"level": 95.0, "type": "swing_low", "basis": "ohlcv", "pct_from_last": -0.05},
        {"level": 112.0, "type": "swing_high", "basis": "ohlcv", "pct_from_last": 0.12},
        {"level": 120.0, "type": "call_wall", "basis": "options_oi",
         "pct_from_last": 0.20},
    ]
    return {
        "skill": "technical-analysis",
        "rubric_version": "1.0.0",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "score": 70,
        "subscores": [
            {"name": "trend_structure", "points": 22, "max": 30,
             "arithmetic": "price 100 > ma50 96: +8"},
            {"name": "momentum", "points": 18, "max": 25,
             "arithmetic": "rsi 58 -> 15/15"},
        ],
        "trend_claim": "uptrend",
        "ladder": ladder,
        "flags": {"divergence": "none", "divergence_justification": None},
        "renormalized": False,
    }


def _risk_doc():
    downside_map = [
        {"level": 95.0, "type": "swing_low", "basis": "ohlcv", "pct_from_last": -0.05},
        {"level": 94.0, "type": "valuation_floor", "basis": "valuation",
         "method": "pe_5yr_median x eps_ntm", "pct_from_last": -0.06},
        {"level": 90.0, "type": "ma200", "basis": "ohlcv", "pct_from_last": -0.10},
        {"level": 82.0, "type": "swing_low", "basis": "ohlcv", "pct_from_last": -0.18},
        {"level": 70.0, "type": "stress_scenario", "basis": "judgment",
         "risk": "HBM oversupply", "pct_from_last": -0.30},
    ]
    return {
        "skill": "risk-analytics",
        "rubric_version": "1.0.0",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "score": 45,
        "subscores": [
            {"name": "volatility_state", "points": 14, "max": 25,
             "arithmetic": "rv30_vs_10yr_pctile 45 -> 14/20"},
        ],
        "tables": {
            "downside_map": downside_map,
            "vol_profile": {"rv20_ann": 0.9, "rv30_ann": 0.92, "beta": 1.3},
        },
        "flags": {"stress_pct": -0.30, "top_risk": "HBM oversupply"},
        "renormalized": False,
    }


def _sentiment_doc():
    return {
        "skill": "sentiment-positioning",
        "rubric_version": "1.0.0",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "score": 62,
        "subscores": [
            {"name": "street_view", "points": 18, "max": 25,
             "arithmetic": "buy_pct 72% -> 10/10"},
        ],
        "tables": {
            "positioning": {
                "short_interest_pct": 3.5,
                "put_call_ratio_full_chain": 0.9,
                "iv30": 0.55,
                "iv_pctile_1yr": 20.0,
                "implied_move_next_earnings_pct": 0.085,
            },
            "momentum_vs_spy": {"ret_3m": 0.12, "spy_ret_3m": 0.04, "rel_3m": 0.08},
            "hedging_cost_note": None,
        },
        "flags": {},
        "renormalized": False,
    }


def _fundamental_doc():
    return {
        "skill": "fundamental",
        "rubric_version": "1.0.0",
        "fundamental_mode": "compressed_snapshot_pass",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "score": 55,
        "subscores": [
            {"name": "quality", "points": 30, "max": 50,
             "arithmetic": "rev_growth_latest_q 0.25 -> 15/15"},
            {"name": "valuation", "points": 25, "max": 50,
             "arithmetic": "pe_fwd 12 / pe_5yr_median 15 = 0.8 -> 14/20"},
        ],
        "tables": {
            "quality": {"rev_growth_latest_q": 0.25, "gm_ttm": 0.45},
            "valuation": {"pe_fwd": 12.0, "pe_5yr_median": 15.0, "peg": 0.8},
        },
        "flags": {},
        "renormalized": False,
    }


def _composite_doc(score=59.9, ev_at_current=_EV_AT_CURRENT, profile="balanced"):
    """module_composite mirroring score_composite: dimensions (score/weight/
    contribution), thesis_conviction, ev (scenarios/ev_at_current/breakeven),
    sensitivity (3 profiles w/ grade), flags."""
    # dimensions: weights .25/.25/.20/.15/.15, scores 70/55/62/45/60 (thesis 60).
    dims = [
        {"name": "technical", "score": 70, "weight": 0.25,
         "weight_renormalized": 0.25, "contribution": 17.5, "source": "module_technical.json"},
        {"name": "fundamental", "score": 55, "weight": 0.25,
         "weight_renormalized": 0.25, "contribution": 13.75, "source": "module_fundamental.json"},
        {"name": "sentiment", "score": 62, "weight": 0.20,
         "weight_renormalized": 0.20, "contribution": 12.4, "source": "module_sentiment.json"},
        {"name": "risk", "score": 45, "weight": 0.15,
         "weight_renormalized": 0.15, "contribution": 6.75, "source": "module_risk.json"},
        {"name": "thesis_conviction", "score": 60, "weight": 0.15,
         "weight_renormalized": 0.15, "contribution": 9.0, "source": "computed"},
    ]
    # Σ contribution = 17.5+13.75+12.4+6.75+9.0 = 59.4 -> use that as score.
    composite_score = round(sum(d["contribution"] for d in dims), 4)
    grade = "C" if composite_score < 60 else "B"
    return {
        "skill": "composite-score",
        "rubric_version": "1.1.0",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "profile": profile,
        "score": composite_score,
        "grade": grade,
        "action": "Hold/Trim" if grade == "C" else "Hold/Accumulate-on-weakness",
        "dimensions": dims,
        "thesis_conviction": {
            "score": 60,
            "subscores": ["ev_asymmetry: ratio 0.97 -> 24/40",
                          "variant some -> 12/20",
                          "catalyst_clarity clear -> 20/20",
                          "invalidation both-legs -> 20/20"],
        },
        "ev": {
            "scenarios": _SCENARIOS,
            "scenario_reasoning": "HBM demand asymmetric into the ramp",
            "ev_at_current": ev_at_current,
            "hurdle_total": _HURDLE,
            "horizon_years_convention": 1.5,
            "ev_breakeven_entry": _EV_BREAKEVEN,
            "ev_at_levels": [],
        },
        "sensitivity": {
            "trader": {"score": 61.2, "grade": "B"},
            "balanced": {"score": composite_score, "grade": grade},
            "long-term": {"score": 57.3, "grade": "C"},
            # score_composite stamps this string label alongside the profile dicts;
            # the fixture MUST carry it so the suite exercises the real shape (a
            # real-data E2E found render_report crashing on this key — build_composite_table
            # iterated it and called .get() on the string).
            "weight_set": "standard v1",
        },
        "flags": {
            "variant": "some", "variant_justification": "consensus underrates HBM",
            "catalyst_clarity": "clear", "catalyst_clarity_justification": "print in 14d",
            "invalidation": "both-legs", "invalidation_justification": "stop + metric",
            # composite-v1.1.0 (Goal A): base-rate check skipped here (no history in
            # this fixture) -> disclosed, not a hard gate.
            "base_rate_check": {
                "base_rates": {"bull": None, "base": None, "bear": None},
                "deviations": {}, "flagged": False, "n_history": 0,
                "threshold_pp": 25, "skipped": True,
                "skip_reason": "insufficient earnings-move history (n=0 < 4); "
                               "base-rate check skipped",
            },
        },
        "renormalization_note": None,
        # composite-v1.1.0 (Goal C): tension auto-populates when the evidence spread
        # fires; this fixture's spread (70-45=25) does NOT exceed 25 -> stays null.
        "tension": None,
        "note": "composite-v1.1.0 PROVISIONAL",
        "signal": None,
    }


def _tradeplan_doc(entry1=95.0, invalidation_metric="HBM revenue growth"):
    """module_tradeplan mirroring trade_plan (synthesized). entries/exits/
    invalidation (both legs)/sizing/hedge/dont_chase + expression w/ structures."""
    return {
        "skill": "trade-plan",
        "rubric_version": "1.1.0",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "profile": "balanced",
        "stock_plan": {
            "entries": [
                {"level": entry1, "type": "swing_low",
                 "basis": "swing_low, confluent with valuation anchor 94",
                 "confluence": True, "confluence_anchor": 94.0,
                 "condition": "resting limit at 95 (swing_low, confluent with 94)",
                 "ev_at_level": 0.2368},
                {"level": 90.0, "type": "ma200",
                 "basis": "ma200", "confluence": False, "confluence_anchor": None,
                 "condition": "resting limit at 90 (ma200)",
                 "ev_at_level": 0.3056},
            ],
            "exits": {
                "profit_take": {"level": 112.0, "type": "swing_high"},
                # tradeplan-v1.1.0 (Goal B): no coverage anchors in this fixture ->
                # bull target is the raw scenario bull, untriangulated (disclosed).
                "bull_target": {"level": 150.0, "scenario_raw": 150.0,
                                "dcf_bull": None, "comps_high": None,
                                "triangulated": False, "required_multiple": 27.3,
                                "note": "implies 27.3x fwd EPS"},
            },
            "invalidation": {
                "technical_leg": {"level": 82.0, "condition": "weekly close below"},
                "fundamental_leg": {"metric": invalidation_metric,
                                    "threshold": "< 20% for 2 consecutive quarters",
                                    "justification": "HBM is the margin thesis"},
            },
            "sizing": {
                "entry_level": entry1, "profile": "balanced",
                "binary_event_within_30d": True,
                "f_star": 0.28, "half": 0.14, "quarter": 0.07,
                "recommended_pct": 0.04, "cap_pct": 0.04,
                "rationale": "quarter-Kelly, half-cap on binary event",
                # tradeplan-v1.1.0 (Goal D): headline keeps f* tied to entry + cap.
                "headline": "f* 28.0% at entry 95; capped to 4.0% (4.0% cap)",
                "arithmetic": ("f* 28.0% at entry 95; quarter-Kelly 7.0%; "
                               "binary_event_within_30d=True; cap 4.0%; "
                               "recommended 4.0% -- quarter-Kelly, half-cap on binary event"),
            },
            "hedge": {
                "required": True,
                "trigger": "iv_pctile_1yr 20 <= 25 (cheap protection)",
                "structure": "put spread or collar",
                "strikes_from": [95.0, 94.0],
                "expiry_rule": "first monthly expiry after the event",
                "premium_cap_pct": 0.015,
            },
            "dont_chase": {"above": 99.75, "convention": "5% above top entry"},
        },
        "expression": {
            "rule_version": "expression-v1.0.0",
            "selector_fired": "catalyst",
            "days_to_catalyst": 14,
            "catalyst_in_thesis": True,
            "catalyst_in_thesis_justification": "bull case rests on the print",
            "mode_per_profile": {
                "trader": "defined-risk directional spreads, tenor past catalyst",
                "balanced": "half stock core, half defined-risk options tenored past catalyst",
                "long-term": "stock core + small defined-risk options kicker",
            },
            "modulators": ["IV cheap vs realized: long-premium structures viable",
                           "defined-risk only into the event"],
            "recommended_for_profile": ("half stock core, half defined-risk options "
                                        "tenored past catalyst"),
            "synthesized": True,
            "structures_selected": [
                {"name": "bull_put_spread", "strikes": [90.0, 95.0],
                 "expiry": "2026-08-21"},
            ],
            "hedge_structure": {"type": "put_spread", "cost": 1.5},
            "executable": True,
        },
        "flags": {
            "catalyst_in_thesis": True,
            "fund_invalidation_metric": invalidation_metric,
            "fund_invalidation_threshold": "< 20% for 2 consecutive quarters",
            "fund_invalidation_justification": "HBM is the margin thesis",
        },
        "event_playbook": None,
        "note": "tradeplan-v1.1.0 PROVISIONAL",
        "signal": None,
    }


def _options_doc():
    """module_options mirroring options_strategy: vol_dashboard, recommended_
    structures (legs/strikes/pop/pop_method), declined, hedge_structure."""
    bull_put = {
        "name": "bull_put_spread", "type": "credit_spread", "expiry": "2026-08-21",
        "legs": [
            {"side": "short", "type": "put", "strike": 95.0, "delta": -0.30,
             "mark": 2.0, "oi": 500, "bid": 1.9, "ask": 2.1},
            {"side": "long", "type": "put", "strike": 90.0, "delta": -0.20,
             "mark": 1.0, "oi": 500, "bid": 0.9, "ask": 1.1},
        ],
        "net_credit": 1.0, "max_profit": 1.0, "max_loss": 4.0,
        "breakevens": [94.0], "pop": 0.70,
        "pop_method": "PoP approx = 1 - |delta of short strike| (delta-as-ITM-probability)",
        "arithmetic": "credit = short 95 mark 2 - long 90 mark 1 = 1; width 5",
        "management": ["profit target: close at 50% of max credit"],
        "warnings": [],
        "strikes": [90.0, 95.0],
    }
    return {
        "skill": "options-strategy",
        "rubric_version": "1.1.0",
        "ticker": "MU",
        "as_of": "2026-07-16",
        "mode": "pipeline",
        "direction": "bullish",
        "direction_source": "composite grade C",
        "selected_expiry": "2026-08-21",
        "vol_dashboard": {
            "verdict": "cheap_vs_realized",
            "iv30": 0.55, "rv20": 0.90, "diff": -0.06,
            "iv_pctile_1yr": 20.0,
            "atm_iv_by_expiry": [{"expiry": "2026-08-21", "atm_iv": 0.55}],
            "term_structure": "flat", "skew_25d_30d": 0.02,
        },
        "term_structure": "flat",
        "expected_moves": [
            {"expiry": "2026-08-21", "straddle": 8.5, "one_sigma": 7.2,
             "one_sigma_pct": 0.072},
        ],
        "flow": {"pc_oi": 0.9, "max_pain_by_expiry": [], "oi_walls": None},
        "recommended_structures": [bull_put],
        "declined": [
            {"name": "cash_secured_put",
             "reason": "earnings within 30d: CSP excluded"},
        ],
        "hedge_structure": {
            "type": "put_spread", "expiry": "2026-08-21",
            "legs": [
                {"side": "long", "type": "put", "strike": 95.0, "mark": 2.0, "oi": 500},
                {"side": "short", "type": "put", "strike": 90.0, "mark": 1.0, "oi": 500},
            ],
            "cost": 1.0, "cost_pct_of_spot": 0.01, "premium_cap_pct": 0.015,
            "collar_alternative": None,
        },
        "liquidity_verdict": "adequate",
        "warnings_global": ["BINARY EVENT: earnings in 14d -- defined-risk only"],
        "signal": None,
    }


def _mk_bundle(dir_, *, composite=True, technical=True, risk=True,
               sentiment=True, fundamental=True, tradeplan=True, options=True,
               composite_override=None, tradeplan_override=None):
    """Assemble a full bundle directory. Any block flag False -> omit that file."""
    with open(os.path.join(dir_, "snapshot_MU_2026-07-16.json"), "w") as fh:
        json.dump(_snapshot_doc(), fh)
    with open(os.path.join(dir_, "chain_MU.json"), "w") as fh:
        json.dump(_chain_file(), fh)
    writers = {
        "module_technical.json": (technical, _technical_doc),
        "module_risk.json": (risk, _risk_doc),
        "module_sentiment.json": (sentiment, _sentiment_doc),
        "module_fundamental.json": (fundamental, _fundamental_doc),
        "module_composite.json": (composite,
                                  (lambda: composite_override) if composite_override
                                  else _composite_doc),
        "module_tradeplan.json": (tradeplan,
                                  (lambda: tradeplan_override) if tradeplan_override
                                  else _tradeplan_doc),
        "module_options.json": (options, _options_doc),
    }
    for name, (flag, builder) in writers.items():
        if flag:
            with open(os.path.join(dir_, name), "w") as fh:
                json.dump(builder(), fh)


def _render(bundle, extra=None):
    """Run render_report.py; return (returncode, stdout, stderr)."""
    argv = ["--bundle", bundle] + (extra or [])
    proc = subprocess.run(
        [sys.executable, RENDER] + argv,
        capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _find_report(bundle, delta=False):
    for name in os.listdir(bundle):
        if delta and "Delta_Report" in name and name.endswith(".md"):
            return os.path.join(bundle, name)
        if not delta and "Trade_Report" in name and name.endswith(".md"):
            return os.path.join(bundle, name)
    return None


def _read_file(path):
    with open(path) as fh:
        return fh.read()


# A clean, number-free prose block for a slot (safe to fill anywhere).
_CLEAN_PROSE = ("The setup is constructive but not a table-pound; wait for the "
                "confluence rather than chasing strength into the print.")


def _fill_slots(report_path, overrides=None):
    """Replace every `<!-- SLOT:name -->` with clean prose (or an override)."""
    overrides = overrides or {}
    with open(report_path) as fh:
        text = fh.read()

    def repl(m):
        name = m.group(1)
        return overrides.get(name, _CLEAN_PROSE)

    text = re.sub(r"<!-- SLOT:([a-z_]+) -->", repl, text)
    with open(report_path, "w") as fh:
        fh.write(text)


def _qc(bundle, report, extra=None):
    proc = subprocess.run(
        [sys.executable, QC, "--bundle", bundle, "--report", report]
        + (extra or []),
        capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- #
# Render: exit 0, file exists, all slots present, determinism.
# --------------------------------------------------------------------------- #

class TestRenderSkeleton(unittest.TestCase):
    def test_render_exit0_and_file_exists(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            rc, out, err = _render(d)
            self.assertEqual(rc, 0, err)
            report = _find_report(d)
            self.assertIsNotNone(report, "report file not written")
            self.assertTrue(os.path.isfile(report))

    def test_three_page_headers_present(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _render(d)
            text = _read_file(_find_report(d))
            self.assertIn("## Page 1 — Decision", text)
            self.assertIn("## Page 2 — Evidence", text)
            self.assertIn("## Page 3 — Context & Protocol", text)

    def test_all_expected_slot_markers_present(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _render(d)
            text = _read_file(_find_report(d))
            for slot in ("tension", "event_playbook", "brief_technical",
                         "brief_fundamental", "brief_sentiment", "brief_risk",
                         "brief_thesis", "signal_technical", "catalyst_notes",
                         "monitoring_notes"):
                self.assertIn(f"<!-- SLOT:{slot} -->", text,
                              f"missing slot {slot}")

    def test_skeleton_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            _mk_bundle(d1)
            _mk_bundle(d2)
            _render(d1)
            _render(d2)
            t1 = _read_file(_find_report(d1))
            t2 = _read_file(_find_report(d2))
            self.assertEqual(t1, t2)


# --------------------------------------------------------------------------- #
# Scripted table values trace to module JSONs (spot-check 6+ values).
# --------------------------------------------------------------------------- #

class TestScriptedValuesTrace(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        _mk_bundle(self.d)
        _render(self.d)
        with open(_find_report(self.d)) as fh:
            self.text = fh.read()

    def test_composite_score_in_report(self):
        # composite score 59.4 (Σ contributions) printed.
        self.assertIn("59.4", self.text)

    def test_last_price_in_report(self):
        self.assertIn("100", self.text)

    def test_entry_level_in_report(self):
        self.assertIn("95", self.text)   # entry_1

    def test_entry2_level_in_report(self):
        self.assertIn("90", self.text)   # entry_2

    def test_strike_in_report(self):
        # bull_put_spread strikes 90/95 both appear.
        self.assertIn("95", self.text)
        self.assertIn("90", self.text)

    def test_ev_breakeven_in_report(self):
        # The renderer's _fmt uses %g (6 sig figs): 104.9107 -> "104.911".
        self.assertIn(rr._fmt(_EV_BREAKEVEN), self.text)

    def test_bull_target_in_report(self):
        self.assertIn("150", self.text)

    def test_implied_move_in_report(self):
        # implied_move_next_earnings_pct 0.085 rendered (as 0.085 or 8.5%).
        self.assertTrue("0.085" in self.text or "8.5" in self.text)

    def test_grade_and_action_in_report(self):
        self.assertIn("Hold/Trim", self.text)

    def test_disclaimer_present(self):
        self.assertIn("not financial advice", self.text.lower())

    def test_rubric_versions_in_footer(self):
        self.assertIn("expression-v1.0.0", self.text)


class TestProvisionalDisclosuresFooter(unittest.TestCase):
    """Code-review fix: the PROVISIONAL module notes (stamped into the module
    JSONs by the v1.1.0 scorers) must actually RENDER in the integrity footer, so
    a reader sees a rubric is UNRATIFIED, not just its version number."""

    def _footer(self, modules):
        snap = {"meta": {"as_of_utc": "2026-07-21T00:00:00Z",
                         "qc": {"passed": True, "attestation": "QC passed"}}}
        return rr.build_integrity_footer(snap, modules)

    def test_provisional_note_rendered(self):
        modules = {
            "module_risk": {
                "rubric_version": "1.1.0",
                "module_note": "risk-v1.1.0 PROVISIONAL -- event/tail weights "
                               "unratified pending B9; falsifier pre-registered",
            },
        }
        footer = self._footer(modules)
        self.assertIn("Provisional disclosures:", footer)
        self.assertIn("PROVISIONAL", footer)
        self.assertIn("unratified pending B9", footer)

    def test_no_provisional_note_reads_none(self):
        modules = {"module_risk": {"rubric_version": "1.0.0"}}  # no note
        footer = self._footer(modules)
        self.assertIn("Provisional disclosures: none", footer)


# --------------------------------------------------------------------------- #
# Missing required module -> exit 2 naming it.
# --------------------------------------------------------------------------- #

class TestMissingModule(unittest.TestCase):
    def test_missing_options_exit2_names_it(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d, options=False)
            rc, out, err = _render(d)
            self.assertEqual(rc, 2)
            self.assertIn("module_options", err)

    def test_missing_composite_exit2_names_it(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d, composite=False)
            rc, out, err = _render(d)
            self.assertEqual(rc, 2)
            self.assertIn("module_composite", err)

    def test_missing_tradeplan_exit2_names_it(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d, tradeplan=False)
            rc, out, err = _render(d)
            self.assertEqual(rc, 2)
            self.assertIn("module_tradeplan", err)


# --------------------------------------------------------------------------- #
# Delta mode.
# --------------------------------------------------------------------------- #

class TestDeltaMode(unittest.TestCase):
    def test_delta_shows_old_new_delta_and_structures(self):
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            # old bundle: composite ~59.4; new bundle: bump technical score so the
            # composite changes, and change entry_1 90->92 & drop a structure.
            _mk_bundle(old)
            new_comp = _composite_doc()
            # bump the composite score in the new bundle by editing a contribution.
            new_comp["score"] = 65.0
            new_comp["grade"] = "B"
            new_comp["dimensions"][0]["score"] = 90
            new_comp["dimensions"][0]["contribution"] = 22.5
            new_tp = _tradeplan_doc(entry1=92.0)
            # drop the recommended structure in the new plan (structures removed).
            new_tp["expression"]["structures_selected"] = []
            _mk_bundle(new, composite_override=new_comp, tradeplan_override=new_tp)

            rc, out, err = _render(new, ["--delta", "--previous", old])
            self.assertEqual(rc, 0, err)
            report = _find_report(new, delta=True)
            self.assertIsNotNone(report, "delta report not written")
            text = _read_file(report)
            # composite old 59.4 and new 65 both present.
            self.assertIn("59.4", text)
            self.assertIn("65", text)
            # entry level change: old 95/90, new 95/92 -> 92 appears.
            self.assertIn("92", text)
            # structures removed: bull_put_spread named as removed.
            self.assertIn("bull_put_spread", text)
            # delta interpretation slot present.
            self.assertIn("<!-- SLOT:delta_interpretation -->", text)

    def test_delta_qc_passes_with_previous_and_runs_only_1_9_11(self):
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            _mk_bundle(old)
            new_comp = _composite_doc()
            new_comp["score"] = 65.0
            new_comp["grade"] = "B"
            new_comp["dimensions"][0]["score"] = 90
            new_comp["dimensions"][0]["contribution"] = 22.5
            _mk_bundle(new, composite_override=new_comp)
            _render(new, ["--delta", "--previous", old])
            report = _find_report(new, delta=True)
            _fill_slots(report)
            # With --previous the Δ column (65 - 59.4 = 5.6) is in-bundle -> PASS,
            # and only the 3 delta checks run.
            rc, out, err = _qc(new, report, ["--previous", old])
            self.assertEqual(rc, 0, out + err)
            for c in ("number_provenance", "footer_integrity", "no_empty_slots"):
                self.assertIn(c, out)
            # full-report-only checks must NOT run for a delta report.
            self.assertNotIn("composite_arithmetic", out)
            self.assertNotIn("word_cap", out)


# --------------------------------------------------------------------------- #
# Change 3: Trade Report naming + H1 + trading_desk folder layout.
# --------------------------------------------------------------------------- #

class TestTradeReportNaming(unittest.TestCase):
    """Full-mode default output is <TICKER>_Trade_Report_<date>.md with an H1
    that says 'Trade Report'; the delta name/H1 are unchanged."""

    def test_full_report_filename_is_trade_report(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            rc, out, err = _render(d)
            self.assertEqual(rc, 0, err)
            names = [n for n in os.listdir(d) if n.endswith(".md")]
            self.assertEqual(len(names), 1, names)
            self.assertEqual(names[0], "MU_Trade_Report_2026-07-16.md")
            self.assertNotIn("Trade_Decision", names[0])

    def test_full_report_h1_says_trade_report(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _render(d)
            text = _read_file(_find_report(d))
            self.assertIn("# MU — Trade Report (2026-07-16)", text)
            self.assertNotIn("Trade Decision Report", text)

    def test_default_out_helper_names(self):
        # Pure-function contract for _default_out.
        snap = _snapshot_doc()
        full = rr._default_out("/bundle", snap, delta=False)
        delta = rr._default_out("/bundle", snap, delta=True)
        self.assertTrue(full.endswith("MU_Trade_Report_2026-07-16.md"))
        self.assertTrue(delta.endswith("MU_Delta_Report_2026-07-16.md"))

    def test_delta_name_and_h1_unchanged(self):
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            _mk_bundle(old)
            _mk_bundle(new)
            rc, out, err = _render(new, ["--delta", "--previous", old])
            self.assertEqual(rc, 0, err)
            report = _find_report(new, delta=True)
            self.assertIsNotNone(report)
            self.assertIn("Delta_Report", os.path.basename(report))
            text = _read_file(report)
            self.assertIn("# MU — Delta Report (2026-07-16)", text)


class TestDetailReportsFolderLayout(unittest.TestCase):
    """New layout trading_desk_<T>/detail_reports_<date>/: when the bundle dir's
    basename starts with 'detail_reports' the report lands in the PARENT dir;
    legacy bundle names keep the report inside the bundle. --out still overrides."""

    def test_detail_reports_bundle_writes_to_parent(self):
        with tempfile.TemporaryDirectory() as parent:
            bundle = os.path.join(parent, "detail_reports_2026-07-17")
            os.makedirs(bundle)
            _mk_bundle(bundle)
            rc, out, err = _render(bundle)
            self.assertEqual(rc, 0, err)
            # report lands in the PARENT, not the bundle.
            parent_reports = [n for n in os.listdir(parent)
                              if n.endswith(".md")]
            bundle_reports = [n for n in os.listdir(bundle)
                              if n.endswith(".md")]
            self.assertEqual(parent_reports, ["MU_Trade_Report_2026-07-16.md"])
            self.assertEqual(bundle_reports, [])
            self.assertIn(os.path.join(parent, "MU_Trade_Report_2026-07-16.md"),
                          out)

    def test_legacy_bundle_writes_inside_bundle(self):
        with tempfile.TemporaryDirectory() as parent:
            bundle = os.path.join(parent, "MU_2026-07-16")
            os.makedirs(bundle)
            _mk_bundle(bundle)
            rc, out, err = _render(bundle)
            self.assertEqual(rc, 0, err)
            bundle_reports = [n for n in os.listdir(bundle)
                              if n.endswith(".md")]
            self.assertEqual(bundle_reports, ["MU_Trade_Report_2026-07-16.md"])

    def test_out_override_beats_detail_reports_rule(self):
        with tempfile.TemporaryDirectory() as parent:
            bundle = os.path.join(parent, "detail_reports_2026-07-17")
            os.makedirs(bundle)
            _mk_bundle(bundle)
            custom = os.path.join(parent, "custom_name.md")
            rc, out, err = _render(bundle, ["--out", custom])
            self.assertEqual(rc, 0, err)
            self.assertTrue(os.path.isfile(custom))

    def test_detail_reports_delta_writes_to_parent(self):
        with tempfile.TemporaryDirectory() as p_old, \
                tempfile.TemporaryDirectory() as p_new:
            old = os.path.join(p_old, "detail_reports_2026-07-10")
            new = os.path.join(p_new, "detail_reports_2026-07-17")
            os.makedirs(old)
            os.makedirs(new)
            _mk_bundle(old)
            _mk_bundle(new)
            rc, out, err = _render(new, ["--delta", "--previous", old])
            self.assertEqual(rc, 0, err)
            parent_reports = [n for n in os.listdir(p_new)
                              if "Delta_Report" in n and n.endswith(".md")]
            self.assertEqual(parent_reports, ["MU_Delta_Report_2026-07-16.md"])

    def test_report_qc_passes_on_detail_reports_layout(self):
        # A rendered + slot-filled report under the new layout still passes QC
        # (delta detection by filename intact; page headers/H1 allowance intact).
        with tempfile.TemporaryDirectory() as parent:
            bundle = os.path.join(parent, "detail_reports_2026-07-17")
            os.makedirs(bundle)
            _mk_bundle(bundle)
            _render(bundle)
            report = os.path.join(parent, "MU_Trade_Report_2026-07-16.md")
            self.assertTrue(os.path.isfile(report))
            _fill_slots(report)
            rc, out, err = _qc(bundle, report)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("PASS", out)


# --------------------------------------------------------------------------- #
# report_qc: no_empty_slots on the unfilled skeleton.
# --------------------------------------------------------------------------- #

class TestReportQCEmptySlots(unittest.TestCase):
    def test_unfilled_skeleton_fails_no_empty_slots(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _render(d)
            report = _find_report(d)
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1)
            self.assertIn("no_empty_slots", out)
            # Verdict line must say FAIL.
            self.assertIn("FAIL", out)


# --------------------------------------------------------------------------- #
# report_qc: clean fill -> exit 0 all checks.
# --------------------------------------------------------------------------- #

class TestReportQCCleanFill(unittest.TestCase):
    def test_clean_prose_fill_passes_all_checks(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _render(d)
            report = _find_report(d)
            _fill_slots(report)
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("PASS", out)


# --------------------------------------------------------------------------- #
# report_qc: number_provenance catches a rogue number.
# --------------------------------------------------------------------------- #

class TestReportQCNumberProvenance(unittest.TestCase):
    def test_rogue_number_in_prose_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _render(d)
            report = _find_report(d)
            # Inject a number that is NOT anywhere in the bundle into a slot.
            _fill_slots(report, overrides={
                "tension": "The setup targets $123.45 as a hidden objective."})
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1)
            self.assertIn("number_provenance", out)
            self.assertIn("123.45", out)


# --------------------------------------------------------------------------- #
# report_qc hardening: whitelisted string paths + exact-match dates/versions +
# anchored page headers (Findings 1 & 2). End-to-end through the CLI on a real
# skeleton so the whole allowed-set construction is exercised.
# --------------------------------------------------------------------------- #

class TestReportQCProvenanceHardening(unittest.TestCase):
    """Numbers that appear ONLY inside a non-whitelisted string leaf, and
    date/version/page-header shapes that used to be blindly scrubbed, must now
    orphan; legitimate bundle-backed citations must still pass."""

    def _prep(self, d):
        _mk_bundle(d)
        _render(d)
        report = _find_report(d)
        return report

    def test_prose_number_not_a_numeric_leaf_fails(self):
        # "a 72 multiple and RSI 58" -- 72 and 58 are NOT numeric leaves of the
        # bundle and do not live inside any whitelisted string -> both orphan.
        # (58 does appear only inside _technical_doc's momentum arithmetic
        # "rsi 58"; that subscore arithmetic string IS whitelisted, so 58 is
        # in-bundle. 72 appears nowhere -> it must orphan. Use two clearly-absent
        # integers to make the assertion robust regardless of fixture drift.)
        with tempfile.TemporaryDirectory() as d:
            report = self._prep(d)
            _fill_slots(report, overrides={
                "tension": "Targets a 7231 multiple and a 6197 handle."})
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("number_provenance", out)
            self.assertIn("7231", out)
            self.assertIn("6197", out)

    def test_number_only_in_arithmetic_string_but_not_whitelisted_fails(self):
        # A number that lives ONLY inside a NON-whitelisted string leaf must
        # orphan. _snapshot_doc's price.prev_close is 98.0 (a numeric leaf, so
        # allowed); pick an integer that appears in no leaf and no whitelisted
        # string: 4444 is nowhere in the bundle.
        with tempfile.TemporaryDirectory() as d:
            report = self._prep(d)
            _fill_slots(report, overrides={
                "brief_technical": "A resistance shelf sits near 4444 on the tape."})
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("4444", out)

    def test_bogus_version_in_prose_fails(self):
        with tempfile.TemporaryDirectory() as d:
            report = self._prep(d)
            _fill_slots(report, overrides={
                "tension": "Scored under rubric v9.99.99 for this run."})
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("number_provenance", out)
            self.assertIn("v9.99.99", out)

    def test_fake_date_in_prose_fails(self):
        with tempfile.TemporaryDirectory() as d:
            report = self._prep(d)
            _fill_slots(report, overrides={
                "tension": "The real catalyst lands 2031-01-01, far out."})
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("number_provenance", out)
            self.assertIn("2031-01-01", out)

    def test_bundle_timestamp_time_digits_do_not_orphan(self):
        # Live-refresh finding: reused sources put full ISO timestamps in the
        # footer; the date scrub left the :MM:SS digits to orphan as numbers.
        # A timestamp whose DATE is bundle-sourced must pass whole.
        with tempfile.TemporaryDirectory() as d:
            report = self._prep(d)
            # the fixture bundle's as_of date with an arbitrary time-of-day,
            # as a retrieved_utc echo would appear in the Sources footer line
            _fill_slots(report)
            with open(report, "a") as fh:
                fh.write("\nSource retrieved 2026-07-16T18:38:07Z.\n")
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 0, out + err)

    def test_fake_timestamp_date_still_orphans(self):
        with tempfile.TemporaryDirectory() as d:
            report = self._prep(d)
            _fill_slots(report, overrides={
                "tension": "Data pulled 2031-01-01T09:30:00Z, allegedly."})
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("number_provenance", out)

    def test_bogus_page_header_in_prose_fails(self):
        with tempfile.TemporaryDirectory() as d:
            report = self._prep(d)
            _fill_slots(report, overrides={
                "tension": "\n\n## Page 777 — Hidden\n\nsecret content"})
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("number_provenance", out)
            self.assertIn("777", out)

    def test_legit_citations_still_pass(self):
        # A real bundle expiry date, a real rubric version, a numeric-leaf number,
        # and a whitelisted-string number (technical arithmetic "rsi 58") all
        # cited in prose -> pass.
        with tempfile.TemporaryDirectory() as d:
            report = self._prep(d)
            _fill_slots(report, overrides={
                "brief_technical": ("Chain dated 2026-08-21 under rubric v1.0.0; "
                                    "entry near 95 with momentum at rsi 58."),
            })
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("PASS", out)

    def test_footer_attestation_and_api_tier_numbers_pass(self):
        # The scripted footer echoes snapshot meta.qc.attestation ("8 passed ...")
        # and api_tier_notes ("75 req/min") verbatim; those numbers must be
        # in-bundle (whitelisted) so a clean fill passes.
        with tempfile.TemporaryDirectory() as d:
            report = self._prep(d)
            _fill_slots(report)
            text = _read_file(report)
            # Sanity: the footer really carries the attestation / api-tier numbers.
            self.assertIn("8 passed", text)
            self.assertIn("75 req/min", text)
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("PASS", out)


# --------------------------------------------------------------------------- #
# report_qc: composite_arithmetic catches a corrupt composite.
# --------------------------------------------------------------------------- #

class TestReportQCCompositeArithmetic(unittest.TestCase):
    def test_corrupt_composite_score_fails_arithmetic(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _composite_doc()
            # Corrupt: set score to something that != Σ(weight*score).
            comp["score"] = 42.0
            _mk_bundle(d, composite_override=comp)
            _render(d)
            report = _find_report(d)
            _fill_slots(report)
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1)
            self.assertIn("composite_arithmetic", out)


# --------------------------------------------------------------------------- #
# report_qc: invalidation both-legs.
# --------------------------------------------------------------------------- #

class TestReportQCInvalidation(unittest.TestCase):
    def test_missing_fundamental_leg_text_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _render(d)
            report = _find_report(d)
            _fill_slots(report)
            # Strip the fundamental invalidation metric text from the report.
            with open(report) as fh:
                text = fh.read()
            text = text.replace("HBM revenue growth", "REDACTED_METRIC")
            with open(report, "w") as fh:
                fh.write(text)
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1)
            self.assertIn("invalidation_both_legs", out)


# --------------------------------------------------------------------------- #
# report_qc: word_cap.
# --------------------------------------------------------------------------- #

class TestReportQCWordCap(unittest.TestCase):
    def test_word_cap_breach_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _render(d)
            report = _find_report(d)
            padding = " ".join(["padding"] * 2200)
            _fill_slots(report, overrides={"brief_technical": padding})
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1)
            self.assertIn("word_cap", out)


# --------------------------------------------------------------------------- #
# report_qc: waiver mechanics.
# --------------------------------------------------------------------------- #

class TestReportQCWaiver(unittest.TestCase):
    def test_waiver_flips_a_failure(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _render(d)
            report = _find_report(d)
            _fill_slots(report, overrides={
                "tension": "The setup targets $123.45 as a hidden objective."})
            # Without waiver: fails.
            rc, out, err = _qc(d, report)
            self.assertEqual(rc, 1)
            # With waiver on number_provenance: passes (0).
            rc2, out2, err2 = _qc(
                d, report,
                ["--waive", "number_provenance:manual price target disclosed"])
            self.assertEqual(rc2, 0, out2 + err2)
            self.assertIn("WAIVED", out2)


# --------------------------------------------------------------------------- #
# Pure-function unit tests (builder + extraction).
# --------------------------------------------------------------------------- #

class TestNumberExtraction(unittest.TestCase):
    def test_extract_dollar_and_pct_and_decimal(self):
        toks = rq.extract_numbers("entry $95.00, up 8.5%, ev 0.175 on 2026-07-16")
        # Raw tokens are returned verbatim (so an orphan reports as it appears):
        # $ and % are kept on the token.
        self.assertIn("$95.00", toks)
        self.assertIn("8.5%", toks)
        self.assertIn("0.175", toks)
        # the date 2026-07-16 must NOT appear as three orphan numbers.
        self.assertNotIn("2026", toks)
        self.assertFalse(any("2026" in t for t in toks))

    def test_allowed_set_matches_rounding_and_pct(self):
        allowed = rq.build_allowed_set({"a": 0.085, "b": 95.0})
        # 0.085 as a fraction; 8.5 as its %-form; 8.50 rounding all allowed.
        self.assertTrue(rq.is_allowed("0.085", allowed))
        self.assertTrue(rq.is_allowed("8.5", allowed))
        self.assertTrue(rq.is_allowed("95", allowed))
        self.assertTrue(rq.is_allowed("95.0", allowed))

    def test_orphan_not_allowed(self):
        allowed = rq.build_allowed_set({"a": 95.0})
        self.assertFalse(rq.is_allowed("123.45", allowed))

    def test_unparseable_token_is_not_allowed(self):
        # Finding 3: is_allowed returns False for an unparseable token
        # (belt-and-braces; extract_numbers already prefilters parseable tokens).
        allowed = rq.build_allowed_set({"a": 95.0})
        self.assertFalse(rq.is_allowed("--", allowed))
        self.assertFalse(rq.is_allowed("$", allowed))

    def test_numeric_leaves_do_not_scan_strings(self):
        # A number that lives only inside an arbitrary (non-whitelisted) string
        # leaf is NOT admitted by the numeric-leaf scan anymore.
        allowed = rq.build_allowed_set({"note": "target is 777.5 someday"})
        self.assertFalse(rq.is_allowed("777.5", allowed))

    def test_whitelisted_string_numbers_admitted(self):
        # A number inside a whitelisted path (options declined[].reason) is
        # admitted via _iter_whitelisted_string_numbers.
        docs = {"module_options": {"declined": [
            {"name": "csp", "reason": "excluded at 33.7% IV rank"}]}}
        nums = set(rq._iter_whitelisted_string_numbers(docs))
        self.assertIn(33.7, nums)

    def test_allowed_dates_collects_bundle_dates(self):
        docs = {
            "snapshot": {"meta": {"as_of_utc": "2026-07-16T00:00:00Z"}},
            "module_options": {"selected_expiry": "2026-08-21"},
        }
        dates = rq.build_allowed_dates(docs)
        self.assertIn("2026-07-16", dates)
        self.assertIn("2026-08-21", dates)
        self.assertNotIn("2031-01-01", dates)

    def test_allowed_versions_raw_and_v_forms(self):
        docs = {
            "snapshot": {"meta": {"schema_version": "0.2.1"}},
            "module_technical": {"rubric_version": "1.0.0"},
            "module_tradeplan": {"expression": {"rule_version": "expression-v1.0.0"}},
        }
        vers = rq.build_allowed_versions(docs)
        self.assertIn("1.0.0", vers)
        self.assertIn("v1.0.0", vers)
        self.assertIn("0.2.1", vers)
        self.assertIn("v0.2.1", vers)
        self.assertNotIn("9.99.99", vers)

    def test_allowed_stamps_collects_scale_and_weight_set(self):
        docs = {
            "module_fundamental": {"sector_scale": "memory_semis@2026.1"},
            "module_composite": {"weight_set": "CUSTOM deep-value@1.0"},
        }
        stamps = rq.build_allowed_stamps(docs)
        self.assertIn("memory_semis@2026.1", stamps)
        self.assertIn("CUSTOM deep-value@1.0", stamps)
        self.assertIn("deep-value@1.0", stamps)  # the CUSTOM-prefix-free form

    def test_stamp_cited_verbatim_passes_provenance(self):
        # V5 live finding: "2026.1" is not X.Y.Z-shaped, so citing the active
        # scale orphaned its digits. The FULL bundle-carried stamp must pass.
        docs = {
            "snapshot": {"meta": {}},
            "module_fundamental": {"sector_scale": "memory_semis@2026.1"},
            "module_composite": {"weight_set": "CUSTOM deep-value@1.0"},
        }
        res = rq.check_number_provenance(
            "Scored under the memory_semis@2026.1 scale, "
            "weights deep-value@1.0.", docs)
        self.assertTrue(res["passed"], res["detail"])

    def test_fabricated_stamp_orphans(self):
        docs = {
            "snapshot": {"meta": {}},
            "module_fundamental": {"sector_scale": "memory_semis@2026.1"},
        }
        res = rq.check_number_provenance(
            "Scored under memory_semis@2027.9.", docs)
        self.assertFalse(res["passed"])
        self.assertIn("2027.9", res["detail"])

    def test_bare_stamp_version_tail_still_orphans(self):
        # The name@version IS the identity — a bare tail is not admitted.
        docs = {
            "snapshot": {"meta": {}},
            "module_fundamental": {"sector_scale": "memory_semis@2026.1"},
        }
        res = rq.check_number_provenance("Scale version 2026.1 applies.", docs)
        self.assertFalse(res["passed"])
        self.assertIn("2026.1", res["detail"])


# --------------------------------------------------------------------------- #
# QF4: build_catalyst_calendar past-event labeling + empty-note replacement
# --------------------------------------------------------------------------- #

class TestBuildCatalystCalendar(unittest.TestCase):
    """QF4 regression: past rows get '(past)' suffix; empty notes become '—'."""

    def _snap(self, as_of_utc, ne_date, ne_eps, catalysts=None):
        """Minimal snapshot for build_catalyst_calendar."""
        return {
            "meta": {"as_of_utc": as_of_utc},
            "events": {
                "next_earnings": {"date": ne_date, "consensus_eps": ne_eps},
                "catalysts": catalysts or [],
            },
        }

    def test_future_earnings_not_labeled_past(self):
        snap = self._snap("2026-07-16T00:00:00Z", "2026-09-25", 1.88)
        result = rr.build_catalyst_calendar(snap)
        self.assertNotIn("(past)", result)
        self.assertIn("2026-09-25", result)

    def test_past_earnings_labeled_past(self):
        snap = self._snap("2026-07-16T00:00:00Z", "2026-07-01", None)
        result = rr.build_catalyst_calendar(snap)
        self.assertIn("(past)", result)

    def test_empty_earnings_note_replaced_with_dash(self):
        # consensus_eps is None -> note would be empty -> must become "—".
        snap = self._snap("2026-07-16T00:00:00Z", "2026-09-25", None)
        result = rr.build_catalyst_calendar(snap)
        # "—" must appear (either as note or as no-catalyst fallback).
        self.assertIn("—", result)
        # Specifically, the note cell for this row should NOT be a raw empty string.
        # The table contains a pipe-separated row; check there's no "| |" pattern
        # (two consecutive pipes with just spaces indicating empty cell).
        # A simple proxy: "consensus EPS n/a" should NOT appear; "—" should.
        self.assertNotIn("consensus EPS n/a", result)

    def test_past_catalyst_labeled(self):
        snap = self._snap(
            "2026-07-16T00:00:00Z", "2026-09-25", 1.88,
            catalysts=[
                {"name": "product_launch", "date": "2026-07-10", "note": "old launch"},
                {"name": "conf", "date": "2026-08-01", "note": "upcoming conf"},
            ]
        )
        result = rr.build_catalyst_calendar(snap)
        # Past catalyst note must contain "(past)".
        self.assertIn("old launch (past)", result)
        # Future catalyst note must NOT contain "(past)".
        self.assertIn("upcoming conf", result)
        self.assertNotIn("upcoming conf (past)", result)

    def test_empty_catalyst_note_replaced_with_dash(self):
        snap = self._snap(
            "2026-07-16T00:00:00Z", "2026-09-25", 1.88,
            catalysts=[{"name": "ev", "date": "2026-08-01", "note": ""}]
        )
        result = rr.build_catalyst_calendar(snap)
        # Empty note must be replaced by em-dash, not left blank.
        self.assertIn("—", result)

    def test_no_as_of_date_no_past_label(self):
        # When meta.as_of_utc is absent, no "(past)" should appear.
        snap = {
            "meta": {},
            "events": {
                "next_earnings": {"date": "2020-01-01", "consensus_eps": None},
                "catalysts": [],
            },
        }
        result = rr.build_catalyst_calendar(snap)
        self.assertNotIn("(past)", result)


# --------------------------------------------------------------------------- #
# QF5: score_sentiment loud pre-earnings warning when revisions null
# --------------------------------------------------------------------------- #

class TestQF5SentimentPreEarningsWarning(unittest.TestCase):
    """QF5 regression: score_sentiment surfaces a loud warning when revisions_90d
    is null and the snapshot is within 14 days of next_earnings."""

    def _snap_with(self, days_to_earnings, revisions=None, revisions_null_reason=None):
        """Minimal snapshot for build_module."""
        from datetime import date, timedelta
        as_of = date(2026, 7, 16)
        ne_date = (as_of + timedelta(days=days_to_earnings)).isoformat()
        return {
            "meta": {"ticker": "MU", "as_of_utc": "2026-07-16T00:00:00Z"},
            "sentiment": {
                "ratings": {"strong_buy": 10, "buy": 8, "hold": 5,
                            "sell": 1, "strong_sell": 0, "n": 24},
                "pt_vs_price_pct": 0.10,
                "insider_net_90d_usd": 1000.0,
                "short_interest_pct": 5.0,
                "put_call_ratio_full_chain": 0.9,
                "iv_pctile_1yr": 50.0,
            },
            "technicals": {"rsi14": 55.0, "ret_3m": 0.05, "ret_6m": 0.10,
                           "ret_12m": 0.30},
            "benchmark": {"spy_ret_3m": 0.02, "spy_ret_12m": 0.10},
            "fundamentals": {
                "revisions_90d": revisions,
                "revisions_null_reason": revisions_null_reason,
            },
            "events": {
                "next_earnings": {"date": ne_date, "consensus_eps": None},
                "catalysts": [],
            },
        }

    def _build(self, snap):
        from scripts import score_sentiment as ss
        return ss.build_module(snap, "neutral", None, "unknown", None,
                               "normal", None)

    def test_warning_fires_when_null_within_14d(self):
        snap = self._snap_with(10, revisions=None,
                               revisions_null_reason="no_future_fy_row")
        doc = self._build(snap)
        warning = doc["flags"]["revisions_null_pre_earnings_warning"]
        self.assertIsNotNone(warning)
        self.assertIn("WARNING", warning)
        self.assertIn("renormalized", warning.lower())
        self.assertIn("no_future_fy_row", warning)

    def test_warning_in_renormalization_note(self):
        snap = self._snap_with(7, revisions=None,
                               revisions_null_reason="no_future_fy_row")
        doc = self._build(snap)
        note = doc.get("renormalization_note") or ""
        self.assertIn("WARNING", note)

    def test_no_warning_when_revisions_present(self):
        rev = {"pct": 0.02, "up_30d": 9, "down_30d": 3,
               "eps_now": 7.5, "eps_90d_ago": 7.0}
        snap = self._snap_with(10, revisions=rev)
        doc = self._build(snap)
        self.assertIsNone(doc["flags"]["revisions_null_pre_earnings_warning"])

    def test_no_warning_when_beyond_14d(self):
        snap = self._snap_with(20, revisions=None,
                               revisions_null_reason="no_future_fy_row")
        doc = self._build(snap)
        self.assertIsNone(doc["flags"]["revisions_null_pre_earnings_warning"])

    def test_no_warning_on_day_zero_exactly_14(self):
        # 14 days is still within the window (0 <= days <= 14).
        snap = self._snap_with(14, revisions=None,
                               revisions_null_reason="no_future_fy_row")
        doc = self._build(snap)
        self.assertIsNotNone(doc["flags"]["revisions_null_pre_earnings_warning"])


# --------------------------------------------------------------------------- #
# Wave 1: Confidence badge render + QC regression tests (Step 6).
# --------------------------------------------------------------------------- #

def _confidence_block(level, source_level, source_why,
                      depth_level, depth_why,
                      staleness_level, staleness_why):
    """Helper: a well-formed per-module confidence block (mirrors confidence.py output).

    ``level`` is the min(source, depth, staleness); each axis carries its own level
    and why tag so the weakest-axis resolution in _confidence_badge works correctly.
    """
    return {
        "level": level,
        "source": {"level": source_level, "why": source_why},
        "depth": {"level": depth_level, "why": depth_why},
        "staleness": {"level": staleness_level, "why": staleness_why},
        "rule": "min(source, depth, staleness)",
        "version": "1.0.0",
    }


def _composite_confidence_block(level, why):
    """Helper: a well-formed composite roll-up confidence block."""
    return {
        "level": level,
        "why": why,
        "rule": "min over evidence dimensions",
        "version": "1.0.0",
    }


def _inject_confidence_blocks(dir_):
    """Inject well-formed confidence blocks into all module JSONs in a bundle dir.

    Uses MEDIUM for all evidence dimensions (realistic for a premium-AV run at
    rubric 1.0.0 with standard depth).  The why tags are verbatim from
    confidence.py — word-only (digit-free).
    """
    # Per-module confidence blocks (matching confidence.py output for a fresh
    # alpha_vantage premium run at rubric 1.0.0 with standard depth).
    # Axis levels mirror real compute_module() output: source=HIGH for technical/risk,
    # depth=MEDIUM for all (rubric 1.0.0, pre-R-wave), staleness=HIGH (fresh print),
    # sentiment source=MEDIUM (web short-interest by design).
    module_confs = {
        "module_technical.json": _confidence_block(
            "MEDIUM",
            "HIGH", "AV premium",
            "MEDIUM", "pre-regime",
            "HIGH", "fresh print"),
        "module_risk.json": _confidence_block(
            "MEDIUM",
            "HIGH", "AV premium",
            "MEDIUM", "pre-event-aware",
            "HIGH", "fresh print"),
        "module_sentiment.json": _confidence_block(
            "MEDIUM",
            "MEDIUM", "AV premium; web short-interest",
            "MEDIUM", "pre-positioning-dynamics",
            "HIGH", "fresh print"),
        "module_fundamental.json": _confidence_block(
            "MEDIUM",
            "HIGH", "coverage + AV",
            "MEDIUM", "snapshot pass",
            "MEDIUM", "snapshot pass"),
    }
    # Roll-up for composite: MEDIUM (all evidence dims are MEDIUM here).
    comp_conf = _composite_confidence_block(
        "MEDIUM",
        "MEDIUM -- technical pre-regime; sentiment web short-interest",
    )
    for fname, conf in module_confs.items():
        path = os.path.join(dir_, fname)
        if os.path.isfile(path):
            with open(path) as fh:
                doc = json.load(fh)
            doc["confidence"] = conf
            with open(path, "w") as fh:
                json.dump(doc, fh)
    comp_path = os.path.join(dir_, "module_composite.json")
    if os.path.isfile(comp_path):
        with open(comp_path) as fh:
            doc = json.load(fh)
        doc["confidence"] = comp_conf
        with open(comp_path, "w") as fh:
            json.dump(doc, fh)


class TestConfidenceBadgeRender(unittest.TestCase):
    """Step 6 — render/QC integration tests for the Wave 1 confidence badge layer.

    Asserts:
    - per-dimension badge text appears on each evidence dimension headline;
    - the roll-up badge appears on the call line;
    - a rendered report carrying badges passes report_qc exit 0
      (number_provenance clean — all badge tags are digit-free).
    """

    def setUp(self):
        self.d = tempfile.mkdtemp()
        _mk_bundle(self.d)
        _inject_confidence_blocks(self.d)
        _render(self.d)
        report = _find_report(self.d)
        self.assertIsNotNone(report, "report not written")
        with open(report) as fh:
            self.text = fh.read()
        self.report = report

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    # -- Per-dimension headlines carry a badge --------------------------------

    def test_technical_headline_has_badge(self):
        # The technical headline must carry the MEDIUM badge with the depth why-tag
        # (depth = MEDIUM pre-regime is the weakest for a premium AV run).
        self.assertIn("◐ MEDIUM", self.text)
        # The technical-specific depth why must appear on a line starting with ###
        # Technical.
        for line in self.text.splitlines():
            if line.startswith("### Technical"):
                self.assertIn("◐ MEDIUM", line, "no badge on Technical headline")
                self.assertIn("pre-regime", line, "why tag missing on Technical headline")
                break
        else:
            self.fail("### Technical headline not found")

    def test_risk_headline_has_badge(self):
        for line in self.text.splitlines():
            if line.startswith("### Risk"):
                self.assertIn("◐ MEDIUM", line, "no badge on Risk headline")
                self.assertIn("pre-event-aware", line,
                              "why tag missing on Risk headline")
                break
        else:
            self.fail("### Risk headline not found")

    def test_sentiment_headline_has_badge(self):
        for line in self.text.splitlines():
            if "Sentiment" in line and line.startswith("### "):
                self.assertIn("◐ MEDIUM", line, "no badge on Sentiment headline")
                # Sentiment why resolves to 'AV premium; web short-interest'
                # (source is the first axis at MEDIUM level for sentiment).
                self.assertIn("web short-interest", line,
                              "sentiment why tag missing")
                break
        else:
            self.fail("### Sentiment headline not found")

    def test_fundamental_headline_has_badge(self):
        for line in self.text.splitlines():
            if line.startswith("### Fundamental"):
                self.assertIn("◐ MEDIUM", line, "no badge on Fundamental headline")
                break
        else:
            self.fail("### Fundamental headline not found")

    # -- The call line carries the roll-up badge ------------------------------

    def test_call_line_has_rollup_badge(self):
        # The call line starts with '**<grade> — ...' and must contain the roll-up.
        found = False
        for line in self.text.splitlines():
            if line.startswith("**") and "Confidence:" in line:
                self.assertIn("◐ MEDIUM", line, "roll-up glyph missing")
                found = True
                break
        self.assertTrue(found, "call line with Confidence roll-up not found")

    # -- QC regression: badges must pass number_provenance -------------------

    def test_badges_pass_report_qc_number_provenance(self):
        """Rendered report with badges must pass report_qc exit 0.

        This guards against a future why-tag that accidentally contains a digit:
        confidence.py's contract is word-only tags, and if that ever breaks, this
        test will catch the regression before the report ships.
        """
        _fill_slots(self.report)
        rc, out, err = _qc(self.d, self.report)
        self.assertEqual(rc, 0,
                         "report_qc failed with badges present:\n" + out + err)
        self.assertIn("PASS", out)

    # -- Footer carries confidence-version -----------------------------------

    def test_footer_carries_confidence_version(self):
        self.assertIn("confidence-v1.0.0", self.text)

    # -- Older bundles without confidence blocks render gracefully -----------

    def test_no_badge_without_confidence_block(self):
        """Older bundle (no confidence block in modules) still renders without error."""
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)  # no confidence blocks injected
            rc, out, err = _render(d)
            self.assertEqual(rc, 0, err)
            report = _find_report(d)
            text = ""
            with open(report) as fh:
                text = fh.read()
            # Badge glyphs should NOT appear (no confidence block in modules).
            # The report should still render correctly.
            # (The footer may still carry confidence-v1.0.0 from the fallback import.)
            self.assertIn("### Technical", text)
            self.assertIn("### Risk", text)


if __name__ == "__main__":
    unittest.main()
