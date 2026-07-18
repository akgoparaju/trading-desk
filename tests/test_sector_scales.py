"""Tests for scripts/sector_scales.py -- the sector valuation-scale library.

WHY: a single fwd-P/E band cannot value a memory-semis cyclical, an E&P driller
and a REIT on the same axis. A sector scale is a VERSIONED JSON contract that
declares how to compute a fair-value band from first-principles fundamentals
(justified P/B from Gordon residual income, justified forward P/E, or a
pass-through NAV multiple) plus the falsifiers that would break its thesis. This
module validates + evaluates that contract deterministically; the band math IS
the rubric of record, so every formula is pinned to a hand-computed reference
case, and validation names every issue so a malformed scale can never silently
drive a number.

Tests exercise: validate_scale negatives (each required field missing, bad
formula, r<=g, malformed evidence/falsifiers), the pinned band math for all three
formulas (the roe .35 / r .12 / g .04 -> mid 3.875, low 2.7125, high 5.0375
reference), formula dispatch, falsifier evaluation (tripped / not-tripped /
unresolvable-metric + consecutive_quarters passthrough), load_scale (parse +
raise on invalid), and find_scale_for path resolution.

stdlib-only; unittest.
"""

import json
import os
import tempfile
import unittest

from scripts import sector_scales as ss


# --------------------------------------------------------------------------- #
# Helpers: a valid scale per formula; override per test.
# --------------------------------------------------------------------------- #

def _pb_scale(**over):
    base = {
        "scale": "memory_semis",
        "name": "Memory Semis",
        "version": "2026.1",
        "effective": "2026-07-01",
        "basis": "Gordon residual-income justified P/B for a mid-cycle DRAM name.",
        "formula": "justified_pb",
        "parameters": {"roe_normalized": 0.35, "r": 0.12, "g": 0.04},
        "evidence": ["C1", "C3"],
        "falsifiers": [
            {"metric": "fundamentals.roe", "op": "<", "value": 0.10,
             "consecutive_quarters": 2,
             "meaning": "structural ROE collapse below cost of equity"},
        ],
        "prior": None,
    }
    base.update(over)
    return base


def _pe_scale(**over):
    base = {
        "scale": "software_saas",
        "version": "2026.1",
        "effective": "2026-07-01",
        "basis": "Justified forward P/E from durable SaaS economics.",
        "formula": "justified_pe",
        "parameters": {"roe_normalized": 0.30, "r": 0.10, "g": 0.05},
        "evidence": ["C2"],
        "falsifiers": [],
        "prior": None,
    }
    base.update(over)
    return base


