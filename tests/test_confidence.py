"""Tests for scripts/confidence.py -- the confidence/provenance layer (v1.0.0).

WHY: this module is the versioned rubric of record for confidence. Its ONLY
arithmetic is an ordinal min over LOW/MEDIUM/HIGH, so every branch is exactly
pinnable. If the code and these numbers ever diverge, the confidence rubric has
silently changed -- and that must surface as a failure, not a shifted badge.

The layer has three axes -- SOURCE (where the data came from), DEPTH (rubric
maturity, a GOVERNED BELIEF), STALENESS (print freshness / reuse) -- combined as the
weakest link. This file pins each SOURCE rule, each DEPTH table row, each STALENESS
rule, the min combiner, the roll-up, and the hard invariant that every ``why`` tag
is DIGIT-FREE (a report_qc number_provenance constraint).

stdlib-only; unittest.
"""

import json
import os
import re
import tempfile
import unittest

from scripts import confidence as C


# --------------------------------------------------------------------------- #
# Snapshot / module-doc fixtures.
# --------------------------------------------------------------------------- #

def _snap(data_mode="alpha_vantage", latest="2026-07-20",
          as_of="2026-07-20T12:00:00Z", series_source=None,
          web_transcribed=None):
    """A minimal snapshot carrying only the provenance fields the layer reads."""
    tech = {}
    if series_source is not None:
        tech["series_source"] = series_source
    return {
        "meta": {
            "data_mode": data_mode,
            "latest_trading_day": latest,
            "as_of_utc": as_of,
        },
        "technicals": tech,
        "fundamentals": {"web_transcribed_fields": web_transcribed or []},
    }


def _tech_doc(rubric="1.0.0"):
    return {"skill": "technical-analysis", "rubric_version": rubric}


def _risk_doc(rubric="1.0.0"):
    return {"skill": "risk-analytics", "rubric_version": rubric}


def _sent_doc(rubric="1.0.0"):
    return {"skill": "sentiment-positioning", "rubric_version": rubric}


def _fund_doc(mode="compressed_snapshot_pass", rubric="1.2.0"):
    return {"skill": "fundamental", "rubric_version": rubric,
            "fundamental_mode": mode}


# --------------------------------------------------------------------------- #
# Ordinal min combiner (the ONLY arithmetic).
# --------------------------------------------------------------------------- #

class TestMinCombiner(unittest.TestCase):
    def test_min_level_low_dominates(self):
        self.assertEqual(C._min_level("HIGH", "MEDIUM", "LOW"), "LOW")

    def test_min_level_medium_when_no_low(self):
        self.assertEqual(C._min_level("HIGH", "MEDIUM", "HIGH"), "MEDIUM")

    def test_min_level_all_high(self):
        self.assertEqual(C._min_level("HIGH", "HIGH", "HIGH"), "HIGH")

    def test_min_level_ignores_none(self):
        self.assertEqual(C._min_level("HIGH", None, "MEDIUM"), "MEDIUM")

    def test_min_level_all_none(self):
        self.assertIsNone(C._min_level(None, None))

    def test_levels_ordering(self):
        self.assertLess(C._LEVELS["LOW"], C._LEVELS["MEDIUM"])
        self.assertLess(C._LEVELS["MEDIUM"], C._LEVELS["HIGH"])


# --------------------------------------------------------------------------- #
# SOURCE axis -- every rule.
# --------------------------------------------------------------------------- #

