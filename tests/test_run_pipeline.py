"""Tests for scripts/run_pipeline.py -- the headless NO-EVENT re-score (FR-4).

WHY: a downstream orchestrator runs cheap scheduled re-scores of an existing
ticker workspace with the model OUT of the control path. The deterministic scorer
chain is safe to run headlessly, but several scorers REQUIRE model-authored inputs
(scenarios, composite conviction flags, trade-plan flags, moat, module_context).
On a no-event refresh those are carried forward verbatim from the previous bundle;
on an EVENT (earnings/dividend between runs) they must be re-derived by the model.

These tests pin the NEW logic in run_pipeline (not the scorers, which have their
own suites):

  1. carry-forward extraction: the exact field map + carried-tag assembly.
  2. argv assembly for the three flag-carrying scorers (guards a scorer flag rename).
  3. the event gate (straddling earnings -> EXIT_EVENT; no event -> proceeds).
  4. up-front refusals (missing bundle / manifest / --previous / coverage).
  5. gate-failure propagation: a nonzero step stops the chain (no later step runs).
  6. the no-render invariant: run_pipeline never imports/invokes render_*.

stdlib-only; unittest; isolated tempdirs; the scorers are monkeypatched to keep
these unit tests fast and independent of any absolute private path.
"""

import json
import os
import tempfile
import unittest

from scripts import run_pipeline as rp


# --------------------------------------------------------------------------- #
# Fixtures: minimal previous-bundle module shapes (only the carried fields).
# --------------------------------------------------------------------------- #

def _prev_composite(profile="balanced"):
    return {
        "profile": profile,
        "as_of": "2026-07-21",
        "ev": {"scenario_reasoning": "Base 0.5 -> 365 (C4); bull 0.3; bear 0.2 (C10)."},
        "flags": {
            "variant": "some",
            "variant_justification": "differentiated on the DCF/comps split (C15, C16)",
            "catalyst_clarity": "clear",
            "catalyst_clarity_justification": "Q2 print dated, ~4.9% implied (C5)",
            "invalidation": "both-legs",
            "invalidation_justification": "Search negative YoY (C10) or Cloud <30% (C5)",
        },
    }


def _prev_fundamental():
    return {
        "as_of": "2026-07-21",
        "flags": {
            "moat": "wide",
            "moat_justification": "search network effects + query share (C4), TPU (C8)",
        },
    }


def _prev_tradeplan(catalyst_in_thesis=False):
    return {
        "as_of": "2026-07-21",
        "profile": "balanced",
        "flags": {
            "catalyst_in_thesis": catalyst_in_thesis,
            "catalyst_in_thesis_justification": "thesis is structural (C4, C5)",
            "fund_invalidation_metric": "Search revenue growth + Cloud growth/margin",
            "fund_invalidation_threshold": "Search negative YoY, or Cloud <30% 2Q",
            "fund_invalidation_justification": "Search annuity (C10) funds Cloud (C5)",
        },
    }


def _write_prev_bundle(d, *, composite=None, fundamental=None, tradeplan=None,
                       scenarios=True, context=True, snapshot_as_of="2026-07-21"):
    """Write a synthetic previous bundle into dir ``d``. Returns ``d``."""
    def dump(name, obj):
        with open(os.path.join(d, name), "w") as fh:
            json.dump(obj, fh)

    if composite is not None:
        dump("module_composite.json", composite)
    if fundamental is not None:
        dump("module_fundamental.json", fundamental)
    if tradeplan is not None:
        dump("module_tradeplan.json", tradeplan)
    if scenarios:
        dump("scenarios.json", [{"name": "base", "prob": 0.5, "price_target": 365}])
    if context:
        dump("module_context.json",
             {"findings": [{"id": "C4"}, {"id": "C5"}], "live_tape": {"x": 1},
              "qc": {"qc_passed": True}})
    if snapshot_as_of is not None:
        dump("snapshot_GOOG_%s.json" % snapshot_as_of,
             {"meta": {"ticker": "GOOG", "as_of_utc": snapshot_as_of + "T17:00:00Z"}})
    return d


# --------------------------------------------------------------------------- #
# 1. Carry-forward extraction
# --------------------------------------------------------------------------- #

