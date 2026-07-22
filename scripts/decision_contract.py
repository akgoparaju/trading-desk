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
import datetime
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Ensure the repo root is importable when run directly so ``from
# scripts._artifact import ...`` resolves; ``-m scripts.decision_contract``
# already has it.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts._artifact import emit_json

SKILL = "decision-contract"
CONTRACT_VERSION = "2.0.0"

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
         |dcf_base - comps_mid| / ((dcf_base + comps_mid) / 2) where comps_mid =
         (comps_low + comps_high) / 2; conflict when disagreement > 0.25. This is
         the SAME, authoritative formula score_fundamental._dcf_band_position
         computes (and valuation_reconcile.disagreement recomputes) — one
         disagreement formula, one 0.25 edge, three consumers. Authoritative path.
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
            denom = (dcf_base + comps_mid) / 2.0
            if denom != 0:
                disagreement = abs(dcf_base - comps_mid) / abs(denom)
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
# O19: Entry-state (deterministic, derived from blockers + eligibility).
# --------------------------------------------------------------------------- #

def _derive_entry_state(capital_blockers, capital_eligible):
    """Derive the four-state entry_state disclosure field from the blocker set.

    Precedence (deterministic, no heuristics):
      1. EARNINGS_WITHIN_1_DAY in blockers  -> WAIT_FOR_EVENT
      2. EV_BELOW_HURDLE in blockers        -> WATCH_ZONE
         (price is above the hurdle-clearing entry; wait for it to come to you)
      3. capital_eligible is True           -> HURDLE_CLEARING_ENTRY
      4. else                               -> NO_ENTRY_AT_CURRENT

    This is a DISCLOSURE field complementing ``action_unowned``.  It does NOT
    block the report -- it labels the current price vs the entry ladder for the
    Portfolio-OS handoff.
    """
    if "EARNINGS_WITHIN_1_DAY" in capital_blockers:
        return "WAIT_FOR_EVENT"
    if "EV_BELOW_HURDLE" in capital_blockers:
        return "WATCH_ZONE"
    if capital_eligible:
        return "HURDLE_CLEARING_ENTRY"
    return "NO_ENTRY_AT_CURRENT"


# --------------------------------------------------------------------------- #
# FR-2: catalyst assembly (the ONE new derivation -- days_out).
# --------------------------------------------------------------------------- #

def _days_out(date_iso, as_of_date):
    """Calendar-day gap ``date_iso - as_of_date`` (integer, may be 0 or negative).

    The ONLY new arithmetic in the FR-2 catalysts section (consumer-sanctioned:
    "days_out computed from as_of"). Uses ``datetime.date.fromisoformat`` on both
    ISO date strings. If either is absent or unparseable -> None (never guessed).
    """
    if not isinstance(date_iso, str) or not isinstance(as_of_date, str):
        return None
    try:
        d1 = datetime.date.fromisoformat(date_iso)
        d0 = datetime.date.fromisoformat(as_of_date)
    except ValueError:
        return None
    return (d1 - d0).days


def build_catalysts(snapshot, tradeplan, as_of_date):
    """Assemble the FR-2 ``catalysts[]`` array from the snapshot events + tradeplan.

    Pure over parsed docs. ``as_of_date`` is the ISO date the ``days_out`` gaps are
    measured from (``composite.as_of`` or ``snapshot.meta.as_of_utc[:10]``; chosen
    by the caller). Emits one element per KNOWN catalyst; an element whose date leaf
    is absent is omitted (never fabricated). Field map is spec-verbatim except the
    single derived ``days_out``.

      earnings  <- snapshot.events.next_earnings (+ implied_move, catalyst_in_thesis)
      dividend  <- snapshot.events.dividends
      (any pre-existing snapshot.events.catalyst elements are appended verbatim)
    """
    events = _dig(snapshot, "events") or {}
    catalysts = []

    # -- earnings ---------------------------------------------------------------
    next_earnings = events.get("next_earnings")
    earnings_date = _dig(events, "next_earnings", "date")
    if isinstance(next_earnings, dict) and earnings_date:
        catalyst_in_thesis = _dig(tradeplan, "expression", "catalyst_in_thesis")
        item = {
            "label": "earnings",
            "date_iso": earnings_date,
            "type": "earnings",
            "in_thesis": catalyst_in_thesis,
            "days_out": _days_out(earnings_date, as_of_date),
        }
        implied_move = events.get("implied_move")
        if implied_move is not None:
            item["implied_move_pct"] = implied_move
        consensus_eps = next_earnings.get("consensus_eps")
        if consensus_eps is not None:
            item["consensus_eps"] = consensus_eps
        catalysts.append(item)

    # -- dividend ---------------------------------------------------------------
    dividends = events.get("dividends")
    ex_date = _dig(events, "dividends", "ex_date")
    if isinstance(dividends, dict) and ex_date:
        item = {
            "label": "dividend ex-date",
            "date_iso": ex_date,
            "type": "dividend",
            "in_thesis": False,
            "days_out": _days_out(ex_date, as_of_date),
        }
        per_share = dividends.get("per_share")
        if per_share is not None:
            item["per_share"] = per_share
        catalysts.append(item)

    # -- any pre-existing verbatim catalysts (the events.catalysts array; [] today)
    existing = events.get("catalysts")
    if isinstance(existing, list) and existing:
        catalysts.extend(existing)

    return catalysts


