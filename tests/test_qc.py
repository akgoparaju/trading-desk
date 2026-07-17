import unittest

from scripts import qc as Q

# as_of anchors staleness math; sources retrieved same-day are always fresh.
AS_OF = "2026-07-16T20:00:00Z"


def make_snapshot():
    """Fully consistent snapshot fixture: every QC check passes on it.

    Arithmetic is hand-tuned so each check has a true, non-skipped pass:
      mktcap  100.0 * 1000 * 1e6 = 100e9 == mktcap_overview
      MA      uptrend: last 100 > ma50 95 > ma200 90
      P/E     ttm 100/5 = 20.0; fwd 100/5.5 = 18.1818...
      net     10e9 + 5e9 - 3e9 = 12e9
      options chain_as_of == last_ohlcv_date; pc 0.85 vs 0.90 (|d|=0.05)
      spot    web_spot_check 100.0 == last (0% off)
    Every present block is covered by meta.sources[].covers; all sources are
    retrieved same-day as as_of (fresh under every window).
    """
    return {
        "price": {
            "last": 100.0,
            "prev_close": 99.0,
            "wk52_high": 130.0,
            "wk52_low": 70.0,
            "shares_diluted_m": 1000.0,
            "mktcap_computed": 100_000_000_000.0,
            "mktcap_overview": 100_000_000_000.0,
            "adv_dollar_3m": 500_000_000.0,
            "web_spot_check": {"price": 100.0, "source_url": "https://example.com/AAA"},
        },
        "technicals": {
            "ma50": 95.0,
            "ma200": 90.0,
            "ma50_slope_20d": 0.02,
            "ma200_slope_20d": 0.01,
            "rsi14": 55.0,
            "macd": 1.2,
            "macd_signal": 0.9,
            "ret_1m": 0.03, "ret_3m": 0.08, "ret_6m": 0.15, "ret_12m": 0.30,
            "rv20_ann": 0.28,
            "rv30_ann": 0.30,
            "rv30_vs_10yr_pctile": 45.0,
            "dist_from_ath_pct": -0.10,
            "vol_20d_vs_90d": 1.1,
            "ohlcv_rows": 260,
            "last_ohlcv_date": "2026-07-15",
            "trend_claim": "uptrend",
        },
        "benchmark": {
            "spy_ret_1m": 0.02, "spy_ret_3m": 0.05, "spy_ret_6m": 0.09, "spy_ret_12m": 0.18,
            "beta": 1.1,
            "corr": 0.75,
        },
        "fundamentals": {
            "rev_ttm": 50_000_000_000.0,
            "eps_ttm": 5.0,
            "eps_ntm_consensus": 5.5,
            "net_cash_defined": {
                "cash_st": 10_000_000_000.0,
                "lt_inv": 5_000_000_000.0,
                "total_debt": 3_000_000_000.0,
                "net": 12_000_000_000.0,
            },
        },
        "valuation": {
            "pe_ttm": 20.0,
            "pe_fwd": 100.0 / 5.5,
        },
        "sentiment": {
            "put_call_ratio_full_chain": 1.25,          # OI-based; never compared to realtime
            "put_call_ratio_full_chain_volume": 0.85,   # volume-based; the realtime comparand
            "put_call_ratio_realtime": 0.90,
            "iv30": 0.32,
        },
        "options": {
            "chain_as_of": "2026-07-15",
        },
        "events": {
            "next_earnings_date": "2026-08-05",
        },
        "meta": {
            "ticker": "AAA",
            "as_of_utc": AS_OF,
            "missing": [],
            "sources": [
                {"field_group": "global_quote", "endpoint_or_url": "GLOBAL_QUOTE",
                 "retrieved_utc": AS_OF, "covers": ["price"]},
                {"field_group": "web_spot_check", "endpoint_or_url": "https://example.com/AAA",
                 "retrieved_utc": AS_OF, "covers": ["price"]},
                {"field_group": "daily_adjusted", "endpoint_or_url": "TIME_SERIES_DAILY_ADJUSTED",
                 "retrieved_utc": AS_OF, "covers": ["technicals"]},
                {"field_group": "spy_daily_adjusted", "endpoint_or_url": "TIME_SERIES_DAILY_ADJUSTED",
                 "retrieved_utc": AS_OF, "covers": ["benchmark"]},
                {"field_group": "balance_sheet", "endpoint_or_url": "BALANCE_SHEET",
                 "retrieved_utc": AS_OF, "covers": ["fundamentals"]},
                {"field_group": "overview", "endpoint_or_url": "COMPANY_OVERVIEW",
                 "retrieved_utc": AS_OF, "covers": ["valuation"]},
                {"field_group": "pc_ratio_realtime", "endpoint_or_url": "REALTIME_PUT_CALL_RATIO",
                 "retrieved_utc": AS_OF, "covers": ["sentiment"]},
                {"field_group": "options_chain", "endpoint_or_url": "HISTORICAL_OPTIONS",
                 "retrieved_utc": AS_OF, "covers": ["options"]},
                {"field_group": "earnings_calendar", "endpoint_or_url": "EARNINGS_CALENDAR",
                 "retrieved_utc": AS_OF, "covers": ["events"]},
            ],
            "qc": {"passed": None, "checks": [], "waivers": []},
        },
    }