class TestCarryForward(unittest.TestCase):

    def _extract(self, **overrides):
        with tempfile.TemporaryDirectory() as d:
            _write_prev_bundle(
                d,
                composite=overrides.get("composite", _prev_composite()),
                fundamental=overrides.get("fundamental", _prev_fundamental()),
                tradeplan=overrides.get("tradeplan", _prev_tradeplan()))
            return rp.extract_carry_forward(d, "2026-07-21")

    def test_flag_values_carried_verbatim(self):
        c = self._extract()
        self.assertEqual(c["variant"], "some")
        self.assertEqual(c["catalyst_clarity"], "clear")
        self.assertEqual(c["invalidation"], "both-legs")
        self.assertEqual(c["moat"], "wide")
        self.assertEqual(c["profile"], "balanced")
        self.assertEqual(c["fund_invalidation_metric"],
                         "Search revenue growth + Cloud growth/margin")
        self.assertEqual(c["fund_invalidation_threshold"],
                         "Search negative YoY, or Cloud <30% 2Q")

    def test_justifications_get_carried_tag(self):
        c = self._extract()
        tag = " [carried forward from 2026-07-21]"
        for key in ("variant_justification", "catalyst_clarity_justification",
                    "invalidation_justification", "scenario_reasoning",
                    "moat_justification", "catalyst_in_thesis_justification",
                    "fund_invalidation_justification"):
            self.assertTrue(c[key].endswith(tag),
                            "%s should carry the tag, got %r" % (key, c[key]))
        # the C-IDs are preserved BEFORE the tag (grounding gate still resolves them)
        self.assertIn("C15", c["variant_justification"])
        self.assertIn("C4", c["moat_justification"])

    def test_catalyst_in_thesis_bool_maps_to_yes_no(self):
        self.assertEqual(self._extract(
            tradeplan=_prev_tradeplan(catalyst_in_thesis=False))["catalyst_in_thesis"],
            "no")
        self.assertEqual(self._extract(
            tradeplan=_prev_tradeplan(catalyst_in_thesis=True))["catalyst_in_thesis"],
            "yes")

    def test_scenario_reasoning_sourced_from_ev(self):
        c = self._extract()
        self.assertTrue(c["scenario_reasoning"].startswith("Base 0.5 -> 365"))

    def test_missing_source_module_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            # composite present, fundamental + tradeplan absent
            _write_prev_bundle(d, composite=_prev_composite(),
                               fundamental=None, tradeplan=None)
            with self.assertRaises(rp.PipelineError) as ctx:
                rp.extract_carry_forward(d, "2026-07-21")
            self.assertEqual(ctx.exception.code, rp.EXIT_USAGE)

    def test_missing_carried_field_refuses(self):
        comp = _prev_composite()
        del comp["flags"]["variant"]
        with self.assertRaises(rp.PipelineError) as ctx:
            self._extract(composite=comp)
        self.assertEqual(ctx.exception.code, rp.EXIT_USAGE)

    def test_non_bool_catalyst_in_thesis_refuses(self):
        tp = _prev_tradeplan()
        tp["flags"]["catalyst_in_thesis"] = "yes"   # wrong type on disk
        with self.assertRaises(rp.PipelineError) as ctx:
            self._extract(tradeplan=tp)
        self.assertEqual(ctx.exception.code, rp.EXIT_USAGE)


# --------------------------------------------------------------------------- #
# 2. argv assembly (guards against a scorer flag rename)
# --------------------------------------------------------------------------- #

