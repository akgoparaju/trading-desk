"""Tests for scripts/render_charts.py -- the deterministic 16-chart pack.

WHY: the charts carry NUMBERS onto the page, so -- exactly like the md report --
the architecture forbids the LLM from touching them. Each chart is split into a
PURE ``extract_<name>(docs) -> dict`` (unit-tested here against fixture bundles
for EXACT arrays, so a mis-wired data source is a test failure) and a
``draw_<name>(data, path)`` that only paints the extracted dict. ``docs`` is the
loaded bundle: {snapshot, module_<x> dicts, daily rows}. Missing inputs -> the
chart is skipped with a ``charts_manifest.json`` entry, never fabricated.

matplotlib-dependent draw smoke tests are guarded by
``skipUnless(find_spec("matplotlib"))`` so the base suite is green without it.

stdlib-only for the extract tests; unittest.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import render_charts as rc


_HAS_MPL = importlib.util.find_spec("matplotlib") is not None
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDER_CHARTS = os.path.join(_REPO_ROOT, "scripts", "render_charts.py")


# --------------------------------------------------------------------------- #
# Fixture bundle -- mirrors the REAL bundle shapes for chart-relevant fields.
# The snapshot carries technicals/fundamentals/valuation/events blocks (that is
# where drawdowns_by_year, revisions_90d, pe_median_method, eps_ttm actually
# live -- NOT in the module files), plus a small daily series. Module files
# mirror composite.ev.scenarios, technical.ladder, risk.tables.downside_map /
# vol_profile, options.vol_dashboard / expected_moves / flow.oi_walls, and the
# per-dimension subscores.
# --------------------------------------------------------------------------- #

_LAST = 100.0
_WK52_HI, _WK52_LO = 150.0, 60.0


def _daily_rows(n=300):
    """Ascending daily rows (oldest-first), like parse_daily_rows output. A gentle
    arc from 70 up to 130 and back to 100 so windowing/last-value is testable."""
    rows = []
    import math
    for i in range(n):
        t = i / (n - 1)
        px = 70 + 60 * math.sin(min(t * 2.4, math.pi)) * 0.8
        rows.append({
            "date": "2025-%02d-%02d" % (1 + (i // 28) % 12, 1 + i % 28),
            "open": px, "high": px * 1.01, "low": px * 0.99,
            "close": px, "adjusted_close": px, "volume": 1000.0 + i,
        })
    # Force a known last close for last-price assertions.
    rows[-1]["adjusted_close"] = _LAST
    rows[-1]["close"] = _LAST
    rows[-1]["volume"] = 5000.0  # a volume spike to test the highlighted last bar
    return rows


def _snapshot():
    return {
        "meta": {"ticker": "MU", "as_of_utc": "2026-07-17T00:00:00Z"},
        "price": {"last": _LAST, "prev_close": 106.0,
                  "wk52_high": _WK52_HI, "wk52_low": _WK52_LO},
        "technicals": {
            "ma50": 108.0, "ma200": 90.0,
            "rv20_ann": 0.90, "rv30_ann": 1.10, "rv30_vs_10yr_pctile": 95.0,
            "max_dd_10yr": -0.55,
            "drawdowns_by_year": [
                {"year": 2022, "max_dd": -0.50},
                {"year": 2023, "max_dd": -0.20},
                {"year": 2024, "max_dd": -0.30},
                {"year": 2025, "max_dd": -0.15},
            ],
        },
        "benchmark": {"beta": 1.66},
        "fundamentals": {
            "eps_ttm": 4.5,
            "revisions_90d": {"eps_now": 5.0, "eps_90d_ago": 4.2,
                              "pct": 0.19, "up_30d": 8.0, "down_30d": 1.0},
        },
        "valuation": {
            "pe_ttm": 22.0, "pe_fwd": 12.0,
            "pe_5yr_median": 15.0, "pe_median_method": "trailing_gaap",
        },
        "sentiment": {"consensus_pt": 130.0},
        "events": {
            "next_earnings": {"date": "2026-09-29", "time": "amc",
                              "consensus_eps": 5.2},
            "catalysts": [
                {"date": "2026-07-16",
                 "event": "Second down day -5.65%", "impact": "Bearish"},
                {"date": "2026-09-29",
                 "event": "Q4 FY2026 earnings (AMC)", "impact": "High"},
            ],
        },
    }


_SCENARIOS = [
    {"name": "bull", "prob": 0.35, "price_target": 130.0},
    {"name": "base", "prob": 0.40, "price_target": 110.0},
    {"name": "bear", "prob": 0.25, "price_target": 80.0},
]


def _module_composite():
    dims = [
        {"name": "technical", "score": 47, "weight": 0.25},
        {"name": "fundamental", "score": 75, "weight": 0.25},
        {"name": "sentiment", "score": 80, "weight": 0.20},
        {"name": "risk", "score": 45, "weight": 0.15},
        {"name": "thesis_conviction", "score": 64, "weight": 0.15},
    ]
    return {
        "skill": "composite-score", "ticker": "MU", "score": 62.9, "grade": "B",
        "dimensions": dims,
        "ev": {
            "scenarios": _SCENARIOS,
            "ev_at_current": 0.066, "hurdle_total": 0.12,
            "ev_breakeven_entry": 90.0, "ev_at_levels": [],
        },
    }


def _module_technical():
    return {
        "skill": "technical-analysis", "ticker": "MU", "score": 47,
        "ladder": [
            {"level": 82.0, "type": "swing_low", "basis": "ohlcv",
             "pct_from_last": -0.18},
            {"level": 90.0, "type": "ma200", "basis": "ohlcv",
             "pct_from_last": -0.10},
            {"level": 112.0, "type": "swing_high", "basis": "ohlcv",
             "pct_from_last": 0.12},
            {"level": 130.0, "type": "round_number", "basis": "psychological",
             "pct_from_last": 0.30},
        ],
        "subscores": [
            {"name": "trend_structure", "points": 22, "max": 30},
            {"name": "momentum", "points": 12, "max": 25},
            {"name": "structure_levels", "points": 5, "max": 25},
            {"name": "volume_extension", "points": 8, "max": 20},
        ],
    }


def _module_risk():
    return {
        "skill": "risk-analytics", "ticker": "MU", "score": 45,
        "tables": {
            "downside_map": [
                {"level": 95.0, "type": "swing_low", "basis": "ohlcv",
                 "pct_from_last": -0.05},
                {"level": 90.0, "type": "ma200", "basis": "ohlcv",
                 "pct_from_last": -0.10},
                {"level": 82.0, "type": "swing_low", "basis": "ohlcv",
                 "pct_from_last": -0.18},
                {"level": 70.0, "type": "valuation_floor", "basis": "valuation",
                 "method": "pe_5yr_median x eps_ntm", "pct_from_last": -0.30},
                {"level": 60.0, "type": "stress_scenario", "basis": "judgment",
                 "risk": "DRAM oversupply", "pct_from_last": -0.40},
            ],
            "vol_profile": {"rv20_ann": 0.90, "rv30_ann": 1.10,
                            "rv30_vs_10yr_pctile": 95.0, "beta": 1.66,
                            "max_dd_10yr": -0.55},
        },
        "subscores": [
            {"name": "volatility_state", "points": 6, "max": 25},
            {"name": "drawdown_profile", "points": 8, "max": 25},
            {"name": "margin_of_safety", "points": 14, "max": 30},
            {"name": "liquidity_solvency", "points": 17, "max": 20},
        ],
    }


def _module_sentiment():
    return {
        "skill": "sentiment-positioning", "ticker": "MU", "score": 80,
        "subscores": [
            {"name": "street_view", "points": 23, "max": 25},
            {"name": "revisions_momentum", "points": 20, "max": 20},
            {"name": "smart_money_insiders", "points": 8, "max": 20},
            {"name": "positioning_derivatives", "points": 14, "max": 20},
            {"name": "price_momentum", "points": 15, "max": 15},
        ],
    }


def _module_fundamental():
    return {
        "skill": "fundamental", "ticker": "MU", "score": 75,
        "subscores": [
            {"name": "quality", "points": 50, "max": 50},
            {"name": "valuation", "points": 25, "max": 50},
        ],
    }


def _module_tradeplan():
    return {
        "skill": "trade-plan", "ticker": "MU",
        "stock_plan": {
            "entries": [
                {"level": 90.0, "type": "swing_low", "basis": "swing_low",
                 "ev_at_level": 0.30},
                {"level": 82.0, "type": "ma200", "basis": "ma200",
                 "ev_at_level": 0.45},
            ],
            "exits": {
                "profit_take": {"level": 112.0, "type": "swing_high"},
                "bull_target": {"level": 130.0, "note": "implies 14.7x fwd EPS"},
            },
        },
    }


def _module_options(with_chain=True):
    doc = {
        "skill": "options-strategy", "ticker": "MU", "selected_expiry": "2026-08-21",
        "vol_dashboard": {
            "verdict": "cheap_vs_realized", "iv30": 0.55, "rv20": 0.90,
            "diff": -0.06, "iv_pctile_1yr": 20.0, "skew_25d_30d": 0.068,
            "term_structure": "flat",
            "atm_iv_by_expiry": [
                {"expiry": "2026-07-20", "atm_iv": 0.85},
                {"expiry": "2026-08-21", "atm_iv": 0.62},
                {"expiry": "2026-09-18", "atm_iv": 0.58},
            ],
        },
        "expected_moves": [
            {"expiry": "2026-07-20", "one_sigma": 5.0, "one_sigma_pct": 0.05,
             "straddle": 6.0, "range_low": 95.0, "range_high": 105.0},
            {"expiry": "2026-08-21", "one_sigma": 12.0, "one_sigma_pct": 0.12,
             "straddle": 14.0, "range_low": 88.0, "range_high": 112.0},
        ],
        "flow": {
            "pc_oi": 0.9,
            "oi_walls": {
                "call_wall": {"strike": 130.0, "oi": 442},
                "put_wall": {"strike": 80.0, "oi": 2234},
                "near_money_clusters": [
                    {"strike": 80.0, "oi": 2234, "type": "put"},
                    {"strike": 95.0, "oi": 1053, "type": "put"},
                    {"strike": 130.0, "oi": 442, "type": "call"},
                ],
            } if with_chain else None,
        },
    }
    return doc


def _mk_bundle(dir_, *, with_options_chain=True, with_daily=True):
    """Write a full fixture bundle to ``dir_`` and return the loaded docs dict."""
    with open(os.path.join(dir_, "snapshot_MU_2026-07-17.json"), "w") as fh:
        json.dump(_snapshot(), fh)
    modules = {
        "module_composite.json": _module_composite(),
        "module_technical.json": _module_technical(),
        "module_risk.json": _module_risk(),
        "module_sentiment.json": _module_sentiment(),
        "module_fundamental.json": _module_fundamental(),
        "module_tradeplan.json": _module_tradeplan(),
        "module_options.json": _module_options(with_chain=with_options_chain),
    }
    for name, doc in modules.items():
        with open(os.path.join(dir_, name), "w") as fh:
            json.dump(doc, fh)
    if with_daily:
        raw = os.path.join(dir_, "raw")
        os.makedirs(raw, exist_ok=True)
        # Write an AV-shaped daily file so load_docs' parse path is exercised.
        ts = {}
        for r in _daily_rows():
            ts[r["date"]] = {
                "1. open": str(r["open"]), "2. high": str(r["high"]),
                "3. low": str(r["low"]), "4. close": str(r["close"]),
                "5. adjusted close": str(r["adjusted_close"]),
                "6. volume": str(int(r["volume"]))}
        with open(os.path.join(raw, "daily_adjusted.json"), "w") as fh:
            json.dump({"Time Series (Daily)": ts}, fh)
    return rc.load_docs(dir_)


# --------------------------------------------------------------------------- #
# Pure geometry helpers (no matplotlib) -- exact positions.
# --------------------------------------------------------------------------- #

class TestStaggerPositions(unittest.TestCase):
    def test_two_close_values_pushed_apart_symmetrically(self):
        # span 14 < gap 40 -> spread to exactly 40, recentred on mean 857.
        self.assertEqual(rc.stagger_positions([850, 864], 40), [837.0, 877.0])

    def test_already_spaced_values_unchanged(self):
        self.assertEqual(rc.stagger_positions([100, 200], 40), [100.0, 200.0])
        # exactly at the gap is also "spaced" -> untouched.
        self.assertEqual(rc.stagger_positions([100, 140], 40), [100.0, 140.0])

    def test_three_cluster_spreads_around_mean(self):
        # all three within the gap -> even 40-spacing, recentred on mean 860.
        self.assertEqual(rc.stagger_positions([850, 860, 870], 40),
                         [820.0, 860.0, 900.0])

    def test_order_preserved_for_unsorted_input(self):
        # output is aligned to input order: reversed in -> reversed out.
        self.assertEqual(rc.stagger_positions([864, 850], 40), [877.0, 837.0])

    def test_edge_cases(self):
        self.assertEqual(rc.stagger_positions([], 40), [])
        self.assertEqual(rc.stagger_positions([500], 40), [500.0])


class TestClampCalloutY(unittest.TestCase):
    def test_inside_band_unchanged(self):
        self.assertEqual(rc.clamp_callout_y(50, (0, 100)), 50.0)

    def test_above_clamped_to_padded_top(self):
        # pad_frac 0.04 of span 100 = 4 -> top edge is 96.
        self.assertEqual(rc.clamp_callout_y(99, (0, 100)), 96.0)

    def test_below_clamped_to_padded_bottom(self):
        self.assertEqual(rc.clamp_callout_y(-5, (0, 100)), 4.0)

    def test_reversed_ylim_is_normalised(self):
        self.assertEqual(rc.clamp_callout_y(99, (100, 0)), 96.0)

    def test_custom_pad_frac(self):
        # pad_frac 0.1 of span 100 = 10 -> top edge 90.
        self.assertEqual(rc.clamp_callout_y(99, (0, 100), pad_frac=0.1), 90.0)


# --------------------------------------------------------------------------- #
# Extract functions: exact arrays vs the fixture.
# --------------------------------------------------------------------------- #

class TestExtractScenarioFan(unittest.TestCase):
    def test_scenario_fan_exact(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_scenario_fan(docs)
            self.assertEqual(data["last"], 100.0)
            names = [s["name"] for s in data["scenarios"]]
            self.assertEqual(names, ["bull", "base", "bear"])
            targets = [s["price_target"] for s in data["scenarios"]]
            self.assertEqual(targets, [130.0, 110.0, 80.0])
            probs = [s["prob"] for s in data["scenarios"]]
            self.assertEqual(probs, [0.35, 0.40, 0.25])
            # prob-weighted EV endpoint = .35*130 + .40*110 + .25*80 = 109.5
            self.assertAlmostEqual(data["ev_price"], 109.5, places=4)


class TestExtractFootballField(unittest.TestCase):
    def test_football_field_anchors_and_current(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_football_field(docs)
            self.assertEqual(data["last"], 100.0)
            labels = {r["label"] for r in data["rows"]}
            # ladder supports, valuation floor, consensus PT, and scenario targets.
            self.assertIn("Consensus PT", labels)
            self.assertIn("Bull target", labels)
            self.assertIn("Bear target", labels)
            # Consensus PT is a point (lo==hi==130).
            cpt = next(r for r in data["rows"] if r["label"] == "Consensus PT")
            self.assertEqual(cpt["lo"], 130.0)
            self.assertEqual(cpt["hi"], 130.0)
            # Bull target endpoint is the bull scenario price 130.
            bull = next(r for r in data["rows"] if r["label"] == "Bull target")
            self.assertEqual(bull["hi"], 130.0)

    def test_football_field_ladder_support_two_nearest(self):
        # CONTRACT CHANGE (review finding #5): the "Ladder support" anchor used to
        # span min..max of ALL supports below the price -- on the real MU bundle
        # that was 370..850, a meaningless ~480-wide bar. NEW contract: the band
        # between the TWO nearest proven supports BELOW last (the floor directly
        # beneath the price). This fixture injects a dense ladder below last=100:
        # supports below = [70, 80, 90, 95]; two nearest = [90, 95].
        #   OLD expectation: lo=70,  hi=95  (min..max of all four)
        #   NEW expectation: lo=90,  hi=95  (two nearest below the price)
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            docs["module_technical"]["ladder"] = [
                {"level": float(x), "type": "swing_low"}
                for x in (70, 80, 90, 95, 112, 130)]
            data = rc.extract_football_field(docs)
            support = next(r for r in data["rows"]
                           if r["label"] == "Ladder support")
            self.assertEqual(support["kind"], "band")
            self.assertEqual(support["lo"], 90.0)
            self.assertEqual(support["hi"], 95.0)

    def test_football_field_ladder_support_single_is_dot(self):
        # With only ONE support below the price the anchor degrades to a dot at
        # that level (NEW contract; the OLD code made a zero-width band).
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            docs["module_technical"]["ladder"] = [
                {"level": 85.0, "type": "swing_low"},
                {"level": 130.0, "type": "round_number"}]
            data = rc.extract_football_field(docs)
            support = next(r for r in data["rows"]
                           if r["label"] == "Ladder support")
            self.assertEqual(support["kind"], "dot")
            self.assertEqual(support["lo"], 85.0)
            self.assertEqual(support["hi"], 85.0)


class TestExtractScoreBars(unittest.TestCase):
    def test_score_bars_five_dims_with_weights(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_score_bars(docs)
            self.assertEqual([b["label"] for b in data["bars"]],
                             ["Technical", "Fundamental", "Sentiment", "Risk",
                              "Conviction"])
            self.assertEqual([b["score"] for b in data["bars"]],
                             [47, 75, 80, 45, 64])
            # Weights are the contribution weights (as %), visible ticks.
            self.assertEqual([b["weight_pct"] for b in data["bars"]],
                             [25.0, 25.0, 20.0, 15.0, 15.0])
            self.assertAlmostEqual(data["composite"], 62.9, places=4)


class TestExtractRange52w(unittest.TestCase):
    def test_range52w_entry_band_from_tradeplan(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_range52w(docs)
            self.assertEqual(data["low"], 60.0)
            self.assertEqual(data["high"], 150.0)
            self.assertEqual(data["last"], 100.0)
            # entry band = span of tradeplan entry levels 82..90.
            self.assertEqual(data["entry_low"], 82.0)
            self.assertEqual(data["entry_high"], 90.0)


class TestExtractPeBand(unittest.TestCase):
    def test_pe_band_series_and_method(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_pe_band(docs)
            # method label from valuation.pe_median_method.
            self.assertEqual(data["method"], "trailing_gaap")
            self.assertEqual(data["median_pe"], 15.0)
            self.assertEqual(data["eps_ttm"], 4.5)
            # last P/E = last close / eps_ttm = 100 / 4.5.
            self.assertAlmostEqual(data["pe_series"][-1], 100.0 / 4.5, places=4)
            # series length matches the windowed daily series.
            self.assertGreater(len(data["pe_series"]), 100)
            self.assertEqual(len(data["pe_series"]), len(data["dates"]))


class TestExtractRevisions(unittest.TestCase):
    def test_revisions_from_snapshot_fundamentals(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_revisions(docs)
            self.assertEqual(data["eps_now"], 5.0)
            self.assertEqual(data["eps_90d_ago"], 4.2)
            self.assertAlmostEqual(data["pct"], 0.19, places=4)
            self.assertEqual(data["up_30d"], 8.0)
            self.assertEqual(data["down_30d"], 1.0)


class TestExtractCatalystTimeline(unittest.TestCase):
    def test_catalyst_timeline_events(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_catalyst_timeline(docs)
            # next_earnings appears as an event with a date.
            dates = [e["date"] for e in data["events"]]
            self.assertIn("2026-09-29", dates)
            # collision-free placement: 'side' alternates so no two adjacent
            # events share the same side.
            sides = [e["side"] for e in data["events"]]
            for a, b in zip(sides, sides[1:]):
                self.assertNotEqual(a, b)


class TestExtractPriceVolume(unittest.TestCase):
    def test_price_volume_series_and_events(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_price_volume(docs)
            self.assertEqual(data["closes"][-1], 100.0)
            self.assertEqual(len(data["closes"]), len(data["volumes"]))
            self.assertEqual(len(data["closes"]), len(data["dates"]))
            # windowed to ~1yr (<= 260 sessions).
            self.assertLessEqual(len(data["closes"]), 260)
            # ladder shelves present as labeled shelves (not bare markers).
            self.assertTrue(all("label" in s for s in data["shelves"]))

    def test_price_volume_shelves_capped_to_nearest(self):
        # A dense ladder (many rungs) must be pruned to the few shelves nearest
        # the current price so labels do not overlap into an unreadable stack
        # (the price_volume mockup-nit / real-bundle bug). Cap reduced 4 -> 3
        # (review finding #1): four right-edge labels within ~$96 overprinted
        # each other and the price dot.
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            # Inject a 12-rung ladder like the real MU bundle.
            docs["module_technical"]["ladder"] = [
                {"level": float(x), "type": "swing_low"}
                for x in (40, 55, 68, 82, 90, 95, 105, 112, 120, 130, 140, 149)]
            data = rc.extract_price_volume(docs)
            self.assertLessEqual(len(data["shelves"]), 3)
            # The kept shelves are the ones closest to last (100): 95, 105, 90.
            kept = sorted(s["level"] for s in data["shelves"])
            self.assertIn(95.0, kept)
            self.assertIn(105.0, kept)


# --------------------------------------------------------------------------- #
# Detail set extracts.
# --------------------------------------------------------------------------- #

class TestExtractDownsideLadder(unittest.TestCase):
    def test_downside_ladder_from_risk_map(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_downside_ladder(docs)
            levels = [r["level"] for r in data["rungs"]]
            self.assertIn(70.0, levels)   # valuation floor
            self.assertIn(60.0, levels)   # stress scenario
            self.assertEqual(data["last"], 100.0)


class TestExtractDrawdownHistory(unittest.TestCase):
    def test_drawdown_history_from_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_drawdown_history(docs)
            self.assertEqual([b["year"] for b in data["bars"]],
                             [2022, 2023, 2024, 2025])
            self.assertEqual([b["max_dd"] for b in data["bars"]],
                             [-0.50, -0.20, -0.30, -0.15])


class TestExtractVolRegime(unittest.TestCase):
    def test_vol_regime_rv_pctile_beta(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_vol_regime(docs)
            self.assertAlmostEqual(data["rv30"], 1.10, places=4)
            self.assertAlmostEqual(data["pctile"], 95.0, places=4)
            self.assertAlmostEqual(data["beta"], 1.66, places=4)


class TestExtractVolTermStructure(unittest.TestCase):
    def test_vol_term_structure_windowed(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_vol_term_structure(docs)
            self.assertEqual([p["expiry"] for p in data["points"]],
                             ["2026-07-20", "2026-08-21", "2026-09-18"])
            self.assertEqual([p["atm_iv"] for p in data["points"]],
                             [0.85, 0.62, 0.58])


class TestExtractSkew(unittest.TestCase):
    def test_skew_value_and_context(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_skew(docs)
            self.assertAlmostEqual(data["skew_25d_30d"], 0.068, places=4)


class TestExtractExpectedMoveCone(unittest.TestCase):
    def test_expected_move_cone(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_expected_move_cone(docs)
            self.assertEqual(data["last"], 100.0)
            self.assertEqual([m["expiry"] for m in data["moves"]],
                             ["2026-07-20", "2026-08-21"])
            self.assertEqual([m["one_sigma"] for m in data["moves"]],
                             [5.0, 12.0])


class TestExtractOiWalls(unittest.TestCase):
    def test_oi_walls_clusters(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_oi_walls(docs)
            self.assertEqual(data["last"], 100.0)
            strikes = [w["strike"] for w in data["walls"]]
            self.assertIn(80.0, strikes)
            self.assertIn(130.0, strikes)
            # OI values carried through.
            put_wall = next(w for w in data["walls"] if w["strike"] == 80.0)
            self.assertEqual(put_wall["oi"], 2234)


class TestExtractSubscoreBreakdown(unittest.TestCase):
    def test_subscore_breakdown_grid(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d)
            data = rc.extract_subscore_breakdown(docs)
            dims = {p["dimension"] for p in data["panels"]}
            self.assertIn("technical", dims)
            self.assertIn("risk", dims)
            tech = next(p for p in data["panels"]
                        if p["dimension"] == "technical")
            names = [s["name"] for s in tech["subscores"]]
            self.assertIn("trend_structure", names)
            ts = next(s for s in tech["subscores"]
                      if s["name"] == "trend_structure")
            self.assertEqual(ts["points"], 22)
            self.assertEqual(ts["max"], 30)


# --------------------------------------------------------------------------- #
# Missing-input handling: skipped-chart manifest.
# --------------------------------------------------------------------------- #

class TestSkippedManifest(unittest.TestCase):
    def test_oi_walls_skipped_when_no_chain(self):
        with tempfile.TemporaryDirectory() as d:
            docs = _mk_bundle(d, with_options_chain=False)
            # extract returns None (no data) -> the chart is skipped.
            self.assertIsNone(rc.extract_oi_walls(docs))

    def test_manifest_records_skips_with_reason(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d, with_options_chain=False)
            out = os.path.join(d, "charts")
            rc_, o, e = _run(["--bundle", d, "--set", "detail", "--out", out])
            self.assertEqual(rc_, 0, o + e)
            manifest = _load_manifest(out)
            oi = next(m for m in manifest["charts"] if m["chart"] == "oi_walls")
            self.assertEqual(oi["status"], "skipped")
            self.assertIn("reason", oi)
            self.assertTrue(oi["reason"])


# --------------------------------------------------------------------------- #
# CLI + manifest structure (matplotlib-independent parts).
# --------------------------------------------------------------------------- #

def _run(args):
    proc = subprocess.run([sys.executable, RENDER_CHARTS] + args,
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _load_manifest(out):
    with open(os.path.join(out, "charts_manifest.json")) as fh:
        return json.load(fh)


class TestManifestStructure(unittest.TestCase):
    def test_exec_set_lists_eight_charts(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            out = os.path.join(d, "charts")
            rc_, o, e = _run(["--bundle", d, "--set", "exec", "--out", out])
            self.assertEqual(rc_, 0, o + e)
            manifest = _load_manifest(out)
            names = {m["chart"] for m in manifest["charts"]}
            self.assertEqual(names, set(rc.EXEC_CHARTS))
            for m in manifest["charts"]:
                self.assertIn(m["status"], ("ok", "skipped"))

    def test_all_set_lists_sixteen_charts(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            out = os.path.join(d, "charts")
            rc_, o, e = _run(["--bundle", d, "--set", "all", "--out", out])
            self.assertEqual(rc_, 0, o + e)
            manifest = _load_manifest(out)
            names = {m["chart"] for m in manifest["charts"]}
            self.assertEqual(len(names), 16)
            self.assertEqual(names, set(rc.EXEC_CHARTS) | set(rc.DETAIL_CHARTS))


# --------------------------------------------------------------------------- #
# Draw smoke tests -- guarded behind the matplotlib skip.
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_HAS_MPL, "matplotlib not installed")
class TestDrawSmoke(unittest.TestCase):
    def test_all_charts_render_pngs_over_10kb(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            out = os.path.join(d, "charts")
            rc_, o, e = _run(["--bundle", d, "--set", "all", "--out", out])
            self.assertEqual(rc_, 0, o + e)
            manifest = _load_manifest(out)
            ok = [m for m in manifest["charts"] if m["status"] == "ok"]
            # With a full fixture bundle, all 16 should render.
            self.assertEqual(len(ok), 16, [m for m in manifest["charts"]
                                           if m["status"] != "ok"])
            for m in ok:
                png = os.path.join(out, m["png"])
                self.assertTrue(os.path.isfile(png), png)
                self.assertGreater(os.path.getsize(png), 10_000, png)


if __name__ == "__main__":
    unittest.main()