# --------------------------------------------------------------------------- #
# FR-5: thesis identity (deterministic + refresh-stable).
# --------------------------------------------------------------------------- #

def build_thesis(ticker, coverage_manifest, composite_flags):
    """Build the FR-5 ``thesis`` block, or None when its coverage source is absent.

    ``registered_date`` <- coverage_manifest.generated_utc[:10] -- the coverage
    INITIATION date. It is refresh-STABLE because coverage is built once and carried
    forward, so ``thesis.id`` = f"{ticker}-{registered_date}" is deterministic across
    refreshes even as the composite / as_of move. If ``coverage_manifest`` is absent
    (or carries no ``generated_utc``), the whole block is omitted (never fabricated).
    ``next_review`` is deferred (no deterministic bundle source in P0).
    """
    generated_utc = _dig(coverage_manifest, "generated_utc")
    if not isinstance(generated_utc, str) or len(generated_utc) < 10:
        return None
    registered_date = generated_utc[:10]
    flags = composite_flags or {}
    thesis = {
        "registered_date": registered_date,
        "variant": flags.get("variant"),
        "catalyst_clarity": flags.get("catalyst_clarity"),
    }
    if ticker:
        thesis["id"] = f"{ticker}-{registered_date}"
    return thesis


def build_rubric_versions(docs, composite):
    """Assemble the ``rubric_versions`` block. Each key is a module's
    ``rubric_version`` leaf; ``confidence`` is ``module_composite.confidence.version``.
    A key whose module is absent (or whose module carries no ``rubric_version``) is
    OMITTED, never fabricated.
    """
    module_keys = {
        "technical": "module_technical",
        "fundamental": "module_fundamental",
        "sentiment": "module_sentiment",
        "risk": "module_risk",
        "composite": "module_composite",
        "tradeplan": "module_tradeplan",
        "options": "module_options",
    }
    out = {}
    for label, doc_key in module_keys.items():
        mod = docs.get(doc_key)
        version = _dig(mod, "rubric_version")
        if version is not None:
            out[label] = version
    conf_version = _dig(composite, "confidence", "version")
    if conf_version is not None:
        out["confidence"] = conf_version
    return out


# --------------------------------------------------------------------------- #
# The contract builder (pure).
# --------------------------------------------------------------------------- #

