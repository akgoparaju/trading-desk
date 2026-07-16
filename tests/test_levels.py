"""Tests for scripts/levels.py -- the S/R ladder builder.

WHY: the ladder is the SHARED level vocabulary for every Phase-2 evidence skill.
"No level may appear in a report that is not in this ladder" (spec), so the
detectors here must be exact and reproducible, not approximate. Each test uses a
HAND-CONSTRUCTED price series with known swing points, known round-number grids,
and a Task-3-style options chain fixture so every level, type-label, and basis
string is asserted against a value computed by hand rather than by the code
under test.

stdlib-only; unittest.
"""

import datetime as _dt
import json
import os
import tempfile
import unittest

from scripts import levels


# --------------------------------------------------------------------------- #
# Constructed OHLCV series with KNOWN swing points.
# --------------------------------------------------------------------------- #

def _dates(n, end=_dt.date(2026, 7, 15)):
    """n ascending business-dayish ISO dates ending at ``end`` (oldest-first)."""
    out = []
    day = end
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day = day - _dt.timedelta(days=1)
    out.reverse()
    return out


def _rows_from_closes(closes):
    """Wrap a list of adjusted closes into OHLCV row dicts (oldest-first)."""
    dates = _dates(len(closes))
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "date": dates[i],
            "open": c, "high": c * 1.001, "low": c * 0.999,
            "close": c, "adjusted_close": float(c), "volume": 1_000_000,
        })
    return rows


def _flat_series(n, base=100.0):
    """A perfectly flat series of length n (no extrema of any kind)."""
    return [base] * n


# We build a 300-row series. levels.swing_levels uses only the LAST 252 rows,
# i.e. full-array indices 48..299. We plant unambiguous extrema INSIDE that
# window with full ``window`` margins on both sides.
#
#   - full index 260: a plateau PEAK at 120.0 (a clean swing high)
#   - full index 280: a TROUGH at 80.0 (a clean swing low)
#   - full index 55: a peak at 130.0 that IS inside the last-252 window but we
#     use it to prove window-edge inclusion works when margins are present.
#
# Everything else rides a gentle non-extremal ramp so no accidental extrema.
def _constructed_closes():
    closes = [100.0 + 0.001 * i for i in range(300)]  # gentle monotone ramp
    # Plateau peak at 120 near index 260 (full window margins both sides).
    closes[260] = 120.0
    # Trough at 80 near index 280.
    closes[280] = 80.0
    return closes


class TestSwingLevels(unittest.TestCase):
    def setUp(self):
        self.closes = _constructed_closes()
        self.rows = _rows_from_closes(self.closes)

    def test_detects_known_high_and_low(self):
        levs = levels.swing_levels(self.rows, window=5, dedupe_pct=0.01)
        highs = [x["level"] for x in levs if x["type"] == "swing_high"]
        lows = [x["level"] for x in levs if x["type"] == "swing_low"]
        self.assertIn(120.0, highs)
        self.assertIn(80.0, lows)

    def test_type_and_basis_labels(self):
        levs = levels.swing_levels(self.rows)
        for x in levs:
            self.assertIn(x["type"], ("swing_high", "swing_low"))
            self.assertEqual(x["basis"], "ohlcv")
            self.assertIn("date", x)
            self.assertIsInstance(x["level"], float)

    def test_swing_high_date_matches_the_peak_bar(self):
        levs = levels.swing_levels(self.rows)
        peak = [x for x in levs if x["type"] == "swing_high" and x["level"] == 120.0]
        self.assertEqual(len(peak), 1)
        self.assertEqual(peak[0]["date"], self.rows[260]["date"])

    def test_window_edge_exclusion(self):
        # A peak WITHOUT a full window on both sides must NOT be detected.
        # Put a spike at index 251 within the last-252 window: that is the LAST
        # row, so it has zero right-side margin -> excluded.
        closes = _flat_series(300)
        closes[299] = 999.0  # last bar, no right margin
        rows = _rows_from_closes(closes)
        levs = levels.swing_levels(rows, window=5)
        self.assertNotIn(999.0, [x["level"] for x in levs])

    def test_only_last_252_rows_considered(self):
        # A clean peak at full index 20 (well OUTSIDE the last-252 window, which
        # starts at index 48) must be ignored as stale.
        closes = _flat_series(300)
        closes[20] = 500.0
        rows = _rows_from_closes(closes)
        levs = levels.swing_levels(rows, window=5)
        self.assertNotIn(500.0, [x["level"] for x in levs])

    def test_dedupe_keeps_most_recent(self):
        # Two swing highs within dedupe_pct of each other; the MORE RECENT one
        # (higher index) must survive.
        closes = _flat_series(300, base=100.0)
        # earlier peak at index 200 -> value 150.0
        closes[200] = 150.0
        # later peak at index 260 -> value 150.5 (within 1% of 150.0)
        closes[260] = 150.5
        rows = _rows_from_closes(closes)
        levs = levels.swing_levels(rows, window=5, dedupe_pct=0.01)
        highs = [x for x in levs if x["type"] == "swing_high"]
        surviving = [x for x in highs if abs(x["level"] - 150.0) / 150.0 < 0.02]
        self.assertEqual(len(surviving), 1)
        self.assertEqual(surviving[0]["level"], 150.5)          # the recent one
        self.assertEqual(surviving[0]["date"], rows[260]["date"])