class TestSourceAxis(unittest.TestCase):
    def test_technical_premium_high(self):
        src = C.compute_module(_tech_doc(), _snap())["source"]
        self.assertEqual(src["level"], "HIGH")

    def test_risk_premium_high(self):
        src = C.compute_module(_risk_doc(), _snap())["source"]
        self.assertEqual(src["level"], "HIGH")

    def test_fundamental_premium_high(self):
        # premium run, no web-transcribed fields -> HIGH.
        src = C.compute_module(_fund_doc(), _snap())["source"]
        self.assertEqual(src["level"], "HIGH")

    def test_degraded_medium_technical(self):
        src = C.compute_module(_tech_doc(),
                               _snap(data_mode="av_free_degraded"))["source"]
        self.assertEqual(src["level"], "MEDIUM")

    def test_degraded_medium_risk(self):
        src = C.compute_module(_risk_doc(),
                               _snap(data_mode="av_free_degraded"))["source"]
        self.assertEqual(src["level"], "MEDIUM")

    def test_degraded_medium_fundamental(self):
        src = C.compute_module(_fund_doc(),
                               _snap(data_mode="av_free_degraded"))["source"]
        self.assertEqual(src["level"], "MEDIUM")

    def test_web_fallback_low(self):
        for doc in (_tech_doc(), _risk_doc(), _fund_doc()):
            src = C.compute_module(doc, _snap(data_mode="web_fallback"))["source"]
            self.assertEqual(src["level"], "LOW", doc["skill"])

    def test_sentiment_always_medium_via_short_interest(self):
        # sentiment SCORES short_interest (a by-design web input) -> MEDIUM even on
        # a premium run.
        src = C.compute_module(_sent_doc(), _snap())["source"]
        self.assertEqual(src["level"], "MEDIUM")

    def test_sentiment_web_fallback_low(self):
        # web_fallback still dominates to LOW.
        src = C.compute_module(_sent_doc(),
                               _snap(data_mode="web_fallback"))["source"]
        self.assertEqual(src["level"], "LOW")

    def test_fundamental_web_transcribed_medium(self):
        # premium run but web-transcribed fields present -> MEDIUM.
        src = C.compute_module(_fund_doc(),
                               _snap(web_transcribed=["rev_ttm"]))["source"]
        self.assertEqual(src["level"], "MEDIUM")

    def test_technical_stooq_medium(self):
        src = C.compute_module(_tech_doc(),
                               _snap(series_source="stooq"))["source"]
        self.assertEqual(src["level"], "MEDIUM")

    def test_by_design_web_map_declares_sentiment(self):
        self.assertEqual(C._SOURCE_BY_DESIGN_WEB, {"sentiment": ["short_interest"]})


# --------------------------------------------------------------------------- #
# DEPTH axis -- every table row (the governed belief).
# --------------------------------------------------------------------------- #