class TestArgvAssembly(unittest.TestCase):

    def setUp(self):
        with tempfile.TemporaryDirectory() as d:
            _write_prev_bundle(d, composite=_prev_composite(),
                               fundamental=_prev_fundamental(),
                               tradeplan=_prev_tradeplan())
            self.carried = rp.extract_carry_forward(d, "2026-07-21")

    def test_score_composite_argv(self):
        argv = rp.build_score_composite_argv("/b", self.carried, "balanced")
        # exact required flags the scorer asserts
        for flag in ("--bundle", "--scenarios", "--scenario-reasoning", "--variant",
                     "--variant-justification", "--catalyst-clarity",
                     "--catalyst-clarity-justification", "--invalidation",
                     "--invalidation-justification", "--profile"):
            self.assertIn(flag, argv, "missing %s" % flag)
        self.assertEqual(argv[argv.index("--variant") + 1], "some")
        self.assertEqual(argv[argv.index("--profile") + 1], "balanced")
        self.assertEqual(argv[argv.index("--scenarios") + 1],
                         os.path.join("/b", "scenarios.json"))

    def test_trade_plan_stock_argv(self):
        argv = rp.build_trade_plan_stock_argv("/b", self.carried, "balanced")
        self.assertIn("--stock-plan", argv)
        for flag in ("--catalyst-in-thesis", "--catalyst-in-thesis-justification",
                     "--fund-invalidation-metric", "--fund-invalidation-threshold",
                     "--fund-invalidation-justification", "--profile"):
            self.assertIn(flag, argv, "missing %s" % flag)
        self.assertEqual(argv[argv.index("--catalyst-in-thesis") + 1], "no")

    def test_score_fundamental_argv_with_coverage(self):
        with tempfile.TemporaryDirectory() as cov:
            open(os.path.join(cov, "valuation_anchors.json"), "w").write("{}")
            open(os.path.join(cov, "adjusted_financials.json"), "w").write("{}")
            argv = rp.build_score_fundamental_argv("/b", self.carried, cov)
        self.assertIn("--moat", argv)
        self.assertEqual(argv[argv.index("--moat") + 1], "wide")
        self.assertIn("--moat-justification", argv)
        self.assertIn("--anchors", argv)
        self.assertIn("--adjusted", argv)

    def test_score_fundamental_argv_omits_absent_coverage_files(self):
        with tempfile.TemporaryDirectory() as cov:
            # empty coverage dir -> no --anchors / --adjusted appended
            argv = rp.build_score_fundamental_argv("/b", self.carried, cov)
        self.assertNotIn("--anchors", argv)
        self.assertNotIn("--adjusted", argv)


# --------------------------------------------------------------------------- #
# 3. Event gate
# --------------------------------------------------------------------------- #

class TestEventGate(unittest.TestCase):

    def _snap(self, as_of, earnings=None, ex_date=None):
        return {
            "meta": {"as_of_utc": as_of + "T17:00:00Z"},
            "events": {
                "next_earnings": {"date": earnings},
                "dividends": {"ex_date": ex_date},
            },
        }

    def test_earnings_between_runs_flagged(self):
        # prev 07-21, new 07-22, earnings on 07-22 -> (prev, new] contains it
        evt = rp.event_between(self._snap("2026-07-22", earnings="2026-07-22"),
                               "2026-07-21")
        self.assertIsNotNone(evt)
        self.assertTrue(evt["earnings_between"])

    def test_dividend_between_runs_flagged(self):
        evt = rp.event_between(self._snap("2026-07-22", ex_date="2026-07-22"),
                               "2026-07-21")
        self.assertIsNotNone(evt)
        self.assertTrue(evt["dividend_between"])

    def test_no_event_when_dates_outside_interval(self):
        # earnings a week out, dividend already past -> no event between runs
        evt = rp.event_between(
            self._snap("2026-07-22", earnings="2026-07-30", ex_date="2026-06-08"),
            "2026-07-21")
        self.assertIsNone(evt)

    def test_event_on_prev_as_of_is_not_between(self):
        # an event dated the PREVIOUS run was already reflected (half-open left)
        evt = rp.event_between(self._snap("2026-07-22", earnings="2026-07-21"),
                               "2026-07-21")
        self.assertIsNone(evt)


# --------------------------------------------------------------------------- #
# 4. Up-front refusals (EXIT_USAGE)
# --------------------------------------------------------------------------- #