class TestRoundNumbers(unittest.TestCase):
    def test_grid_for_spot_327(self):
        levs = levels.round_numbers(327.0, count=2)
        got = sorted(x["level"] for x in levs)
        self.assertEqual(got, [250.0, 300.0, 350.0, 400.0])
        for x in levs:
            self.assertEqual(x["type"], "round_number")
            self.assertEqual(x["basis"], "psychological")

    def test_grid_for_spot_85(self):
        levs = levels.round_numbers(85.0, count=2)
        got = sorted(x["level"] for x in levs)
        self.assertEqual(got, [75.0, 80.0, 90.0, 95.0])

    def test_count_one(self):
        levs = levels.round_numbers(327.0, count=1)
        got = sorted(x["level"] for x in levs)
        self.assertEqual(got, [300.0, 350.0])

    def test_spot_on_grid_line_excluded_strictly(self):
        # spot exactly ON a grid line: strictly-above/strictly-below must skip it.
        levs = levels.round_numbers(300.0, count=2)   # step 50
        got = sorted(x["level"] for x in levs)
        self.assertEqual(got, [200.0, 250.0, 350.0, 400.0])


# --------------------------------------------------------------------------- #
# Options chain fixture (Task-3 style) for options_levels.
# --------------------------------------------------------------------------- #

def _mk(exp, k, t, mark=5.0, iv=0.5, delta=0.5, oi=100, vol=10):
    return {"expiration": exp, "strike": str(k), "type": t, "mark": str(mark),
            "bid": str(mark - 0.1), "ask": str(mark + 0.1),
            "implied_volatility": str(iv), "delta": str(delta),
            "open_interest": str(oi), "volume": str(vol)}


# ~30 days from 2026-07-16 is 2026-08-14; add a further-out expiry too.
_OPT_CHAIN = [
    _mk("2026-08-14", 90,  "put",  mark=2.0, iv=0.60, delta=-0.25, oi=500),
    _mk("2026-08-14", 100, "put",  mark=5.0, iv=0.55, delta=-0.45, oi=1000),
    _mk("2026-08-14", 100, "call", mark=6.0, iv=0.50, delta=0.55,  oi=800),
    _mk("2026-08-14", 110, "call", mark=2.5, iv=0.48, delta=0.25,  oi=2000),
    _mk("2026-09-18", 100, "call", mark=8.0, iv=0.45, delta=0.50,  oi=300),
    _mk("2026-09-18", 100, "put",  mark=7.0, iv=0.47, delta=-0.50, oi=400),
]


class TestOptionsLevels(unittest.TestCase):
    def setUp(self):
        from scripts import chain
        self.contracts = [c for c in (chain._normalize(r) for r in _OPT_CHAIN)
                          if c is not None]

    def test_types_and_basis(self):
        levs = levels.options_levels(self.contracts, spot=100.0,
                                     as_of_date="2026-07-16")
        types = {x["type"] for x in levs}
        # every options-derived level must carry the options-derived basis
        for x in levs:
            self.assertEqual(x["basis"], "options-derived")
            self.assertIn(x["type"], {"max_pain", "call_wall", "put_wall",
                                      "oi_cluster"})
        # the ~30d expiry (2026-08-14): call_wall=110, put_wall=100, max_pain=100
        self.assertIn("call_wall", types)
        self.assertIn("put_wall", types)
        self.assertIn("max_pain", types)
        self.assertIn("oi_cluster", types)

    def test_call_and_put_wall_values(self):
        levs = levels.options_levels(self.contracts, spot=100.0,
                                     as_of_date="2026-07-16")
        cw = [x for x in levs if x["type"] == "call_wall"]
        pw = [x for x in levs if x["type"] == "put_wall"]
        self.assertEqual(cw[0]["level"], 110.0)    # max call OI strictly above
        self.assertEqual(pw[0]["level"], 100.0)    # max put OI at-or-below spot

    def test_oi_clusters_top3(self):
        levs = levels.options_levels(self.contracts, spot=100.0,
                                     as_of_date="2026-07-16")
        clusters = [x for x in levs if x["type"] == "oi_cluster"]
        self.assertLessEqual(len(clusters), 3)
        self.assertGreaterEqual(len(clusters), 1)