class TestDepthAxis(unittest.TestCase):
    def test_fundamental_anchored_high(self):
        d = C.compute_module(_fund_doc(mode="coverage_anchored_pass"),
                             _snap())["depth"]
        self.assertEqual(d["level"], "HIGH")

    def test_fundamental_compressed_medium(self):
        d = C.compute_module(_fund_doc(mode="compressed_snapshot_pass"),
                             _snap())["depth"]
        self.assertEqual(d["level"], "MEDIUM")

    def test_technical_rubric_100_medium(self):
        d = C.compute_module(_tech_doc("1.0.0"), _snap())["depth"]
        self.assertEqual(d["level"], "MEDIUM")

    def test_sentiment_rubric_100_medium(self):
        d = C.compute_module(_sent_doc("1.0.0"), _snap())["depth"]
        self.assertEqual(d["level"], "MEDIUM")

    def test_sentiment_rubric_110_still_medium_provisional(self):
        # sentiment-v1.1.0 is positioning-aware BUT PROVISIONAL (unratified pending
        # B9). Its explicit row keeps depth MEDIUM -- it does NOT auto-promote to
        # HIGH via the "rubric past 1.0.0" fallthrough.
        d = C.compute_module(_sent_doc("1.1.0"), _snap())["depth"]
        self.assertEqual(d["level"], "MEDIUM")

    def test_sentiment_rubric_110_overall_medium_source_cap(self):
        # OVERALL badge for sentiment 1.1.0 on a clean premium fresh-print snapshot:
        # source MEDIUM (short_interest web-by-design), depth MEDIUM (provisional),
        # staleness HIGH -> min = MEDIUM. The source cap alone holds it at MEDIUM;
        # sentiment must NEVER read HIGH regardless of depth.
        block = C.compute_module(_sent_doc("1.1.0"), _snap())
        self.assertEqual(block["source"]["level"], "MEDIUM")
        self.assertEqual(block["depth"]["level"], "MEDIUM")
        self.assertEqual(block["level"], "MEDIUM")

    def test_risk_rubric_100_medium(self):
        d = C.compute_module(_risk_doc("1.0.0"), _snap())["depth"]
        self.assertEqual(d["level"], "MEDIUM")

    def test_risk_rubric_110_still_medium_provisional(self):
        # risk-v1.1.0 is event-aware BUT PROVISIONAL (unratified pending B9
        # calibration). It must STAY depth MEDIUM -- it does NOT auto-promote to
        # HIGH via the generic "rubric past 1.0.0" fallthrough; the explicit 1.1.0
        # row overrides it. Promotion to HIGH is gated on B9 ratification only.
        d = C.compute_module(_risk_doc("1.1.0"), _snap())["depth"]
        self.assertEqual(d["level"], "MEDIUM")

    def test_risk_rubric_110_module_level_medium(self):
        # The OVERALL confidence level for a risk 1.1.0 module on a clean premium
        # fresh-print snapshot: source HIGH, depth MEDIUM (provisional), staleness
        # HIGH -> min = MEDIUM. The provisional depth pins it; it must not read HIGH.
        block = C.compute_module(_risk_doc("1.1.0"), _snap())
        self.assertEqual(block["depth"]["level"], "MEDIUM")
        self.assertEqual(block["level"], "MEDIUM")

    def test_depth_never_low(self):
        # a scored module is at least MEDIUM depth.
        for doc in (_tech_doc(), _risk_doc(), _sent_doc(), _fund_doc()):
            d = C.compute_module(doc, _snap())["depth"]
            self.assertIn(d["level"], ("MEDIUM", "HIGH"), doc["skill"])

    def test_technical_promotes_to_high_at_110(self):
        # v1.1.0 (Wave 4A / R5): technical depth promotes to HIGH via its explicit
        # DEPTH_TABLE row (regime-conditional pass landed). Unlike sentiment/risk
        # 1.1.0, this is a real promotion -- technical's source is AV-premium, not
        # web-capped.
        d = C.compute_module(_tech_doc("1.1.0"), _snap())["depth"]
        self.assertEqual(d["level"], "HIGH")
        self.assertEqual(d["why"], "regime-conditional depth")

    def test_technical_120_depth_stays_high_discloses_sector_rs(self):
        # Track O4: technical bumps to v1.2.0 (adds the PROVISIONAL sector-RS
        # factor). The DEPTH TIER does NOT change (still HIGH -- promotion is a
        # separate gated task); the why-tag DISCLOSES the new provisional factor.
        d = C.compute_module(_tech_doc("1.2.0"), _snap())["depth"]
        self.assertEqual(d["level"], "HIGH")
        self.assertIn("sector", d["why"].lower())

    def test_technical_120_overall_high_on_premium_fresh(self):
        # Same end-to-end HIGH as 1.1.0: source HIGH + depth HIGH + staleness HIGH.
        block = C.compute_module(_tech_doc("1.2.0"), _snap())
        self.assertEqual(block["level"], "HIGH")

    def test_technical_110_overall_high_on_premium_fresh(self):
        # The OVERALL badge for technical 1.1.0 on a clean premium fresh-print
        # snapshot: source HIGH (AV premium, not web-capped) + depth HIGH
        # (regime-conditional) + staleness HIGH (fresh print) -> min = HIGH. This
        # is the E2E-gate promotion: technical reads HIGH end-to-end, and there is
        # NO fall-through bug (the explicit 1.1.0 row, not the generic fallthrough,
        # drives depth).
        block = C.compute_module(_tech_doc("1.1.0"), _snap())
        self.assertEqual(block["source"]["level"], "HIGH")
        self.assertEqual(block["depth"]["level"], "HIGH")
        self.assertEqual(block["staleness"]["level"], "HIGH")
        self.assertEqual(block["level"], "HIGH")

    def test_depth_table_rows_present(self):
        # the governed-belief table carries exactly the four modules.
        self.assertEqual(set(C.DEPTH_TABLE),
                         {"fundamental", "technical", "sentiment", "risk"})
        self.assertEqual(C.DEPTH_TABLE["fundamental"]["coverage_anchored_pass"][0],
                         "HIGH")
        self.assertEqual(
            C.DEPTH_TABLE["fundamental"]["compressed_snapshot_pass"][0], "MEDIUM")
        self.assertEqual(C.DEPTH_TABLE["technical"]["1.0.0"][0], "MEDIUM")
        # technical-v1.1.0 is a REAL depth promotion (regime-conditional) -> HIGH.
        self.assertEqual(C.DEPTH_TABLE["technical"]["1.1.0"][0], "HIGH")
        self.assertEqual(C.DEPTH_TABLE["technical"]["1.1.0"][1],
                         "regime-conditional depth")
        # technical-v1.2.0 (Track O4): tier UNCHANGED (still HIGH); the why-tag
        # discloses the new PROVISIONAL sector-RS factor.
        self.assertEqual(C.DEPTH_TABLE["technical"]["1.2.0"][0], "HIGH")
        self.assertIn("sector", C.DEPTH_TABLE["technical"]["1.2.0"][1].lower())
        self.assertEqual(C.DEPTH_TABLE["sentiment"]["1.0.0"][0], "MEDIUM")
        # sentiment-v1.1.0 is PROVISIONAL -> stays MEDIUM (explicit row).
        self.assertEqual(C.DEPTH_TABLE["sentiment"]["1.1.0"][0], "MEDIUM")
        self.assertEqual(C.DEPTH_TABLE["risk"]["1.0.0"][0], "MEDIUM")
        # risk-v1.1.0 is PROVISIONAL -> stays MEDIUM (explicit row, no auto-promote).
        self.assertEqual(C.DEPTH_TABLE["risk"]["1.1.0"][0], "MEDIUM")


