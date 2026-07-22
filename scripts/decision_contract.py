"""Canonical decision contract (G4a) for the trading-desk plugin.

WHY THIS MODULE EXISTS: today page-1 capital language (action, horizon, hurdle,
"capital-eligible" status) is authored by the LLM in free prose and only checked
numerically. The GOOG review validation showed that lets a correctly-provenanced-
but-wrong-in-context statement reach page 1 ("the first positive-EV entry is
334.69" while ev_at_current is already +0.059). This module builds a DETERMINISTIC
decision object from the already-scored bundle so the capital status can be
rendered from data, not narrated. Every field is sourced from a REAL bundle leaf
(cited inline). This is the G4a slice: the contract object + the writer. Rewiring
page-1 rendering to consume it is G4b (deferred).

DESIGN CONTRACT (mirrors the four scorers + composite):
- ``build_contract(docs)`` is PURE over the already-parsed bundle docs dict (the
  same shape ``render_report.load_bundle`` / ``run_report_qc`` build: keys
  ``module_composite`` / ``module_tradeplan`` / ``module_fundamental`` /
  ``snapshot``). It reads no files and mints no numbers of its own beyond the
  explicit derivations documented per field.
- Capital blockers are computed by EXPLICIT, documented rules (no heuristics, no
  guessing). A blocker whose inputs are absent is simply not emitted (it is never
  fabricated). ``capital_eligible`` is exactly ``len(capital_blockers) == 0``.
- The action fields are DISCLOSURE fields (a machine-readable statement of what the
  contract implies for an unowned vs owned position). They do NOT yet BLOCK the
  report — the govern-vs-disclose gate (whether LOW confidence / EV<hurdle should
  HARD-STOP new capital) is a separate task and a user decision.

stdlib-only; the build function is pure over parsed inputs.
"""

import argparse
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

SKILL = "decision-contract"
CONTRACT_VERSION = "1.1.0"

# --------------------------------------------------------------------------- #
# O10b PROVISIONAL constants (v1.1.0): the EV-uncertainty band.
# These are DISCLOSED, versioned, and killable (the calibration philosophy: ship
# a cited, versioned, falsifiable default; ratify or retire at B9). They are
# module-level named consts so they are greppable and one-line-ratifiable.
#
# k(confidence) scales the bull-bear scenario spread into a half-width around the
# point EV. Keyed on composite/data confidence as a v1.1.0 PROXY for forecast
# uncertainty (the review's first numbers). See ``PROVISIONAL_NOTE`` for the
# pre-registered B9 falsifier.
# --------------------------------------------------------------------------- #
_EV_BAND_K = {"LOW": 0.15, "MEDIUM": 0.10, "HIGH": 0.05}
# Confidence level used (and k selected) when the composite confidence level is
# absent or unrecognized: fall back to the WIDEST (most conservative) band.
_EV_BAND_DEFAULT_LEVEL = "LOW"

PROVISIONAL_NOTE = (
    "decision-contract-v1.1.0 PROVISIONAL: EV-uncertainty band = k(confidence) x "
    "(r_bull - r_bear), k={LOW:0.15,MEDIUM:0.10,HIGH:0.05} (softened 2026-07-21 "
    "from LOW 0.25/MED 0.15 per user tuning: the LOW robustness bar was ~22% EV; "
    "0.15 sets it ~18% at GOOG's spread. Keyed on composite/data confidence as a "
    "v1.1.0 proxy for forecast "
    "uncertainty). Falsifier (B9): the k table and the "
    "EV_NOT_ROBUST_UNDER_UNCERTAINTY gate are falsified if, across the "
    "calibration set, (i) realized forward returns for LOW-confidence names land "
    "within their disclosed ev_band at a rate inconsistent with a ~1-sigma "
    "interval (provisionally: <40% or >90% coverage => k mis-scaled), or (ii) "
    "names blocked ONLY by EV_NOT_ROBUST do not realize worse risk-adjusted "
    "outcomes than names that passed. Not calibrated."
)

# The disagreement threshold above which the fundamental valuation module widens
# its band + applies a confidence haircut (verified: score_fundamental writes
# "disagreement ... > 0.25 -> WIDEN band" into the valuation subscore arithmetic).
# We reuse the SAME 0.25 edge when recomputing disagreement from the anchors so the
# blocker fires on exactly the condition the fundamental module already flags.
_VALUATION_DISAGREEMENT_TOL = 0.25