def build_contract(docs):
    """Build the deterministic decision contract dict from parsed bundle docs.

    ``docs`` = the dict ``load_docs`` / ``run_report_qc`` builds (keys
    ``module_composite`` / ``module_tradeplan`` / ``module_fundamental`` /
    ``module_technical`` / ``module_sentiment`` / ``module_risk`` /
    ``module_options`` / ``module_context`` / ``snapshot`` /
    ``valuation_anchors`` / ``coverage_manifest`` -- a missing key or module simply
    maps to None). Every field below cites its source leaf.

    contract_version 2.0.0 is PURELY ADDITIVE over 1.1.0: every 1.1.0 top-level
    field is kept EXACTLY as-is (the consumer consumes them all) and the consolidated
    sections (composite / dimensions / ev / flags / sizing / plan / risk_units /
    invalidation / expression / valuation_anchors / coverage / catalysts / thesis /
    rubric_versions / run_utc / latest_trading_day + data_mode/degraded/missing) are
    ADDED. The module's rule holds for every added field too: a section whose source
    leaf is absent is OMITTED, never fabricated. This function stays PURE -- it reads
    no files and mints no numbers beyond the documented derivations (``days_out`` and
    ``thesis.id``/``registered_date`` are the only new ones).
    """
    composite = docs.get("module_composite") or {}
    fundamental = docs.get("module_fundamental") or {}
    snapshot = docs.get("snapshot") or {}
    tradeplan = docs.get("module_tradeplan") or {}
    options = docs.get("module_options") or {}
    valuation_anchors = docs.get("valuation_anchors")
    coverage_manifest = docs.get("coverage_manifest")
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

    # -- O19: entry_state (deterministic, derived from blockers + eligibility) --
    # Four-state disclosure field for the Portfolio-OS handoff.  Precedence:
    #   1. EARNINGS_WITHIN_1_DAY in blockers -> WAIT_FOR_EVENT
    #   2. EV_BELOW_HURDLE in blockers       -> WATCH_ZONE
    #   3. capital_eligible True             -> HURDLE_CLEARING_ENTRY
    #   4. else                              -> NO_ENTRY_AT_CURRENT
    entry_state = _derive_entry_state(capital_blockers, capital_eligible)

    # ======================================================================= #
    # 2.0.0 ADDED consolidated sections (FR-1/FR-2/FR-5/FR-6).
    # Every added section is OMITTED (not emitted) when its source leaf is absent;
    # the ``contract`` dict below is assembled first with the always-present 1.1.0
    # fields, then each added section is attached only when its source exists.
    # ======================================================================= #

    contract = {
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
        # -- O19: entry_state -----------------------------------------------
        "entry_state": entry_state,
    }

    # ---- run_utc / latest_trading_day + optional meta flags (spec §2) --------
    # <- snapshot.meta.{as_of_utc, latest_trading_day, data_mode|primary_source,
    #    degraded, missing}. Each attached only if its exact leaf exists.
    meta = _dig(snapshot, "meta") or {}
    if meta.get("as_of_utc") is not None:
        contract["run_utc"] = meta["as_of_utc"]
    if meta.get("latest_trading_day") is not None:
        contract["latest_trading_day"] = meta["latest_trading_day"]
    # data_mode (or primary_source alias), degraded, missing: only if present.
    if meta.get("data_mode") is not None:
        contract["data_mode"] = meta["data_mode"]
    elif meta.get("primary_source") is not None:
        contract["data_mode"] = meta["primary_source"]
    if meta.get("degraded") is not None:
        contract["degraded"] = meta["degraded"]
    if meta.get("missing") is not None:
        contract["missing"] = meta["missing"]

    # ---- composite (verbatim leaves) -----------------------------------------
    # <- module_composite.{score,grade,action,sensitivity,confidence}. The block is
    # attached only when at least one of its leaves exists (empty composite -> omit).
    composite_block = {}
    for key in ("score", "grade", "action", "sensitivity", "confidence"):
        if composite.get(key) is not None:
            composite_block[key] = composite[key]
    if composite_block:
        contract["composite"] = composite_block

    # ---- dimensions / ev / flags (verbatim from module_composite) ------------
    if composite.get("dimensions") is not None:
        contract["dimensions"] = composite["dimensions"]
    if composite.get("ev") is not None:
        contract["ev"] = composite["ev"]
    if composite.get("flags") is not None:
        contract["flags"] = composite["flags"]

    # ---- sizing / plan / risk_units / invalidation (from tradeplan.stock_plan) -
    stock_plan = _dig(tradeplan, "stock_plan") or {}
    if stock_plan.get("sizing") is not None:
        contract["sizing"] = stock_plan["sizing"]

    # plan: entries + exits + dont_chase (each verbatim, attach only if present).
    plan_block = {}
    for key in ("entries", "exits", "dont_chase"):
        if stock_plan.get(key) is not None:
            plan_block[key] = stock_plan[key]
    if plan_block:
        contract["plan"] = plan_block

    if stock_plan.get("risk_units") is not None:
        contract["risk_units"] = stock_plan["risk_units"]

    # invalidation: technical leg (+ FR-6 operator, read DEFENSIVELY -- may be
    # absent in the current on-disk bundle since the trade_plan author adds it going
    # forward; carry None if absent) + fundamental leg. Verbatim otherwise.
    invalidation_src = _dig(stock_plan, "invalidation") or {}
    invalidation_block = {}
    technical_leg = invalidation_src.get("technical_leg")
    if isinstance(technical_leg, dict):
        tech = {}
        if technical_leg.get("condition") is not None:
            tech["condition"] = technical_leg["condition"]
        if technical_leg.get("level") is not None:
            tech["level"] = technical_leg["level"]
        # FR-6 enum: carried through; None when the trade_plan author has not yet
        # emitted it (defensive .get -- never fabricate an enum).
        tech["operator"] = technical_leg.get("operator")
        invalidation_block["technical"] = tech
    fundamental_leg = invalidation_src.get("fundamental_leg")
    if fundamental_leg is not None:
        invalidation_block["fundamental"] = fundamental_leg
    if invalidation_block:
        contract["invalidation"] = invalidation_block

    # ---- expression (verbatim from tradeplan.expression + options leaves) -----
    expression_src = _dig(tradeplan, "expression") or {}
    expression_block = {}
    for key in ("executable", "recommended_for_profile",
                "recommended_for_profile_options_tilted", "structures_selected",
                "catalyst_in_thesis", "days_to_catalyst", "mode_per_profile",
                "rule_version", "selector_fired"):
        if expression_src.get(key) is not None:
            expression_block[key] = expression_src[key]
    if options.get("liquidity_verdict") is not None:
        expression_block["options_liquidity_verdict"] = options["liquidity_verdict"]
    if options.get("declined") is not None:
        expression_block["options_declined"] = options["declined"]
    if expression_block:
        contract["expression"] = expression_block

    # ---- valuation_anchors / coverage (verbatim from coverage/*.json) --------
    if valuation_anchors is not None:
        contract["valuation_anchors"] = valuation_anchors
    if coverage_manifest is not None:
        contract["coverage"] = coverage_manifest

    # ---- catalysts (FR-2, assembled; the ONE derivation is days_out) ---------
    # as_of_date <- composite.as_of or snapshot.meta.as_of_utc[:10] (ISO date the
    # days_out gaps are measured from).
    as_of_date = composite.get("as_of")
    if not as_of_date:
        as_of_utc = meta.get("as_of_utc")
        as_of_date = as_of_utc[:10] if isinstance(as_of_utc, str) else None
    catalysts = build_catalysts(snapshot, tradeplan, as_of_date)
    if catalysts:
        contract["catalysts"] = catalysts

    # ---- thesis (FR-5 id; omitted whole when coverage_manifest absent) -------
    thesis = build_thesis(
        ticker=contract.get("ticker"),
        coverage_manifest=coverage_manifest,
        composite_flags=composite.get("flags"),
    )
    if thesis is not None:
        contract["thesis"] = thesis

    # ---- rubric_versions (verbatim assembly; omit any absent module) ---------
    rubric_versions = build_rubric_versions(docs, composite)
    if rubric_versions:
        contract["rubric_versions"] = rubric_versions

    return contract


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


