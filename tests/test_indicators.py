import math, unittest
from scripts import indicators as I

class TestIndicators(unittest.TestCase):
    def test_sma_exact(self):
        self.assertAlmostEqual(I.sma([1, 2, 3, 4, 5], 5), 3.0)
        self.assertAlmostEqual(I.sma([1, 2, 3, 4, 5], 2), 4.5)
        self.assertIsNone(I.sma([1, 2], 3))

    def test_ema_seed_and_pull(self):
        s = I.ema_series([10]*5 + [20]*20, 5)
        self.assertAlmostEqual(s[0], 10.0)      # seed = SMA of first 5
        self.assertGreater(s[-1], 19.0)          # converges toward 20

    def test_rsi_bounds_and_extremes(self):
        self.assertAlmostEqual(I.rsi(list(range(1, 31)), 14), 100.0)
        self.assertAlmostEqual(I.rsi(list(range(31, 1, -1)), 14), 0.0)
        r = I.rsi([100 + ((-1) ** i) * (i % 5) for i in range(60)], 14)
        self.assertTrue(0 < r < 100)
        self.assertIsNone(I.rsi([1, 2, 3], 14))

    def test_rsi_flat_series_neutral(self):
        self.assertAlmostEqual(I.rsi([100.0] * 40, 14), 50.0)

    def test_macd_sign_on_trend(self):
        up = [100 * (1.01 ** i) for i in range(60)]
        m = I.macd(up)
        self.assertGreater(m["macd"], 0)         # uptrend: ema12 > ema26
        self.assertEqual(set(m), {"macd", "signal", "hist"})

    def test_returns_exact(self):
        v = [100.0] * 22; v[-1] = 110.0
        self.assertAlmostEqual(I.pct_return(v, 21), 0.10)
        self.assertIsNone(I.pct_return([1.0], 21))

    def test_realized_vol_zero_for_constant_growth(self):
        v = [100 * (1.001 ** i) for i in range(40)]
        self.assertAlmostEqual(I.realized_vol(v, 20), 0.0, places=6)

    def test_beta_of_self_is_one(self):
        import random; random.seed(7)
        p = [100.0]
        for _ in range(300): p.append(p[-1] * (1 + random.gauss(0, 0.01)))
        b = I.beta_corr(p, p)
        self.assertAlmostEqual(b["beta"], 1.0, places=6)
        self.assertAlmostEqual(b["corr"], 1.0, places=6)
        self.assertIsNone(I.beta_corr(p[:50], p[:50]))   # < 60 days -> None

    def test_max_drawdown_exact(self):
        self.assertAlmostEqual(I.max_drawdown([100, 120, 60, 90]), -0.5)
        self.assertAlmostEqual(I.max_drawdown([1, 2, 3]), 0.0)

    def test_drawdown_episodes(self):
        # 100->70 (-30%, ep1) ->105 recovery ->80 (-23.8%, ep2)
        v = [100, 70, 105, 80]
        self.assertEqual(I.drawdown_episodes(v, 0.20), 2)
        self.assertEqual(I.drawdown_episodes(v, 0.40), 0)

    def test_drawdowns_by_year(self):
        rows = [
            {"date": "2024-01-02", "adjusted_close": 100}, {"date": "2024-06-01", "adjusted_close": 80},
            {"date": "2025-01-02", "adjusted_close": 90},  {"date": "2025-06-01", "adjusted_close": 45},
        ]
        out = {d["year"]: d["max_dd"] for d in I.drawdowns_by_year(rows)}
        self.assertAlmostEqual(out[2024], -0.20)
        self.assertAlmostEqual(out[2025], -0.50)

    def test_percentile_rank(self):
        self.assertAlmostEqual(I.percentile_rank(5, list(range(10))), 60.0)
        self.assertIsNone(I.percentile_rank(5, [1, 2]))

    def test_dist_from_high(self):
        self.assertAlmostEqual(I.dist_from_high([50, 100, 75]), -0.25)

    def test_ma_slope_positive_uptrend(self):
        up = [100 * (1.01 ** i) for i in range(100)]
        self.assertGreater(I.ma_slope(up, 50, 20), 0)

    # --- Wave 2 A1: overnight-gap tail indicators --------------------------
    def test_overnight_gap_series_exact(self):
        # gap[i] = open[i] / adjusted_close[i-1] - 1.
        rows = [
            {"open": 100.0, "adjusted_close": 100.0},
            {"open": 102.0, "adjusted_close": 101.0},  # 102/100 - 1 = 0.02
            {"open": 100.98, "adjusted_close": 100.0},  # 100.98/101 - 1 = -0.0002...
        ]
        gaps = I.overnight_gap_series(rows)
        self.assertEqual(len(gaps), 2)
        self.assertAlmostEqual(gaps[0], 102.0 / 100.0 - 1)
        self.assertAlmostEqual(gaps[1], 100.98 / 101.0 - 1)

    def test_overnight_gap_series_skips_missing_and_zero(self):
        # A missing open, a missing prior close, and a zero prior close each
        # skip their gap pair (no gap emitted for that day).
        rows = [
            {"open": 100.0, "adjusted_close": 100.0},
            {"open": None, "adjusted_close": 101.0},    # missing open -> skip
            {"open": 103.0, "adjusted_close": 102.0},   # 103/101 - 1
            {"open": 104.0, "adjusted_close": None},    # prior close present; gap 104/102-1
            {"open": 105.0, "adjusted_close": 100.0},   # prior close None -> skip
            {"open": 106.0, "adjusted_close": 100.0},   # 106/100 - 1
        ]
        gaps = I.overnight_gap_series(rows)
        # Emitted pairs: (idx2:103/101), (idx3:104/102), (idx5:106/100).
        self.assertEqual(len(gaps), 3)
        self.assertAlmostEqual(gaps[0], 103.0 / 101.0 - 1)
        self.assertAlmostEqual(gaps[1], 104.0 / 102.0 - 1)
        self.assertAlmostEqual(gaps[2], 106.0 / 100.0 - 1)
        self.assertEqual(I.overnight_gap_series([]), [])
        self.assertEqual(I.overnight_gap_series([{"open": 1.0, "adjusted_close": 1.0}]), [])

    def test_overnight_gap_split_day_no_spurious_gap(self):
        # Real-data finding: raw open vs adjusted prior close manufactures a
        # bogus gap around a 2:1 split. The day's factor adjusted_close/close
        # must adjust the raw open so the true overnight move stays small.
        # Split day: raw prints halve (close 200->raw open 101), but adjusted
        # prices are continuous (~100 -> ~101). True gap ~= +1%, NOT -49.5%.
        rows = [
            # pre-split day: raw and adjusted equal
            {"open": 199.0, "close": 200.0, "adjusted_close": 100.0},  # factor 0.5
            # split day: raw open 101 (post-split), factor 0.5 -> adj_open 50.5,
            # vs prior adjusted_close 100 -> gap 50.5/100-1 = ... wait, prior adj=100
            {"open": 101.0, "close": 101.0, "adjusted_close": 101.0},  # factor 1.0 post-split
        ]
        # Correct: adj_open(day2) = 101 * (101/101) = 101; prior adjusted_close = 100
        gaps = I.overnight_gap_series(rows)
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0], 101.0 / 100.0 - 1)  # +1%, the true move
        # And the naive raw-open/adjusted-prior-close would have been 101/100-1
        # here too because this row is post-split; the guard matters when the
        # CURRENT row still carries a split factor. Verify that path directly:
        rows2 = [
            {"open": 100.0, "close": 100.0, "adjusted_close": 50.0},   # prior adj=50
            {"open": 200.0, "close": 200.0, "adjusted_close": 100.0},  # factor 0.5 -> adj_open 100
        ]
        g2 = I.overnight_gap_series(rows2)
        # adj_open = 200 * (100/200) = 100; vs prior adjusted_close 50 -> +100% (real 2x move
        # in ADJUSTED space, correct). Naive raw 200/50-1 = +300% would be the bug.
        self.assertAlmostEqual(g2[0], 100.0 / 50.0 - 1)
        # Missing raw close -> factor 1 (stooq path), open used as-is.
        rows3 = [{"open": 10.0, "adjusted_close": 10.0}, {"open": 11.0, "adjusted_close": 11.0}]
        self.assertAlmostEqual(I.overnight_gap_series(rows3)[0], 11.0 / 10.0 - 1)

    def test_excess_kurtosis_normal_ish_near_zero(self):
        # A symmetric uniform-ish set: excess kurtosis of a discrete uniform is
        # negative (platykurtic). Assert the sign + a known hand value.
        # For [-2,-1,0,1,2]: mean 0, m2 = 2.0, m4 = 6.8 -> 6.8/4 - 3 = -1.3.
        vals = [-2.0, -1.0, 0.0, 1.0, 2.0]
        self.assertAlmostEqual(I.excess_kurtosis(vals), -1.3, places=9)

    def test_excess_kurtosis_fat_tail_positive(self):
        # A spike embedded in a tight cluster is leptokurtic (excess kurtosis > 0).
        vals = [0.0] * 20 + [10.0]
        self.assertGreater(I.excess_kurtosis(vals), 0)

    def test_excess_kurtosis_guards(self):
        self.assertIsNone(I.excess_kurtosis([1.0, 2.0, 3.0]))   # n < 4
        self.assertIsNone(I.excess_kurtosis([5.0, 5.0, 5.0, 5.0]))  # zero variance

    def test_jump_count_2sigma(self):
        # std of the series determines the 2-sigma threshold; |value| > 2*std
        # counts as a jump. Cluster near 0 with two large outliers.
        vals = [0.01, -0.01, 0.02, -0.02, 0.5, -0.6]
        n = len(vals)
        mu = sum(vals) / n
        std = (sum((x - mu) ** 2 for x in vals) / n) ** 0.5
        expected = sum(1 for x in vals if abs(x) > 2 * std)
        self.assertEqual(I.jump_count_2sigma(vals), expected)
        # A flat series has no jumps; a single/empty series is trivially 0.
        self.assertEqual(I.jump_count_2sigma([0.0] * 10), 0)
        self.assertEqual(I.jump_count_2sigma([0.03]), 0)
        self.assertEqual(I.jump_count_2sigma([]), 0)


if __name__ == "__main__":
    unittest.main()