# --------------------------------------------------------------------------- #
# build_ladder / nearest_support / nearest_resistance
# --------------------------------------------------------------------------- #

def _snapshot(last, ma50=None, ma200=None, consensus_pt=None,
              as_of_utc="2026-07-16T20:10:00Z"):
    # meta.as_of_utc is what build_ladder uses to pick the ~30d options expiry.
    return {
        "meta": {"ticker": "MU", "as_of_utc": as_of_utc},
        "price": {"last": last},
        "technicals": {"ma50": ma50, "ma200": ma200},
        "sentiment": {"consensus_pt": consensus_pt},
    }


class TestBuildLadder(unittest.TestCase):
    def setUp(self):
        self.closes = _constructed_closes()
        self.rows = _rows_from_closes(self.closes)
        self.last = self.rows[-1]["adjusted_close"]  # ~100.3

    def test_sorted_ascending_and_pct_from_last(self):
        snap = _snapshot(self.last, ma50=99.0, ma200=98.0, consensus_pt=115.0)
        ladder = levels.build_ladder(snap, self.rows)
        # ascending by level
        levs = [x["level"] for x in ladder]
        self.assertEqual(levs, sorted(levs))
        # pct_from_last arithmetic on every entry
        for x in ladder:
            self.assertAlmostEqual(x["pct_from_last"], x["level"] / self.last - 1,
                                   places=9)

    def test_ma_ath_types_present(self):
        # Use a series whose ATH is distinct from any swing high, so the "ath"
        # type is not absorbed by cross-type dedupe. Ramp up to a fresh high on
        # the LAST bar (no right window -> not a swing) that is within +/-60%.
        closes = [100.0 + 0.001 * i for i in range(300)]
        closes[299] = 108.0                 # fresh ATH on the last bar
        rows = _rows_from_closes(closes)
        last = 105.0                        # ATH 108 is +2.9% -> kept
        snap = _snapshot(last, ma50=99.0, ma200=98.0)
        ladder = levels.build_ladder(snap, rows)
        types = {x["type"] for x in ladder}
        self.assertIn("ma50", types)
        self.assertIn("ma200", types)
        self.assertIn("ath", types)
        ath = [x for x in ladder if x["type"] == "ath"]
        self.assertEqual(len(ath), 1)
        self.assertEqual(ath[0]["level"], 108.0)
        self.assertEqual(ath[0]["basis"], "ohlcv")

    def test_analyst_pt_included_when_present(self):
        snap = _snapshot(self.last, consensus_pt=112.0)
        ladder = levels.build_ladder(snap, self.rows)
        pt = [x for x in ladder if x["type"] == "analyst_pt"]
        self.assertEqual(len(pt), 1)
        self.assertEqual(pt[0]["level"], 112.0)
        self.assertEqual(pt[0]["basis"], "consensus")

    def test_analyst_pt_absent_when_missing(self):
        snap = _snapshot(self.last)  # no consensus_pt
        ladder = levels.build_ladder(snap, self.rows)
        self.assertEqual([x for x in ladder if x["type"] == "analyst_pt"], [])

    def test_60pct_cutoff(self):
        # a round number or PT far beyond +/-60% of spot must be dropped
        snap = _snapshot(100.0, consensus_pt=200.0)   # +100% -> dropped
        ladder = levels.build_ladder(snap, self.rows)
        self.assertEqual([x for x in ladder if x["type"] == "analyst_pt"], [])
        # nothing beyond +/-60%
        for x in ladder:
            self.assertLessEqual(abs(x["pct_from_last"]), 0.60 + 1e-9)

    def test_cross_type_dedupe_prefers_higher_evidence_rank(self):
        # Plant a round number coincident (<0.5%) with ma50; ma50 outranks
        # round_number in the evidence order and must be the survivor.
        # ma50 at 100.0; round grid for spot 100 has a line at 100.0 too.
        snap = _snapshot(100.0, ma50=100.0)
        ladder = levels.build_ladder(snap, self.rows)
        near_100 = [x for x in ladder if abs(x["level"] - 100.0) / 100.0 < 0.005]
        types = [x["type"] for x in near_100]
        self.assertIn("ma50", types)
        self.assertNotIn("round_number", types)

    def test_options_levels_included_when_contracts_given(self):
        from scripts import chain
        contracts = [c for c in (chain._normalize(r) for r in _OPT_CHAIN)
                     if c is not None]
        snap = _snapshot(100.0, ma50=99.0, ma200=98.0)
        ladder = levels.build_ladder(snap, self.rows, contracts=contracts)
        types = {x["type"] for x in ladder}
        self.assertTrue({"max_pain", "call_wall", "put_wall"} & types)