def _nav_scale(**over):
    base = {
        "scale": "ep_upstream",
        "version": "2026.1",
        "effective": "2026-07-01",
        "basis": "Appraised NAV multiples off strip-deck PV-10.",
        "formula": "nav_based",
        "parameters": {"nav_multiple_low": 0.8, "nav_multiple_mid": 1.0,
                       "nav_multiple_high": 1.25},
        "evidence": ["C4"],
        "falsifiers": [],
        "prior": None,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Validation: the happy path + each required field missing.
# --------------------------------------------------------------------------- #

class TestValidateHappyPath(unittest.TestCase):
    def test_valid_pb_scale(self):
        self.assertEqual(ss.validate_scale(_pb_scale()), [])

    def test_valid_pe_scale(self):
        self.assertEqual(ss.validate_scale(_pe_scale()), [])

    def test_valid_nav_scale(self):
        self.assertEqual(ss.validate_scale(_nav_scale()), [])

    def test_name_is_optional(self):
        scale = _pe_scale()
        self.assertNotIn("name", scale)
        self.assertEqual(ss.validate_scale(scale), [])


class TestValidateMissingFields(unittest.TestCase):
    def _missing(self, field):
        scale = _pb_scale()
        del scale[field]
        return ss.validate_scale(scale)

    def test_not_a_dict(self):
        issues = ss.validate_scale(["not", "a", "dict"])
        self.assertTrue(any("not a JSON object" in i for i in issues))

    def test_missing_scale(self):
        issues = self._missing("scale")
        self.assertTrue(any("scale" in i for i in issues))

    def test_missing_version(self):
        issues = self._missing("version")
        self.assertTrue(any("version" in i for i in issues))

    def test_missing_effective(self):
        issues = self._missing("effective")
        self.assertTrue(any("effective" in i for i in issues))

    def test_missing_basis(self):
        issues = self._missing("basis")
        self.assertTrue(any("basis" in i for i in issues))

    def test_missing_formula(self):
        issues = self._missing("formula")
        self.assertTrue(any("formula" in i for i in issues))

    def test_missing_parameters(self):
        issues = self._missing("parameters")
        self.assertTrue(any("parameters" in i for i in issues))

    def test_missing_evidence(self):
        issues = self._missing("evidence")
        self.assertTrue(any("evidence" in i for i in issues))

    def test_missing_falsifiers(self):
        issues = self._missing("falsifiers")
        self.assertTrue(any("falsifiers" in i for i in issues))

    def test_missing_prior(self):
        issues = self._missing("prior")
        self.assertTrue(any("prior" in i for i in issues))


class TestValidateFormula(unittest.TestCase):
    def test_unknown_formula(self):
        issues = ss.validate_scale(_pb_scale(formula="justified_ebitda"))
        self.assertTrue(any("formula" in i and "not in" in i for i in issues))

    def test_formula_in_set(self):
        self.assertEqual(ss.FORMULAS, {"justified_pb", "justified_pe", "nav_based"})


class TestValidateParameters(unittest.TestCase):
    def test_pb_missing_required_param(self):
        issues = ss.validate_scale(
            _pb_scale(parameters={"roe_normalized": 0.35, "r": 0.12}))  # no g
        self.assertTrue(any("g" in i for i in issues))

    def test_pb_nonnumeric_param(self):
        issues = ss.validate_scale(
            _pb_scale(parameters={"roe_normalized": "high", "r": 0.12, "g": 0.04}))
        self.assertTrue(any("roe_normalized" in i and "numeric" in i
                            for i in issues))

    def test_r_must_exceed_g(self):
        # r == g -> denominator zero; r < g -> nonsense growth. Both rejected.
        issues = ss.validate_scale(
            _pb_scale(parameters={"roe_normalized": 0.35, "r": 0.04, "g": 0.04}))
        self.assertTrue(any("r > g" in i for i in issues))

    def test_r_less_than_g_rejected(self):
        issues = ss.validate_scale(
            _pb_scale(parameters={"roe_normalized": 0.35, "r": 0.03, "g": 0.06}))
        self.assertTrue(any("r > g" in i for i in issues))

    def test_pe_requires_r_gt_g(self):
        issues = ss.validate_scale(
            _pe_scale(parameters={"roe_normalized": 0.30, "r": 0.05, "g": 0.05}))
        self.assertTrue(any("r > g" in i for i in issues))

    def test_nav_missing_multiple(self):
        issues = ss.validate_scale(
            _nav_scale(parameters={"nav_multiple_low": 0.8,
                                   "nav_multiple_mid": 1.0}))  # no high
        self.assertTrue(any("nav_multiple_high" in i for i in issues))

    def test_nav_does_not_require_r_gt_g(self):
        # nav_based has no r/g constraint.
        self.assertEqual(ss.validate_scale(_nav_scale()), [])


class TestValidateEvidence(unittest.TestCase):
    def test_evidence_must_be_list(self):
        issues = ss.validate_scale(_pb_scale(evidence="C1"))
        self.assertTrue(any("evidence" in i for i in issues))

    def test_evidence_bad_cid(self):
        issues = ss.validate_scale(_pb_scale(evidence=["C1", "finding-3"]))
        self.assertTrue(any("finding-3" in i for i in issues))

    def test_evidence_empty_list_ok(self):
        # empty list is structurally valid (it is a list of zero C-IDs).
        self.assertEqual(ss.validate_scale(_pb_scale(evidence=[])), [])


class TestValidateFalsifiers(unittest.TestCase):
    def test_falsifiers_must_be_list(self):
        issues = ss.validate_scale(_pb_scale(falsifiers={"metric": "x"}))
        self.assertTrue(any("falsifiers" in i for i in issues))

    def test_falsifier_bad_op(self):
        issues = ss.validate_scale(_pb_scale(falsifiers=[
            {"metric": "fundamentals.roe", "op": "==", "value": 0.1,
             "meaning": "x"}]))
        self.assertTrue(any("op" in i for i in issues))

    def test_falsifier_missing_metric(self):
        issues = ss.validate_scale(_pb_scale(falsifiers=[
            {"op": "<", "value": 0.1, "meaning": "x"}]))
        self.assertTrue(any("metric" in i for i in issues))

    def test_falsifier_nonnumeric_value(self):
        issues = ss.validate_scale(_pb_scale(falsifiers=[
            {"metric": "fundamentals.roe", "op": "<", "value": "low",
             "meaning": "x"}]))
        self.assertTrue(any("value" in i and "numeric" in i for i in issues))

    def test_falsifier_missing_meaning(self):
        issues = ss.validate_scale(_pb_scale(falsifiers=[
            {"metric": "fundamentals.roe", "op": "<", "value": 0.1}]))
        self.assertTrue(any("meaning" in i for i in issues))

    def test_falsifier_bad_consecutive_quarters(self):
        issues = ss.validate_scale(_pb_scale(falsifiers=[
            {"metric": "fundamentals.roe", "op": "<", "value": 0.1,
             "consecutive_quarters": 0, "meaning": "x"}]))
        self.assertTrue(any("consecutive_quarters" in i for i in issues))

    def test_falsifier_consecutive_quarters_optional(self):
        # a falsifier without consecutive_quarters is valid (default single-q).
        self.assertEqual(ss.validate_scale(_pb_scale(falsifiers=[
            {"metric": "fundamentals.roe", "op": "<", "value": 0.1,
             "meaning": "x"}])), [])


class TestValidatePrior(unittest.TestCase):
    def test_prior_null_ok(self):
        self.assertEqual(ss.validate_scale(_pb_scale(prior=None)), [])

    def test_prior_dict_ok(self):
        self.assertEqual(ss.validate_scale(
            _pb_scale(prior={"mean": 3.0, "weight": 0.5})), [])

    def test_prior_bad_type(self):
        issues = ss.validate_scale(_pb_scale(prior="strong"))
        self.assertTrue(any("prior" in i for i in issues))


# --------------------------------------------------------------------------- #
# Band math: pinned reference cases.
# --------------------------------------------------------------------------- #

class TestJustifiedPb(unittest.TestCase):
    def test_reference_case(self):
        # roe .35, r .12, g .04 -> mid = (.35-.04)/(.12-.04) = 0.31/0.08 = 3.875
        band = ss.justified_pb({"roe_normalized": 0.35, "r": 0.12, "g": 0.04})
        self.assertAlmostEqual(band["mid"], 3.875, places=10)

    def test_reference_case_band_spread_default_030(self):
        # spread .30 -> low = 3.875 * 0.70 = 2.7125 ; high = 3.875 * 1.30 = 5.0375
        band = ss.justified_pb({"roe_normalized": 0.35, "r": 0.12, "g": 0.04})
        self.assertAlmostEqual(band["low"], 2.7125, places=10)
        self.assertAlmostEqual(band["high"], 5.0375, places=10)

    def test_custom_band_spread(self):
        band = ss.justified_pb({"roe_normalized": 0.35, "r": 0.12, "g": 0.04,
                                "band_spread": 0.20})
        self.assertAlmostEqual(band["mid"], 3.875, places=10)
        self.assertAlmostEqual(band["low"], 3.875 * 0.80, places=10)
        self.assertAlmostEqual(band["high"], 3.875 * 1.20, places=10)


class TestJustifiedPe(unittest.TestCase):
    def test_reference_case(self):
        # roe .30, r .10, g .05 -> payout = 1 - .05/.30 = 1 - 0.16667 = 0.83333
        # mid = 0.83333 / (.10 - .05) = 0.83333 / 0.05 = 16.6667
        band = ss.justified_pe({"roe_normalized": 0.30, "r": 0.10, "g": 0.05})
        self.assertAlmostEqual(band["mid"], (1 - 0.05 / 0.30) / 0.05, places=10)
        self.assertAlmostEqual(band["mid"], 16.66666666667, places=6)

    def test_band_spread_envelope(self):
        band = ss.justified_pe({"roe_normalized": 0.30, "r": 0.10, "g": 0.05})
        self.assertAlmostEqual(band["low"], band["mid"] * 0.70, places=10)
        self.assertAlmostEqual(band["high"], band["mid"] * 1.30, places=10)


class TestNavBased(unittest.TestCase):
    def test_passthrough(self):
        band = ss.nav_based({"nav_multiple_low": 0.8, "nav_multiple_mid": 1.0,
                             "nav_multiple_high": 1.25})
        self.assertEqual(band, {"low": 0.8, "mid": 1.0, "high": 1.25})


class TestComputeBandDispatch(unittest.TestCase):
    def test_dispatch_pb(self):
        band = ss.compute_band(_pb_scale())
        self.assertAlmostEqual(band["mid"], 3.875, places=10)

    def test_dispatch_pe(self):
        band = ss.compute_band(_pe_scale())
        self.assertAlmostEqual(band["mid"], (1 - 0.05 / 0.30) / 0.05, places=10)

    def test_dispatch_nav(self):
        band = ss.compute_band(_nav_scale())
        self.assertEqual(band, {"low": 0.8, "mid": 1.0, "high": 1.25})


# --------------------------------------------------------------------------- #
# Falsifier evaluation.
# --------------------------------------------------------------------------- #

class TestEvaluateFalsifiers(unittest.TestCase):
    def _scale(self, falsifiers):
        return _pb_scale(falsifiers=falsifiers)

    def test_tripped_true(self):
        # roe 0.08 < 0.10 -> tripped.
        scale = self._scale([
            {"metric": "fundamentals.roe", "op": "<", "value": 0.10,
             "meaning": "ROE collapse"}])
        snap = {"fundamentals": {"roe": 0.08}}
        res = ss.evaluate_falsifiers(scale, snap)
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["tripped"])
        self.assertEqual(res[0]["observed"], 0.08)
        self.assertEqual(res[0]["meaning"], "ROE collapse")

    def test_not_tripped(self):
        # roe 0.30 is NOT < 0.10 -> not tripped.
        scale = self._scale([
            {"metric": "fundamentals.roe", "op": "<", "value": 0.10,
             "meaning": "ROE collapse"}])
        snap = {"fundamentals": {"roe": 0.30}}
        res = ss.evaluate_falsifiers(scale, snap)
        self.assertFalse(res[0]["tripped"])
        self.assertEqual(res[0]["observed"], 0.30)

    def test_unresolvable_metric(self):
        scale = self._scale([
            {"metric": "fundamentals.missing_field", "op": ">", "value": 1.0,
             "meaning": "n/a"}])
        snap = {"fundamentals": {"roe": 0.30}}
        res = ss.evaluate_falsifiers(scale, snap)
        self.assertIsNone(res[0]["tripped"])
        self.assertIsNone(res[0]["observed"])
        self.assertEqual(res[0]["reason"], "metric not in snapshot")

    def test_unresolvable_nested_non_dict(self):
        # intermediate node is not a dict -> unresolvable, not a crash.
        scale = self._scale([
            {"metric": "fundamentals.roe.deep", "op": ">", "value": 1.0,
             "meaning": "n/a"}])
        snap = {"fundamentals": {"roe": 0.30}}
        res = ss.evaluate_falsifiers(scale, snap)
        self.assertIsNone(res[0]["tripped"])

    def test_op_gte(self):
        scale = self._scale([
            {"metric": "valuation.pe_fwd", "op": ">=", "value": 40.0,
             "meaning": "extreme multiple"}])
        snap = {"valuation": {"pe_fwd": 40.0}}
        res = ss.evaluate_falsifiers(scale, snap)
        self.assertTrue(res[0]["tripped"])  # 40 >= 40

    def test_op_lte(self):
        scale = self._scale([
            {"metric": "valuation.pe_fwd", "op": "<=", "value": 5.0,
             "meaning": "distressed"}])
        snap = {"valuation": {"pe_fwd": 5.0}}
        res = ss.evaluate_falsifiers(scale, snap)
        self.assertTrue(res[0]["tripped"])  # 5 <= 5

    def test_consecutive_quarters_passthrough(self):
        # consecutive_quarters is METADATA for the caller; a single-snapshot
        # check cannot count quarters, so it is passed through untouched.
        scale = self._scale([
            {"metric": "fundamentals.roe", "op": "<", "value": 0.10,
             "consecutive_quarters": 3, "meaning": "sustained collapse"}])
        snap = {"fundamentals": {"roe": 0.08}}
        res = ss.evaluate_falsifiers(scale, snap)
        self.assertEqual(res[0]["consecutive_quarters"], 3)
        self.assertTrue(res[0]["tripped"])  # this snapshot trips; caller counts

    def test_consecutive_quarters_absent_not_in_result(self):
        scale = self._scale([
            {"metric": "fundamentals.roe", "op": "<", "value": 0.10,
             "meaning": "collapse"}])
        snap = {"fundamentals": {"roe": 0.08}}
        res = ss.evaluate_falsifiers(scale, snap)
        self.assertNotIn("consecutive_quarters", res[0])

    def test_empty_falsifiers(self):
        self.assertEqual(ss.evaluate_falsifiers(_pe_scale(), {}), [])

    def test_bool_not_treated_as_number(self):
        # a boolean observed value is NOT a comparable number -> unresolvable.
        scale = self._scale([
            {"metric": "fundamentals.flag", "op": ">", "value": 0,
             "meaning": "n/a"}])
        snap = {"fundamentals": {"flag": True}}
        res = ss.evaluate_falsifiers(scale, snap)
        self.assertIsNone(res[0]["tripped"])