# --------------------------------------------------------------------------- #
# STALENESS axis -- every rule.
# --------------------------------------------------------------------------- #

class TestStalenessAxis(unittest.TestCase):
    def test_fresh_print_high(self):
        s = C.compute_module(_tech_doc(),
                             _snap(latest="2026-07-20",
                                   as_of="2026-07-20T12:00:00Z"))["staleness"]
        self.assertEqual(s["level"], "HIGH")

    def test_weekend_stale_print_medium(self):
        s = C.compute_module(_tech_doc(),
                             _snap(latest="2026-07-17",
                                   as_of="2026-07-20T12:00:00Z"))["staleness"]
        self.assertEqual(s["level"], "MEDIUM")

    def test_null_latest_low(self):
        s = C.compute_module(_tech_doc(), _snap(latest=None))["staleness"]
        self.assertEqual(s["level"], "LOW")

    def test_reuse_in_window_medium(self):
        with tempfile.TemporaryDirectory() as d:
            plan = {"groups": {"daily_adjusted": {"action": "reuse",
                                                  "age_days": 2}}}
            with open(os.path.join(d, "refresh_plan.json"), "w") as fh:
                json.dump(plan, fh)
            s = C.compute_module(_tech_doc(), _snap(), bundle_dir=d)["staleness"]
            self.assertEqual(s["level"], "MEDIUM")

    def test_reuse_over_window_low(self):
        with tempfile.TemporaryDirectory() as d:
            # daily_adjusted window is 4 days; age 5 is over -> LOW.
            plan = {"groups": {"daily_adjusted": {"action": "reuse",
                                                  "age_days": 5}}}
            with open(os.path.join(d, "refresh_plan.json"), "w") as fh:
                json.dump(plan, fh)
            s = C.compute_module(_tech_doc(), _snap(), bundle_dir=d)["staleness"]
            self.assertEqual(s["level"], "LOW")

    def test_no_plan_uses_print_freshness(self):
        # bundle_dir with no refresh_plan.json -> staleness from print only.
        with tempfile.TemporaryDirectory() as d:
            s = C.compute_module(_tech_doc(), _snap(), bundle_dir=d)["staleness"]
            self.assertEqual(s["level"], "HIGH")

    def test_fundamental_anchored_staleness_high(self):
        s = C.compute_module(_fund_doc(mode="coverage_anchored_pass"),
                             _snap())["staleness"]
        self.assertEqual(s["level"], "HIGH")

    def test_fundamental_compressed_staleness_medium(self):
        s = C.compute_module(_fund_doc(mode="compressed_snapshot_pass"),
                             _snap())["staleness"]
        self.assertEqual(s["level"], "MEDIUM")

    def test_refetch_not_treated_as_reuse(self):
        # a refetched price group is NOT a reuse -> print freshness governs.
        with tempfile.TemporaryDirectory() as d:
            plan = {"groups": {"daily_adjusted": {"action": "refetch",
                                                  "age_days": 5}}}
            with open(os.path.join(d, "refresh_plan.json"), "w") as fh:
                json.dump(plan, fh)
            s = C.compute_module(_tech_doc(), _snap(), bundle_dir=d)["staleness"]
            self.assertEqual(s["level"], "HIGH")


