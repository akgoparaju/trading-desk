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

if __name__ == "__main__":
    unittest.main()