class TestRefusals(unittest.TestCase):

    def _good_previous(self, parent):
        prev = os.path.join(parent, "detail_reports_2026-07-21")
        os.makedirs(prev)
        _write_prev_bundle(prev, composite=_prev_composite(),
                           fundamental=_prev_fundamental(),
                           tradeplan=_prev_tradeplan())
        return prev

    def test_missing_bundle_dir(self):
        with self.assertRaises(rp.PipelineError) as ctx:
            rp.run_pipeline("GOOG", "/no/such/bundle", "/no/such/prev")
        self.assertEqual(ctx.exception.code, rp.EXIT_USAGE)
        self.assertIn("bundle", ctx.exception.message)

    def test_missing_manifest(self):
        with tempfile.TemporaryDirectory() as parent:
            bundle = os.path.join(parent, "detail_reports_2026-07-22")
            os.makedirs(bundle)              # dir exists but no manifest.json
            prev = self._good_previous(parent)
            with self.assertRaises(rp.PipelineError) as ctx:
                rp.run_pipeline("GOOG", bundle, prev)
        self.assertEqual(ctx.exception.code, rp.EXIT_USAGE)
        self.assertIn("manifest", ctx.exception.message)

    def test_missing_previous(self):
        with tempfile.TemporaryDirectory() as parent:
            bundle = os.path.join(parent, "detail_reports_2026-07-22")
            os.makedirs(bundle)
            open(os.path.join(bundle, "manifest.json"), "w").write("{}")
            os.makedirs(os.path.join(parent, "coverage"))
            with self.assertRaises(rp.PipelineError) as ctx:
                rp.run_pipeline("GOOG", bundle, "/no/such/prev")
        self.assertEqual(ctx.exception.code, rp.EXIT_USAGE)
        self.assertIn("previous", ctx.exception.message)

    def test_missing_coverage(self):
        with tempfile.TemporaryDirectory() as parent:
            bundle = os.path.join(parent, "detail_reports_2026-07-22")
            os.makedirs(bundle)
            open(os.path.join(bundle, "manifest.json"), "w").write("{}")
            prev = self._good_previous(parent)   # no coverage/ created
            with self.assertRaises(rp.PipelineError) as ctx:
                rp.run_pipeline("GOOG", bundle, prev)
        self.assertEqual(ctx.exception.code, rp.EXIT_USAGE)
        self.assertIn("coverage", ctx.exception.message)


# --------------------------------------------------------------------------- #
# 5. Gate-failure propagation + event routing through the full run_pipeline
# --------------------------------------------------------------------------- #

