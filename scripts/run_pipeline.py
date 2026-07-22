#!/usr/bin/env python3
"""Headless, deterministic NO-EVENT re-score pipeline (FR-4).

WHY -- the LLM out of the mechanical control path
==================================================
A downstream orchestrator runs cheap, scheduled RE-SCORES of an existing ticker
workspace inside a locked-down sandbox and wants the model OUT of the loop for the
purely mechanical pipeline. The scoring scripts are all deterministic, but several
of them REQUIRE model-authored inputs that no amount of arithmetic can invent:

  - scenarios.json (the bull/base/bear probability set),
  - the composite conviction flags (variant / catalyst-clarity / invalidation),
  - the trade-plan flags (catalyst-in-thesis + the fundamental-invalidation leg),
  - the fundamental moat judgment,
  - module_context.json (the company-context findings the conviction cites).

On a NO-EVENT refresh those judgments are still valid, so they are CARRIED FORWARD
verbatim from the previous bundle (the refresh design already established this).
On an EVENT (earnings or a dividend ex-date fell BETWEEN the two runs) the print
changes the facts the judgments rest on, so they MUST be re-derived by the model --
that path is NOT headless. This CLI therefore implements exactly the *no-event
deterministic re-score*: it copies the model-authored inputs forward from
``--previous``, runs the deterministic scorer chain in-process, honours every
blocking gate, emits the decision contract as JSON, and renders NOTHING.

The event gate is the safety valve: if an earnings/dividend event is detected
between the previous run's as-of and the new snapshot's as-of, the CLI refuses to
carry judgments across it and exits with a distinct code so the caller routes the
refresh to the model path instead.

Determinism: the CLI invents no timestamps of its own -- every scorer stamps its
own as_of from the snapshot -- so identical inputs yield byte-identical outputs.

stdlib-only; the deterministic scorers are called in-process via their argparse
``main([...])`` entry points (return code checked; a nonzero return stops the chain).
"""

