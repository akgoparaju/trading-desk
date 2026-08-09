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
            # QC3: clean three-way TTM EPS reconciliation (all within 3%).
            "eps_ttm_computed": 5.02,
            "eps_ttm_from_ni": 4.98,
            # QC3: clean per-quarter implied-share reconciliation (well under 5%).
            "eps_share_reconciliation": [
                {"fiscal_date_ending": "2026-06-30", "net_income": 1_000_000_000.0,
                 "reported_eps": 1.25, "implied_shares": 800_000_000.0,
                 "balance_shares_same_quarter": 800_000_000.0, "divergence_pct": 0.0},
                {"fiscal_date_ending": "2026-03-31", "net_income": 960_000_000.0,
                 "reported_eps": 1.20, "implied_shares": 800_000_000.0,
                 "balance_shares_same_quarter": 802_000_000.0, "divergence_pct": -0.0025},
                {"fiscal_date_ending": "2025-12-31", "net_income": 920_000_000.0,
                 "reported_eps": 1.15, "implied_shares": 800_000_000.0,
                 "balance_shares_same_quarter": 805_000_000.0, "divergence_pct": -0.0062},
                {"fiscal_date_ending": "2025-09-30", "net_income": 880_000_000.0,
                 "reported_eps": 1.10, "implied_shares": 800_000_000.0,
                 "balance_shares_same_quarter": 808_000_000.0, "divergence_pct": -0.0099},
            ],
            "net_cash_defined": {
                "cash_and_equivalents": 7_000_000_000.0,
                "short_term_investments": 3_000_000_000.0,
                "cash_st": 10_000_000_000.0,
                "lt_inv": 5_000_000_000.0,
                "short_term_debt": 1_000_000_000.0,
                "long_term_debt": 2_000_000_000.0,
                "total_debt": 3_000_000_000.0,
                "net": 12_000_000_000.0,
                # Clean vendor pair (no drop-sti defect signature): the
                # aggregate (10B) != cash_and_equivalents alone (7B).
                "vendor_aggregates": {
                    "cash_and_short_term_investments": 10_000_000_000.0,
                    "short_long_term_debt_total": 3_000_000_000.0,
                },
            },
        },
        "valuation": {
            "pe_ttm": 20.0,
            "pe_fwd": 100.0 / 5.5,
            # QC1: vendor ForwardPE counterparty; equal to pe_fwd -> 0% diff.
            "pe_overview_fwd": 100.0 / 5.5,
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

    def test_all_fifteen_checks_ran(self):
        # O15 added check_security_master (10th check); QC4 added
        # check_net_cash_vendor_signature (11th, disclosure-only); QC1 added
        # check_forward_pe_crossvendor (12th); QC3 added check_eps (13th) and
        # check_eps_quarterly_shares (14th); post-QC1 added
        # check_beta_crossvendor (15th, blocking).
        r = Q.run_qc(make_snapshot())
        self.assertEqual(len(r["checks"]), 15)

    def test_attestation_mentions_ticker_and_date(self):
        r = Q.run_qc(make_snapshot())
        self.assertIn("AAA", r["attestation"])
        self.assertIn("2026-07-16", r["attestation"])

    def test_attestation_stale_print_note_matches_confidence_phrasing(self):
        # QC8 fixed scripts/confidence.py's staleness label to stop claiming
        # "weekend" (a weekday-ignorant guess with no weekday computation
        # backing it) and say only what actually fired: "stale print
        # (latest_trading_day != as_of)". qc.py's attestation Note carried the
        # identical "(weekend/stale print)" defect independently -- it must
        # use the SAME phrasing confidence.py established, so the two never
        # silently diverge on the same condition.
        s = make_snapshot()
        s["meta"]["latest_trading_day"] = "2026-07-15"  # as_of date is 2026-07-16
        r = Q.run_qc(s)
        self.assertIn(
            "Note: as_of 2026-07-16 vs latest trading day 2026-07-15 "
            "(stale print (latest_trading_day != as_of)).",
            r["attestation"],
        )
        self.assertNotIn("weekend", r["attestation"])


class TestPerCheckMutations(unittest.TestCase):
    """Each mutation must flip exactly its target check to failed."""

    def _run_one(self, mutate, check_name):
        s = make_snapshot()
        mutate(s)
        checks = {c.__name__: c for c in Q.ALL_CHECKS}
        return checks[check_name](s)

    def test_mktcap_fails_on_overview_mismatch(self):
        # G1: ratio must be OUTSIDE the multi-class band (0.15, 1.0) to fail.
        # overview × 10 → ratio = computed / (overview×10) = 0.1 < 0.15 → FAIL.
        def m(s): s["price"]["mktcap_overview"] *= 10
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
        # G1: ratio must be outside the multi-class band to fail.  overview × 10
        # → ratio ≈ 0.1 < 0.15 (implausible for a class split) → still FAIL.
        def m(s):
            s["price"]["mktcap_overview"] *= 10
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

    def test_net_cash_fails_on_cash_st_sourcing_mismatch(self):
        # QC4: cash_st must equal the sum of its disclosed components.
        def m(s): s["fundamentals"]["net_cash_defined"]["cash_and_equivalents"] = 1.0
        r = self._run_one(m, "check_net_cash")
        self.assertIs(r["passed"], False)
        self.assertIn("cash_and_equivalents", r["detail"])

    def test_net_cash_fails_on_total_debt_sourcing_mismatch(self):
        # QC4: total_debt must equal the sum of its disclosed components.
        def m(s): s["fundamentals"]["net_cash_defined"]["long_term_debt"] = 1.0
        r = self._run_one(m, "check_net_cash")
        self.assertIs(r["passed"], False)
        self.assertIn("long_term_debt", r["detail"])

    def test_net_cash_sourcing_skips_when_components_absent(self):
        # Vendor-fallback case (every component of a leg absent): the sourcing
        # leg has nothing to check and is skipped, not failed -- the main
        # arithmetic leg still governs the check.
        def m(s):
            ncd = s["fundamentals"]["net_cash_defined"]
            del ncd["cash_and_equivalents"]
            del ncd["short_term_investments"]
            del ncd["short_term_debt"]
            del ncd["long_term_debt"]
        r = self._run_one(m, "check_net_cash")
        self.assertIs(r["passed"], True)
        self.assertIn("components absent", r["detail"])

    def test_net_cash_sourcing_skips_when_debt_components_incomplete(self):
        # QC4-REGRESSION: total_debt may legitimately be vendor-sourced when
        # EXACTLY ONE of shortTermDebt/longTermDebt is absent (components
        # INCOMPLETE, distinct from "both absent") -- the strict sourcing leg
        # (present-components must sum to total_debt) must SKIP this shape,
        # not FAIL it, since total_debt is correctly NOT equal to the partial
        # sum by design (build_snapshot._build_net_cash, XOM shape).
        def m(s):
            ncd = s["fundamentals"]["net_cash_defined"]
            ncd["short_term_debt"] = 10_139_000_000.0
            ncd["long_term_debt"] = None
            ncd["total_debt"] = 42_368_000_000.0          # vendor-sourced fallback
            ncd["vendor_aggregates"]["short_long_term_debt_total"] = 42_368_000_000.0
            ncd["net"] = ncd["cash_st"] + ncd["lt_inv"] - ncd["total_debt"]
        r = self._run_one(m, "check_net_cash")
        self.assertIs(r["passed"], True)
        self.assertIn("components incomplete", r["detail"])
        self.assertIn("may be vendor-sourced", r["detail"])

    def test_net_cash_sourcing_discloses_understated_when_no_vendor_to_fall_back_to(self):
        # D4: the reviewer-flagged sub-case -- exactly one of shortTermDebt/
        # longTermDebt absent AND there is NO vendor shortLongTermDebtTotal
        # to fall back to either (build_snapshot._build_net_cash's
        # total_debt_source == "incomplete_no_vendor"). The old wording
        # claimed "total_debt may be vendor-sourced" here even though NO
        # vendor number was ever consulted -- the detail must say so
        # honestly (understated, not vendor-sourced) instead.
        def m(s):
            ncd = s["fundamentals"]["net_cash_defined"]
            ncd["short_term_debt"] = 500_000_000.0
            ncd["long_term_debt"] = None
            ncd["total_debt"] = 500_000_000.0
            ncd["total_debt_source"] = "incomplete_no_vendor"
            ncd["vendor_aggregates"]["short_long_term_debt_total"] = None
            ncd["net"] = ncd["cash_st"] + ncd["lt_inv"] - ncd["total_debt"]
        r = self._run_one(m, "check_net_cash")
        self.assertIs(r["passed"], True)
        self.assertIn("components incomplete", r["detail"])
        self.assertNotIn("may be vendor-sourced", r["detail"])
        self.assertIn("no vendor", r["detail"].lower())

    def test_net_cash_lt_inv_sourcing_unaffected_on_pre_d5_snapshot_shape(self):
        # Backward compatibility: a snapshot fixture that predates D5 (no
        # long_term_investments key at all in net_cash_defined) must see NO
        # new disclosure text -- absence of the KEY (old bundle shape) is
        # not the same as a disclosed absent COMPONENT (D5).
        r = self._run_one(lambda s: None, "check_net_cash")
        self.assertIs(r["passed"], True)
        self.assertNotIn("long_term_investments", r["detail"])

    def test_net_cash_lt_inv_sourcing_discloses_absent_component(self):
        # D5: real UNH shape -- long_term_investments component absent, so
        # lt_inv silently defaults to 0.0. The sourcing leg must disclose
        # this (never fail the gate over it -- a missing component is not
        # an arithmetic inconsistency).
        def m(s):
            ncd = s["fundamentals"]["net_cash_defined"]
            ncd["long_term_investments"] = None
            ncd["lt_inv"] = 0.0
            ncd["net"] = ncd["cash_st"] + ncd["lt_inv"] - ncd["total_debt"]
        r = self._run_one(m, "check_net_cash")
        self.assertIs(r["passed"], True)
        self.assertIn("long_term_investments", r["detail"])
        self.assertIn("absent", r["detail"].lower())

    def test_net_cash_lt_inv_sourcing_silent_when_present(self):
        # A genuinely present (possibly zero) long_term_investments needs no
        # disclosure -- nothing is missing.
        def m(s):
            ncd = s["fundamentals"]["net_cash_defined"]
            ncd["long_term_investments"] = ncd["lt_inv"]
        r = self._run_one(m, "check_net_cash")
        self.assertIs(r["passed"], True)
        self.assertNotIn("long_term_investments", r["detail"])

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


class TestNetCashVendorSignatureDisclosure(unittest.TestCase):
    """QC4: a distinct, NEVER-failing disclosure check for the measured
    vendor cashAndShortTermInvestments defect (== cash_and_equivalents alone
    while short_term_investments > 0) -- visible in checks[], never blocks
    the gate. net_cash_defined already routes around it via component sums;
    this check exists purely to surface that a vendor field is broken."""

    def test_discloses_defect_signature_but_never_fails(self):
        s = make_snapshot()
        ncd = s["fundamentals"]["net_cash_defined"]
        ncd["cash_and_equivalents"] = 24_995_000_000.0
        ncd["short_term_investments"] = 1_027_000_000.0
        ncd["vendor_aggregates"]["cash_and_short_term_investments"] = 24_995_000_000.0
        r = Q.check_net_cash_vendor_signature(s)
        self.assertIs(r["passed"], True)  # NEVER fails -- disclosure only
        self.assertIn("DISCLOSURE", r["detail"])
        self.assertIn("cashAndShortTermInvestments", r["detail"])

    def test_no_signature_when_vendor_matches_full_component_sum(self):
        # Base fixture is clean: aggregate (10B) != cce alone (7B).
        s = make_snapshot()
        r = Q.check_net_cash_vendor_signature(s)
        self.assertIs(r["passed"], True)
        self.assertNotIn("DISCLOSURE", r["detail"])

    def test_skips_when_vendor_aggregate_absent(self):
        s = make_snapshot()
        del s["fundamentals"]["net_cash_defined"]["vendor_aggregates"]
        r = Q.check_net_cash_vendor_signature(s)
        self.assertIsNone(r["passed"])

    def test_registered_in_gate_and_never_fails_it(self):
        s = make_snapshot()
        ncd = s["fundamentals"]["net_cash_defined"]
        ncd["cash_and_equivalents"] = 24_995_000_000.0
        ncd["short_term_investments"] = 1_027_000_000.0
        ncd["cash_st"] = ncd["cash_and_equivalents"] + ncd["short_term_investments"]
        ncd["net"] = ncd["cash_st"] + ncd["lt_inv"] - ncd["total_debt"]
        ncd["vendor_aggregates"]["cash_and_short_term_investments"] = 24_995_000_000.0
        r = Q.run_qc(s)
        self.assertIs(r["passed"], True)
        checks = _names(r)
        self.assertIn("check_net_cash_vendor_signature", checks)
        self.assertIs(checks["check_net_cash_vendor_signature"]["passed"], True)
        self.assertIn("DISCLOSURE", checks["check_net_cash_vendor_signature"]["detail"])


class TestNetCashVendorSignatureDebtAggregateClassification(unittest.TestCase):
    """QC4-REGRESSION: check_net_cash_vendor_signature also classifies the
    debt-aggregate relationship between our component-built total_debt and
    vendor_aggregates.short_long_term_debt_total, as matches-(a) (vendor ==
    shortTermDebt+longTermDebt), matches-(b) (vendor == longTermDebt+
    capitalLeaseObligations, OMITTING shortTermDebt), matches-neither, or
    components-incomplete (shortTermDebt or longTermDebt absent). Measured
    (22-ticker survey): 4/22 matches-(a), 4/22 matches-(b), 13/22
    matches-neither, 1/22 not testable (every component absent). This leg
    NEVER fails the gate -- it exists purely so the disagreement (including
    the JPM/XOM/UNH/CAT incomplete-components cases from the Problem-1 fix)
    is always visible in checks[], never silent."""

    def test_matches_a_is_not_flagged_as_a_disagreement(self):
        # Base fixture: std=1e9, ltd=2e9, vendor total=3e9 -- exact match.
        s = make_snapshot()
        r = Q.check_net_cash_vendor_signature(s)
        self.assertIs(r["passed"], True)
        self.assertIn("matches-(a)", r["detail"])

    def test_matches_b_real_mu_shape(self):
        s = make_snapshot()
        ncd = s["fundamentals"]["net_cash_defined"]
        ncd["short_term_debt"] = 582_000_000.0
        ncd["long_term_debt"] = 3_052_000_000.0
        ncd["capital_lease_obligations"] = 3_324_000_000.0
        ncd["vendor_aggregates"]["short_long_term_debt_total"] = 6_376_000_000.0
        r = Q.check_net_cash_vendor_signature(s)
        self.assertIs(r["passed"], True)
        self.assertIn("matches-(b)", r["detail"])

    def test_matches_neither_real_msft_shape_is_loud(self):
        # MSFT: std 18.905B + ltd 31.067B + leases 16.532B = 66.504B vs a
        # vendor aggregate of 128.808B -- our component build (49.972B) is
        # the more defensible number, but the disagreement must be LOUD.
        s = make_snapshot()
        ncd = s["fundamentals"]["net_cash_defined"]
        ncd["short_term_debt"] = 18_905_300_000.0
        ncd["long_term_debt"] = 31_067_000_000.0
        ncd["capital_lease_obligations"] = 16_532_000_000.0
        ncd["total_debt"] = 49_972_300_000.0
        ncd["vendor_aggregates"]["short_long_term_debt_total"] = 128_808_300_000.0
        r = Q.check_net_cash_vendor_signature(s)
        self.assertIs(r["passed"], True)   # NEVER fails
        self.assertIn("matches-neither", r["detail"])
        self.assertIn("DISCLOSURE", r["detail"])

    def test_components_incomplete_real_jpm_shape_is_loud_no_sector_logic(self):
        # JPM: shortLongTermDebtTotal is $1.24 TRILLION (deposits/trading
        # liabilities bleeding into an industrial-company schema) with
        # longTermDebt absent. No sector special-casing -- the SAME
        # components-incomplete classification that fires for XOM/UNH/CAT
        # fires here too; the huge number is simply carried and disclosed.
        s = make_snapshot()
        ncd = s["fundamentals"]["net_cash_defined"]
        ncd["short_term_debt"] = 777_348_000_000.0
        ncd["long_term_debt"] = None
        ncd["total_debt"] = 1_237_871_000_000.0
        ncd["vendor_aggregates"]["short_long_term_debt_total"] = 1_237_871_000_000.0
        r = Q.check_net_cash_vendor_signature(s)
        self.assertIs(r["passed"], True)
        self.assertIn("components-incomplete", r["detail"])
        self.assertIn("DISCLOSURE", r["detail"])

    def test_skips_debt_leg_when_vendor_debt_absent(self):
        s = make_snapshot()
        del s["fundamentals"]["net_cash_defined"]["vendor_aggregates"]["short_long_term_debt_total"]
        r = Q.check_net_cash_vendor_signature(s)
        # cash leg still evaluable -> overall still True, not a SKIP.
        self.assertIs(r["passed"], True)
        self.assertIn("SKIP", r["detail"])


class TestWaivers(unittest.TestCase):
    def test_waived_failure_does_not_fail_gate(self):
        # G1: to trigger a genuine check_mktcap FAIL the ratio must be outside
        # the multi-class band.  overview × 10 → ratio ≈ 0.1 < 0.15 → FAIL
        # (implausible for any known class-structure reason) → waiveable.
        s = make_snapshot()
        s["price"]["mktcap_overview"] *= 10  # breaks check_mktcap (out-of-band)
        s["meta"]["qc"]["waivers"] = [
            {"check": "check_mktcap", "reason": "known share-count lag"}
        ]
        r = Q.run_qc(s)
        self.assertIs(r["passed"], True)
        checks = _names(r)
        self.assertTrue(checks["check_mktcap"]["detail"].startswith("WAIVED"))
        self.assertIn("known share-count lag", checks["check_mktcap"]["detail"])

    def test_unwaived_failure_fails_gate(self):
        # G1: ratio must be outside the multi-class band to remain a hard FAIL.
        s = make_snapshot()
        s["price"]["mktcap_overview"] *= 10  # out-of-band → still FAIL
        r = Q.run_qc(s)
        self.assertIs(r["passed"], False)


def _goog_secmaster_snapshot():
    """Minimal snapshot carrying a valid derived multi-class GOOG security_master.

    Mirrors the real GOOG shape: price.mktcap = issuer cap (overview_authoritative),
    security_master.issuer_total_shares_m derived = mktcap / last (exact round-trip).
    """
    last = 351.37
    issuer_mktcap = 4287617827000.0
    issuer_total_m = issuer_mktcap / last / 1e6  # ~12202.57
    return {
        "price": {
            "last": last,
            "shares_diluted_m": 5499.638,
            "mktcap": issuer_mktcap,
            "mktcap_basis": "overview_authoritative",
        },
        "security_master": {
            "ticker": "GOOG",
            "share_class": "C",
            "class_shares_m": 5499.638,
            "issuer_total_shares_m": issuer_total_m,
            "issuer_diluted_shares_m": issuer_total_m,
            "issuer_mktcap": issuer_mktcap,
            "mktcap_basis": "overview_authoritative",
            "shares_source": "derived: issuer mktcap / class price",
            "reconciled_to_filing": False,
            "other_listed_classes": ["GOOGL"],
        },
    }


class TestSecurityMasterCheck(unittest.TestCase):
    def test_valid_goog_block_passes(self):
        r = Q.check_security_master(_goog_secmaster_snapshot())
        self.assertIs(r["passed"], True)

    def test_derived_unreconciled_block_still_passes(self):
        # reconciled_to_filing=False (derived) is DISCLOSURE, never a FAIL.
        s = _goog_secmaster_snapshot()
        self.assertIs(s["security_master"]["reconciled_to_filing"], False)
        self.assertIs(Q.check_security_master(s)["passed"], True)

    def test_issuer_mktcap_mismatch_fails(self):
        s = _goog_secmaster_snapshot()
        s["security_master"]["issuer_mktcap"] = 999.0  # != price.mktcap
        r = Q.check_security_master(s)
        self.assertIs(r["passed"], False)
        self.assertIn("issuer_mktcap", r["detail"])

    def test_class_exceeds_issuer_fails(self):
        s = _goog_secmaster_snapshot()
        s["security_master"]["issuer_total_shares_m"] = 1.0  # < class_shares_m
        r = Q.check_security_master(s)
        self.assertIs(r["passed"], False)
        self.assertIn("class_shares_m", r["detail"])

    def test_roundtrip_incoherence_fails(self):
        s = _goog_secmaster_snapshot()
        # Break the cap/shares/price identity while keeping class<=issuer and
        # issuer_mktcap==price.mktcap: halve issuer_total so cap != shares*last.
        s["security_master"]["issuer_total_shares_m"] = 6000.0
        r = Q.check_security_master(s)
        self.assertIs(r["passed"], False)
        self.assertIn("round-trip", r["detail"])

    def test_missing_disclosure_fields_fail(self):
        s = _goog_secmaster_snapshot()
        s["security_master"]["shares_source"] = None
        s["security_master"]["reconciled_to_filing"] = None
        r = Q.check_security_master(s)
        self.assertIs(r["passed"], False)
        self.assertIn("shares_source absent", r["detail"])
        self.assertIn("reconciled_to_filing absent", r["detail"])

    def test_absent_block_skips(self):
        r = Q.check_security_master({"price": {"mktcap": 1.0, "last": 1.0}})
        self.assertIsNone(r["passed"])
        self.assertIn("SKIP", r["detail"])

    def test_degraded_block_with_nulls_passes(self):
        # No overview/last -> nulls + "unavailable"; the cap-equality it can still
        # assert holds (both null), numeric legs skip -> PASS.
        s = {
            "price": {"last": None, "mktcap": None, "mktcap_basis": None},
            "security_master": {
                "ticker": "XYZ", "share_class": None,
                "class_shares_m": None, "issuer_total_shares_m": None,
                "issuer_diluted_shares_m": None, "issuer_mktcap": None,
                "mktcap_basis": None, "shares_source": "unavailable",
                "reconciled_to_filing": False, "other_listed_classes": [],
            },
        }
        self.assertIs(Q.check_security_master(s)["passed"], True)

    def test_gate_still_passes_with_valid_secmaster(self):
        # Full gate: a clean snapshot augmented with a valid security_master
        # keeps passing and now runs 10 checks with check_security_master PASS.
        s = make_snapshot()
        s["price"]["mktcap"] = s["price"]["mktcap_overview"]
        s["price"]["mktcap_basis"] = "reconciled_agree"
        s["security_master"] = {
            "ticker": "AAA", "share_class": None,
            "class_shares_m": 1000.0,
            "issuer_total_shares_m": 1000.0,
            "issuer_diluted_shares_m": 1000.0,
            "issuer_mktcap": s["price"]["mktcap_overview"],
            "mktcap_basis": "reconciled_agree",
            "shares_source": "av_class_shares",
            "reconciled_to_filing": True,
            "other_listed_classes": [],
        }
        r = Q.run_qc(s)
        self.assertIs(r["passed"], True)
        checks = _names(r)
        self.assertIs(checks["check_security_master"]["passed"], True)


class TestForwardPECrossVendor(unittest.TestCase):
    """QC1: check_forward_pe_crossvendor(valuation.pe_fwd vs pe_overview_fwd).

    Pre-registered claim under test (docs/QC_REMEDIATION_TRACKER.md QC1): "the
    cross-vendor leg alone catches this [NTM-EPS] defect class on both names."
    Measured relative deltas against the SHIPPED (buggy) pe_fwd values say
    otherwise -- AAPL's shipped pe_fwd is only 11.1% off vendor ForwardPE
    (under the 25% tolerance: the leg does NOT catch it there; the blend fix
    itself is what corrects AAPL's number). MU's shipped pe_fwd is 125.0% off
    (over tolerance: the leg DOES catch it). The two tests below pin both
    outcomes explicitly so this claim can never silently regress back to
    "the QC leg alone is sufficient."
    """

    def _run(self, pe_fwd, pe_overview_fwd):
        return Q.check_forward_pe_crossvendor(
            {"valuation": {"pe_fwd": pe_fwd, "pe_overview_fwd": pe_overview_fwd}})

    def test_skips_when_pe_overview_fwd_absent(self):
        r = Q.check_forward_pe_crossvendor({"valuation": {"pe_fwd": 20.0}})
        self.assertIsNone(r["passed"])

    def test_skips_when_pe_fwd_absent(self):
        r = Q.check_forward_pe_crossvendor({"valuation": {"pe_overview_fwd": 20.0}})
        self.assertIsNone(r["passed"])

    def test_skips_when_either_non_positive(self):
        self.assertIsNone(self._run(-5.0, 20.0)["passed"])
        self.assertIsNone(self._run(20.0, 0.0)["passed"])

    def test_fails_beyond_25pct(self):
        # 20 vs 10 -> 100% diff.
        r = self._run(20.0, 10.0)
        self.assertIs(r["passed"], False)

    def test_passes_within_25pct(self):
        # 12 vs 10 -> 20% diff.
        r = self._run(12.0, 10.0)
        self.assertIs(r["passed"], True)

    def test_falsified_claim_aapl_shipped_pair_is_NOT_caught(self):
        # AAPL shipped (buggy) pe_fwd 35.501943225982416 vs vendor ForwardPE
        # 31.95: |35.5019-31.95|/31.95 = 11.1% -- UNDER the 25% tolerance.
        r = self._run(35.501943225982416, 31.95)
        self.assertIs(r["passed"], True,
                      "the cross-vendor leg does NOT catch AAPL's shipped NTM-EPS "
                      "defect -- the falsified claim this test pins")

    def test_mu_shipped_pair_IS_caught(self):
        # MU shipped (buggy) pe_fwd 11.948831762975873 vs vendor ForwardPE
        # 5.31: |11.9488-5.31|/5.31 = 125.0% -- well OVER the 25% tolerance.
        r = self._run(11.948831762975873, 5.31)
        self.assertIs(r["passed"], False)

    def test_corrected_pair_passes_on_both_names(self):
        # After the QC1 blend fix, BOTH corrected pe_fwd values reconcile to
        # their vendor ForwardPE counterparty comfortably within tolerance.
        aapl = self._run(33.192740411305984, 31.95)   # 3.9% diff
        mu = self._run(5.919185272403049, 5.31)        # 11.5% diff
        self.assertIs(aapl["passed"], True)
        self.assertIs(mu["passed"], True)

    def test_registered_in_gate(self):
        s = make_snapshot()
        r = Q.run_qc(s)
        checks = _names(r)
        self.assertIn("check_forward_pe_crossvendor", checks)
        self.assertIs(checks["check_forward_pe_crossvendor"]["passed"], True)


class TestBetaCrossVendor(unittest.TestCase):
    """New BLOCKING check (post-QC1): check_beta_crossvendor(benchmark.beta
    vs benchmark.beta_vendor). QC1 gave forward-P/E a blocking cross-vendor
    leg; beta had none until now.

    HYBRID tolerance, deliberately: fails only when BOTH the relative delta
    exceeds 25% AND the absolute delta exceeds 0.15 (denominator = the
    vendor figure, matching check_forward_pe_crossvendor's convention of
    dividing by the vendor's own number). A pure relative tolerance is
    fragile at low beta (JNJ's real beta is 0.231, where a 0.05 absolute
    miss alone is a ~22% relative miss); a pure absolute tolerance is
    fragile at high beta. Measured relative deltas with simple returns
    across a 16-ticker study: 15 of 16 land within 4% (NVDA 0.0%, INTC
    0.04%, AAPL 0.5%, JPM 0.5%, TSLA 0.7%, MU 0.8%, MSFT 1.0%, GOOG 1.8%,
    WMT 2.0%, PG 2.1%, UNH 2.2%, COHR 2.3%, KO 2.6%, JNJ 3.5%, PLTR 3.8%)
    and exactly one fires: T (AT&T) at 45.1% (ours 0.229 vs AV 0.417,
    absolute 0.188).

    T is a TRUE POSITIVE, not a false alarm -- traced to the April-2022
    WarnerMedia/Discovery spinoff, which AV encodes as split_coefficient
    1.324 on 2022-04-11 (a ~19% one-day drop recorded as a synthetic split).
    That artifact depresses OUR covariance-based beta; AT&T's real beta is
    around 0.5-0.6, so OUR number is the corrupted one. This check is
    therefore a genuine correctness signal about our own value, which is
    why it is BLOCKING rather than disclosure-only (unlike check_eps, where
    the divergences were structural vendor-basis differences rather than
    errors in our number).
    """

    def _run(self, beta, beta_vendor):
        return Q.check_beta_crossvendor(
            {"benchmark": {"beta": beta, "beta_vendor": beta_vendor}})

    def test_skips_when_beta_absent(self):
        r = Q.check_beta_crossvendor({"benchmark": {"beta_vendor": 1.0}})
        self.assertIsNone(r["passed"])

    def test_skips_when_beta_vendor_absent(self):
        r = Q.check_beta_crossvendor({"benchmark": {"beta": 1.0}})
        self.assertIsNone(r["passed"])

    def test_skips_when_either_non_positive(self):
        self.assertIsNone(self._run(-0.5, 1.0)["passed"])
        self.assertIsNone(self._run(1.0, 0.0)["passed"])
        self.assertIsNone(self._run(1.0, -0.2)["passed"])

    def test_fires_on_real_t_shape(self):
        # ours 0.229 vs AV 0.417: 45.1% relative AND 0.188 absolute -- BOTH
        # over tolerance -> fails. Real, corrupted-by-spinoff defect.
        r = self._run(0.229, 0.417)
        self.assertIs(r["passed"], False)

    def test_passes_on_real_aapl_pair(self):
        r = self._run(1.081, 1.086)
        self.assertIs(r["passed"], True)

    def test_passes_on_real_mu_pair(self):
        r = self._run(2.196, 2.213)
        self.assertIs(r["passed"], True)

    def test_passes_low_beta_large_relative_small_absolute(self):
        # JNJ-shaped: vendor 0.231, ours 0.281 -- 21.6% relative (< 25% tol)
        # but only 0.05 absolute (< 0.15 tol) -- proves the hybrid AND gate
        # does not fire just because the relative delta looks large at low
        # beta.
        r = self._run(0.281, 0.231)
        self.assertIs(r["passed"], True)

    def test_fails_only_when_both_legs_exceed_tolerance(self):
        # Large relative (100%) but tiny absolute (0.01) -> must PASS: the
        # absolute leg alone blocks a spurious fail at near-zero beta.
        r = self._run(0.02, 0.01)
        self.assertIs(r["passed"], True)
        # Small relative (10%) but huge absolute (0.5, e.g. beta 5.5 vs 5.0)
        # -> must PASS: the relative leg alone blocks a spurious fail at
        # high beta.
        r2 = self._run(5.5, 5.0)
        self.assertIs(r2["passed"], True)

    def test_registered_in_gate(self):
        s = make_snapshot()
        s["benchmark"]["beta_vendor"] = 1.12  # close to fixture beta 1.1
        r = Q.run_qc(s)
        checks = _names(r)
        self.assertIn("check_beta_crossvendor", checks)
        self.assertIs(checks["check_beta_crossvendor"]["passed"], True)

    def test_skips_in_gate_when_beta_vendor_absent(self):
        # make_snapshot()'s benchmark block carries no beta_vendor.
        r = Q.run_qc(make_snapshot())
        checks = _names(r)
        self.assertIsNone(checks["check_beta_crossvendor"]["passed"])


class TestCheckEpsReconciliation(unittest.TestCase):
    """QC3 leg A / D1-REGRESSION: check_eps -- three-way TTM EPS
    reconciliation (vendor vs sum-of-reportedEPS vs net-income-implied) --
    DISCLOSURE-ONLY (never fails the gate). Measured max pairwise divergence
    on the two CALIBRATION names (AAPL 1.63%, MU 1.89%) originally set the 3%
    tolerance, but an 8-ticker out-of-sample survey (rebuilt archived
    bundles, real gate) found vendor eps_ttm (GAAP-diluted) and
    eps_ttm_computed (often non-GAAP) are genuinely different quantities
    whose real dispersion routinely exceeds 3%: NVDA 12.05%, TSLA 36.79%,
    UNH 40.57%, ORCL 23.06%, TSM 22.01% (plus a 3705.10% currency-mismatch
    eps_ttm_from_ni leg), PLTR 6.74%, MRVL 6.65% -- 7 of 8 measured tickers
    exceed tolerance on this leg alone. Since skills/market-snapshot/
    SKILL.md requires gate exit 0 to proceed with no default waiver, a
    BLOCKING leg here halted nearly every analysis run outside the two
    calibration names. The check now may return True or None (SKIP) but
    NEVER False; the offending pair(s) and measured divergence are still
    named in the detail so a corrupt figure remains visible."""

    def test_skips_when_fewer_than_two_values_present(self):
        r = Q.check_eps({"fundamentals": {"eps_ttm": 5.0}})
        self.assertIsNone(r["passed"])

    def test_passes_within_tolerance(self):
        s = {"fundamentals": {"eps_ttm": 8.75, "eps_ttm_computed": 8.61,
                              "eps_ttm_from_ni": 8.74083798419856}}
        r = Q.check_eps(s)
        self.assertIs(r["passed"], True)

    def test_measured_aapl_pair_passes(self):
        # Real AAPL ground truth (docs/QC_REMEDIATION_TRACKER.md QC3): max
        # pairwise divergence 1.63% -- under the 3% tolerance.
        s = {"fundamentals": {"eps_ttm": 8.75, "eps_ttm_computed": 8.61,
                              "eps_ttm_from_ni": 8.74083798419856}}
        self.assertIs(Q.check_eps(s)["passed"], True)

    def test_measured_mu_pair_passes(self):
        # Real MU ground truth: max pairwise divergence 1.89% -- under 3%.
        s = {"fundamentals": {"eps_ttm": 44.05, "eps_ttm_computed": 44.90,
                              "eps_ttm_from_ni": 44.08122270742358}}
        self.assertIs(Q.check_eps(s)["passed"], True)

    def test_beyond_tolerance_discloses_but_never_fails(self):
        # D1-REGRESSION: this used to FAIL the gate; now it must DISCLOSE
        # (True, never False) with the offending pair(s) named.
        s = {"fundamentals": {"eps_ttm": 10.0, "eps_ttm_computed": 8.0,
                              "eps_ttm_from_ni": 9.9}}
        r = Q.check_eps(s)
        self.assertIs(r["passed"], True)   # NEVER False -- disclosure only
        self.assertIn("DISCLOSURE", r["detail"])
        self.assertIn("vendor=10", r["detail"])
        self.assertIn("computed=8", r["detail"])

    def test_measured_nvda_shape_discloses_but_never_fails(self):
        # Real NVDA out-of-sample rebuild: vendor 6.52 vs computed 5.84
        # (10.43%), computed 5.84 vs from_ni 6.543930138165717 (12.05%) --
        # max 12.05%, well above the 3% tolerance.
        s = {"fundamentals": {"eps_ttm": 6.52, "eps_ttm_computed": 5.84,
                              "eps_ttm_from_ni": 6.543930138165717}}
        r = Q.check_eps(s)
        self.assertIs(r["passed"], True)
        self.assertIn("DISCLOSURE", r["detail"])

    def test_measured_tsla_shape_discloses_but_never_fails(self):
        # Real TSLA out-of-sample rebuild: vendor 1.06 vs computed 1.45 ->
        # 36.79% diff, the largest measured leg-A divergence.
        s = {"fundamentals": {"eps_ttm": 1.06, "eps_ttm_computed": 1.45,
                              "eps_ttm_from_ni": 1.0785310734463276}}
        r = Q.check_eps(s)
        self.assertIs(r["passed"], True)
        self.assertIn("36.79%", r["detail"])

    def test_measured_tsm_currency_mismatch_never_fails(self):
        # Real TSM out-of-sample rebuild: netIncome in TWD vs reportedEPS in
        # USD/ADS inflates eps_ttm_from_ni ~31x-3705% divergence -- even at
        # this magnitude the check must still only ever return True.
        s = {"fundamentals": {"eps_ttm": 11.36, "eps_ttm_computed": 13.86,
                              "eps_ttm_from_ni": 432.2589433904057}}
        r = Q.check_eps(s)
        self.assertIs(r["passed"], True)

    def test_registered_in_gate(self):
        checks = _names(Q.run_qc(make_snapshot()))
        self.assertIn("check_eps", checks)
        self.assertIs(checks["check_eps"]["passed"], True)

    def test_never_blocks_gate_even_with_gross_divergence(self):
        # D1 acceptance: a snapshot whose ONLY problem is a grossly divergent
        # check_eps triple must still pass run_qc() end to end. eps_ttm is
        # left at the fixture's base 5.0 (check_pe_arithmetic's pe_ttm leg
        # depends on it, not on eps_ttm_computed/eps_ttm_from_ni) so this
        # isolates check_eps as the ONLY divergent input.
        s = make_snapshot()
        s["fundamentals"]["eps_ttm_computed"] = 7.0    # 40% vs eps_ttm 5.0
        s["fundamentals"]["eps_ttm_from_ni"] = 5.05
        result = Q.run_qc(s)
        checks = _names(result)
        self.assertIs(checks["check_eps"]["passed"], True)
        self.assertIn("DISCLOSURE", checks["check_eps"]["detail"])
        self.assertTrue(result["passed"])


class TestCheckEpsQuarterlyShares(unittest.TestCase):
    """QC3 leg B / QC3-REGRESSION: check_eps_quarterly_shares -- per-quarter
    implied-share-count divergence, joined on the SAME quarter's balance
    sheet. DISCLOSURE-ONLY (never fails the gate): the 22-ticker survey
    measured this leg's fire rate at the shipped 5% tolerance to be 43/88 =
    48.9% of ALL quarters -- a coin flip, so it cannot function as a blocking
    gate. Full distribution (88 quarters, |divergence_pct|): min 0.04%, p50
    4.94%, p90 188.5%, p95 656.9%, max 3062.9%.

    Two STRUCTURAL (not corruption) causes were pinned:
      1. Currency mismatch on foreign issuers: TSM's netIncome is in New
         Taiwan Dollars while reportedEPS is USD-per-ADS -- dividing them
         inflates implied shares ~31.6x; all four TSM quarters land at
         +2886%..+3063%. The check is mechanically INAPPLICABLE here.
      2. Near-zero-EPS amplification: INTC's quarterly EPS ($0.13-$0.30)
         means a few cents of GAAP-vs-reported basis difference produces
         -820%/-353%/-181%/+290%; same pattern on RUN, BE, OUST, UNH.
    No threshold separates AAPL's genuine corrupt quarter (+5.74%,
    reportedEPS 1.91 where GAAP was 2.02) from these structural artifacts, so
    the check now reports (never blocks): it may return True or None (SKIP)
    but NEVER False. The offending quarter(s) and measured percentage are
    still named in the detail so a human or LLM reader sees AAPL's corrupt
    row in meta.qc.checks. _EPS_IMPLIED_SHARES_TOL is kept -- it now selects
    what gets REPORTED (flagged in the detail), not what fails the gate.
    """

    def test_skips_when_no_rows(self):
        r = Q.check_eps_quarterly_shares({"fundamentals": {}})
        self.assertIsNone(r["passed"])

    def test_passes_within_tolerance(self):
        s = {"fundamentals": {"eps_share_reconciliation": [
            {"fiscal_date_ending": "2026-06-30", "divergence_pct": 0.02},
            {"fiscal_date_ending": "2026-03-31", "divergence_pct": -0.03},
        ]}}
        self.assertIs(Q.check_eps_quarterly_shares(s)["passed"], True)

    def test_beyond_tolerance_discloses_but_never_fails(self):
        s = {"fundamentals": {"eps_share_reconciliation": [
            {"fiscal_date_ending": "2026-06-30", "divergence_pct": 0.08},
        ]}}
        r = Q.check_eps_quarterly_shares(s)
        self.assertIs(r["passed"], True)   # NEVER False -- disclosure only
        self.assertIn("2026-06-30", r["detail"])
        self.assertIn("8.00%", r["detail"])

    def test_corrupt_reportedEPS_discloses_but_never_fails_leg_b(self):
        # QC3-REGRESSION: AAPL's real FQ3-2026 reportedEPS (1.91) is corrupt.
        # implied_shares = 29,789,000,000 / 1.91; same-quarter shares
        # 14,750,302,000 -> +5.7357% divergence -- named in the detail, but
        # this leg must no longer FAIL the gate over it (see class docstring:
        # 48.9% fire rate makes this leg unusable as a blocking gate).
        implied = 29_789_000_000.0 / 1.91
        shares = 14_750_302_000.0
        row = {
            "fiscal_date_ending": "2026-06-30",
            "net_income": 29_789_000_000.0, "reported_eps": 1.91,
            "implied_shares": implied, "balance_shares_same_quarter": shares,
            "divergence_pct": (implied - shares) / shares,
        }
        s = {"fundamentals": {"eps_share_reconciliation": [row]}}
        r = Q.check_eps_quarterly_shares(s)
        self.assertIs(r["passed"], True)          # NEVER False
        self.assertIn("2026-06-30", r["detail"])  # offending quarter still named
        self.assertIn("+5.74%", r["detail"])      # measured percentage still named

    def test_corrected_reportedEPS_passes_leg_b(self):
        # The same quarter with the corrected EPS (~2.0196, i.e. NI / shares)
        # must PASS cleanly: implied_shares reconciles almost exactly to the
        # actual same-quarter balance-sheet count.
        eps = round(29_789_000_000.0 / 14_750_302_000.0, 4)  # 2.0196
        shares = 14_750_302_000.0
        implied = 29_789_000_000.0 / eps
        row = {
            "fiscal_date_ending": "2026-06-30",
            "net_income": 29_789_000_000.0, "reported_eps": eps,
            "implied_shares": implied, "balance_shares_same_quarter": shares,
            "divergence_pct": (implied - shares) / shares,
        }
        s = {"fundamentals": {"eps_share_reconciliation": [row]}}
        r = Q.check_eps_quarterly_shares(s)
        self.assertIs(r["passed"], True)

    def test_extreme_currency_mismatch_never_fails(self):
        # TSM structural cause: netIncome in TWD vs reportedEPS in USD/ADS
        # inflates implied shares ~31.6x -- measured +2886%..+3063%. Even at
        # this magnitude the check must still only ever return True.
        row = {"fiscal_date_ending": "2026-06-30", "divergence_pct": 30.629}
        s = {"fundamentals": {"eps_share_reconciliation": [row]}}
        r = Q.check_eps_quarterly_shares(s)
        self.assertIs(r["passed"], True)
        self.assertIn("2026-06-30", r["detail"])

    def test_near_zero_eps_amplification_never_fails(self):
        # INTC structural cause: near-zero quarterly EPS amplifies a few
        # cents of GAAP-vs-reported basis difference into triple digits.
        row = {"fiscal_date_ending": "2026-03-31", "divergence_pct": -8.20}
        s = {"fundamentals": {"eps_share_reconciliation": [row]}}
        r = Q.check_eps_quarterly_shares(s)
        self.assertIs(r["passed"], True)

    def test_registered_in_gate(self):
        checks = _names(Q.run_qc(make_snapshot()))
        self.assertIn("check_eps_quarterly_shares", checks)
        self.assertIs(checks["check_eps_quarterly_shares"]["passed"], True)


if __name__ == "__main__":
    unittest.main()