# --------------------------------------------------------------------------- #
# compute_module: the min combiner drives level; block shape.
# --------------------------------------------------------------------------- #

class TestComputeModule(unittest.TestCase):
    def test_low_dominates_overall_level(self):
        # premium technical, but null print -> staleness LOW -> overall LOW.
        block = C.compute_module(_tech_doc(), _snap(latest=None))
        self.assertEqual(block["level"], "LOW")

    def test_medium_depth_pins_level_when_source_and_staleness_high(self):
        block = C.compute_module(_tech_doc(), _snap())
        # HIGH source, MEDIUM depth, HIGH staleness -> MEDIUM.
        self.assertEqual(block["level"], "MEDIUM")

    def test_fundamental_anchored_all_high(self):
        block = C.compute_module(_fund_doc(mode="coverage_anchored_pass"), _snap())
        self.assertEqual(block["level"], "HIGH")

    def test_block_shape(self):
        block = C.compute_module(_tech_doc(), _snap())
        self.assertEqual(set(block),
                         {"level", "source", "depth", "staleness", "rule",
                          "version"})
        self.assertEqual(block["rule"], "min(source, depth, staleness)")
        self.assertEqual(block["version"], "1.0.0")
        for axis in ("source", "depth", "staleness"):
            self.assertEqual(set(block[axis]), {"level", "why"})

    def test_determinism(self):
        a = C.compute_module(_tech_doc(), _snap())
        b = C.compute_module(_tech_doc(), _snap())
        self.assertEqual(a, b)


# --------------------------------------------------------------------------- #
# Roll-up: min over evidence dims; renormalized dim skipped; thesis excluded.
# --------------------------------------------------------------------------- #

def _block(level, dimension, why="tag"):
    """A minimal per-dimension confidence block for roll-up tests."""
    return {
        "level": level,
        "source": {"level": level, "why": why},
        "depth": {"level": level, "why": why},
        "staleness": {"level": level, "why": why},
        "dimension": dimension,
    }


class TestRollup(unittest.TestCase):
    def test_min_over_evidence_dims(self):
        r = C.rollup([_block("HIGH", "technical"),
                      _block("MEDIUM", "risk"),
                      _block("HIGH", "fundamental")])
        self.assertEqual(r["level"], "MEDIUM")

    def test_low_dimension_dominates(self):
        r = C.rollup([_block("HIGH", "technical"),
                      _block("LOW", "sentiment"),
                      _block("MEDIUM", "risk")])
        self.assertEqual(r["level"], "LOW")

    def test_renormalized_dim_skipped(self):
        # a renormalized-away dimension contributes None -> ignored in the min.
        r = C.rollup([_block("HIGH", "technical"), None, _block("HIGH", "risk")])
        self.assertEqual(r["level"], "HIGH")

    def test_all_high(self):
        r = C.rollup([_block("HIGH", "technical"), _block("HIGH", "fundamental")])
        self.assertEqual(r["level"], "HIGH")

    def test_why_names_weakest_dimension(self):
        r = C.rollup([_block("HIGH", "technical"),
                      _block("MEDIUM", "risk", why="pre-event-aware")])
        self.assertIn("risk", r["why"])
        self.assertIn("MEDIUM", r["why"])

    def test_empty_rollup_none_level(self):
        r = C.rollup([None, None])
        self.assertIsNone(r["level"])

    def test_rollup_shape_and_version(self):
        r = C.rollup([_block("HIGH", "technical")])
        self.assertEqual(r["version"], "1.0.0")
        self.assertIn("rule", r)
        self.assertIn("why", r)
        self.assertIn("level", r)

    def test_thesis_conviction_not_passed_in(self):
        # The composite EXCLUDES thesis-conviction from the roll-up (it never
        # appears in the dimension_confidences list). A roll-up over just the four
        # evidence blocks is the whole contract; there is no thesis block to skip.
        # We assert the min is unaffected by the absence of any 5th dimension.
        r = C.rollup([_block("HIGH", "technical"), _block("HIGH", "fundamental"),
                      _block("HIGH", "sentiment"), _block("HIGH", "risk")])
        self.assertEqual(r["level"], "HIGH")


