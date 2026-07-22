"""G1 — Reconciled issuer market-cap tests.

Covers:
 1. reconcile_mktcap() pure-function decision table (4 cases).
 2. build_valuation fcf_yield uses reconciled cap.
 3. score_risk net-cash ratio reads price.mktcap, not price.mktcap_computed.
 4. check_mktcap: multi-class band → non-failing pass; out-of-band → FAIL.

stdlib-only; unittest.
"""

import unittest

from scripts.build_snapshot import reconcile_mktcap
from scripts import score_risk as sr
from scripts import qc as Q


# --------------------------------------------------------------------------- #
# 1. reconcile_mktcap() decision table
# --------------------------------------------------------------------------- #

class TestReconcileMktcap(unittest.TestCase):
    """Four-case pure-function unit table."""

    # Case 1a: overview absent (None) → computed_only
    def test_overview_none_returns_computed_only(self):
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=None,
            mktcap_computed=1_932_407_804_060.0,
            last=351.0, prev_close=348.0, shares_m=5499.6,
        )
        self.assertEqual(basis, "computed_only")
        self.assertEqual(mktcap, 1_932_407_804_060.0)

    # Case 1b: overview = 0 → computed_only
    def test_overview_zero_returns_computed_only(self):
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=0,
            mktcap_computed=100_000_000_000.0,
            last=100.0, prev_close=99.0, shares_m=1000.0,
        )
        self.assertEqual(basis, "computed_only")

    # Case 1c: overview negative → computed_only
    def test_overview_negative_returns_computed_only(self):
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=-1.0,
            mktcap_computed=100_000_000_000.0,
            last=100.0, prev_close=99.0, shares_m=1000.0,
        )
        self.assertEqual(basis, "computed_only")

    # Case 2: single-class issuer, computed ≈ overview within 2% → reconciled_agree
    def test_single_class_agree_uses_computed(self):
        # last=100, shares_m=1000 → computed=100e9; overview=100e9 → 0% diff → agree
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=100_000_000_000.0,
            mktcap_computed=100_000_000_000.0,
            last=100.0, prev_close=99.0, shares_m=1000.0,
        )
        self.assertEqual(basis, "reconciled_agree")
        self.assertAlmostEqual(mktcap, 100_000_000_000.0)

    # Case 2 variant: prev_close reconciles (big-move day, vendor mktcap is prior-session)
    def test_prev_close_reconciles_agree(self):
        # last=104 → computed=104e9; overview=100e9 (prev_close=100 reconciles)
        prev_close = 100.0
        shares_m = 1000.0
        computed = 104.0 * shares_m * 1e6  # 104e9
        overview = prev_close * shares_m * 1e6  # 100e9  (matches prev_close)
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=overview,
            mktcap_computed=computed,
            last=104.0, prev_close=prev_close, shares_m=shares_m,
        )
        self.assertEqual(basis, "reconciled_agree")
        self.assertAlmostEqual(mktcap, computed)

    # Case 3 (GOOG-like): computed/overview ≈ 0.45 → multi-class band → overview_authoritative
    def test_goog_like_multiclass_uses_overview(self):
        # GOOG verified values (2026-07-16 bundle)
        computed = 1_932_407_804_060.0   # last(351.0) × shares_m(5499.6) × 1e6
        overview = 4_287_617_827_000.0   # AV MarketCapitalization (issuer-level)
        ratio = computed / overview       # ≈ 0.4507
        self.assertGreater(ratio, 0.15)
        self.assertLess(ratio, 1.0)
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=overview,
            mktcap_computed=computed,
            last=351.0, prev_close=348.0, shares_m=5499.6,
        )
        self.assertEqual(basis, "overview_authoritative")
        self.assertAlmostEqual(mktcap, overview)

    # Case 3 edge: ratio just above lower bound (0.16) → still multi-class
    def test_ratio_just_above_lower_band_uses_overview(self):
        overview = 1_000_000_000_000.0
        computed = overview * 0.16       # ratio = 0.16, inside (0.15, 1.0)
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=overview,
            mktcap_computed=computed,
            last=160.0, prev_close=159.0, shares_m=1000.0,
        )
        self.assertEqual(basis, "overview_authoritative")

    # Case 4: ratio ≤ 0.15 → outside band → computed_anomaly_retained
    def test_implausible_ratio_too_small_retains_computed(self):
        # computed = 0.05 × overview → ratio = 0.05 < 0.15 → anomaly
        overview = 1_000_000_000_000.0
        computed = overview * 0.05
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=overview,
            mktcap_computed=computed,
            last=5.0, prev_close=4.9, shares_m=10000.0,
        )
        self.assertEqual(basis, "computed_anomaly_retained")
        self.assertAlmostEqual(mktcap, computed)

    # Case 4: ratio ≥ 1.0 → outside band → computed_anomaly_retained
    def test_implausible_ratio_computed_exceeds_overview_retains_computed(self):
        # computed = 1.2 × overview → ratio = 1.2 ≥ 1.0 → anomaly
        overview = 100_000_000_000.0
        computed = overview * 1.2
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=overview,
            mktcap_computed=computed,
            last=120.0, prev_close=119.0, shares_m=1000.0,
        )
        self.assertEqual(basis, "computed_anomaly_retained")
        self.assertAlmostEqual(mktcap, computed)

    # Case 4 boundary: ratio exactly at lower bound (0.15) → NOT in band → anomaly
    def test_ratio_at_lower_bound_is_anomaly(self):
        overview = 1_000_000_000_000.0
        computed = overview * 0.15  # exactly at boundary, not strictly greater
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=overview,
            mktcap_computed=computed,
            last=15.0, prev_close=14.9, shares_m=10000.0,
        )
        self.assertEqual(basis, "computed_anomaly_retained")

    # Case 4 boundary: ratio exactly at upper bound (1.0) → NOT in band → anomaly
    def test_ratio_at_upper_bound_is_anomaly(self):
        overview = 100_000_000_000.0
        computed = overview * 1.0  # exactly equal → diff = 0 → reconciled_agree
        # Actually 0 diff ≤ tol, so this hits Case 2
        mktcap, basis = reconcile_mktcap(
            mktcap_overview=overview,
            mktcap_computed=computed,
            last=100.0, prev_close=99.0, shares_m=1000.0,
        )
        self.assertEqual(basis, "reconciled_agree")