class TestPipelineFlow(unittest.TestCase):
    """Drives run_pipeline with the scorers monkeypatched, so we test ordering /
    stop-on-failure / event routing without depending on real raw data."""

    def _stage(self, parent, *, new_earnings=None, new_ex_date=None):
        """Build a valid bundle + previous + coverage under ``parent``. Returns
        (bundle, previous). The snapshot the (patched) build_snapshot 'produces' is
        pre-written so event_between + qc_gate see it."""
        bundle = os.path.join(parent, "detail_reports_2026-07-22")
        os.makedirs(bundle)
        open(os.path.join(bundle, "manifest.json"), "w").write(
            json.dumps({"ticker": "GOOG"}))
        os.makedirs(os.path.join(parent, "coverage"))
        # the snapshot build_snapshot would emit (patched to a no-op that trusts it)
        with open(os.path.join(bundle, "snapshot_GOOG_2026-07-22.json"), "w") as fh:
            json.dump({"meta": {"as_of_utc": "2026-07-22T17:00:00Z"},
                       "events": {"next_earnings": {"date": new_earnings},
                                  "dividends": {"ex_date": new_ex_date}}}, fh)
        prev = os.path.join(parent, "detail_reports_2026-07-21")
        os.makedirs(prev)
        _write_prev_bundle(prev, composite=_prev_composite(),
                           fundamental=_prev_fundamental(),
                           tradeplan=_prev_tradeplan())
        return bundle, prev

    def _patch_all_ok(self, calls):
        """Return a dict mapping each scorer module to a recording no-op main()."""
        def make(name, rc=0):
            def _main(argv):
                calls.append(name)
                return rc
            return _main
        return make

    def _apply_patches(self, monkeypatch_targets, calls, fail=None):
        """Patch every scorer's main to a recorder; ``fail`` names a step to fail."""
        recorders = {}
        for name, mod in monkeypatch_targets:
            rc = 1 if name == fail else 0
            recorders[name] = (mod, mod.main)

            def _make(nm, code):
                def _main(argv):
                    calls.append(nm)
                    return code
                return _main
            mod.main = _make(name, rc)
        return recorders

    def _restore(self, recorders):
        for _name, (mod, orig) in recorders.items():
            mod.main = orig

    STEP_MODS = None  # set in setUp

    def setUp(self):
        from scripts import (build_snapshot, qc_gate, score_technical,
                             score_sentiment, score_risk, score_fundamental,
                             score_composite, trade_plan, options_strategy,
                             valuation_reconcile, decision_contract, report_qc)
        # order mirrors the pipeline; trade_plan appears once (both passes share main)
        self.STEP_MODS = [
            ("build_snapshot", build_snapshot),
            ("qc_gate", qc_gate),
            ("score_technical", score_technical),
            ("score_sentiment", score_sentiment),
            ("score_risk", score_risk),
            ("score_fundamental", score_fundamental),
            ("score_composite", score_composite),
            ("trade_plan", trade_plan),
            ("options_strategy", options_strategy),
            ("valuation_reconcile", valuation_reconcile),
            ("decision_contract", decision_contract),
            ("report_qc", report_qc),
        ]

    def test_no_event_runs_full_chain(self):
        calls = []
        with tempfile.TemporaryDirectory() as parent:
            bundle, prev = self._stage(parent, new_earnings="2026-08-15",
                                       new_ex_date="2026-06-08")
            recorders = self._apply_patches(self.STEP_MODS, calls)
            # decision_contract's recorder writes no file; pre-seed module_decision
            with open(os.path.join(bundle, "module_decision.json"), "w") as fh:
                json.dump({"contract_version": "2.0.0", "action_unowned": "HOLD"}, fh)
            try:
                path, decision = rp.run_pipeline("GOOG", bundle, prev)
            finally:
                self._restore(recorders)
        self.assertTrue(path.endswith("module_decision.json"))
        self.assertEqual(decision["contract_version"], "2.0.0")
        # every step ran, build_snapshot first, report_qc last
        self.assertEqual(calls[0], "build_snapshot")
        self.assertEqual(calls[-1], "report_qc")
        self.assertIn("options_strategy", calls)

    def test_event_routes_to_model_exit_4(self):
        calls = []
        with tempfile.TemporaryDirectory() as parent:
            # earnings on the new as_of -> between (prev 07-21, new 07-22]
            bundle, prev = self._stage(parent, new_earnings="2026-07-22")
            recorders = self._apply_patches(self.STEP_MODS, calls)
            try:
                with self.assertRaises(rp.PipelineError) as ctx:
                    rp.run_pipeline("GOOG", bundle, prev)
            finally:
                self._restore(recorders)
        self.assertEqual(ctx.exception.code, rp.EXIT_EVENT)
        # build_snapshot ran (event needs the new snapshot); NOTHING after qc ran
        self.assertEqual(calls, ["build_snapshot"])

    def test_gate_failure_stops_chain_exit_3(self):
        calls = []
        with tempfile.TemporaryDirectory() as parent:
            bundle, prev = self._stage(parent, new_earnings="2026-08-15")
            # fail score_risk -> chain must stop there
            recorders = self._apply_patches(self.STEP_MODS, calls, fail="score_risk")
            try:
                with self.assertRaises(rp.PipelineError) as ctx:
                    rp.run_pipeline("GOOG", bundle, prev)
            finally:
                self._restore(recorders)
        self.assertEqual(ctx.exception.code, rp.EXIT_GATE)
        # ran up to and INCLUDING score_risk; nothing after it
        self.assertIn("score_risk", calls)
        self.assertNotIn("score_fundamental", calls)
        self.assertNotIn("score_composite", calls)

    def test_module_context_carried_with_disclosure(self):
        calls = []
        with tempfile.TemporaryDirectory() as parent:
            bundle, prev = self._stage(parent, new_earnings="2026-08-15")
            recorders = self._apply_patches(self.STEP_MODS, calls)
            with open(os.path.join(bundle, "module_decision.json"), "w") as fh:
                json.dump({"contract_version": "2.0.0"}, fh)
            try:
                rp.run_pipeline("GOOG", bundle, prev)
            finally:
                self._restore(recorders)
            # scenarios + context copied into the NEW bundle
            self.assertTrue(os.path.isfile(os.path.join(bundle, "scenarios.json")))
            with open(os.path.join(bundle, "module_context.json")) as fh:
                ctx = json.load(fh)
            self.assertEqual(ctx["carried_forward_from"], "2026-07-21")
            self.assertEqual([f["id"] for f in ctx["findings"]], ["C4", "C5"])
            # the carried copy MUST carry a top-level schema_version (written via
            # emit_json) so the decision-gates' schema_version_present check passes
            # -- the previous context module predates that requirement.
            self.assertIn("schema_version", ctx)