class TestNearest(unittest.TestCase):
    def _ladder(self):
        # A hand-built ladder around last=100.
        return [
            {"level": 80.0, "type": "swing_low", "basis": "ohlcv", "pct_from_last": -0.2},
            {"level": 92.0, "type": "ma50", "basis": "ohlcv", "pct_from_last": -0.08},
            {"level": 95.0, "type": "analyst_pt", "basis": "consensus", "pct_from_last": -0.05},
            {"level": 97.0, "type": "round_number", "basis": "psychological", "pct_from_last": -0.03},
            {"level": 105.0, "type": "round_number", "basis": "psychological", "pct_from_last": 0.05},
            {"level": 110.0, "type": "swing_high", "basis": "ohlcv", "pct_from_last": 0.1},
        ]

    def test_nearest_resistance_lowest_above(self):
        r = levels.nearest_resistance(self._ladder(), 100.0)
        self.assertEqual(r["level"], 105.0)

    def test_nearest_support_proven_only_skips_pt_and_round(self):
        # proven_only=True: 97.0 (round_number) and 95.0 (analyst_pt) between
        # spot and the swing/ma must be SKIPPED; nearest proven support is
        # ma50 at 92.0.
        s = levels.nearest_support(self._ladder(), 100.0, proven_only=True)
        self.assertEqual(s["level"], 92.0)
        self.assertEqual(s["type"], "ma50")

    def test_nearest_support_all_types(self):
        s = levels.nearest_support(self._ladder(), 100.0, proven_only=False)
        self.assertEqual(s["level"], 97.0)      # highest below, any type

    def test_nearest_support_none_when_nothing_below(self):
        ladder = [{"level": 105.0, "type": "swing_high", "basis": "ohlcv",
                   "pct_from_last": 0.05}]
        self.assertIsNone(levels.nearest_support(ladder, 100.0))

    def test_nearest_resistance_none_when_nothing_above(self):
        ladder = [{"level": 80.0, "type": "swing_low", "basis": "ohlcv",
                   "pct_from_last": -0.2}]
        self.assertIsNone(levels.nearest_resistance(ladder, 100.0))


class TestCLI(unittest.TestCase):
    """The CLI is a thin wrapper; assert it loads a real snapshot bundle and
    writes a ladder.json with the expected top-level shape."""

    def setUp(self):
        import subprocess, sys
        import tests.test_build_snapshot as tb
        self.tb = tb
        self.subprocess = subprocess
        self.sys = sys
        self.dir = tempfile.mkdtemp()
        import shutil
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.b = tb.BundleBuilder(self.dir).build_full()
        proc = tb._run_build(self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_cli_writes_ladder(self):
        LEVELS = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "levels.py")
        out = os.path.join(self.dir, "ladder.json")
        proc = self.subprocess.run(
            [self.sys.executable, LEVELS, "--bundle", self.dir, "--out", out],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertTrue(os.path.exists(out))
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["ticker"], "MU")
        self.assertIn("as_of", doc)
        self.assertIsInstance(doc["ladder"], list)
        self.assertTrue(len(doc["ladder"]) >= 1)
        # every ladder entry has the required keys
        for x in doc["ladder"]:
            for key in ("level", "type", "basis", "pct_from_last"):
                self.assertIn(key, x)


if __name__ == "__main__":
    unittest.main()