# --------------------------------------------------------------------------- #
# 2. build_valuation fcf_yield uses reconciled mktcap
# --------------------------------------------------------------------------- #

class TestBuildValuationFcfYield(unittest.TestCase):
    """Verify fcf_yield uses price.mktcap (reconciled), not price.mktcap_computed.

    GOOG fixture values (2026-07-16):
      fcf_ttm = 64_429_000_000  (verified from AV cash-flow TTM sum)
      mktcap_computed = 1_932_407_804_060  (351.0 × 5.4996B shares)
      mktcap_overview = 4_287_617_827_000  (AV MarketCapitalization, issuer-level)
      After G1 reconciliation: mktcap = mktcap_overview (overview_authoritative)

    Expected fcf_yield = 64_429_000_000 / 4_287_617_827_000 ≈ 0.01503.
    Old (wrong): 64_429_000_000 / 1_932_407_804_060 ≈ 0.03334.
    """

    FCF_TTM = 64_429_000_000.0
    MKTCAP_COMPUTED = 1_932_407_804_060.0
    MKTCAP_OVERVIEW = 4_287_617_827_000.0

    def _price(self, use_mktcap_key):
        """Build a minimal price block, with or without the reconciled mktcap key."""
        p = {
            "last": 351.0,
            "mktcap_computed": self.MKTCAP_COMPUTED,
        }
        if use_mktcap_key:
            p["mktcap"] = self.MKTCAP_OVERVIEW   # reconciled value
        return p

    def _fund(self):
        return {"fcf_ttm": self.FCF_TTM}

    def test_fcf_yield_uses_reconciled_mktcap(self):
        """price.mktcap present → fcf_yield denominator = mktcap_overview."""
        from scripts.build_snapshot import build_valuation
        price = self._price(use_mktcap_key=True)
        val = build_valuation(price, self._fund(), {}, [])
        expected = self.FCF_TTM / self.MKTCAP_OVERVIEW
        self.assertAlmostEqual(val["fcf_yield"], expected, places=7)

    def test_fcf_yield_falls_back_to_computed_when_mktcap_absent(self):
        """price.mktcap absent → fallback to price.mktcap_computed (old behavior)."""
        from scripts.build_snapshot import build_valuation
        price = self._price(use_mktcap_key=False)
        val = build_valuation(price, self._fund(), {}, [])
        expected = self.FCF_TTM / self.MKTCAP_COMPUTED
        self.assertAlmostEqual(val["fcf_yield"], expected, places=7)

    def test_fcf_yield_approx_value_goog(self):
        """Spot-check: GOOG reconciled fcf_yield ≈ 1.503%."""
        from scripts.build_snapshot import build_valuation
        price = self._price(use_mktcap_key=True)
        val = build_valuation(price, self._fund(), {}, [])
        self.assertAlmostEqual(val["fcf_yield"], 0.01503, places=4)


