"""Valuation reconciliation (O17) for the trading-desk plugin.

WHY THIS MODULE EXISTS: an FSI initiation's DCF and comps rails frequently disagree
(GOOG: dcf_base 145.47 vs comps_mid 365 -> 0.8601 disagreement, using the average
denominator score_fundamental discloses). Today the
fundamental scorer already WIDENS its valuation band and haircuts confidence on
that split, and the decision contract recomputes the SAME disagreement to fire
VALUATION_MODEL_CONFLICT. This module is the DISCLOSURE-and-GOVERN companion:

  * ``disagreement`` / ``disagreement_state`` classify the split into a small state
    machine (CONSISTENT / UNRESOLVED_CONFLICT / MODEL_INVALID / None) reusing the
    module_fundamental valuation ``inputs.anchors`` and the SAME 0.25 edge the
    scorer and the contract use — one number, one edge, three consumers.
  * ``reverse_dcf`` solves the implied terminal growth g* that makes the DCF equal
    the CURRENT price, holding the transcribed FCF/WACC fixed. This is a DISCLOSURE
    (what perpetual growth the market is pricing), NOT a new price target — the
    bull/base/bear fan, EV, and trade plan are unchanged (spec O17: augment).

The CLI reads a scored bundle + the optional (cited, transcribed)
``coverage/scenario_drivers.json`` and writes ``module_valuation_reconcile.json``
carrying the state, the reverse-DCF, and a passthrough of the driver scenarios.
Both effects degrade gracefully: absent anchors -> state None; absent
scenario_drivers -> state still emitted (from fundamental) but reverse_dcf and
scenarios None. No number is invented — every value is transcribed (cited) or
computed from transcribed inputs.

stdlib-only; the reconciliation functions are pure over already-parsed inputs.
"""

import argparse
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

SKILL = "valuation-reconcile"
RECONCILE_VERSION = "1.0.0"

# The disagreement edge above which the DCF-vs-comps split is UNRESOLVED. This is
# the SAME 0.25 edge score_fundamental widens on and decision_contract fires
# VALUATION_MODEL_CONFLICT on (decision_contract._VALUATION_DISAGREEMENT_TOL) —
# kept in lockstep by value so all three consumers classify one number identically.
_DISAGREEMENT_TOL = 0.25

# State machine values (module-level so they are greppable + ratifiable).
STATE_CONSISTENT = "CONSISTENT"
STATE_UNRESOLVED = "UNRESOLVED_CONFLICT"
STATE_MODEL_INVALID = "MODEL_INVALID"


# --------------------------------------------------------------------------- #
# Local helpers (kept independent so this module is importable standalone).
# --------------------------------------------------------------------------- #

def _dig(obj, *path):
    """Follow a key path through nested dicts; None if any hop is absent."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _num(x):
    """Return x as a float when it is a real (non-bool) number, else None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


def _valuation_anchors(fundamental):
    """The valuation subscore ``inputs.anchors`` dict from module_fundamental, or
    None. Mirrors decision_contract._valuation_subscore's traversal so both read
    the SAME leaf."""
    if not isinstance(fundamental, dict):
        return None
    for sub in fundamental.get("subscores") or []:
        if isinstance(sub, dict) and sub.get("name") == "valuation":
            return _dig(sub, "inputs", "anchors")
    return None


# --------------------------------------------------------------------------- #
# Disagreement + state machine (reuse the fundamental anchors + the 0.25 edge).
# --------------------------------------------------------------------------- #

def disagreement(fundamental):
    """Return ``|dcf_base - comps_mid| / ((dcf_base + comps_mid) / 2)`` from the
    fundamental valuation anchors, or None if the anchors are absent / unusable.

    ``comps_mid = (comps_low + comps_high) / 2``; the denominator is the AVERAGE of
    dcf_base and comps_mid. This is the SAME, authoritative formula
    ``score_fundamental._dcf_band_position`` computes (and prints into
    module_fundamental's valuation arithmetic, e.g. GOOG 0.8601) and that
    ``decision_contract._valuation_conflict`` recomputes — one number, one edge,
    three consumers. Returns None when the anchors block is absent, a required anchor
    is missing/non-numeric, or the average denominator is 0 (an invalid mid, not a
    disagreement). A NEGATIVE / zero DCF or comps value still yields a ratio here
    (state classification handles MODEL_INVALID separately) — this function reports
    the numeric split; the state machine judges validity.
    """
    anchors = _valuation_anchors(fundamental)
    if not isinstance(anchors, dict):
        return None
    dcf_base = _num(anchors.get("dcf_base"))
    comps_low = _num(anchors.get("comps_low"))
    comps_high = _num(anchors.get("comps_high"))
    if dcf_base is None or comps_low is None or comps_high is None:
        return None
    comps_mid = (comps_low + comps_high) / 2.0
    denom = (dcf_base + comps_mid) / 2.0
    if denom == 0:
        return None
    return abs(dcf_base - comps_mid) / abs(denom)