def _load_coverage(bundle, name):
    """Load a ``coverage/<name>`` file, resolving it relative to the bundle then to
    the bundle's PARENT.

    In the live layout coverage lives at ``<ticker_dir>/coverage/`` -- i.e. a SIBLING
    of the report bundle ``<ticker_dir>/detail_reports_<date>/``, not inside it. We
    check ``<bundle>/coverage/<name>`` first (self-contained bundles) and fall back
    to ``<bundle>/../coverage/<name>`` (the live sibling layout). Missing -> None.
    """
    in_bundle = os.path.join(bundle, "coverage", name)
    if os.path.isfile(in_bundle):
        return _load_json(in_bundle)
    sibling = os.path.join(os.path.dirname(os.path.normpath(bundle)), "coverage", name)
    if os.path.isfile(sibling):
        return _load_json(sibling)
    return None


def load_docs(bundle):
    """Load the subset of bundle docs build_contract reads. Missing files map to
    None -- build_contract degrades field-by-field.

    2.0.0 expands the load set to the full module family plus the two coverage
    leaves the contract consolidates. ``coverage/*`` are resolved via
    ``_load_coverage`` (bundle-local, then the sibling ``../coverage/`` live layout).
    """
    snap_path = _find_snapshot(bundle)
    return {
        "snapshot": _load_json(snap_path) if snap_path else None,
        "module_composite": _load_json(os.path.join(bundle, "module_composite.json")),
        "module_tradeplan": _load_json(os.path.join(bundle, "module_tradeplan.json")),
        "module_fundamental": _load_json(os.path.join(bundle, "module_fundamental.json")),
        "module_technical": _load_json(os.path.join(bundle, "module_technical.json")),
        "module_sentiment": _load_json(os.path.join(bundle, "module_sentiment.json")),
        "module_risk": _load_json(os.path.join(bundle, "module_risk.json")),
        "module_options": _load_json(os.path.join(bundle, "module_options.json")),
        "module_context": _load_json(os.path.join(bundle, "module_context.json")),
        "valuation_anchors": _load_coverage(bundle, "valuation_anchors.json"),
        "coverage_manifest": _load_coverage(bundle, "coverage_manifest.json"),
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
    emit_json(contract, out)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