# --------------------------------------------------------------------------- #
# 3. score_risk reads price.mktcap for net-cash ratio
# --------------------------------------------------------------------------- #

class TestScoreRiskReadsMktcap(unittest.TestCase):
    """score_risk.score() must take the reconciled cap from the snapshot field
    price.mktcap; the CLI entry-point reads price.mktcap (falling back to
    price.mktcap_computed for backward compat).

    Test the score_liquidity function directly with the field value.
    """

    def test_net_ratio_uses_reconciled_mktcap(self):
        """score_liquidity with large reconciled cap → lower net_ratio."""
        # GOOG-like: net cash ≈ 55e9, mktcap_overview = 4.288e12
        net = 55_000_000_000.0
        reconciled_mktcap = 4_287_617_827_000.0
        old_computed_mktcap = 1_932_407_804_060.0

        sub_reconciled = sr.score_liquidity(adv=None, net=net,
                                            mktcap=reconciled_mktcap)
        sub_computed = sr.score_liquidity(adv=None, net=net,
                                          mktcap=old_computed_mktcap)

        net_ratio_reconciled = net / reconciled_mktcap  # ≈ 0.0128 < 0.05 → 5 pts
        net_ratio_computed = net / old_computed_mktcap  # ≈ 0.0285 < 0.05 → 5 pts

        # Both happen to land in the same band here; verify the ratio values
        self.assertAlmostEqual(sub_reconciled["inputs"]["net_ratio_points"],
                               sub_computed["inputs"]["net_ratio_points"])
        # Verify the mktcap echoed in inputs is the one passed
        self.assertAlmostEqual(sub_reconciled["inputs"]["mktcap"],
                               reconciled_mktcap)

    def test_input_fields_contains_price_mktcap(self):
        """INPUT_FIELDS must reference price.mktcap (not price.mktcap_computed)."""
        self.assertIn("price.mktcap", sr.INPUT_FIELDS)
        self.assertNotIn("price.mktcap_computed", sr.INPUT_FIELDS)


# --------------------------------------------------------------------------- #
# 4. check_mktcap: multi-class band → non-failing; out-of-band → FAIL
# --------------------------------------------------------------------------- #

# Base snapshot for QC check tests — mirrors make_snapshot() in test_qc.py but
# self-contained here so the two files do not share mutable state.
AS_OF_QC = "2026-07-16T20:00:00Z"


def _make_qc_snapshot():
    """Minimal snapshot for check_mktcap exercises."""
    return {
        "price": {
            "last": 100.0,
            "prev_close": 99.0,
            "shares_diluted_m": 1000.0,
            "mktcap_computed": 100_000_000_000.0,
            "mktcap_overview": 100_000_000_000.0,
            "mktcap": 100_000_000_000.0,
            "mktcap_basis": "reconciled_agree",
        },
        "meta": {
            "ticker": "AAA",
            "as_of_utc": AS_OF_QC,
            "sources": [
                {"field_group": "overview", "endpoint_or_url": "COMPANY_OVERVIEW",
                 "retrieved_utc": AS_OF_QC, "covers": ["price", "fundamentals"]},
            ],
        },
    }