def disagreement_state(fundamental):
    """Classify the DCF-vs-comps split into the O17 state machine.

    Returns:
      ``"MODEL_INVALID"``       a required DCF/comps anchor is <= 0 (a value the
                                valuation math cannot use — takes precedence over
                                the ratio classification).
      ``"UNRESOLVED_CONFLICT"`` disagreement > 0.25 (the split is a RISK: prose may
                                explain it but the models still numerically disagree).
      ``"CONSISTENT"``          disagreement <= 0.25.
      ``None``                  no usable anchors (degrade gracefully; do NOT guess).

    MODEL_INVALID is checked FIRST because a <=0 anchor makes the ratio meaningless.
    """
    anchors = _valuation_anchors(fundamental)
    if not isinstance(anchors, dict):
        return None
    dcf_base = _num(anchors.get("dcf_base"))
    comps_low = _num(anchors.get("comps_low"))
    comps_high = _num(anchors.get("comps_high"))
    # Missing any required anchor -> no usable state (graceful None).
    if dcf_base is None or comps_low is None or comps_high is None:
        return None
    # A non-positive DCF/comps value is a broken model input, not a mere split.
    if dcf_base <= 0 or comps_low <= 0 or comps_high <= 0:
        return STATE_MODEL_INVALID
    d = disagreement(fundamental)
    if d is None:
        return None
    return STATE_UNRESOLVED if d > _DISAGREEMENT_TOL else STATE_CONSISTENT


# --------------------------------------------------------------------------- #
# Reverse-DCF (DISCLOSURE, not a new price target).
# --------------------------------------------------------------------------- #

def reverse_dcf(dcf_reverse_inputs, last):
    """Solve the implied terminal growth g* that makes the DCF equal ``last``.

    Holds the transcribed explicit-FCF PV, terminal-base PV, WACC, net cash, and
    diluted shares FIXED; only the terminal growth is freed to whatever the market
    price implies. Closed-form linear solve (no iteration):

        target_equity = last * diluted_shares_m
        target_ev     = target_equity - net_cash_m
        pv_terminal_needed = target_ev - pv_explicit_fcf_m
        ratio  = pv_terminal_needed / pv_terminal_base_m
        base_factor = (1 + g_base) / (wacc - g_base)          # the terminal shape
        needed = ratio * base_factor                          # target terminal shape
        (1 + g*) / (wacc - g*) = needed  ->  g* = (needed*wacc - 1) / (1 + needed)

    Returns a dict:
      ``implied_terminal_g``  g* (rounded 4dp), or None when there is no finite
                              economic solution (see ``note``).
      ``g_base``              the transcribed base terminal growth.
      ``wacc``                the transcribed WACC.
      ``implied_vs_base``     g* - g_base (rounded 4dp), or None when g* is None.
      ``note``                a disclosure string when g* has no finite solution.

    No finite solution (``implied_terminal_g = None`` + note "market prices FCF
    above the model path") when ``needed <= 0`` (the price implies a NEGATIVE
    terminal value) or ``g* >= wacc`` (a terminal growth at/above the discount rate
    diverges — the market is pricing FCF growth the perpetuity model cannot express).
    Returns None entirely when any required input is missing/non-numeric or the base
    factor is undefined (wacc == g_base). No guessing.
    """
    if not isinstance(dcf_reverse_inputs, dict):
        return None
    last = _num(last)
    pv_explicit = _num(dcf_reverse_inputs.get("pv_explicit_fcf_m"))
    pv_terminal_base = _num(dcf_reverse_inputs.get("pv_terminal_base_m"))
    g_base = _num(dcf_reverse_inputs.get("terminal_g_base"))
    wacc = _num(dcf_reverse_inputs.get("wacc"))
    net_cash = _num(dcf_reverse_inputs.get("net_cash_m"))
    diluted_shares = _num(dcf_reverse_inputs.get("diluted_shares_m"))
    if None in (last, pv_explicit, pv_terminal_base, g_base, wacc, net_cash,
                diluted_shares):
        return None
    # The terminal-base shape must be defined and the base PV usable.
    if wacc == g_base or pv_terminal_base == 0:
        return None

    target_equity = last * diluted_shares
    target_ev = target_equity - net_cash
    pv_terminal_needed = target_ev - pv_explicit
    ratio = pv_terminal_needed / pv_terminal_base
    base_factor = (1.0 + g_base) / (wacc - g_base)
    needed = ratio * base_factor

    no_finite = {
        "implied_terminal_g": None,
        "g_base": round(g_base, 4),
        "wacc": round(wacc, 4),
        "implied_vs_base": None,
        "note": "market prices FCF above the model path (no finite g)",
    }

    # A non-positive target terminal shape has no economic g solution.
    if needed <= 0:
        return no_finite

    g_star = (needed * wacc - 1.0) / (1.0 + needed)
    # g* at/above WACC diverges (the perpetuity model cannot express it).
    if g_star >= wacc:
        return no_finite

    return {
        "implied_terminal_g": round(g_star, 4),
        "g_base": round(g_base, 4),
        "wacc": round(wacc, 4),
        "implied_vs_base": round(g_star - g_base, 4),
        "note": None,
    }