# --------------------------------------------------------------------------- #
# HARD INVARIANT: every ``why`` string is DIGIT-FREE (report_qc constraint).
# --------------------------------------------------------------------------- #

_DIGIT_RE = re.compile(r"\d")


class TestWhyDigitFree(unittest.TestCase):
    def _assert_block_digit_free(self, block, ctx=""):
        for axis in ("source", "depth", "staleness"):
            ax = block.get(axis)
            if isinstance(ax, dict) and ax.get("why") is not None:
                self.assertIsNone(_DIGIT_RE.search(ax["why"]),
                                  f"{ctx} {axis} why has a digit: {ax['why']!r}")
        if block.get("why") is not None:
            self.assertIsNone(_DIGIT_RE.search(block["why"]),
                              f"{ctx} rollup why has a digit: {block['why']!r}")

    def test_every_compute_module_why_digit_free(self):
        # Sweep every branch: 4 modules x {premium, degraded, web_fallback} x
        # {fresh, weekend, null} + stooq + web-transcribed + anchored/compressed.
        docs = [_tech_doc(), _risk_doc(), _sent_doc(),
                _fund_doc("compressed_snapshot_pass"),
                _fund_doc("coverage_anchored_pass"),
                _tech_doc("1.1.0"), _risk_doc("1.1.0"), _sent_doc("1.1.0"),
                _tech_doc("1.2.0")]
        modes = ["alpha_vantage", "av_free_degraded", "web_fallback"]
        latests = ["2026-07-20", "2026-07-17", None]
        for doc in docs:
            for mode in modes:
                for latest in latests:
                    snap = _snap(data_mode=mode, latest=latest)
                    block = C.compute_module(doc, snap)
                    self._assert_block_digit_free(block, ctx=f"{doc['skill']}/{mode}/{latest}")
        # stooq + web-transcribed branches.
        self._assert_block_digit_free(
            C.compute_module(_tech_doc(), _snap(series_source="stooq")), "stooq")
        self._assert_block_digit_free(
            C.compute_module(_fund_doc(), _snap(web_transcribed=["rev_ttm"])),
            "web-transcribed")

    def test_reuse_why_digit_free(self):
        for age in (2, 5):
            with tempfile.TemporaryDirectory() as d:
                plan = {"groups": {"daily_adjusted": {"action": "reuse",
                                                      "age_days": age}}}
                with open(os.path.join(d, "refresh_plan.json"), "w") as fh:
                    json.dump(plan, fh)
                block = C.compute_module(_tech_doc(), _snap(), bundle_dir=d)
                self._assert_block_digit_free(block, ctx=f"reuse-age-{age}")

    def test_every_rollup_why_digit_free(self):
        cases = [
            [_block("HIGH", "technical", why="AV premium")],
            [_block("MEDIUM", "risk", why="pre-event-aware"),
             _block("HIGH", "technical")],
            [_block("LOW", "sentiment", why="web fallback"), None],
            [None, None],
        ]
        for blocks in cases:
            self._assert_block_digit_free(C.rollup(blocks), ctx="rollup")


if __name__ == "__main__":
    unittest.main()