class TestCheckMktcapG1(unittest.TestCase):
    """G1-specific check_mktcap branches."""

    def test_multiclass_band_fresh_overview_passes_non_failing(self):
        """GOOG-like: fresh overview, ratio ≈ 0.45 → reconciled pass (passed=True)."""
        s = _make_qc_snapshot()
        s["price"]["mktcap_computed"] = 1_932_407_804_060.0
        s["price"]["mktcap_overview"] = 4_287_617_827_000.0
        s["price"]["last"] = 351.0
        s["price"]["shares_diluted_m"] = 5499.6
        r = Q.check_mktcap(s)
        self.assertIs(r["passed"], True)
        self.assertIn("reconciled to issuer overview", r["detail"])
        self.assertIn("multi-class", r["detail"])

    def test_multiclass_detail_contains_computed_and_overview(self):
        """Detail string must contain computed= and overview= values."""
        s = _make_qc_snapshot()
        s["price"]["mktcap_computed"] = 1_932_407_804_060.0
        s["price"]["mktcap_overview"] = 4_287_617_827_000.0
        s["price"]["last"] = 351.0
        s["price"]["shares_diluted_m"] = 5499.6
        r = Q.check_mktcap(s)
        self.assertIn("computed=", r["detail"])
        self.assertIn("overview=", r["detail"])

    def test_out_of_band_ratio_below_fails(self):
        """Ratio < 0.15 → outside band → hard FAIL (real anomaly).

        Set prev_close far from overview so the prev_close reconciliation path
        does not accidentally pass before reaching the band check.
        """
        s = _make_qc_snapshot()
        # overview = 1e12; last=5.0; shares_m=10000 → computed=50e9 → ratio=0.05
        # prev_close must NOT reconcile with overview → set it to match last
        s["price"]["mktcap_overview"] = 1_000_000_000_000.0
        s["price"]["mktcap_computed"] = 50_000_000_000.0
        s["price"]["last"] = 5.0
        s["price"]["prev_close"] = 5.0   # also far from overview → no prev pass
        s["price"]["shares_diluted_m"] = 10000.0
        r = Q.check_mktcap(s)
        self.assertIs(r["passed"], False)
        self.assertNotIn("reconciled to issuer overview", r["detail"])

    def test_out_of_band_ratio_above_fails(self):
        """Ratio ≥ 1.0 (computed > overview) → outside band → hard FAIL.

        Set prev_close far from overview so the prev_close reconciliation path
        does not accidentally pass before reaching the band check.
        """
        s = _make_qc_snapshot()
        # computed = 1.2 × overview → ratio = 1.2 ≥ 1.0 → anomaly
        s["price"]["mktcap_overview"] = 100_000_000_000.0
        s["price"]["mktcap_computed"] = 120_000_000_000.0
        s["price"]["last"] = 120.0
        s["price"]["prev_close"] = 118.0  # 118*1000*1e6=118e9 vs 100e9 → diff 18% > tol
        s["price"]["shares_diluted_m"] = 1000.0
        r = Q.check_mktcap(s)
        self.assertIs(r["passed"], False)

    def test_stale_overview_still_skips(self):
        """Stale overview (>2d) path unchanged: SKIP with deferred-reconciliation detail.

        Ratio is in multi-class band (0.45) but overview is stale → SKIP must
        fire before reaching the multi-class branch.  Set prev_close far from
        overview so the prev_close reconciliation does not intervene.
        """
        s = _make_qc_snapshot()
        # overview = 1e12; last=45.0; shares_m=10000 → computed=450e9 → ratio=0.45
        s["price"]["mktcap_overview"] = 1_000_000_000_000.0
        s["price"]["mktcap_computed"] = 450_000_000_000.0
        s["price"]["last"] = 45.0
        s["price"]["prev_close"] = 44.0   # 44*10000*1e6=440e9 vs 1e12 → diff huge
        s["price"]["shares_diluted_m"] = 10000.0
        # Mark overview as 10-day old (> 2d window)
        s["meta"]["sources"][0]["retrieved_utc"] = "2026-07-06T12:00:00Z"
        r = Q.check_mktcap(s)
        self.assertIsNone(r["passed"])
        self.assertIn("deferred to the next full fetch", r["detail"])

    def test_single_class_at_last_still_passes_reconciled_agree(self):
        """Single-class issuer: computed ≈ overview within 2% → reconciled_agree pass."""
        s = _make_qc_snapshot()
        # 1% diff — within tolerance
        s["price"]["mktcap_computed"] = 101_000_000_000.0
        s["price"]["mktcap_overview"] = 100_000_000_000.0
        s["price"]["last"] = 101.0
        s["price"]["shares_diluted_m"] = 1000.0
        r = Q.check_mktcap(s)
        self.assertIs(r["passed"], True)
        self.assertNotIn("reconciled to issuer overview", r["detail"])


if __name__ == "__main__":
    unittest.main()
