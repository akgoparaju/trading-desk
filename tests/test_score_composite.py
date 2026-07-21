"""Tests for scripts/score_composite.py -- the composite-score decision skill.

WHY: this is the L3 decision layer. Unlike the four evidence modules (whose
arithmetic scores the snapshot), the composite consumes the four module JSONs plus
an in-script fifth dimension (thesis conviction, built from EV asymmetry + three
judgment flags) and produces the weighted composite, letter grade, action, and EV
block. Its arithmetic IS the composite rubric of record (rubric v1.0.0). Every
weight, band edge, hurdle, and EV formula is pinned to a hand-computed value here;
if the code and these numbers ever diverge the composite has silently changed and
that must surface as a test failure, not a shifted call.

All EV math is delegated to scripts.ev_kelly (ev_at, scenario_ev) -- these tests
pin the values that ev_kelly returns for the fixture scenario set and assert the
composite reproduces them per-profile.

stdlib-only; unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from scripts import score_composite as sc


SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "score_composite.py")

# Pinned fixture scenario set: targets 150/120/80, probs .25/.5/.25, last 100.
#   ev_at = .25*(1.5-1) + .5*(1.2-1) + .25*(0.8-1) = .125 + .10 - .05 = 0.175
#   sum(p*t) = .25*150 + .5*120 + .25*80 = 117.5
_SCENARIOS = [
    {"name": "bull", "prob": 0.25, "price_target": 150.0},
    {"name": "base", "prob": 0.50, "price_target": 120.0},
    {"name": "bear", "prob": 0.25, "price_target": 80.0},
]
_LAST = 100.0

# The four evidence-module scores the fixture writes.
_MOD_SCORES = {"technical": 70, "fundamental": 60, "sentiment": 50, "risk": 40}

# Full set of judgment flags used across the pinned cases:
#   variant some -> 12, catalyst clarity clear -> 20, invalidation both-legs -> 20.
_FLAGS = dict(
    variant="some", variant_justification="differentiated read on gross-margin path",
    catalyst_clarity="clear",
    catalyst_clarity_justification="HBM ramp dated to next print",
    invalidation="both-legs", invalidation_justification="thesis + trade stops named",
)


# --------------------------------------------------------------------------- #
# Thesis conviction: EV asymmetry (max 40) + 3 flags (20/20/20).
# --------------------------------------------------------------------------- #

class TestThesisConviction(unittest.TestCase):
    def test_ev_asymmetry_bands_balanced(self):
        # balanced hurdle = 0.08 * 1.5 = 0.12 ; ratio 0.175/0.12 = 1.4583 in
        # [1.0,1.5) -> 24.
        tc = sc.score_thesis_conviction(
            _SCENARIOS, "reasoning", _LAST, "balanced", **_FLAGS)
        self.assertEqual(tc["subscore_points"]["ev_asymmetry"], 24)

    def test_ev_asymmetry_bands_trader(self):
        # trader hurdle = 0.08 * 0.5 = 0.04 ; ratio 4.375 >= 2 -> 40.
        tc = sc.score_thesis_conviction(
            _SCENARIOS, "reasoning", _LAST, "trader", **_FLAGS)
        self.assertEqual(tc["subscore_points"]["ev_asymmetry"], 40)

    def test_ev_asymmetry_bands_longterm(self):
        # long-term hurdle = 0.08 * 4.0 = 0.32 ; ratio 0.5469 in [0.5,1.0) -> 12.
        tc = sc.score_thesis_conviction(
            _SCENARIOS, "reasoning", _LAST, "long-term", **_FLAGS)
        self.assertEqual(tc["subscore_points"]["ev_asymmetry"], 12)

    def test_flag_points(self):
        tc = sc.score_thesis_conviction(
            _SCENARIOS, "reasoning", _LAST, "balanced", **_FLAGS)
        self.assertEqual(tc["subscore_points"]["variant"], 12)
        self.assertEqual(tc["subscore_points"]["catalyst_clarity"], 20)
        self.assertEqual(tc["subscore_points"]["invalidation"], 20)

    def test_tc_total_balanced(self):
        # 24 + 12 + 20 + 20 = 76
        tc = sc.score_thesis_conviction(
            _SCENARIOS, "reasoning", _LAST, "balanced", **_FLAGS)
        self.assertEqual(tc["score"], 76)

    def test_tc_total_trader(self):
        # 40 + 12 + 20 + 20 = 92
        tc = sc.score_thesis_conviction(
            _SCENARIOS, "reasoning", _LAST, "trader", **_FLAGS)
        self.assertEqual(tc["score"], 92)

    def test_tc_total_longterm(self):
        # 12 + 12 + 20 + 20 = 64
        tc = sc.score_thesis_conviction(
            _SCENARIOS, "reasoning", _LAST, "long-term", **_FLAGS)
        self.assertEqual(tc["score"], 64)

    def test_variant_bands(self):
        for choice, pts in (("strong", 20), ("some", 12), ("none", 4)):
            flags = dict(_FLAGS, variant=choice)
            tc = sc.score_thesis_conviction(
                _SCENARIOS, "r", _LAST, "balanced", **flags)
            self.assertEqual(tc["subscore_points"]["variant"], pts)

    def test_catalyst_bands(self):
        for choice, pts in (("clear", 20), ("partial", 12), ("vague", 4)):
            flags = dict(_FLAGS, catalyst_clarity=choice)
            tc = sc.score_thesis_conviction(
                _SCENARIOS, "r", _LAST, "balanced", **flags)
            self.assertEqual(tc["subscore_points"]["catalyst_clarity"], pts)

    def test_invalidation_bands(self):
        for choice, pts in (("both-legs", 20), ("one-leg", 10), ("none", 0)):
            flags = dict(_FLAGS, invalidation=choice)
            tc = sc.score_thesis_conviction(
                _SCENARIOS, "r", _LAST, "balanced", **flags)
            self.assertEqual(tc["subscore_points"]["invalidation"], pts)

    def test_ev_asymmetry_all_bands(self):
        # Pin every EV-asymmetry band via a synthetic ratio (hurdle fixed at 0.10
        # by using balanced horizon overridden through the ratio helper).
        cases = [
            (2.0, 40), (1.99, 32), (1.5, 32), (1.49, 24), (1.0, 24),
            (0.99, 12), (0.5, 12), (0.49, 6), (0.0, 6), (-0.01, 0),
        ]
        for ratio, expected in cases:
            self.assertEqual(sc.ev_asymmetry_points(ratio), expected,
                             f"ratio {ratio} -> {expected}")

    def test_subscores_are_arithmetic_strings(self):
        tc = sc.score_thesis_conviction(
            _SCENARIOS, "reasoning text", _LAST, "balanced", **_FLAGS)
        joined = " ".join(tc["subscores"])
        self.assertIn("ev_asymmetry", joined)
        self.assertIn("variant", joined)
        self.assertIn("40", joined)  # the ev max appears


# --------------------------------------------------------------------------- #
# Composite weighting (fixed per-profile weights, renormalized over present).
# --------------------------------------------------------------------------- #

def _modules(**over):
    """Four minimal module docs with the pinned scores."""
    mods = {
        "technical": {"skill": "technical-analysis", "score": 70},
        "fundamental": {"skill": "fundamental", "score": 60},
        "sentiment": {"skill": "sentiment-positioning", "score": 50},
        "risk": {"skill": "risk-analytics", "score": 40},
    }
    mods.update(over)
    return mods


class TestComposite(unittest.TestCase):
    def test_weighted_composite_balanced(self):
        # .25*70 + .25*60 + .20*50 + .15*40 + .15*76 = 59.9
        result = sc.score_composite(
            _modules(), 76.0, "balanced")
        self.assertAlmostEqual(result["score"], 59.9, places=4)

    def test_weighted_composite_trader(self):
        # .35*70 + .10*60 + .25*50 + .15*40 + .15*92 = 62.8
        result = sc.score_composite(_modules(), 92.0, "trader")
        self.assertAlmostEqual(result["score"], 62.8, places=4)

    def test_weighted_composite_longterm(self):
        # .10*70 + .40*60 + .15*50 + .15*40 + .20*64 = 57.3
        result = sc.score_composite(_modules(), 64.0, "long-term")
        self.assertAlmostEqual(result["score"], 57.3, places=4)

    def test_dimension_rows_have_source(self):
        result = sc.score_composite(_modules(), 76.0, "balanced")
        by = {d["name"]: d for d in result["dimensions"]}
        self.assertEqual(by["technical"]["source"], "module_technical.json")
        self.assertEqual(by["fundamental"]["source"], "module_fundamental.json")
        self.assertEqual(by["sentiment"]["source"], "module_sentiment.json")
        self.assertEqual(by["risk"]["source"], "module_risk.json")
        self.assertEqual(by["thesis_conviction"]["source"], "computed")

    def test_present_weights_sum_to_one_all_five(self):
        result = sc.score_composite(_modules(), 76.0, "balanced")
        s = sum(d["weight_renormalized"] for d in result["dimensions"])
        self.assertAlmostEqual(s, 1.0, places=6)
        self.assertIsNone(result["renormalization_note"])

    def test_missing_risk_renormalizes(self):
        # risk absent -> present balanced weights (.25/.25/.20/.15) sum .85,
        # rescaled to sum 1; composite over present = 63.4118.
        mods = _modules()
        del mods["risk"]
        result = sc.score_composite(mods, 76.0, "balanced")
        self.assertAlmostEqual(result["score"], 63.4118, places=4)
        s = sum(d["weight_renormalized"] for d in result["dimensions"])
        self.assertAlmostEqual(s, 1.0, places=6)
        self.assertIsNotNone(result["renormalization_note"])
        # excluded dimension named
        self.assertIn("risk", result["renormalization_note"])
        # risk row absent from dimensions (module not provided)
        names = [d["name"] for d in result["dimensions"]]
        self.assertNotIn("risk", names)

    def test_rollup_min_over_evidence_confidence(self):
        # per-module confidence carried on the module docs -> composite rolls up min.
        mods = _modules(
            technical={"skill": "technical-analysis", "score": 70,
                       "confidence": _conf("HIGH")},
            fundamental={"skill": "fundamental", "score": 60,
                         "confidence": _conf("HIGH")},
            sentiment={"skill": "sentiment-positioning", "score": 50,
                       "confidence": _conf("MEDIUM")},
            risk={"skill": "risk-analytics", "score": 40,
                  "confidence": _conf("HIGH")},
        )
        result = sc.score_composite(mods, 76.0, "balanced")
        self.assertEqual(result["confidence"]["level"], "MEDIUM")
        self.assertIn("sentiment", result["confidence"]["why"])

    def test_rollup_excludes_thesis_conviction(self):
        # thesis-conviction (score 76) never contributes to the roll-up min even
        # though it is a dimension row -- only the four evidence dims do.
        mods = _modules(
            technical={"skill": "technical-analysis", "score": 70,
                       "confidence": _conf("HIGH")},
            fundamental={"skill": "fundamental", "score": 60,
                         "confidence": _conf("HIGH")},
            sentiment={"skill": "sentiment-positioning", "score": 50,
                       "confidence": _conf("HIGH")},
            risk={"skill": "risk-analytics", "score": 40,
                  "confidence": _conf("HIGH")},
        )
        result = sc.score_composite(mods, 76.0, "balanced")
        self.assertEqual(result["confidence"]["level"], "HIGH")
        # thesis_conviction row carries confidence "n/a", not a real block.
        by = {d["name"]: d for d in result["dimensions"]}
        self.assertEqual(by["thesis_conviction"]["confidence"], "n/a")

    def test_rollup_renormalized_dim_contributes_none(self):
        # missing risk (renormalized away) -> roll-up over the remaining three.
        mods = _modules(
            technical={"skill": "technical-analysis", "score": 70,
                       "confidence": _conf("HIGH")},
            fundamental={"skill": "fundamental", "score": 60,
                         "confidence": _conf("HIGH")},
            sentiment={"skill": "sentiment-positioning", "score": 50,
                       "confidence": _conf("LOW")},
        )
        del mods["risk"]
        result = sc.score_composite(mods, 76.0, "balanced")
        self.assertEqual(result["confidence"]["level"], "LOW")

    def test_rollup_none_when_no_module_confidence(self):
        # minimal module docs (no confidence blocks) -> roll-up level None.
        result = sc.score_composite(_modules(), 76.0, "balanced")
        self.assertIsNone(result["confidence"]["level"])


# --------------------------------------------------------------------------- #
# Grade bands (fixed): A >=80, B 60-79, C 45-59, D <45.
# --------------------------------------------------------------------------- #

class TestGrades(unittest.TestCase):
    def test_grade_edges(self):
        cases = [
            (80.0, "A", "Buy/Add"),
            (79.99, "B", "Hold/Accumulate-on-weakness"),
            (60.0, "B", "Hold/Accumulate-on-weakness"),
            (59.99, "C", "Hold/Trim"),
            (45.0, "C", "Hold/Trim"),
            (44.99, "D", "Reduce/Avoid"),
            (0.0, "D", "Reduce/Avoid"),
            (100.0, "A", "Buy/Add"),
        ]
        for score, grade, action in cases:
            g, a = sc.grade_for(score)
            self.assertEqual(g, grade, f"{score} -> {grade}")
            self.assertEqual(a, action, f"{score} -> {action}")


# --------------------------------------------------------------------------- #
# EV block.
# --------------------------------------------------------------------------- #

class TestEVBlock(unittest.TestCase):
    def test_ev_at_current(self):
        ev = sc.build_ev_block(_SCENARIOS, "reasoning", _LAST, "balanced", [])
        self.assertAlmostEqual(ev["ev_at_current"], 0.175, places=6)

    def test_hurdle_and_convention(self):
        ev = sc.build_ev_block(_SCENARIOS, "reasoning", _LAST, "balanced", [])
        self.assertAlmostEqual(ev["hurdle_total"], 0.12, places=6)
        self.assertEqual(ev["horizon_years_convention"], 1.5)

    def test_breakeven_entry(self):
        # sum(p*t)/(1+hurdle) = 117.5 / 1.12
        ev = sc.build_ev_block(_SCENARIOS, "reasoning", _LAST, "balanced", [])
        self.assertAlmostEqual(ev["ev_breakeven_entry"], 117.5 / 1.12, places=4)

    def test_entry_levels_ev_exact(self):
        # ev_at(scen, 110) = .25*(150/110-1) + .5*(120/110-1) + .25*(80/110-1).
        # Stored EV passes through _clean (4-dp stable-JSON precision, the
        # project-wide convention), so compare at 4 places.
        from scripts import ev_kelly
        expect_110 = round(ev_kelly.ev_at(_SCENARIOS, 110.0), 4)
        expect_95 = round(ev_kelly.ev_at(_SCENARIOS, 95.0), 4)
        ev = sc.build_ev_block(_SCENARIOS, "reasoning", _LAST, "balanced",
                               [110.0, 95.0])
        by = {round(r["level"], 6): r["ev"] for r in ev["ev_at_levels"]}
        self.assertAlmostEqual(by[110.0], expect_110, places=4)
        self.assertAlmostEqual(by[95.0], expect_95, places=4)

    def test_no_entry_levels_empty(self):
        ev = sc.build_ev_block(_SCENARIOS, "reasoning", _LAST, "balanced", [])
        self.assertEqual(ev["ev_at_levels"], [])

    def test_scenario_reasoning_carried(self):
        ev = sc.build_ev_block(_SCENARIOS, "HBM demand asymmetric", _LAST,
                               "balanced", [])
        self.assertEqual(ev["scenario_reasoning"], "HBM demand asymmetric")


# --------------------------------------------------------------------------- #
# Sensitivity: recompute full composite per-profile (EV re-banded per hurdle).
# --------------------------------------------------------------------------- #

class TestSensitivity(unittest.TestCase):
    def test_sensitivity_grades(self):
        sens = sc.build_sensitivity(_modules(), _SCENARIOS, _LAST, **_FLAGS)
        # balanced 59.9 -> C ; trader 62.8 -> B ; long-term 57.3 -> C
        self.assertAlmostEqual(sens["balanced"]["score"], 59.9, places=4)
        self.assertEqual(sens["balanced"]["grade"], "C")
        self.assertAlmostEqual(sens["trader"]["score"], 62.8, places=4)
        self.assertEqual(sens["trader"]["grade"], "B")
        self.assertAlmostEqual(sens["long-term"]["score"], 57.3, places=4)
        self.assertEqual(sens["long-term"]["grade"], "C")


# --------------------------------------------------------------------------- #
# INPUT_FIELDS declaration (single-mapping governance).
# --------------------------------------------------------------------------- #

class TestInputFields(unittest.TestCase):
    def test_input_fields_empty(self):
        # composite consumes module scores + price.last (EV reference), no snapshot
        # fields scored directly -- single-mapping preserved by construction.
        self.assertEqual(sc.INPUT_FIELDS, set())


# --------------------------------------------------------------------------- #
# CLI end-to-end.
# --------------------------------------------------------------------------- #

def _write_module(dir_, name, score, confidence=None):
    doc = {"skill": name, "score": score}
    if confidence is not None:
        doc["confidence"] = confidence
    with open(os.path.join(dir_, f"module_{name}.json"), "w") as fh:
        json.dump(doc, fh)


def _conf(level):
    """A minimal per-module confidence block (all axes at ``level``)."""
    return {
        "level": level,
        "source": {"level": level, "why": "tag"},
        "depth": {"level": level, "why": "tag"},
        "staleness": {"level": level, "why": "tag"},
        "rule": "min(source, depth, staleness)",
        "version": "1.0.0",
    }


def _write_snapshot(dir_):
    snap = {
        "meta": {"ticker": "MU", "as_of_utc": "2026-07-16T00:00:00Z"},
        "price": {"last": 100.0},
    }
    with open(os.path.join(dir_, "snapshot_MU_2026-07-16.json"), "w") as fh:
        json.dump(snap, fh)


def _write_scenarios(dir_, scenarios=None):
    path = os.path.join(dir_, "scenarios.json")
    with open(path, "w") as fh:
        json.dump(scenarios if scenarios is not None else _SCENARIOS, fh)
    return path


class TestCLI(unittest.TestCase):
    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, s in _MOD_SCORES.items():
            _write_module(self.dir, name, s)
        _write_snapshot(self.dir)
        self.scen = _write_scenarios(self.dir)

    def _base_flags(self):
        return [
            "--scenarios", self.scen,
            "--scenario-reasoning", "asymmetric HBM demand",
            "--variant", "some",
            "--variant-justification", "gross-margin path differentiated",
            "--catalyst-clarity", "clear",
            "--catalyst-clarity-justification", "HBM ramp dated",
            "--invalidation", "both-legs",
            "--invalidation-justification", "thesis + trade stops named",
        ]

    def _run(self, extra=None, flags=True):
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir]
        if flags:
            cmd += self._base_flags()
        if extra:
            cmd += extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def _write_stamped_context(self):
        ctx = {"skill": "company-context", "version": "1.0.0", "ticker": "MU",
               "as_of": "2026-07-16", "mode": "coverage_distilled",
               "findings": [{"id": "C1", "claim": "x", "source": "coverage/research.md"}],
               "qc": {"qc_passed": True, "checked_utc": "2026-07-16T00:00:00Z"}}
        with open(os.path.join(self.dir, "module_context.json"), "w") as fh:
            json.dump(ctx, fh)

    def test_context_grounding_enforced_when_stamped_context_exists(self):
        # coverage-first: with a QC-stamped context module, variant and
        # catalyst-clarity justifications MUST cite finding IDs (C<n>).
        self._write_stamped_context()
        proc = self._run()  # base flags carry no C-IDs
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must cite context finding IDs", proc.stderr)
        # citing IDs passes
        flags = self._base_flags()
        flags[flags.index("gross-margin path differentiated")] = \
            "gross-margin path differentiated (C1)"
        flags[flags.index("HBM ramp dated")] = "HBM ramp dated per C1"
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir] + flags
        proc2 = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)

    def test_no_grounding_requirement_without_context_module(self):
        # compressed floor: no context module -> free-text justifications OK
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_unstamped_context_does_not_enforce(self):
        self._write_stamped_context()
        p = os.path.join(self.dir, "module_context.json")
        with open(p) as fh:
            ctx = json.load(fh)
        ctx["qc"] = None
        with open(p, "w") as fh:
            json.dump(ctx, fh)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_context_grounding_referential_integrity_unresolved_exit2(self):
        # A cited C-ID that does NOT resolve to a context findings[] id (the fixture
        # registry is C1 only) is a broken reference -> exit 2, message names it.
        self._write_stamped_context()  # findings: [C1]
        flags = self._base_flags()
        flags[flags.index("gross-margin path differentiated")] = \
            "gross-margin path differentiated (C99)"
        flags[flags.index("HBM ramp dated")] = "HBM ramp dated per C1"
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir] + flags
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("C99 does not exist", proc.stderr)
        self.assertIn("module_context.json", proc.stderr)
        self.assertIn("C1..C1", proc.stderr)

    def test_context_grounding_referential_integrity_resolved_passes(self):
        # Every cited C-ID resolves to the findings[] registry (C1) -> passes.
        self._write_stamped_context()  # findings: [C1]
        flags = self._base_flags()
        flags[flags.index("gross-margin path differentiated")] = \
            "gross-margin path differentiated (C1)"
        flags[flags.index("HBM ramp dated")] = "HBM ramp dated per C1"
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir] + flags
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_cli_exit0_writes_module_json(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = os.path.join(self.dir, "module_composite.json")
        self.assertTrue(os.path.exists(out))
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["skill"], "composite-score")
        self.assertEqual(doc["rubric_version"], "1.0.0")
        self.assertEqual(doc["ticker"], "MU")
        self.assertEqual(doc["as_of"], "2026-07-16")
        self.assertEqual(doc["profile"], "balanced")
        self.assertAlmostEqual(doc["score"], 59.9, places=4)
        self.assertEqual(doc["grade"], "C")
        self.assertEqual(doc["action"], "Hold/Trim")
        self.assertIsNone(doc["tension"])
        self.assertIsNone(doc["signal"])
        # dimensions include all five
        names = {d["name"] for d in doc["dimensions"]}
        self.assertEqual(names, {"technical", "fundamental", "sentiment",
                                 "risk", "thesis_conviction"})
        # confidence-v1.0.0: every dimension row carries a confidence key; the doc
        # carries a top-level roll-up. The minimal fixture modules carry no
        # confidence block, so the roll-up level is None (no evidence provenance).
        for d in doc["dimensions"]:
            self.assertIn("confidence", d)
        by = {d["name"]: d for d in doc["dimensions"]}
        # thesis-conviction has no data provenance -> confidence n/a on its row.
        self.assertEqual(by["thesis_conviction"]["confidence"], "n/a")
        self.assertIn("confidence", doc)
        self.assertEqual(doc["confidence"]["version"], "1.0.0")
        # thesis conviction block
        self.assertEqual(doc["thesis_conviction"]["score"], 76)
        # ev block
        self.assertAlmostEqual(doc["ev"]["ev_at_current"], 0.175, places=6)
        self.assertAlmostEqual(doc["ev"]["hurdle_total"], 0.12, places=6)
        self.assertAlmostEqual(doc["ev"]["ev_breakeven_entry"],
                               117.5 / 1.12, places=4)
        # sensitivity block, all three profiles
        self.assertEqual(doc["sensitivity"]["balanced"]["grade"], "C")
        self.assertEqual(doc["sensitivity"]["trader"]["grade"], "B")
        self.assertEqual(doc["sensitivity"]["long-term"]["grade"], "C")

    def test_rollup_min_over_evidence_dimensions(self):
        # Inject per-module confidence: technical HIGH, fundamental HIGH, sentiment
        # MEDIUM, risk MEDIUM -> roll-up MEDIUM (weakest evidence dimension).
        _write_module(self.dir, "technical", 70, confidence=_conf("HIGH"))
        _write_module(self.dir, "fundamental", 60, confidence=_conf("HIGH"))
        _write_module(self.dir, "sentiment", 50, confidence=_conf("MEDIUM"))
        _write_module(self.dir, "risk", 40, confidence=_conf("MEDIUM"))
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir, "module_composite.json")) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["confidence"]["level"], "MEDIUM")
        # the driver names a weakest (MEDIUM) dimension.
        self.assertTrue(any(name in doc["confidence"]["why"]
                            for name in ("sentiment", "risk")))
        # rows carry the confidence blocks; thesis-conviction stays n/a.
        by = {d["name"]: d for d in doc["dimensions"]}
        self.assertEqual(by["technical"]["confidence"]["level"], "HIGH")
        self.assertEqual(by["thesis_conviction"]["confidence"], "n/a")

    def test_rollup_over_three_when_one_evidence_module_renormalized(self):
        # Remove risk (renormalized away): roll-up min over the remaining THREE.
        _write_module(self.dir, "technical", 70, confidence=_conf("HIGH"))
        _write_module(self.dir, "fundamental", 60, confidence=_conf("HIGH"))
        _write_module(self.dir, "sentiment", 50, confidence=_conf("LOW"))
        os.remove(os.path.join(self.dir, "module_risk.json"))
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir, "module_composite.json")) as fh:
            doc = json.load(fh)
        # sentiment LOW dominates the surviving three.
        self.assertEqual(doc["confidence"]["level"], "LOW")
        names = {d["name"] for d in doc["dimensions"]}
        self.assertNotIn("risk", names)

    def test_profile_trader(self):
        proc = self._run(extra=["--profile", "trader"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir, "module_composite.json")) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["profile"], "trader")
        self.assertAlmostEqual(doc["score"], 62.8, places=4)
        self.assertEqual(doc["grade"], "B")

    def test_entry_levels(self):
        proc = self._run(extra=["--entry-level", "110", "--entry-level", "95"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir, "module_composite.json")) as fh:
            doc = json.load(fh)
        from scripts import ev_kelly
        by = {round(r["level"], 6): r["ev"] for r in doc["ev"]["ev_at_levels"]}
        # stored EV is _clean-rounded to 4 dp (project-wide stable-JSON convention).
        self.assertAlmostEqual(by[110.0], round(ev_kelly.ev_at(_SCENARIOS, 110.0), 4),
                               places=4)
        self.assertAlmostEqual(by[95.0], round(ev_kelly.ev_at(_SCENARIOS, 95.0), 4),
                               places=4)

    def test_missing_risk_module_renormalizes(self):
        os.remove(os.path.join(self.dir, "module_risk.json"))
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir, "module_composite.json")) as fh:
            doc = json.load(fh)
        self.assertAlmostEqual(doc["score"], 63.4118, places=4)
        self.assertIsNotNone(doc["renormalization_note"])
        s = sum(d["weight_renormalized"] for d in doc["dimensions"])
        self.assertAlmostEqual(s, 1.0, places=6)

    def test_three_missing_modules_exit2(self):
        # remove technical, fundamental, sentiment -> only risk + computed TC
        # present -> >=3 of 5 dimensions missing -> insufficient evidence.
        os.remove(os.path.join(self.dir, "module_technical.json"))
        os.remove(os.path.join(self.dir, "module_fundamental.json"))
        os.remove(os.path.join(self.dir, "module_sentiment.json"))
        proc = self._run()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("insufficient evidence modules", proc.stderr)

    def test_missing_scenarios_exit2(self):
        # omit --scenarios entirely (rebuild the base flags without it)
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir,
               "--scenario-reasoning", "x",
               "--variant", "some", "--variant-justification", "j",
               "--catalyst-clarity", "clear",
               "--catalyst-clarity-justification", "j",
               "--invalidation", "both-legs",
               "--invalidation-justification", "j"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("scenarios", proc.stderr.lower())

    def test_missing_scenario_file_exit2(self):
        proc = self._run(extra=["--scenarios",
                                os.path.join(self.dir, "does_not_exist.json")])
        # --scenarios points at a real base flag; override wins (later arg)
        self.assertEqual(proc.returncode, 2)

    def test_probs_not_summing_to_one_exit2(self):
        bad = _write_scenarios(self.dir, [
            {"name": "bull", "prob": 0.6, "price_target": 150.0},
            {"name": "base", "prob": 0.5, "price_target": 120.0},
        ])
        proc = self._run(extra=["--scenarios", bad])
        self.assertEqual(proc.returncode, 2)

    def test_missing_variant_justification_exit2(self):
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir,
               "--scenarios", self.scen, "--scenario-reasoning", "x",
               "--variant", "some",
               "--catalyst-clarity", "clear",
               "--catalyst-clarity-justification", "j",
               "--invalidation", "both-legs",
               "--invalidation-justification", "j"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_missing_scenario_reasoning_exit2(self):
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir,
               "--scenarios", self.scen,
               "--variant", "some", "--variant-justification", "j",
               "--catalyst-clarity", "clear",
               "--catalyst-clarity-justification", "j",
               "--invalidation", "both-legs",
               "--invalidation-justification", "j"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_custom_out_path(self):
        out = os.path.join(self.dir, "custom_composite.json")
        proc = self._run(extra=["--out", out])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

    def test_determinism(self):
        out1 = os.path.join(self.dir, "run1.json")
        out2 = os.path.join(self.dir, "run2.json")
        p1 = self._run(extra=["--out", out1])
        p2 = self._run(extra=["--out", out2])
        self.assertEqual(p1.returncode, 0, p1.stderr)
        self.assertEqual(p2.returncode, 0, p2.stderr)
        with open(out1) as fh:
            a = fh.read()
        with open(out2) as fh:
            b = fh.read()
        self.assertEqual(a, b)


# --------------------------------------------------------------------------- #
# Weights config (versioned tuning transparency) -- v0.12.0.
# --------------------------------------------------------------------------- #

# A custom balanced column that sums to 1.0 but weights fundamental/technical
# heavier and thesis_conviction lighter than the standard table. Pinned:
#   .30*70 + .30*60 + .15*50 + .15*40 + .10*76 = 60.1   (standard balanced = 59.9)
_CUSTOM_BALANCED = {"technical": .30, "fundamental": .30, "sentiment": .15,
                    "risk": .15, "thesis_conviction": .10}


def _write_config(dir_, profiles, set_name="custom-v1", version="2026-07-18",
                  name="trading_desk_config.json"):
    """Write a trading_desk_config.json with a weights block; return its path."""
    cfg = {"weights": {"set_name": set_name, "version": version,
                       "profiles": profiles}}
    path = os.path.join(dir_, name)
    with open(path, "w") as fh:
        json.dump(cfg, fh)
    return path


class TestWeightsConfigValidation(unittest.TestCase):
    def test_sum_not_one_raises_named(self):
        with tempfile.TemporaryDirectory() as d:
            bad = dict(_CUSTOM_BALANCED, thesis_conviction=.20)  # sums 1.10
            path = _write_config(d, {"balanced": bad})
            with self.assertRaises(sc.WeightsConfigError) as ctx:
                sc.load_weights_config(path)
            msg = str(ctx.exception)
            self.assertIn("balanced", msg)
            self.assertIn("1.1", msg)  # the observed sum surfaces

    def test_sum_within_tolerance_ok(self):
        # 1e-7 drift is inside +/- 1e-6.
        with tempfile.TemporaryDirectory() as d:
            near = {"technical": .30, "fundamental": .30, "sentiment": .15,
                    "risk": .15, "thesis_conviction": .1000001}
            path = _write_config(d, {"balanced": near})
            profiles, label = sc.load_weights_config(path)
            self.assertIn("balanced", profiles)

    def test_unknown_dimension_key_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, {"balanced": {"technical": .5, "momentum": .5}})
            with self.assertRaises(sc.WeightsConfigError) as ctx:
                sc.load_weights_config(path)
            self.assertIn("momentum", str(ctx.exception))

    def test_no_weights_key_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trading_desk_config.json")
            with open(path, "w") as fh:
                json.dump({"fsi_offer": {"asked": True}}, fh)  # no weights key
            profiles, label = sc.load_weights_config(path)
            self.assertIsNone(profiles)
            self.assertIsNone(label)

    def test_label_is_custom_setname_at_version(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, {"balanced": _CUSTOM_BALANCED},
                                 set_name="custom-v1", version="2026-07-18")
            _, label = sc.load_weights_config(path)
            self.assertEqual(label, "CUSTOM custom-v1@2026-07-18")


class TestResolveWeightsFallback(unittest.TestCase):
    def test_customized_profile_uses_custom(self):
        profiles = {"balanced": _CUSTOM_BALANCED}
        label = "CUSTOM custom-v1@2026-07-18"
        w, lab = sc.resolve_weights("balanced", profiles, label)
        self.assertEqual(w, _CUSTOM_BALANCED)
        self.assertEqual(lab, label)

    def test_absent_profile_falls_back_per_profile(self):
        # Only balanced customized -> trader / long-term keep the standard table
        # AND the standard label (per-profile fallback).
        profiles = {"balanced": _CUSTOM_BALANCED}
        label = "CUSTOM custom-v1@2026-07-18"
        w, lab = sc.resolve_weights("trader", profiles, label)
        self.assertEqual(w, sc.WEIGHTS["trader"])
        self.assertEqual(lab, sc.STANDARD_WEIGHT_SET)

    def test_no_config_uses_standard(self):
        w, lab = sc.resolve_weights("balanced", None, None)
        self.assertEqual(w, sc.WEIGHTS["balanced"])
        self.assertEqual(lab, "standard v1")


class TestCompositeUnderCustomWeights(unittest.TestCase):
    def test_custom_balanced_pinned(self):
        # .30*70 + .30*60 + .15*50 + .15*40 + .10*76 = 60.1
        result = sc.score_composite(_modules(), 76.0, "balanced",
                                    _CUSTOM_BALANCED)
        self.assertAlmostEqual(result["score"], 60.1, places=4)
        # dimensions carry the CUSTOM weights actually used.
        by = {d["name"]: d for d in result["dimensions"]}
        self.assertAlmostEqual(by["technical"]["weight"], 0.30, places=6)
        self.assertAlmostEqual(by["thesis_conviction"]["weight"], 0.10, places=6)

    def test_renormalization_identical_under_custom(self):
        # risk absent under custom: present .30/.30/.15/.10 sum .85 rescaled to 1.
        #   composite over present = 63.6471 (hand-recompute).
        mods = _modules()
        del mods["risk"]
        result = sc.score_composite(mods, 76.0, "balanced", _CUSTOM_BALANCED)
        self.assertAlmostEqual(result["score"], 63.6471, places=4)
        # weight_renormalized rows are _clean-rounded to 4 dp (stable-JSON), so the
        # displayed weights sum to ~1.0 within that rounding (not 1e-6 exact).
        s = sum(d["weight_renormalized"] for d in result["dimensions"])
        self.assertAlmostEqual(s, 1.0, places=3)
        self.assertIsNotNone(result["renormalization_note"])
        self.assertIn("risk", result["renormalization_note"])


class TestSensitivityStandardComparison(unittest.TestCase):
    def test_custom_sensitivity_carries_standard_comparison(self):
        profiles = {"balanced": _CUSTOM_BALANCED}
        label = "CUSTOM custom-v1@2026-07-18"
        sens = sc.build_sensitivity(
            _modules(), _SCENARIOS, _LAST, **_FLAGS,
            custom_profiles=profiles, custom_label=label)
        # top-level weight_set label present + custom.
        self.assertEqual(sens["weight_set"], label)
        # balanced customized: custom score 60.1, standard_comparison recomputes 59.9
        self.assertAlmostEqual(sens["balanced"]["score"], 60.1, places=4)
        self.assertEqual(sens["balanced"]["grade"], "B")  # >= 60
        self.assertIn("standard_comparison", sens["balanced"])
        self.assertAlmostEqual(
            sens["balanced"]["standard_comparison"]["score"], 59.9, places=4)
        self.assertEqual(sens["balanced"]["standard_comparison"]["grade"], "C")
        # trader falls back to standard -> NO standard_comparison (it IS standard).
        self.assertNotIn("standard_comparison", sens["trader"])
        self.assertAlmostEqual(sens["trader"]["score"], 62.8, places=4)

    def test_standard_sensitivity_has_standard_label_no_comparison(self):
        sens = sc.build_sensitivity(_modules(), _SCENARIOS, _LAST, **_FLAGS)
        self.assertEqual(sens["weight_set"], "standard v1")
        for profile in ("balanced", "trader", "long-term"):
            self.assertNotIn("standard_comparison", sens[profile])


class TestWeightSetStamping(unittest.TestCase):
    def test_standard_stamp_no_config(self):
        doc = sc.build_module(
            {"meta": {"ticker": "MU", "as_of_utc": "2026-07-16T00:00:00Z"},
             "price": {"last": 100.0}},
            _modules(), _SCENARIOS, "r", "balanced", **_FLAGS, entry_levels=[])
        self.assertEqual(doc["weight_set"], "standard v1")
        self.assertEqual(doc["sensitivity"]["weight_set"], "standard v1")

    def test_custom_stamp_with_config(self):
        doc = sc.build_module(
            {"meta": {"ticker": "MU", "as_of_utc": "2026-07-16T00:00:00Z"},
             "price": {"last": 100.0}},
            _modules(), _SCENARIOS, "r", "balanced", **_FLAGS, entry_levels=[],
            custom_profiles={"balanced": _CUSTOM_BALANCED},
            custom_label="CUSTOM custom-v1@2026-07-18")
        self.assertEqual(doc["weight_set"], "CUSTOM custom-v1@2026-07-18")
        self.assertAlmostEqual(doc["score"], 60.1, places=4)


class TestWeightsConfigCLI(unittest.TestCase):
    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, s in _MOD_SCORES.items():
            _write_module(self.dir, name, s)
        _write_snapshot(self.dir)
        self.scen = _write_scenarios(self.dir)

    def _base_flags(self):
        return [
            "--scenarios", self.scen,
            "--scenario-reasoning", "asymmetric HBM demand",
            "--variant", "some",
            "--variant-justification", "gross-margin path differentiated",
            "--catalyst-clarity", "clear",
            "--catalyst-clarity-justification", "HBM ramp dated",
            "--invalidation", "both-legs",
            "--invalidation-justification", "thesis + trade stops named",
        ]

    def _run(self, extra=None):
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir] + self._base_flags()
        if extra:
            cmd += extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_custom_weights_stamps_and_scores(self):
        cfg = _write_config(self.dir, {"balanced": _CUSTOM_BALANCED},
                            name="weights.json")
        proc = self._run(extra=["--weights-config", cfg])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir, "module_composite.json")) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["weight_set"], "CUSTOM custom-v1@2026-07-18")
        self.assertAlmostEqual(doc["score"], 60.1, places=4)
        # standard_comparison visible on the balanced sensitivity row.
        self.assertAlmostEqual(
            doc["sensitivity"]["balanced"]["standard_comparison"]["score"],
            59.9, places=4)

    def test_bad_sum_config_exit2_names_profile(self):
        bad = dict(_CUSTOM_BALANCED, thesis_conviction=.20)  # sums 1.10
        cfg = _write_config(self.dir, {"balanced": bad}, name="weights.json")
        proc = self._run(extra=["--weights-config", cfg])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("balanced", proc.stderr)
        self.assertIn("1.1", proc.stderr)

    def test_unknown_key_config_exit2(self):
        cfg = _write_config(self.dir,
                            {"balanced": {"technical": .5, "momentum": .5}},
                            name="weights.json")
        proc = self._run(extra=["--weights-config", cfg])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("momentum", proc.stderr)

    def test_missing_config_file_exit2(self):
        proc = self._run(extra=["--weights-config",
                                os.path.join(self.dir, "nope.json")])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not found", proc.stderr)

    def test_no_config_stamps_standard(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir, "module_composite.json")) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["weight_set"], "standard v1")
        self.assertEqual(doc["sensitivity"]["weight_set"], "standard v1")

    def test_default_config_picked_up_from_cwd(self):
        # A ./trading_desk_config.json in the CWD is loaded by default (no flag).
        import shutil
        cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cwd, True)
        _write_config(cwd, {"balanced": _CUSTOM_BALANCED})  # default name
        cmd = [sys.executable, SCRIPT, "--bundle", self.dir] + self._base_flags()
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir, "module_composite.json")) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["weight_set"], "CUSTOM custom-v1@2026-07-18")

    def test_renormalization_under_custom_cli(self):
        os.remove(os.path.join(self.dir, "module_risk.json"))
        cfg = _write_config(self.dir, {"balanced": _CUSTOM_BALANCED},
                            name="weights.json")
        proc = self._run(extra=["--weights-config", cfg])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir, "module_composite.json")) as fh:
            doc = json.load(fh)
        self.assertAlmostEqual(doc["score"], 63.6471, places=4)
        self.assertIsNotNone(doc["renormalization_note"])
        self.assertEqual(doc["weight_set"], "CUSTOM custom-v1@2026-07-18")


if __name__ == "__main__":
    unittest.main()