# --------------------------------------------------------------------------- #
# Small local helpers (kept independent of report_qc / render_report so this
# module is importable standalone by the scorer CLI).
# --------------------------------------------------------------------------- #

def _dig(obj, *path):
    """Follow a key path through nested dicts; None if any hop is absent."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _valuation_subscore(fundamental):
    """The ``valuation`` entry of module_fundamental.subscores, or None."""
    if not isinstance(fundamental, dict):
        return None
    for sub in fundamental.get("subscores") or []:
        if isinstance(sub, dict) and sub.get("name") == "valuation":
            return sub
    return None


def _valuation_conflict(fundamental):
    """True/False/None: does the fundamental valuation reflect the widened /
    haircut band that signals an unresolved DCF-vs-comps conflict?

    Detection, in preference order (spec G4 rule):
      1. RECOMPUTE from the subscore ``inputs.anchors`` — disagreement =
         |dcf_base - comps_mid| / comps_mid where comps_mid = (comps_low +
         comps_high) / 2; conflict when disagreement > 0.25 (the SAME edge
         score_fundamental widens on). This is the authoritative path.
      2. FALL BACK to scanning the subscore ``arithmetic`` for "WIDEN" (the token
         score_fundamental emits when it widened the band).
      3. If neither the anchors NOR the arithmetic are available -> None (omit the
         blocker; do NOT guess).
    """
    sub = _valuation_subscore(fundamental)
    if sub is None:
        return None

    # (1) Authoritative: recompute disagreement from the anchors.
    anchors = _dig(sub, "inputs", "anchors")
    if isinstance(anchors, dict):
        dcf_base = anchors.get("dcf_base")
        comps_low = anchors.get("comps_low")
        comps_high = anchors.get("comps_high")
        if (isinstance(dcf_base, (int, float))
                and isinstance(comps_low, (int, float))
                and isinstance(comps_high, (int, float))):
            comps_mid = (comps_low + comps_high) / 2.0
            if comps_mid != 0:
                disagreement = abs(dcf_base - comps_mid) / abs(comps_mid)
                return disagreement > _VALUATION_DISAGREEMENT_TOL

    # (2) Fallback: the module already recorded a WIDEN in its arithmetic string.
    arithmetic = sub.get("arithmetic")
    if isinstance(arithmetic, str):
        return "WIDEN" in arithmetic.upper()

    # (3) Neither available -> unknown; omit the blocker rather than guess.
    return None


# --------------------------------------------------------------------------- #
# O10b: EV-uncertainty band (PROVISIONAL v1.1.0).
# --------------------------------------------------------------------------- #

def compute_ev_band(last, scenarios, ev_at_current, confidence_level):
    """Compute the PROVISIONAL EV-uncertainty band from data already in the bundle.

    The band GOVERNS the forecast distribution (resolving the residual half of
    backlog O10: LOW confidence should widen, not merely disclose). It is a
    half-width around the point EV proportional to the bull-bear scenario spread,
    scaled by a confidence-keyed constant k (the v1.1.0 proxy for forecast
    uncertainty; see ``PROVISIONAL_NOTE``).

    Inputs (all sourced from the already-scored bundle):
      ``last``              snapshot.price.last
      ``scenarios``         module_composite.ev.scenarios ([{name,price_target,prob}])
      ``ev_at_current``     module_composite.ev.ev_at_current
      ``confidence_level``  module_composite.confidence.level

    Returns a dict of the band fields. When the guard fails (< 2 scenarios, last
    not > 0, or spread < 0), every band field is None so the caller emits a fully
    absent band and does NOT add the new blocker.

    Returns keys:
      ``ev_band``                 [low, high] or None
      ``ev_uncertainty_halfwidth``  k * spread, or None
      ``ev_uncertainty_k``          the k used, or None
      ``ev_uncertainty_confidence_level``  the level whose k was used (may be the
                                    conservative LOW fallback), or None
      ``ev_robust_vs_hurdle``       computed by the caller against the hurdle;
                                    None here (filled in build_contract).
    """
    absent = {
        "ev_band": None,
        "ev_uncertainty_halfwidth": None,
        "ev_uncertainty_k": None,
        "ev_uncertainty_confidence_level": None,
    }

    # Guard: need a usable price, >= 2 scenarios with numeric targets, and a
    # numeric point EV. Absent inputs -> no band (never fabricated).
    if not isinstance(last, (int, float)) or last <= 0:
        return dict(absent)
    if not isinstance(ev_at_current, (int, float)):
        return dict(absent)
    targets = [
        s.get("price_target") for s in (scenarios or [])
        if isinstance(s, dict) and isinstance(s.get("price_target"), (int, float))
    ]
    if len(targets) < 2:
        return dict(absent)

    returns = [pt / last - 1.0 for pt in targets]
    r_bull = max(returns)
    r_bear = min(returns)
    spread = r_bull - r_bear
    if spread < 0:  # defensive; max-min is >= 0, but guard as specified.
        return dict(absent)

    # k selection: unrecognized/absent confidence -> the conservative (widest) k.
    level = confidence_level if confidence_level in _EV_BAND_K else _EV_BAND_DEFAULT_LEVEL
    k = _EV_BAND_K[level]

    halfwidth = k * spread
    return {
        "ev_band": [ev_at_current - halfwidth, ev_at_current + halfwidth],
        "ev_uncertainty_halfwidth": halfwidth,
        "ev_uncertainty_k": k,
        "ev_uncertainty_confidence_level": level,
    }


def ev_robust_vs_hurdle(ev_band, total_return_hurdle):
    """True when the hurdle verdict is the SAME at both band ends (robust); False
    when the band STRADDLES the hurdle; None when the band or hurdle is absent.

    ``(ev_low >= hurdle) == (ev_high >= hurdle)`` -- a robust band clears (or
    fails) the hurdle at both ends; a straddling band flips verdicts.
    """
    if not (isinstance(ev_band, (list, tuple)) and len(ev_band) == 2):
        return None
    if not isinstance(total_return_hurdle, (int, float)):
        return None
    ev_low, ev_high = ev_band
    if not (isinstance(ev_low, (int, float)) and isinstance(ev_high, (int, float))):
        return None
    return (ev_low >= total_return_hurdle) == (ev_high >= total_return_hurdle)


# --------------------------------------------------------------------------- #
# Blocker + action rules (explicit; no heuristics).
# --------------------------------------------------------------------------- #

def compute_blockers(total_return_hurdle, ev_at_current, days_to_event,
                     composite_confidence_level, valuation_conflict,
                     ev_band=None):
    """Return the ordered list of capital-blocker string codes.

    Each rule is explicit and independent; order is deterministic (the list is a
    disclosure, not a priority ranking — the action mapping applies its own
    precedence). A rule whose inputs are None is skipped (never fabricated).

      EV_BELOW_HURDLE          ev_at_current < total_return_hurdle
      EARNINGS_WITHIN_1_DAY    days_to_event is not None and <= 1
      LOW_COMPOSITE_CONFIDENCE composite confidence.level == "LOW"
      VALUATION_MODEL_CONFLICT valuation_conflict is True
      EV_NOT_ROBUST_UNDER_UNCERTAINTY  (O10b PROVISIONAL v1.1.0) the POINT EV
        passes the hurdle (ev_at_current >= total_return_hurdle) but the
        conservative end of the disclosed uncertainty band FAILS it
        (ev_low < total_return_hurdle). When the point EV is already below the
        hurdle, EV_BELOW_HURDLE covers it -> this blocker is NOT double-added.
        Requires a computed ``ev_band`` ([low, high]); absent band -> skipped.
    """
    blockers = []
    if (ev_at_current is not None and total_return_hurdle is not None
            and ev_at_current < total_return_hurdle):
        blockers.append("EV_BELOW_HURDLE")
    if days_to_event is not None and days_to_event <= 1:
        blockers.append("EARNINGS_WITHIN_1_DAY")
    if composite_confidence_level == "LOW":
        blockers.append("LOW_COMPOSITE_CONFIDENCE")
    if valuation_conflict is True:
        blockers.append("VALUATION_MODEL_CONFLICT")
    # O10b: the point PASSES the EV gate but the band's conservative end fails.
    # Only when the point is >= hurdle (else EV_BELOW_HURDLE already fired).
    if (isinstance(ev_band, (list, tuple)) and len(ev_band) == 2
            and isinstance(ev_at_current, (int, float))
            and isinstance(total_return_hurdle, (int, float))
            and isinstance(ev_band[0], (int, float))
            and ev_at_current >= total_return_hurdle
            and ev_band[0] < total_return_hurdle):
        blockers.append("EV_NOT_ROBUST_UNDER_UNCERTAINTY")
    return blockers


def map_actions(blockers, grade, capital_eligible):
    """Map the blocker set + grade to (action_unowned, action_owned).

    Precedence (spec G4a):
      1. EARNINGS_WITHIN_1_DAY   -> unowned WAIT_FOR_EVENT,           owned HOLD_NO_ADD
      2. EV_BELOW_HURDLE         -> unowned WAIT_SUB_HURDLE,          owned HOLD_NO_ADD
      3. capital_eligible & A/B  -> unowned ACCUMULATE_ON_WEAKNESS,   owned HOLD
      4. else                    -> unowned NO_ENTRY,                 owned HOLD

    These are DISCLOSURE outputs — they state what the contract implies; they do
    NOT (yet) hard-block the report.
    """
    if "EARNINGS_WITHIN_1_DAY" in blockers:
        return "WAIT_FOR_EVENT", "HOLD_NO_ADD"
    if "EV_BELOW_HURDLE" in blockers:
        return "WAIT_SUB_HURDLE", "HOLD_NO_ADD"
    if capital_eligible and grade in {"A", "B"}:
        return "ACCUMULATE_ON_WEAKNESS", "HOLD"
    return "NO_ENTRY", "HOLD"


# --------------------------------------------------------------------------- #
# The contract builder (pure).
# --------------------------------------------------------------------------- #

def build_contract(docs):
    """Build the deterministic decision contract dict from parsed bundle docs.

    ``docs`` = the dict ``run_report_qc`` builds (keys ``module_composite`` /
    ``module_tradeplan`` / ``module_fundamental`` / ``snapshot`` — a missing key
    or module simply maps to None). Every field below cites its source leaf.
    """
    composite = docs.get("module_composite") or {}
    fundamental = docs.get("module_fundamental") or {}
    snapshot = docs.get("snapshot") or {}
    ev = composite.get("ev") or {}

    # profile <- module_composite.profile
    profile = composite.get("profile")

    # horizon_months <- module_composite.ev.horizon_years_convention x 12
    horizon_years = ev.get("horizon_years_convention")
    horizon_months = horizon_years * 12 if isinstance(horizon_years, (int, float)) else None

    # scenario_horizon_months <- SAME source (equal by construction; the bundle
    # carries no separate scenario horizon today). If a distinct scenario-horizon
    # field is ever introduced it should be compared here instead of aliased.
    scenario_horizon_months = horizon_months

    # total_return_hurdle <- module_composite.ev.hurdle_total
    # annual_return_hurdle <- derived: total / horizon_years
    total_return_hurdle = ev.get("hurdle_total")
    if (isinstance(total_return_hurdle, (int, float))
            and isinstance(horizon_years, (int, float)) and horizon_years != 0):
        annual_return_hurdle = total_return_hurdle / horizon_years
    else:
        annual_return_hurdle = None

    # ev_at_current <- module_composite.ev.ev_at_current
    ev_at_current = ev.get("ev_at_current")

    # -- O10b EV-uncertainty band (PROVISIONAL v1.1.0) ----------------------
    # last <- snapshot.price.last ; scenarios <- module_composite.ev.scenarios ;
    # confidence level <- module_composite.confidence.level. The band GOVERNS:
    # a straddling band can add EV_NOT_ROBUST_UNDER_UNCERTAINTY (below).
    last = _dig(snapshot, "price", "last")
    band = compute_ev_band(
        last=last,
        scenarios=ev.get("scenarios"),
        ev_at_current=ev_at_current,
        confidence_level=_dig(composite, "confidence", "level"),
    )

    # hurdle_clearing_price <- module_composite.ev.ev_breakeven_entry
    #   (the price at which EV clears the hurdle -- NOT the first positive-EV price).
    hurdle_clearing_price = ev.get("ev_breakeven_entry")

    # grade <- module_composite.grade ; score <- module_composite.score
    #   (score is carried on the contract so the page-1 capital-status block --
    #   which renders "composite {score}/100" -- draws every number from a contract
    #   field, keeping the render's number-provenance surface contract-owned.)
    grade = composite.get("grade")
    score = composite.get("score")

    # -- blocker inputs -----------------------------------------------------
    # days_to_event <- snapshot.events.days_to_event
    days_to_event = _dig(snapshot, "events", "days_to_event")
    # composite confidence level <- module_composite.confidence.level
    composite_confidence_level = _dig(composite, "confidence", "level")
    # valuation conflict <- module_fundamental valuation subscore (anchors recompute
    #   preferred; WIDEN-scan fallback; None if neither available)
    valuation_conflict = _valuation_conflict(fundamental)

    capital_blockers = compute_blockers(
        total_return_hurdle=total_return_hurdle,
        ev_at_current=ev_at_current,
        days_to_event=days_to_event,
        composite_confidence_level=composite_confidence_level,
        valuation_conflict=valuation_conflict,
        ev_band=band["ev_band"],
    )
    capital_eligible = len(capital_blockers) == 0

    # ev_robust_vs_hurdle: same hurdle verdict at both band ends (None if no band).
    robust = ev_robust_vs_hurdle(band["ev_band"], total_return_hurdle)

    action_unowned, action_owned = map_actions(
        capital_blockers, grade, capital_eligible)

    return {
        "skill": SKILL,
        "contract_version": CONTRACT_VERSION,
        "ticker": _dig(snapshot, "meta", "ticker") or composite.get("ticker"),
        "as_of": composite.get("as_of") or _dig(snapshot, "meta", "as_of_utc"),
        "profile": profile,
        "horizon_months": horizon_months,
        "scenario_horizon_months": scenario_horizon_months,
        "annual_return_hurdle": annual_return_hurdle,
        "total_return_hurdle": total_return_hurdle,
        "ev_at_current": ev_at_current,
        "hurdle_clearing_price": hurdle_clearing_price,
        "grade": grade,
        "score": score,
        # -- O10b EV-uncertainty band (PROVISIONAL v1.1.0) ------------------
        "ev_band": band["ev_band"],
        "ev_uncertainty_halfwidth": band["ev_uncertainty_halfwidth"],
        "ev_uncertainty_k": band["ev_uncertainty_k"],
        "ev_uncertainty_confidence_level": band["ev_uncertainty_confidence_level"],
        "ev_robust_vs_hurdle": robust,
        "provisional_note": PROVISIONAL_NOTE,
        "capital_blockers": capital_blockers,
        "capital_eligible": capital_eligible,
        "action_unowned": action_unowned,
        "action_owned": action_owned,
    }


# --------------------------------------------------------------------------- #
# CLI (mirrors the scorers: --bundle / --out).
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _load_json(path):
    if not os.path.isfile(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def load_docs(bundle):
    """Load the subset of bundle docs build_contract reads (snapshot + the three
    modules). Missing files map to None -- build_contract degrades field-by-field.
    """
    snap_path = _find_snapshot(bundle)
    return {
        "snapshot": _load_json(snap_path) if snap_path else None,
        "module_composite": _load_json(os.path.join(bundle, "module_composite.json")),
        "module_tradeplan": _load_json(os.path.join(bundle, "module_tradeplan.json")),
        "module_fundamental": _load_json(os.path.join(bundle, "module_fundamental.json")),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build the canonical decision contract (v%s) from a scored "
                    "bundle: a deterministic {profile, horizon, hurdle, EV, "
                    "capital_blockers[], capital_eligible, action_*} object."
                    % CONTRACT_VERSION)
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--out", default=None,
                        help="output path (default <bundle>/module_decision.json)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 2

    docs = load_docs(args.bundle)
    if docs.get("module_composite") is None:
        print("ERROR: module_composite.json absent -- the decision contract is "
              "built from the composite decision layer; run score_composite first.",
              file=sys.stderr)
        return 2

    contract = build_contract(docs)

    out = args.out or os.path.join(args.bundle, "module_decision.json")
    with open(out, "w") as fh:
        json.dump(contract, fh, indent=2, sort_keys=True)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
