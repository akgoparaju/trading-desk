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

    def test_realized_vol_ex_earnings_strips_print_jump(self):
        # 30 trading days, tiny drift, with ONE big +15% print-day jump.
        import datetime as _dt
        dates, d = [], _dt.date(2026, 1, 1)
        while len(dates) < 30:
            if d.weekday() < 5:
                dates.append(d.isoformat())
            d += _dt.timedelta(days=1)
        closes = [100.0]
        for _ in range(1, 30):
            closes.append(closes[-1] * 1.001)
        closes[15] = closes[14] * 1.15            # +15% print-day jump on day 15
        for j in range(16, 30):
            closes[j] = closes[j - 1] * 1.001
        earnings_day = dates[15]

        contaminated = I.realized_vol(closes, 20)
        ex = I.realized_vol_ex_earnings(closes, dates, [earnings_day], 20)
        self.assertIsNotNone(contaminated)
        self.assertIsNotNone(ex)
        # Stripping the jump makes ex-earnings RV noticeably LOWER.
        self.assertLess(ex, contaminated)
        self.assertLess(ex, contaminated * 0.5)

    def test_realized_vol_ex_earnings_masks_non_trading_day_earnings(self):
        # Earnings on a weekend (not a trading day, absent from the dates list)
        # still masks the trading days immediately before and after it. Put the
        # print-day jump on a MONDAY and "earnings" on the preceding SUNDAY: the
        # Monday is the trading day immediately after the weekend earnings, so its
        # jump return is masked.
        import datetime as _dt
        dates, d = [], _dt.date(2026, 1, 1)
        while len(dates) < 30:
            if d.weekday() < 5:
                dates.append(d.isoformat())
            d += _dt.timedelta(days=1)
        # Find a mid-series Monday to host the jump.
        # Constrain so the jump return lands inside the last-20 window (29
        # returns -> window is return indices 9..28) and mid-series.
        monday_idx = next(i for i, dd in enumerate(dates)
                          if _dt.date.fromisoformat(dd).weekday() == 0 and 11 <= i <= 24)
        closes = [100.0 * (1.001 ** i) for i in range(30)]
        closes[monday_idx] = closes[monday_idx - 1] * 1.12  # +12% Monday jump
        for j in range(monday_idx + 1, 30):
            closes[j] = closes[j - 1] * 1.001
        sunday = (_dt.date.fromisoformat(dates[monday_idx]) - _dt.timedelta(days=1)).isoformat()
        self.assertEqual(_dt.date.fromisoformat(sunday).weekday(), 6)  # Sunday, not a trading day
        contaminated = I.realized_vol(closes, 20)
        ex = I.realized_vol_ex_earnings(closes, dates, [sunday], 20)
        self.assertIsNotNone(ex)
        self.assertLess(ex, contaminated)

    def test_realized_vol_ex_earnings_none_when_too_few(self):
        closes = [100.0 * (1.001 ** i) for i in range(10)]
        dates = [f"2026-01-{i + 1:02d}" for i in range(10)]
        # 20-window on 9 returns -> None. Misaligned lists -> None. n<2 -> None.
        self.assertIsNone(I.realized_vol_ex_earnings(closes, dates, [], 20))
        self.assertIsNone(I.realized_vol_ex_earnings(closes, dates[:5], [], 5))
        self.assertIsNone(I.realized_vol_ex_earnings(closes, dates, [], 1))

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


    # --- Wave 3A: news-heat EWMA half-life + z-score -----------------------
    def test_halflife_weight_exact(self):
        # 0 age -> full weight 1.0; one half-life -> 0.5; two -> 0.25.
        self.assertAlmostEqual(I.halflife_weight(0, 3), 1.0)
        self.assertAlmostEqual(I.halflife_weight(3, 3), 0.5)
        self.assertAlmostEqual(I.halflife_weight(6, 3), 0.25)
        self.assertAlmostEqual(I.halflife_weight(1.5, 3), 0.5 ** 0.5)
        self.assertIsNone(I.halflife_weight(-1, 3))   # negative age
        self.assertIsNone(I.halflife_weight(3, 0))    # non-positive half-life

    def test_ewma_halflife_hand_computed(self):
        # Two observations at ages 0 and 3 days (half-life 3), relevance 1.0.
        # weights: 1.0 (age 0), 0.5 (age 3). scores: +1.0 and -1.0.
        # ewma = (1.0*1.0 + (-1.0)*0.5) / (1.0 + 0.5) = 0.5 / 1.5 = 1/3.
        w0 = I.halflife_weight(0, 3)
        w3 = I.halflife_weight(3, 3)
        pairs = [(1.0, 1.0 * w0), (-1.0, 1.0 * w3)]
        self.assertAlmostEqual(I.ewma_halflife(pairs), (1.0 * w0 - 1.0 * w3) / (w0 + w3))
        self.assertAlmostEqual(I.ewma_halflife(pairs), 1.0 / 3.0)
        # all-zero weights / empty -> None
        self.assertIsNone(I.ewma_halflife([]))
        self.assertIsNone(I.ewma_halflife([(1.0, 0.0), (2.0, 0.0)]))
        # A single fully-weighted observation returns itself.
        self.assertAlmostEqual(I.ewma_halflife([(0.42, 1.0)]), 0.42)

    def test_zscore_exact_and_guards(self):
        # history [1,2,3,4,5]: mean 3, sample stdev sqrt(2.5). z(6) = 3/sqrt(2.5).
        import statistics as S
        hist = [1.0, 2.0, 3.0, 4.0, 5.0]
        expected = (6.0 - 3.0) / S.stdev(hist)
        self.assertAlmostEqual(I.zscore(6.0, hist), expected)
        self.assertAlmostEqual(I.zscore(3.0, hist), 0.0)  # at the mean
        self.assertIsNone(I.zscore(1.0, [1.0, 2.0, 3.0, 4.0]))  # < 5 points
        self.assertIsNone(I.zscore(5.0, [5.0] * 6))             # zero stdev

    # --- Wave 4A: ADX (Wilder trend strength) ------------------------------
    def _bar(self, high, low, close, volume=1_000_000):
        return {"high": high, "low": low, "close": close, "volume": volume}

    def test_adx_high_on_strong_trend(self):
        # A clean one-directional uptrend (every bar strictly higher, tight
        # symmetric range) is a maximally trending series: ALL directional
        # movement is +DM, -DM is 0, so +DI dominates and DX == 100 every bar.
        # ADX must be well above the cited no-trend threshold (>25).
        trend = []
        p = 100.0
        for _ in range(60):
            p += 1.0
            trend.append(self._bar(p + 0.5, p - 0.5, p))
        adx = I.adx(trend, 14)
        self.assertIsNotNone(adx)
        self.assertGreater(adx, 25.0)
        # Monotone perfect trend -> DX == 100 each bar -> ADX == 100.
        self.assertAlmostEqual(adx, 100.0, places=6)

    def test_adx_low_on_choppy_range(self):
        # A rangebound saw-tooth (alternating up/down with no net direction) has
        # +DM and -DM roughly cancelling: the DIs are close, DX is small, ADX
        # stays well below the cited trend threshold (<20).
        chop = []
        base = 100.0
        for i in range(60):
            c = base + (2.0 if i % 2 == 0 else -2.0)
            chop.append(self._bar(c + 0.5, c - 0.5, c))
        adx = I.adx(chop, 14)
        self.assertIsNotNone(adx)
        self.assertLess(adx, 20.0)

    def test_adx_hand_fixture_monotone_uptrend(self):
        # Hand-checkable n=2 fixture (needs 2n+1 = 5 rows). Every bar rises by 1
        # with an identical unit range, so:
        #   +DM = high_t - high_{t-1} = 1 (> down=0) every bar; -DM = 0.
        #   TR  = max(1, |high-prev_close|, |low-prev_close|) each bar.
        #   +DI = 100 (all smoothed movement is up), -DI = 0
        #   DX  = 100 * |100-0| / (100+0) = 100 every bar -> ADX = 100.
        rows = [self._bar(10 + i, 9 + i, 9.5 + i) for i in range(7)]
        self.assertAlmostEqual(I.adx(rows, 2), 100.0, places=9)

    def test_adx_requires_2n_plus_1_rows(self):
        rows = [self._bar(10 + i, 9 + i, 9.5 + i) for i in range(40)]
        # n=14 needs 2*14+1 = 29 rows.
        self.assertIsNone(I.adx(rows[:28], 14))
        self.assertIsNotNone(I.adx(rows[:29], 14))

    def test_adx_null_on_missing_ohlc(self):
        rows = [self._bar(10 + i, 9 + i, 9.5 + i) for i in range(30)]
        rows[5]["high"] = None   # a hole makes the whole computation None
        self.assertIsNone(I.adx(rows, 14))

    # --- Wave 4A: Chaikin A/D line + slope ---------------------------------
    def test_ad_line_hand_computed(self):
        # MFM = ((close-low)-(high-close))/(high-low); MFV = MFM*vol; cumsum.
        rows = [
            self._bar(10.0, 8.0, 9.0, 100),    # MFM = ((1)-(1))/2 = 0  -> +0
            self._bar(12.0, 10.0, 12.0, 200),  # close==high: MFM = +1  -> +200
            self._bar(14.0, 12.0, 12.0, 300),  # close==low:  MFM = -1  -> -300
        ]
        self.assertEqual(I.ad_line(rows), [0.0, 200.0, -100.0])

    def test_ad_line_flat_bar_zero_mfm(self):
        # high == low -> MFM defined as 0 (no divide-by-zero).
        self.assertEqual(I.ad_line([self._bar(5.0, 5.0, 5.0, 100)]), [0.0])
        self.assertEqual(I.ad_line([]), [])

    def test_ad_line_slope_sign_accumulation_vs_distribution(self):
        # Accumulation: close near the high every bar -> A/D rises -> slope > 0.
        acc = [self._bar(100 + i, 99 + i, 99.9 + i) for i in range(30)]
        self.assertGreater(I.ad_line_slope(acc, 20), 0)
        # Distribution: close near the low -> A/D falls -> slope < 0.
        dist = [self._bar(100 + i, 99 + i, 99.1 + i) for i in range(30)]
        self.assertLess(I.ad_line_slope(dist, 20), 0)
        # Too short -> None.
        self.assertIsNone(I.ad_line_slope(acc[:10], 20))

    # --- Wave 4A: up/down volume ratio -------------------------------------
    def test_updown_volume_hand_computed(self):
        rows = [
            self._bar(0, 0, 10.0, 100),   # anchor bar (no prior close)
            self._bar(0, 0, 11.0, 200),   # up (11 > 10) -> up-vol 200
            self._bar(0, 0, 10.5, 300),   # down -> not counted
            self._bar(0, 0, 12.0, 400),   # up -> up-vol 400
        ]
        # Over the last n=3 comparison bars: up = 200+400 = 600, total = 900.
        self.assertAlmostEqual(I.updown_volume(rows, 3), 600.0 / 900.0)

    def test_updown_volume_guards(self):
        rows = [self._bar(0, 0, 10.0 + i, 100) for i in range(4)]
        self.assertIsNone(I.updown_volume(rows, 4))   # need n+1 = 5 rows
        self.assertIsNotNone(I.updown_volume(rows, 3))
        # A hole in the window nulls the ratio (must be a clean n-bar window).
        holed = [self._bar(0, 0, 10.0 + i, 100) for i in range(6)]
        holed[-1]["volume"] = None
        self.assertIsNone(I.updown_volume(holed, 5))

    # --- Wave 4A: anchored VWAP --------------------------------------------
    def test_anchored_vwap_hand_computed(self):
        rows = [
            {"date": "2026-01-01", "high": 10, "low": 8, "close": 9, "volume": 100},
            {"date": "2026-01-02", "high": 12, "low": 10, "close": 11, "volume": 200},
            {"date": "2026-01-03", "high": 14, "low": 12, "close": 13, "volume": 300},
        ]
        # Anchor 2026-01-02: typical prices (12+10+11)/3=11 and (14+12+13)/3=13.
        # vwap = (11*200 + 13*300) / (200+300) = 6100/500 = 12.2.
        self.assertAlmostEqual(I.anchored_vwap(rows, "2026-01-02"), 12.2)
        # Anchor on the first bar spans all three bars.
        # typicals: 9, 11, 13; vwap = (9*100+11*200+13*300)/600 = 7000/600.
        self.assertAlmostEqual(I.anchored_vwap(rows, "2026-01-01"), 7000.0 / 600.0)

    def test_anchored_vwap_guards(self):
        rows = [
            {"date": "2026-01-01", "high": 10, "low": 8, "close": 9, "volume": 100},
        ]
        # A future anchor (no row on/after it) -> None.
        self.assertIsNone(I.anchored_vwap(rows, "2026-02-01"))
        # No anchor -> None.
        self.assertIsNone(I.anchored_vwap(rows, None))
        # Zero total volume -> None.
        zero = [{"date": "2026-01-01", "high": 10, "low": 8, "close": 9, "volume": 0}]
        self.assertIsNone(I.anchored_vwap(zero, "2026-01-01"))


if __name__ == "__main__":
    unittest.main()