import argparse
import glob
import json
import os
import shutil
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Ensure the repo root is importable when run directly (``-m scripts.run_pipeline``
# already has it; a bare ``python3 scripts/run_pipeline.py`` needs the insert).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# The deterministic scorer chain, each a ``main(argv)`` argparse CLI. Imported by
# module so tests can monkeypatch a single ``.main`` to simulate a gate failure.
# NOTE (no-render invariant): NONE of render_report / render_charts / render_pdf is
# imported here, by design -- the headless re-score path produces data, never a
# rendered artifact. The test suite asserts these names are absent from this module.
from scripts import build_snapshot          # noqa: E402
from scripts import qc_gate                 # noqa: E402
from scripts import score_technical         # noqa: E402
from scripts import score_sentiment         # noqa: E402
from scripts import score_risk              # noqa: E402
from scripts import score_fundamental       # noqa: E402
from scripts import score_composite         # noqa: E402
from scripts import trade_plan              # noqa: E402
from scripts import options_strategy        # noqa: E402
from scripts import valuation_reconcile     # noqa: E402
from scripts import decision_contract       # noqa: E402
from scripts import report_qc               # noqa: E402
# Event detection is REUSED wholesale from the refresh planner so the headless path
# and the planner can never disagree on what "an event fell between runs" means.
from scripts import refresh_plan            # noqa: E402
# The canonical artifact writer: stamps a top-level schema_version (FR-3 §5). The
# carried context module MUST be re-emitted through this (not a raw json.dump) so it
# acquires the schema_version the decision-gates require -- exactly how the live
# pipeline's report_qc --context stamps it.
from scripts._artifact import emit_json     # noqa: E402


# --------------------------------------------------------------------------- #
# Exit codes (the CLI contract)
# --------------------------------------------------------------------------- #
EXIT_OK = 0             # success -- decision contract emitted, gates passed
EXIT_USAGE = 2          # usage / missing-input refusal (checked up front)
EXIT_GATE = 3           # a pipeline step OR a blocking gate failed -- chain stopped
EXIT_EVENT = 4          # earnings/dividend between runs -> route to the model path


class PipelineError(Exception):
    """A refusal or a step/gate failure. ``code`` is the process exit code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Small IO helpers (pure)
# --------------------------------------------------------------------------- #

def _load_json(path):
    """Parse a JSON file to a dict, or None on any read/parse failure."""
    try:
        with open(path) as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _carry_tag(prev_as_of):
    """The disclosure suffix appended to every carried-forward justification."""
    return " [carried forward from %s]" % (prev_as_of or "previous run")


def _tagged(justification, prev_as_of):
    """Append the carry-forward disclosure to a justification string.

    A missing/blank justification is left blank (the scorer's own required-flag gate
    will then reject it -- we never fabricate justification text)."""
    if not justification:
        return justification
    return "%s%s" % (justification, _carry_tag(prev_as_of))


def _find_snapshot(bundle):
    """Path to the newest ``snapshot_*.json`` in ``bundle``, or None."""
    matches = sorted(glob.glob(os.path.join(bundle, "snapshot_*.json")))
    return matches[-1] if matches else None


def _coverage_dir(bundle):
    """Resolve the coverage directory for a bundle, or None.

    Accepts BOTH the canonical SIBLING layout (``<B>/../coverage/`` -- the bundle is
    ``trading_desk_<T>/detail_reports_*`` and coverage is ``trading_desk_<T>/coverage``,
    matching trade_plan._find_valuation_anchors / score_fundamental) and a nested
    ``<B>/coverage/`` fallback. Sibling wins when both exist."""
    sibling = os.path.join(os.path.dirname(os.path.abspath(bundle)), "coverage")
    if os.path.isdir(sibling):
        return sibling
    nested = os.path.join(bundle, "coverage")
    if os.path.isdir(nested):
        return nested
    return None


def _previous_as_of(prev_bundle):
    """The previous run's as-of date (YYYY-MM-DD), from its snapshot or a module.

    Preference order: the previous snapshot's ``meta.as_of_utc`` (the canonical
    stamp), then any scored module's ``as_of`` (composite first). None only when the
    previous bundle carries neither (a malformed previous bundle)."""
    snap_path = _find_snapshot(prev_bundle)
    if snap_path is not None:
        snap = _load_json(snap_path) or {}
        as_of = (snap.get("meta") or {}).get("as_of_utc")
        if isinstance(as_of, str) and len(as_of) >= 10:
            return as_of[:10]
    for name in ("module_composite.json", "module_fundamental.json",
                 "module_tradeplan.json"):
        mod = _load_json(os.path.join(prev_bundle, name))
        if mod and isinstance(mod.get("as_of"), str):
            return mod["as_of"][:10]
    return None


# --------------------------------------------------------------------------- #
# Carry-forward extraction (the crux -- a deterministic, pure field copy + tag)
# --------------------------------------------------------------------------- #

def extract_carry_forward(prev_bundle, prev_as_of):
    """Extract the model-authored judgment inputs from the PREVIOUS bundle.

    Returns a dict of the carried values (already tagged with the carry-forward
    disclosure on every justification). Pure over the previous bundle's module
    JSONs -- no re-derivation, only a field copy + tag. A missing source module or
    field surfaces as a PipelineError (EXIT_USAGE): the previous bundle is not a
    valid re-score base.

    Field map (source path in the previous bundle -> carried value):
      module_composite.json .flags.variant                        -> variant
      module_composite.json .flags.variant_justification          -> variant_justification (tagged)
      module_composite.json .flags.catalyst_clarity               -> catalyst_clarity
      module_composite.json .flags.catalyst_clarity_justification -> catalyst_clarity_justification (tagged)
      module_composite.json .flags.invalidation                   -> invalidation
      module_composite.json .flags.invalidation_justification     -> invalidation_justification (tagged)
      module_composite.json .ev.scenario_reasoning                -> scenario_reasoning (tagged)
      module_composite.json .profile                              -> profile (fallback default)
      module_fundamental.json .flags.moat                         -> moat
      module_fundamental.json .flags.moat_justification           -> moat_justification (tagged)
      module_tradeplan.json .flags.catalyst_in_thesis (bool)      -> catalyst_in_thesis ("yes"/"no")
      module_tradeplan.json .flags.catalyst_in_thesis_justification -> catalyst_in_thesis_justification (tagged)
      module_tradeplan.json .flags.fund_invalidation_metric       -> fund_invalidation_metric
      module_tradeplan.json .flags.fund_invalidation_threshold    -> fund_invalidation_threshold
      module_tradeplan.json .flags.fund_invalidation_justification -> fund_invalidation_justification (tagged)
    """
    composite = _load_json(os.path.join(prev_bundle, "module_composite.json"))
    fundamental = _load_json(os.path.join(prev_bundle, "module_fundamental.json"))
    tradeplan = _load_json(os.path.join(prev_bundle, "module_tradeplan.json"))

    for name, mod in (("module_composite.json", composite),
                      ("module_fundamental.json", fundamental),
                      ("module_tradeplan.json", tradeplan)):
        if mod is None:
            raise PipelineError(
                EXIT_USAGE,
                "previous bundle is missing %s -- it is not a valid re-score base "
                "(a headless re-score carries the model's judgments forward from a "
                "fully-scored previous bundle)." % name)

    c_flags = composite.get("flags") or {}
    c_ev = composite.get("ev") or {}
    f_flags = fundamental.get("flags") or {}
    t_flags = tradeplan.get("flags") or {}

    def _require(mapping, key, source):
        val = mapping.get(key)
        if val in (None, ""):
            raise PipelineError(
                EXIT_USAGE,
                "previous bundle %s is missing the carried field %r -- cannot "
                "re-score headlessly without it." % (source, key))
        return val

    # module_composite.flags.catalyst_in_thesis is stored as a BOOL in tradeplan;
    # here composite flags are plain strings from the scorer's choices.
    variant = _require(c_flags, "variant", "module_composite.flags")
    catalyst_clarity = _require(c_flags, "catalyst_clarity", "module_composite.flags")
    invalidation = _require(c_flags, "invalidation", "module_composite.flags")
    scenario_reasoning = _require(c_ev, "scenario_reasoning", "module_composite.ev")

    moat = _require(f_flags, "moat", "module_fundamental.flags")

    # trade-plan stores catalyst_in_thesis as a BOOL -> back to the yes|no flag.
    cit_bool = t_flags.get("catalyst_in_thesis")
    if not isinstance(cit_bool, bool):
        raise PipelineError(
            EXIT_USAGE,
            "previous module_tradeplan.flags.catalyst_in_thesis is not a bool -- "
            "cannot map it back to the yes|no re-score flag.")
    catalyst_in_thesis = "yes" if cit_bool else "no"

    return {
        # composite conviction flags
        "variant": variant,
        "variant_justification": _tagged(
            _require(c_flags, "variant_justification", "module_composite.flags"),
            prev_as_of),
        "catalyst_clarity": catalyst_clarity,
        "catalyst_clarity_justification": _tagged(
            _require(c_flags, "catalyst_clarity_justification",
                     "module_composite.flags"),
            prev_as_of),
        "invalidation": invalidation,
        "invalidation_justification": _tagged(
            _require(c_flags, "invalidation_justification",
                     "module_composite.flags"),
            prev_as_of),
        "scenario_reasoning": _tagged(scenario_reasoning, prev_as_of),
        # profile (fallback when --profile is not passed)
        "profile": composite.get("profile") or "balanced",
        # fundamental moat
        "moat": moat,
        "moat_justification": _tagged(
            _require(f_flags, "moat_justification", "module_fundamental.flags"),
            prev_as_of),
        # trade-plan flags
        "catalyst_in_thesis": catalyst_in_thesis,
        "catalyst_in_thesis_justification": _tagged(
            _require(t_flags, "catalyst_in_thesis_justification",
                     "module_tradeplan.flags"),
            prev_as_of),
        "fund_invalidation_metric": _require(
            t_flags, "fund_invalidation_metric", "module_tradeplan.flags"),
        "fund_invalidation_threshold": _require(
            t_flags, "fund_invalidation_threshold", "module_tradeplan.flags"),
        "fund_invalidation_justification": _tagged(
            _require(t_flags, "fund_invalidation_justification",
                     "module_tradeplan.flags"),
            prev_as_of),
    }


def copy_authored_files(prev_bundle, new_bundle, prev_as_of):
    """Copy the two verbatim model-authored artifacts forward into ``new_bundle``.

    - scenarios.json: copied byte-for-byte (the bull/base/bear probability set).
    - module_context.json: copied, then a top-level ``carried_forward_from`` key is
      added so the copy DISCLOSES that its findings/live_tape were carried (not
      re-authored). The findings' C-IDs are preserved verbatim so the composite /
      fundamental context-grounding gates resolve the SAME C-ids the carried
      conviction justifications cite. It is written through emit_json so it gains the
      top-level ``schema_version`` the decision-gates require (mirroring how the live
      pipeline's report_qc --context stamps the produced context artifact).

    A missing scenarios.json is a hard refusal (the composite requires it). A
    missing module_context.json is allowed (the compressed / FSI-absent floor has no
    context module; the grounding gate is a no-op when it is absent)."""
    src_scen = os.path.join(prev_bundle, "scenarios.json")
    if not os.path.isfile(src_scen):
        raise PipelineError(
            EXIT_USAGE,
            "previous bundle is missing scenarios.json -- the composite layer "
            "requires the carried scenario set.")
    shutil.copyfile(src_scen, os.path.join(new_bundle, "scenarios.json"))

    src_ctx = os.path.join(prev_bundle, "module_context.json")
    if os.path.isfile(src_ctx):
        ctx = _load_json(src_ctx)
        if ctx is None:
            raise PipelineError(
                EXIT_USAGE,
                "previous module_context.json is unreadable -- cannot carry the "
                "company-context findings forward.")
        # Disclose the carry-forward on the copy (top-level, non-destructive), then
        # write via emit_json so the on-disk copy carries schema_version. sort_keys
        # is False so the finding order (C1..Cn) and live_tape prose are preserved
        # exactly as authored (the grounding gate reads C-IDs positionally-agnostic,
        # but keeping the author's order avoids a gratuitous re-ordering of the copy).
        ctx["carried_forward_from"] = prev_as_of
        emit_json(ctx, os.path.join(new_bundle, "module_context.json"),
                  sort_keys=False)


# --------------------------------------------------------------------------- #
# argv assembly (pure -- one builder per scorer, so a flag rename fails a test)
# --------------------------------------------------------------------------- #

def _anchors_arg(coverage_dir):
    """``[--anchors <coverage>/valuation_anchors.json]`` when the file exists, else []."""
    if coverage_dir is None:
        return []
    path = os.path.join(coverage_dir, "valuation_anchors.json")
    return ["--anchors", path] if os.path.isfile(path) else []


def _adjusted_arg(coverage_dir):
    """``[--adjusted <coverage>/adjusted_financials.json]`` when it exists, else []."""
    if coverage_dir is None:
        return []
    path = os.path.join(coverage_dir, "adjusted_financials.json")
    return ["--adjusted", path] if os.path.isfile(path) else []


def build_score_fundamental_argv(bundle, carried, coverage_dir):
    return (["--bundle", bundle,
             "--moat", carried["moat"],
             "--moat-justification", carried["moat_justification"]]
            + _anchors_arg(coverage_dir)
            + _adjusted_arg(coverage_dir))


def build_score_composite_argv(bundle, carried, profile):
    scenarios = os.path.join(bundle, "scenarios.json")
    return ["--bundle", bundle,
            "--scenarios", scenarios,
            "--scenario-reasoning", carried["scenario_reasoning"],
            "--variant", carried["variant"],
            "--variant-justification", carried["variant_justification"],
            "--catalyst-clarity", carried["catalyst_clarity"],
            "--catalyst-clarity-justification",
            carried["catalyst_clarity_justification"],
            "--invalidation", carried["invalidation"],
            "--invalidation-justification", carried["invalidation_justification"],
            "--profile", profile]


def build_trade_plan_stock_argv(bundle, carried, profile):
    return ["--bundle", bundle,
            "--stock-plan",
            "--catalyst-in-thesis", carried["catalyst_in_thesis"],
            "--catalyst-in-thesis-justification",
            carried["catalyst_in_thesis_justification"],
            "--fund-invalidation-metric", carried["fund_invalidation_metric"],
            "--fund-invalidation-threshold", carried["fund_invalidation_threshold"],
            "--fund-invalidation-justification",
            carried["fund_invalidation_justification"],
            "--profile", profile]


# --------------------------------------------------------------------------- #
# Event gate (route events to the model, REUSING refresh_plan's logic)
# --------------------------------------------------------------------------- #

def event_between(new_snapshot, prev_as_of_str):
    """Return an event descriptor dict when an earnings/dividend fell BETWEEN the
    previous run and the new snapshot's as-of, else None.

    REUSES refresh_plan._event_between / refresh_plan._parse_date verbatim so the
    headless gate and the planner agree exactly. The new snapshot supplies both the
    new as-of (meta.as_of_utc) and the event dates (events.next_earnings.date /
    events.dividends.ex_date), so this is evaluated AFTER build_snapshot."""
    meta = new_snapshot.get("meta") or {}
    as_of = refresh_plan._parse_date(meta.get("as_of_utc"))
    prev_as_of = refresh_plan._parse_date(prev_as_of_str)

    events = new_snapshot.get("events") or {}
    ne = events.get("next_earnings") or {}
    earnings_date = ne.get("date") if isinstance(ne, dict) else None
    dividends = events.get("dividends") or {}
    ex_date = dividends.get("ex_date") if isinstance(dividends, dict) else None

    earnings_between = refresh_plan._event_between(earnings_date, prev_as_of, as_of)
    dividend_between = refresh_plan._event_between(ex_date, prev_as_of, as_of)

    if not (earnings_between or dividend_between):
        return None
    return {
        "earnings_between": earnings_between,
        "earnings_date": earnings_date,
        "dividend_between": dividend_between,
        "dividend_ex_date": ex_date,
    }


# --------------------------------------------------------------------------- #
# Step runner
# --------------------------------------------------------------------------- #

def _run_step(label, fn, argv):
    """Call a scorer's ``main(argv)``; raise PipelineError(EXIT_GATE) on nonzero.

    The chain STOPS at the first nonzero return -- a failed step or a blocking gate
    must not let later steps run over a poisoned bundle."""
    rc = fn(argv)
    if rc != 0:
        raise PipelineError(
            EXIT_GATE,
            "step %r failed (exit %d): argv=%s" % (label, rc, " ".join(argv)))


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def run_pipeline(ticker, bundle, previous, profile=None):
    """Execute the no-event deterministic re-score. Returns the decision dict.

    Raises PipelineError with the appropriate exit code on any refusal, event, or
    gate/step failure. All up-front refusals are checked before any scorer runs."""
    ticker = ticker.upper()

    # -- up-front refusals (EXIT_USAGE) ------------------------------------
    if not bundle or not os.path.isdir(bundle):
        raise PipelineError(
            EXIT_USAGE, "--bundle must be an existing directory: %r" % bundle)
    if not os.path.isfile(os.path.join(bundle, "manifest.json")):
        raise PipelineError(
            EXIT_USAGE,
            "no manifest.json in --bundle %r -- the raw Alpha Vantage bundle has "
            "not been fetched; a headless re-score cannot fetch." % bundle)
    if not previous or not os.path.isdir(previous):
        raise PipelineError(
            EXIT_USAGE,
            "--previous must be an existing bundle directory: %r. A first-ever / "
            "initiate run has no prior judgments to carry forward and needs the "
            "model; this CLI is re-score-only." % previous)
    coverage_dir = _coverage_dir(bundle)
    if coverage_dir is None:
        raise PipelineError(
            EXIT_USAGE,
            "no coverage directory found (neither %s/coverage nor %s sibling) -- "
            "the anchored valuation inputs are required for a re-score."
            % (bundle, os.path.join(os.path.dirname(os.path.abspath(bundle)),
                                    "coverage")))

    prev_as_of = _previous_as_of(previous)

    # -- carry-forward extraction (pure, before any step runs) -------------
    carried = extract_carry_forward(previous, prev_as_of)
    profile = profile or carried["profile"]
    copy_authored_files(previous, bundle, prev_as_of)

    # -- 1. build_snapshot (pure over the pre-fetched manifest + raw/) -----
    _run_step("build_snapshot", build_snapshot.main,
              ["--bundle", bundle, "--ticker", ticker])

    snap_path = _find_snapshot(bundle)
    if snap_path is None:
        raise PipelineError(
            EXIT_GATE, "build_snapshot produced no snapshot_*.json in %r" % bundle)
    new_snapshot = _load_json(snap_path) or {}

    # -- event gate (AFTER build_snapshot; EXIT_EVENT) ---------------------
    evt = event_between(new_snapshot, prev_as_of)
    if evt is not None:
        which = []
        if evt["earnings_between"]:
            which.append("earnings (%s)" % evt["earnings_date"])
        if evt["dividend_between"]:
            which.append("dividend ex-date (%s)" % evt["dividend_ex_date"])
        raise PipelineError(
            EXIT_EVENT,
            "event (%s) between runs (%s -> %s) -- headless carry-forward is unsafe "
            "across an event; route to the model refresh path."
            % (" & ".join(which), prev_as_of,
               (new_snapshot.get("meta") or {}).get("as_of_utc", "?")[:10]))

    # -- 2. qc_gate (blocking snapshot gate) -------------------------------
    _run_step("qc_gate", qc_gate.main, [snap_path])

    # -- 3. evidence scorers (pure; judgment flags omitted) ----------------
    _run_step("score_technical", score_technical.main, ["--bundle", bundle])
    _run_step("score_sentiment", score_sentiment.main, ["--bundle", bundle])

    # -- 4. score_risk (anchored when coverage anchors exist) --------------
    _run_step("score_risk", score_risk.main,
              ["--bundle", bundle] + _anchors_arg(coverage_dir))

    # -- 5. score_fundamental (moat carried; anchored + adjusted) ----------
    _run_step("score_fundamental", score_fundamental.main,
              build_score_fundamental_argv(bundle, carried, coverage_dir))

    # -- 6. score_composite (conviction flags carried) ---------------------
    _run_step("score_composite", score_composite.main,
              build_score_composite_argv(bundle, carried, profile))

    # -- 7. trade_plan --stock-plan (trade-plan flags carried) -------------
    _run_step("trade_plan:stock", trade_plan.main,
              build_trade_plan_stock_argv(bundle, carried, profile))

    # -- 8. options_strategy --mode pipeline -------------------------------
    _run_step("options_strategy", options_strategy.main,
              ["--bundle", bundle, "--mode", "pipeline"])

    # -- 9. trade_plan --synthesize ----------------------------------------
    _run_step("trade_plan:synthesize", trade_plan.main,
              ["--bundle", bundle, "--synthesize"])

    # -- 10. valuation_reconcile -------------------------------------------
    _run_step("valuation_reconcile", valuation_reconcile.main, ["--bundle", bundle])

    # -- 11. decision_contract (writes module_decision.json @ 2.0.0) -------
    _run_step("decision_contract", decision_contract.main, ["--bundle", bundle])

    # -- 12. report_qc --decision-gates (blocking) -------------------------
    _run_step("report_qc:decision-gates", report_qc.main,
              ["--bundle", bundle, "--decision-gates"])

    decision_path = os.path.join(bundle, "module_decision.json")
    decision = _load_json(decision_path)
    if decision is None:
        raise PipelineError(
            EXIT_GATE, "decision_contract produced no readable module_decision.json")
    return decision_path, decision


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Headless deterministic NO-EVENT re-score (FR-4): carry the "
                    "model's judgments forward from --previous, run the "
                    "deterministic scorer chain + blocking gates, emit the decision "
                    "contract JSON, render nothing. Refuses first-ever runs and "
                    "refuses to carry judgments across an earnings/dividend event.")
    parser.add_argument("ticker_pos", nargs="?", default=None, metavar="TICKER",
                        help="ticker symbol (positional alias for --ticker)")
    parser.add_argument("--ticker", default=None, help="ticker symbol")
    parser.add_argument("--bundle", default=None,
                        help="NEW bundle directory (pre-fetched: manifest.json + raw/)")
    parser.add_argument("--previous", default=None,
                        help="PREVIOUS scored bundle directory (carry-forward source)")
    parser.add_argument("--profile", default=None,
                        help="scoring profile (default: previous composite's profile)")
    parser.add_argument("--emit", default=None, required=True,
                        help="emit mode (only 'json' is supported)")
    args = parser.parse_args(argv)

    ticker = args.ticker or args.ticker_pos
    if not ticker:
        print("ERROR: a ticker is required (positional TICKER or --ticker).",
              file=sys.stderr)
        return EXIT_USAGE
    if args.emit != "json":
        print("ERROR: --emit only supports 'json' (got %r)." % args.emit,
              file=sys.stderr)
        return EXIT_USAGE

    try:
        decision_path, decision = run_pipeline(
            ticker, args.bundle, args.previous, profile=args.profile)
    except PipelineError as exc:
        print("ERROR: %s" % exc.message, file=sys.stderr)
        return exc.code

    # Success: the decision path on its own line, then the decision JSON to stdout
    # so a caller can capture the contract without re-reading the file.
    print(decision_path)
    print(json.dumps(decision, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