def _names(result):
    return {c["check"]: c for c in result["checks"]}


class TestQCHappyPath(unittest.TestCase):
    def test_clean_snapshot_passes(self):
        r = Q.run_qc(make_snapshot())
        self.assertIs(r["passed"], True)
        failed = [c for c in r["checks"] if c["passed"] is False]
        self.assertEqual(failed, [], f"unexpected failures: {failed}")

    def test_all_nine_checks_ran(self):
        r = Q.run_qc(make_snapshot())
        self.assertEqual(len(r["checks"]), 9)

    def test_attestation_mentions_ticker_and_date(self):
        r = Q.run_qc(make_snapshot())
        self.assertIn("AAA", r["attestation"])
        self.assertIn("2026-07-16", r["attestation"])


class TestPerCheckMutations(unittest.TestCase):
    """Each mutation must flip exactly its target check to failed."""

    def _run_one(self, mutate, check_name):
        s = make_snapshot()
        mutate(s)
        checks = {c.__name__: c for c in Q.ALL_CHECKS}
        return checks[check_name](s)

    def test_mktcap_fails_on_overview_mismatch(self):
        def m(s): s["price"]["mktcap_overview"] *= 1.10
        self.assertIs(self._run_one(m, "check_mktcap")["passed"], False)

    def test_mktcap_passes_on_stale_vendor_cap_matching_prev_close(self):
        # Big move day: vendor cap = shares x prev_close, not shares x last.
        # Share count reconciles -> pass, with staleness disclosed.
        def m(s):
            s["price"]["last"] = s["price"]["prev_close"] * 1.04
            s["price"]["mktcap_computed"] = (
                s["price"]["last"] * s["price"]["shares_diluted_m"] * 1e6)
        r = self._run_one(m, "check_mktcap")
        self.assertIs(r["passed"], True)
        self.assertIn("prior-session stale", r["detail"])

    def test_mktcap_fails_when_neither_last_nor_prev_reconciles(self):
        def m(s):
            s["price"]["mktcap_overview"] *= 1.10
            s["price"]["last"] = s["price"]["prev_close"] * 1.04
        self.assertIs(self._run_one(m, "check_mktcap")["passed"], False)

    def test_mktcap_skips_on_reused_stale_overview_with_moved_price(self):
        # Live-refresh finding: an in-window REUSED overview (vendor cap from its
        # retrieval day) + a multi-session price move is unevaluable, not wrong.
        def m(s):
            # two-session move: BOTH last and prev_close far from the vendor cap
            s["price"]["prev_close"] = 106.0
            s["price"]["last"] = 112.0
            s["price"]["mktcap_computed"] = (
                s["price"]["last"] * s["price"]["shares_diluted_m"] * 1e6)
            for src in s["meta"]["sources"]:
                if src["field_group"] == "overview":
                    src["retrieved_utc"] = "2026-07-06T12:00:00Z"  # 10d before as_of
        r = self._run_one(m, "check_mktcap")
        self.assertIsNone(r["passed"])
        self.assertIn("deferred to the next full fetch", r["detail"])

    def test_mktcap_still_fails_on_fresh_overview(self):
        # Same divergence but a same-day overview: the check keeps its teeth.
        def m(s):
            s["price"]["prev_close"] = 106.0
            s["price"]["last"] = 112.0
        self.assertIs(self._run_one(m, "check_mktcap")["passed"], False)

    def test_ma_ordering_fails(self):
        def m(s): s["technicals"]["ma50"] = 105.0  # ma50 > last breaks uptrend
        self.assertIs(self._run_one(m, "check_ma_ordering")["passed"], False)

    def test_ranges_fails_on_bad_rsi(self):
        def m(s): s["technicals"]["rsi14"] = 140.0
        self.assertIs(self._run_one(m, "check_ranges")["passed"], False)

    def test_spotcheck_fails(self):
        def m(s): s["price"]["web_spot_check"]["price"] = s["price"]["last"] * 1.05
        self.assertIs(self._run_one(m, "check_price_spotcheck")["passed"], False)

    def test_pe_arithmetic_fails(self):
        def m(s): s["valuation"]["pe_ttm"] = 30.0  # last/eps = 20, off by 50%
        self.assertIs(self._run_one(m, "check_pe_arithmetic")["passed"], False)

    def test_net_cash_fails(self):
        def m(s): s["fundamentals"]["net_cash_defined"]["net"] = 999_000_000_000.0
        self.assertIs(self._run_one(m, "check_net_cash")["passed"], False)

    def test_options_freshness_fails(self):
        def m(s): s["options"]["chain_as_of"] = "2020-01-01"
        self.assertIs(self._run_one(m, "check_options_freshness")["passed"], False)

    def test_options_freshness_compares_volume_pc_not_oi_pc(self):
        # OI-based P/C way off realtime must NOT fail (methodology mismatch)...
        def m(s): s["sentiment"]["put_call_ratio_full_chain"] = 2.50
        self.assertIs(self._run_one(m, "check_options_freshness")["passed"], True)
        # ...but volume-based P/C off realtime by > 0.15 must fail.
        def m2(s): s["sentiment"]["put_call_ratio_full_chain_volume"] = 1.50
        self.assertIs(self._run_one(m2, "check_options_freshness")["passed"], False)

    def test_options_freshness_skips_pc_leg_without_volume_pc(self):
        # No volume-based figure -> pc leg suppressed (never OI-vs-volume);
        # the verified date leg still carries the check to PASS, skip disclosed.
        def m(s): s["sentiment"]["put_call_ratio_full_chain_volume"] = None
        r = self._run_one(m, "check_options_freshness")
        self.assertIs(r["passed"], True)
        self.assertIn("methodology mismatch", r["detail"])

    def test_provenance_fails_on_empty_sources(self):
        def m(s): s["meta"]["sources"] = []
        self.assertIs(self._run_one(m, "check_provenance")["passed"], False)

    def test_staleness_fails(self):
        def m(s):
            # global_quote window is 1 day; 30 days old -> fail
            s["meta"]["sources"][0]["retrieved_utc"] = "2026-06-16T20:00:00Z"
        self.assertIs(self._run_one(m, "check_staleness")["passed"], False)