# --------------------------------------------------------------------------- #
# load_scale (parse + validate) and find_scale_for.
# --------------------------------------------------------------------------- #

class TestLoadScale(unittest.TestCase):
    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, obj, name="scale.json"):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh)
        return path

    def test_load_valid(self):
        path = self._write(_pb_scale())
        scale = ss.load_scale(path)
        self.assertEqual(scale["formula"], "justified_pb")

    def test_load_invalid_raises_named(self):
        bad = _pb_scale()
        del bad["formula"]
        path = self._write(bad)
        with self.assertRaises(ValueError) as cm:
            ss.load_scale(path)
        self.assertIn("formula", str(cm.exception))

    def test_load_bad_json_raises(self):
        path = self._write("{not valid json", name="broken.json")
        with self.assertRaises(ValueError) as cm:
            ss.load_scale(path)
        self.assertIn("not valid JSON", str(cm.exception))

    def test_load_missing_file_raises(self):
        with self.assertRaises(ValueError):
            ss.load_scale(os.path.join(self.dir, "nope.json"))


class TestFindScaleFor(unittest.TestCase):
    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_found(self):
        scales_dir = os.path.join(self.dir, "trading_desk_config", "scales")
        os.makedirs(scales_dir)
        path = os.path.join(scales_dir, "memory_semis.json")
        with open(path, "w") as fh:
            json.dump(_pb_scale(), fh)
        found = ss.find_scale_for(self.dir, "memory_semis")
        self.assertEqual(found, path)

    def test_not_found(self):
        self.assertIsNone(ss.find_scale_for(self.dir, "memory_semis"))

    def test_none_name(self):
        self.assertIsNone(ss.find_scale_for(self.dir, None))


if __name__ == "__main__":
    unittest.main()