class TestCopyAuthoredFiles(unittest.TestCase):
    """Direct unit tests for the verbatim-artifact carry-forward."""

    def test_context_copy_is_schema_stamped_and_disclosed(self):
        with tempfile.TemporaryDirectory() as prev, \
                tempfile.TemporaryDirectory() as new:
            _write_prev_bundle(prev, composite=_prev_composite(),
                               fundamental=_prev_fundamental(),
                               tradeplan=_prev_tradeplan())
            # the source context (as authored) carries NO schema_version
            with open(os.path.join(prev, "module_context.json")) as fh:
                src = json.load(fh)
            self.assertNotIn("schema_version", src)

            rp.copy_authored_files(prev, new, "2026-07-21")
            with open(os.path.join(new, "module_context.json")) as fh:
                copied = json.load(fh)
        self.assertEqual(copied["schema_version"], "1.0.0")
        self.assertEqual(copied["carried_forward_from"], "2026-07-21")
        # findings order preserved (sort_keys=False)
        self.assertEqual([f["id"] for f in copied["findings"]], ["C4", "C5"])

    def test_missing_scenarios_refuses(self):
        with tempfile.TemporaryDirectory() as prev, \
                tempfile.TemporaryDirectory() as new:
            _write_prev_bundle(prev, composite=_prev_composite(),
                               fundamental=_prev_fundamental(),
                               tradeplan=_prev_tradeplan(), scenarios=False)
            with self.assertRaises(rp.PipelineError) as ctx:
                rp.copy_authored_files(prev, new, "2026-07-21")
        self.assertEqual(ctx.exception.code, rp.EXIT_USAGE)
        self.assertIn("scenarios.json", ctx.exception.message)

    def test_absent_context_is_allowed(self):
        # the compressed / FSI-absent floor has no context module -> no error, no copy
        with tempfile.TemporaryDirectory() as prev, \
                tempfile.TemporaryDirectory() as new:
            _write_prev_bundle(prev, composite=_prev_composite(),
                               fundamental=_prev_fundamental(),
                               tradeplan=_prev_tradeplan(), context=False)
            rp.copy_authored_files(prev, new, "2026-07-21")
            self.assertTrue(os.path.isfile(os.path.join(new, "scenarios.json")))
            self.assertFalse(os.path.isfile(
                os.path.join(new, "module_context.json")))


# --------------------------------------------------------------------------- #
# 6. No-render invariant + --emit validation
# --------------------------------------------------------------------------- #

class TestNoRenderInvariant(unittest.TestCase):

    def test_render_modules_not_imported_at_module_top(self):
        # run_pipeline must never pull the renderers into its namespace.
        for forbidden in ("render_report", "render_charts", "render_pdf"):
            self.assertNotIn(forbidden, vars(rp),
                             "run_pipeline must not import %s" % forbidden)

    def test_source_has_no_render_calls(self):
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "run_pipeline.py")
        with open(src_path) as fh:
            src = fh.read()
        for forbidden in ("render_report", "render_charts", "render_pdf"):
            # allowed only inside the explanatory 'render nothing' prose, never as
            # an import or call. Assert no import/attribute usage form appears.
            self.assertNotIn("import %s" % forbidden, src)
            self.assertNotIn("%s.main" % forbidden, src)

    def test_emit_must_be_json(self):
        rc = rp.main(["GOOG", "--bundle", "/x", "--previous", "/y", "--emit", "csv"])
        self.assertEqual(rc, rp.EXIT_USAGE)

    def test_ticker_required(self):
        rc = rp.main(["--bundle", "/x", "--previous", "/y", "--emit", "json"])
        self.assertEqual(rc, rp.EXIT_USAGE)

    def test_positional_ticker_alias(self):
        # positional TICKER should satisfy the ticker requirement; refusal then
        # comes from the (nonexistent) bundle, proving the ticker was accepted.
        rc = rp.main(["GOOG", "--bundle", "/no/such", "--previous", "/no/such",
                      "--emit", "json"])
        self.assertEqual(rc, rp.EXIT_USAGE)   # bundle refusal, not ticker refusal


if __name__ == "__main__":
    unittest.main()