# --------------------------------------------------------------------------- #
# Bundle assembly.
# --------------------------------------------------------------------------- #

def build_reconcile(fundamental, scenario_drivers, last):
    """Assemble the module_valuation_reconcile document from parsed inputs.

    ``fundamental``       parsed module_fundamental.json (or None).
    ``scenario_drivers``  parsed coverage/scenario_drivers.json (or None).
    ``last``              snapshot price.last (EV/reverse-DCF reference).

    Degrades field-by-field: absent fundamental anchors -> disagreement/state None;
    absent scenario_drivers -> reverse_dcf and scenarios None (but the state, which
    comes from the fundamental anchors, is still emitted).
    """
    state = disagreement_state(fundamental)
    disagree = disagreement(fundamental)

    reverse = None
    scenarios = None
    citations = None
    if isinstance(scenario_drivers, dict):
        reverse = reverse_dcf(scenario_drivers.get("dcf_reverse_inputs"), last)
        scenarios = scenario_drivers.get("scenarios")
        citations = scenario_drivers.get("citations")

    return {
        "skill": SKILL,
        "reconcile_version": RECONCILE_VERSION,
        "disagreement": round(disagree, 4) if isinstance(disagree, float) else disagree,
        "disagreement_edge": _DISAGREEMENT_TOL,
        "disagreement_state": state,
        "reverse_dcf": reverse,
        "scenarios": scenarios,
        "citations": citations,
    }


def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _load_json(path):
    if path is None or not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _scenario_drivers_path(bundle):
    """Locate coverage/scenario_drivers.json for a bundle.

    Preference order:
      1. ``<bundle>/coverage/scenario_drivers.json`` (coverage sibling of the bundle
         contents).
      2. ``<bundle>/../coverage/scenario_drivers.json`` (detail_reports layout: the
         coverage/ dir is a sibling of the detail bundle under the ticker folder).
    Returns the first existing path, or None.
    """
    candidates = [
        os.path.join(bundle, "coverage", "scenario_drivers.json"),
        os.path.join(os.path.dirname(os.path.normpath(bundle)),
                     "coverage", "scenario_drivers.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Valuation reconciliation (O17, v%s): classify the DCF-vs-comps "
                    "disagreement state and solve the reverse-DCF implied terminal "
                    "growth. Writes module_valuation_reconcile.json. A DISCLOSURE — "
                    "the price fan / EV / trade plan are UNCHANGED." % RECONCILE_VERSION)
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--scenario-drivers", default=None,
                        help="path to coverage/scenario_drivers.json (default: "
                             "<bundle>/coverage/ or the bundle's sibling coverage/)")
    parser.add_argument("--out", default=None,
                        help="output path (default "
                             "<bundle>/module_valuation_reconcile.json)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 2

    fundamental = _load_json(os.path.join(args.bundle, "module_fundamental.json"))
    if fundamental is None:
        print("ERROR: module_fundamental.json absent -- the disagreement state is "
              "read from its valuation anchors; run score_fundamental first.",
              file=sys.stderr)
        return 2

    snap_path = _find_snapshot(args.bundle)
    snapshot = _load_json(snap_path) if snap_path else None
    last = _dig(snapshot, "price", "last") if isinstance(snapshot, dict) else None

    drivers_path = args.scenario_drivers or _scenario_drivers_path(args.bundle)
    scenario_drivers = _load_json(drivers_path)

    doc = build_reconcile(fundamental, scenario_drivers, last)

    out = args.out or os.path.join(args.bundle, "module_valuation_reconcile.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