class TestSkipBehavior(unittest.TestCase):
    def test_spotcheck_skips_when_absent(self):
        s = make_snapshot()
        del s["price"]["web_spot_check"]
        result = Q.check_price_spotcheck(s)
        self.assertIsNone(result["passed"])

    def test_run_qc_still_passes_with_skip(self):
        s = make_snapshot()
        del s["price"]["web_spot_check"]
        r = Q.run_qc(s)
        self.assertIs(r["passed"], True)
        # skipped check disclosed in attestation
        self.assertIn("check_price_spotcheck", r["attestation"])

    def test_ma_ordering_skips_without_trend_claim(self):
        s = make_snapshot()
        del s["technicals"]["trend_claim"]
        self.assertIsNone(Q.check_ma_ordering(s)["passed"])

    def test_options_skips_when_block_missing(self):
        s = make_snapshot()
        s["options"] = None
        self.assertIsNone(Q.check_options_freshness(s)["passed"])

    def test_pe_arithmetic_skips_negative_eps(self):
        s = make_snapshot()
        s["fundamentals"]["eps_ttm"] = -2.0
        s["fundamentals"]["eps_ntm_consensus"] = -1.0
        r = Q.check_pe_arithmetic(s)
        # both legs skipped -> overall skip (None), detail explains n/m
        self.assertIsNone(r["passed"])
        self.assertIn("n/m", r["detail"])


class TestWaivers(unittest.TestCase):
    def test_waived_failure_does_not_fail_gate(self):
        s = make_snapshot()
        s["price"]["mktcap_overview"] *= 1.10  # breaks check_mktcap
        s["meta"]["qc"]["waivers"] = [
            {"check": "check_mktcap", "reason": "known share-count lag"}
        ]
        r = Q.run_qc(s)
        self.assertIs(r["passed"], True)
        checks = _names(r)
        self.assertTrue(checks["check_mktcap"]["detail"].startswith("WAIVED"))
        self.assertIn("known share-count lag", checks["check_mktcap"]["detail"])

    def test_unwaived_failure_fails_gate(self):
        s = make_snapshot()
        s["price"]["mktcap_overview"] *= 1.10
        r = Q.run_qc(s)
        self.assertIs(r["passed"], False)


if __name__ == "__main__":
    unittest.main()
