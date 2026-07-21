"""Trade-plan decision skill (L3) for the trading-desk plugin.

WHY THIS MODULE EXISTS: the composite tells you WHETHER a name is a buy; this module
tells you HOW to put the position on. It turns the composite's expected-value block,
the technical S/R ladder, and the risk downside_map into an EXECUTABLE plan: a
mechanical entry ladder (anchored on confluences of proven support and valuation
anchors), profit-take and bull targets, a BOTH-LEG invalidation (a trade stop AND a
thesis-invalidation metric), Kelly-arithmetic sizing, a hedge spec, and a don't-chase
line. It also encodes the EXPRESSION decision -- stock vs options -- as a decision
table of record (expression-v1.0.0) that formalizes the project owner's lived rule:
**a catalyst in sight selects options for leverage; the horizon profile only
implements**. The LLM layer narrates and picks actual strikes downstream; every level
and every number here is minted in Python from module outputs -- nothing is invented.

DESIGN CONTRACT (project-wide, mirrors composite-score):
- ``INPUT_FIELDS`` is EMPTY. This module scores no snapshot field directly -- it
  consumes module outputs (composite EV, technical ladder, risk downside_map) and
  reads a handful of snapshot facts (price.last, next-earnings date, iv_pctile,
  iv_minus_rv, eps_ntm) only as PLAN references, never as scored rubric inputs. The
  single-mapping rule is therefore preserved BY CONSTRUCTION (an empty scored set
  collides with nothing); the module is added to tests/test_single_mapping.py for
  uniformity with the same guarantee as composite-score.
- ALL sizing / EV / required-multiple arithmetic is delegated to scripts.ev_kelly
  (ev_at, kelly, size_recommendation) -- this module never re-derives an expected
  value or a Kelly fraction; it calls the library and formats the result.
- Two passes. ``--stock-plan`` (pass 1) mints the plan + a PRELIMINARY expression
  from the decision table. ``--synthesize`` (pass 2) re-reads the plan +
  module_options.json and folds the options module's chosen structures into the
  expression. Pass 1 requires module_composite.json (exit 2 if missing) and the two
  judgment-flag groups (catalyst-in-thesis; fundamental-invalidation). Pass 2
  requires module_options.json (exit 2 if missing).

EXPRESSION DECISION TABLE (expression-v1.0.0):
    RULE 1 (selector): days_to_catalyst <= 60 AND catalyst-in-thesis=yes ->
        "options-tilted" for ALL profiles (the catalyst formalizes leverage-for-event).
    RULE 2 (default): otherwise the per-profile default expression.
    MODULATORS (appended in order): iv_minus_rv >= +0.05 (premium-selling); <= -0.05
        (long-premium viable); days_to_catalyst <= 30 (defined-risk only into event).

stdlib-only; ≥3.10 guard. The plan-building functions are pure over parsed inputs.
"""

import argparse
import glob
import json
import os
import sys


if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation (``python3 scripts/trade_plan.py``): ensure the repo root
# is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import build_snapshot, ev_kelly

RUBRIC_VERSION = "1.1.0"
SKILL_NAME = "trade-plan"
EXPRESSION_RULE_VERSION = "expression-v1.0.0"

# PROVISIONAL (tradeplan-v1.1.0, Wave 3B / Philosophy A). Stamped into the module
# note so a reader knows the min(PT, comps_high) triangulation is review's first
# formula, subject to the SKILL falsifier -- not a calibrated default.
_PROVISIONAL_NOTE = (
    "tradeplan-v1.1.0 PROVISIONAL: bull-target triangulation clips the raw max "
    "scenario PT to min(max_scenario_PT, comps_high) when coverage anchors exist "
    "(raw preserved in bull_target.scenario_raw; dcf_bull shown as reference). The "
    "conservative min() is review's first formula, subject to the SKILL falsifier.")

# This module scores NO snapshot field directly (it consumes module outputs and reads
# snapshot facts only as plan references). Empty by construction -> single-mapping safe.
INPUT_FIELDS = set()

# No fields gate/cap a scoring branch here (there is no scoring), so GUARD_FIELDS is
# empty (declared for parity with the scorers and the governance test's getattr).
GUARD_FIELDS = set()

# Confluence tolerance: a candidate support within this relative distance of a
# valuation anchor is labeled confluent.
_CONFLUENCE_PCT = 0.03

# Minimum relative spacing between successive entries (distinct, >=3% apart).
_ENTRY_SPACING = 0.03

# Types the market has actually defended -> eligible as proven support (mirrors
# levels._PROVEN_SUPPORT_TYPES).
_PROVEN_SUPPORT_TYPES = {"swing_low", "ma50", "ma200", "put_wall"}

# Don't-chase convention: 5% above the top entry.
_DONT_CHASE_PCT = 0.05

# Hedge: fires when binary-event size >= this, OR iv_pctile <= the cheap threshold.
_HEDGE_SIZE_THRESHOLD = 0.05
_HEDGE_IV_PCTILE_THRESHOLD = 25
_HEDGE_PREMIUM_CAP_PCT = 0.015

# Expression selector + modulator thresholds.
_CATALYST_SELECTOR_DAYS = 60
_DEFINED_RISK_DAYS = 30
_IV_RICH_THRESHOLD = 0.05
_IV_CHEAP_THRESHOLD = -0.05

_PROFILES = ("trader", "balanced", "long-term")


def _fmt(x):
    """Compact number formatting for arithmetic strings (stable across runs)."""
    if x is None:
        return "n/a"
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return f"{x:g}"


def _clean(x):
    """Normalize a numeric to int when integral, else round to 4 dp for stable
    JSON. Keeps 0.75, 6.0->6, 13.5 exact while avoiding float noise."""
    if x is None:
        return None
    xf = float(x)
    if xf.is_integer():
        return int(xf)
    return round(xf, 4)


# --------------------------------------------------------------------------- #
# Catalyst timing.
# --------------------------------------------------------------------------- #

def days_to_catalyst(as_of, earnings_date):
    """Calendar days from ``as_of`` to ``earnings_date`` (YYYY-MM-DD strings).

    Returns a signed int (positive == earnings in the future), or None if either
    date is missing or unparseable.
    """
    a = build_snapshot._parse_date(as_of)
    e = build_snapshot._parse_date(earnings_date)
    if a is None or e is None:
        return None
    return (e - a).days


def binary_event_within_30d(days):
    """True iff ``days`` is a non-None value in [0, 30] (an event ahead, within 30d)."""
    return days is not None and 0 <= days <= 30


# --------------------------------------------------------------------------- #
# Entry ladder.
# --------------------------------------------------------------------------- #

def valuation_anchors(ev, downside_map):
    """The valuation anchor set: the composite's ev_breakeven_entry plus every
    downside_map row of type ``valuation_floor``. Returns a list of levels (floats).
    """
    anchors = []
    be = ev.get("ev_breakeven_entry")
    if be is not None:
        anchors.append(float(be))
    for row in downside_map or []:
        if row.get("type") == "valuation_floor" and row.get("level") is not None:
            anchors.append(float(row["level"]))
    return anchors


def confluence_for(level, anchors):
    """Return (True, anchor_level) if ``level`` is within 3% (relative) of any
    valuation anchor, else (False, None). The NEAREST qualifying anchor is chosen.
    """
    best = None
    best_dist = None
    for a in anchors:
        if a == 0:
            continue
        dist = abs(level / a - 1)
        if dist <= _CONFLUENCE_PCT and (best_dist is None or dist < best_dist):
            best = a
            best_dist = dist
    if best is None:
        return False, None
    return True, best


def _proven_supports_below(last, ladder):
    """Proven-type ladder entries strictly below ``last``, highest-first."""
    out = [e for e in ladder
           if e.get("level") is not None and e["level"] < last
           and e.get("type") in _PROVEN_SUPPORT_TYPES]
    out.sort(key=lambda e: e["level"], reverse=True)
    return out


def _entry_record(level, entry_type, anchors, scenarios, sized_down=False):
    """Assemble one entry record with basis, condition template, EV-at-level."""
    conf, anchor = confluence_for(level, anchors)
    if conf:
        basis = f"{entry_type}, confluent with valuation anchor {_fmt(_clean(anchor))}"
        condition = (f"resting limit at {_fmt(_clean(level))} ({entry_type}, "
                     f"confluent with {_fmt(_clean(anchor))})")
    else:
        basis = entry_type
        condition = f"resting limit at {_fmt(_clean(level))} ({entry_type})"
    rec = {
        "level": _clean(level),
        "type": entry_type,
        "basis": basis,
        "confluence": conf,
        "confluence_anchor": _clean(anchor) if conf else None,
        "condition": condition,
        "ev_at_level": _clean(ev_kelly.ev_at(scenarios, level)),
    }
    if sized_down:
        rec["sized_down"] = True
    return rec


def build_entries(last, ladder, ev, downside_map):
    """Mint the entry ladder (max 3 entries, distinct, >=3% apart).

    Valuation anchors = {ev_breakeven_entry} ∪ {downside_map valuation_floor rows}.
    Candidate supports = ladder proven-type entries below ``last``, highest-first.

    entry_1:
      - if ev.ev_at_current >= ev.hurdle_total -> the CURRENT price, sized down
        (half of the recommended size), because EV already clears the hurdle;
      - else the highest CONFLUENCE below ``last`` (a candidate support within 3%
        of a valuation anchor), falling back to the highest proven support below.
    entry_2/entry_3: successively lower confluences/proven supports, each >= 3%
      below the previously accepted entry.
    """
    scenarios = ev.get("scenarios") or []
    anchors = valuation_anchors(ev, downside_map)
    candidates = _proven_supports_below(last, ladder)

    ev_at_current = ev.get("ev_at_current")
    hurdle = ev.get("hurdle_total")

    entries = []

    if (ev_at_current is not None and hurdle is not None
            and ev_at_current >= hurdle):
        # EV already clears the hurdle -> take the current price, sized down.
        entries.append(_entry_record(
            last, "current_price", anchors, scenarios, sized_down=True))
        # supplementary lower entries from proven supports below.
        remaining = candidates
    else:
        # Highest confluence below last, else highest proven support below.
        confluent = [e for e in candidates
                     if confluence_for(e["level"], anchors)[0]]
        if confluent:
            first = confluent[0]["level"]
            first_type = confluent[0]["type"]
        elif candidates:
            first = candidates[0]["level"]
            first_type = candidates[0]["type"]
        else:
            return []   # no proven support below and EV below hurdle -> no ladder.
        entries.append(_entry_record(first, first_type, anchors, scenarios))
        remaining = [e for e in candidates if e["level"] < first]

    # Fill entry_2/entry_3 from remaining proven supports, enforcing spacing.
    for cand in remaining:
        if len(entries) >= 3:
            break
        prev_level = entries[-1]["level"]
        if cand["level"] <= prev_level * (1 - _ENTRY_SPACING) + 1e-9:
            entries.append(_entry_record(
                cand["level"], cand["type"], anchors, scenarios))

    return entries


# --------------------------------------------------------------------------- #
# Exits.
# --------------------------------------------------------------------------- #

def _nearest_resistance_above(last, ladder):
    """Lowest-level ladder entry strictly above ``last`` (any type), or None."""
    above = [e for e in ladder if e.get("level") is not None and e["level"] > last]
    if not above:
        return None
    return min(above, key=lambda e: e["level"])


def build_exits(last, ladder, scenarios, eps_ntm, dcf_bull=None, comps_high=None):
    """profit_take = nearest ladder resistance above last (level + type);
    bull_target = the bull price target, with required_multiple = target /
    eps_ntm_consensus (null-safe -> None if eps_ntm missing).

    Goal B (PROVISIONAL) -- bull-target triangulation from coverage anchors:
      raw = max scenario price_target (the LLM's scenario input, always preserved
        in ``bull_target.scenario_raw``).
      When ``comps_high`` is present, the bull target is CLIPPED conservatively to
        ``min(raw, comps_high)`` -- the desk's own coverage comps range caps a raw
        scenario bull that exceeds it. ``dcf_bull`` is carried as a DISPLAYED
        reference (never the clip driver). No anchors -> unchanged (``raw``),
        disclosed via the (null) anchor fields.
    ``required_multiple`` is computed off the (triangulated) ``level``.
    """
    res = _nearest_resistance_above(last, ladder)
    profit_take = None
    if res is not None:
        profit_take = {"level": _clean(res["level"]), "type": res.get("type")}

    scenario_raw = max((sc["price_target"] for sc in scenarios), default=None)

    # Triangulate: clip the raw scenario bull to comps_high when present (min ==
    # conservative, the provisional formula). dcf_bull is a displayed reference.
    level = scenario_raw
    triangulated = False
    if scenario_raw is not None and comps_high is not None:
        level = min(scenario_raw, comps_high)
        triangulated = True

    required_multiple = None
    if level is not None and eps_ntm not in (None, 0):
        required_multiple = _clean(level / eps_ntm)

    if triangulated:
        base = (f"triangulated to min(scenario_raw {_fmt(_clean(scenario_raw))}, "
                f"comps_high {_fmt(_clean(comps_high))}) = {_fmt(_clean(level))}")
        if required_multiple is not None:
            note = base + f"; implies {required_multiple:.1f}x fwd EPS"
        else:
            note = base
    else:
        note = (f"implies {required_multiple:.1f}x fwd EPS"
                if required_multiple is not None else None)

    bull_target = {
        "level": _clean(level),
        "scenario_raw": _clean(scenario_raw),
        "dcf_bull": _clean(dcf_bull),
        "comps_high": _clean(comps_high),
        "triangulated": triangulated,
        "required_multiple": required_multiple,
        "note": note,
    }
    return {"profit_take": profit_take, "bull_target": bull_target}


# --------------------------------------------------------------------------- #
# Invalidation (BOTH legs mandatory).
# --------------------------------------------------------------------------- #

def _first_proven_support_below(level, ladder):
    """Highest proven-type ladder entry strictly below ``level``, or None."""
    below = [e for e in ladder
             if e.get("level") is not None and e["level"] < level
             and e.get("type") in _PROVEN_SUPPORT_TYPES]
    if not below:
        return None
    return max(below, key=lambda e: e["level"])


def build_invalidation(entries, ladder, fund_metric, fund_threshold,
                       fund_justification):
    """Both mandatory legs:
      technical_leg = first proven support strictly below entry_2 (or below entry_1
        if only one entry); condition "weekly close below".
      fundamental_leg = the required judgment flags (metric/threshold/justification).
    """
    if len(entries) >= 2:
        anchor_level = entries[1]["level"]
    elif entries:
        anchor_level = entries[0]["level"]
    else:
        anchor_level = None

    tech_level = None
    if anchor_level is not None:
        sup = _first_proven_support_below(anchor_level, ladder)
        if sup is not None:
            tech_level = _clean(sup["level"])

    return {
        "technical_leg": {
            "level": tech_level,
            "condition": "weekly close below",
        },
        "fundamental_leg": {
            "metric": fund_metric,
            "threshold": fund_threshold,
            "justification": fund_justification,
        },
    }


# --------------------------------------------------------------------------- #
# Sizing (full Kelly arithmetic via ev_kelly).
# --------------------------------------------------------------------------- #

def build_sizing(scenarios, entry_level, profile, binary30d):
    """Kelly sizing at ``entry_level``: full Kelly (ev_kelly.kelly) capped by
    ev_kelly.size_recommendation. Emits the full arithmetic string."""
    k = ev_kelly.kelly(scenarios, entry_level)
    s = ev_kelly.size_recommendation(k["f_star"], profile, binary30d)

    f_star = k["f_star"]
    frac_label = "quarter-Kelly" if binary30d else "half-Kelly"
    frac_val = f_star / 4 if binary30d else f_star / 2
    arithmetic = (
        f"f* {f_star:.1%} at entry {_fmt(_clean(entry_level))}; "
        f"{frac_label} {frac_val:.1%}; "
        f"binary_event_within_30d={binary30d}; "
        f"cap {s['cap_pct']:.1%}; "
        f"recommended {s['recommended_pct']:.1%} -- {s['rationale']}")

    # Goal D (surfacing, no arithmetic change): a one-line headline that keeps f*
    # tied to the ENTRY and the CAP so a reader never sees a bare f* (e.g. 36.7%)
    # sitting next to a 4% cap without the entry context that justifies the gap.
    headline = (
        f"f* {f_star:.1%} at entry {_fmt(_clean(entry_level))}; "
        f"capped to {s['recommended_pct']:.1%} ({s['cap_pct']:.1%} cap)")

    return {
        "entry_level": _clean(entry_level),
        "profile": profile,
        "binary_event_within_30d": binary30d,
        "f_star": _clean(f_star),
        "half": _clean(k["half"]),
        "quarter": _clean(k["quarter"]),
        "recommended_pct": _clean(s["recommended_pct"]),
        "cap_pct": _clean(s["cap_pct"]),
        "rationale": s["rationale"],
        "headline": headline,
        "arithmetic": arithmetic,
    }


# --------------------------------------------------------------------------- #
# Hedge.
# --------------------------------------------------------------------------- #

def build_hedge(binary30d, recommended_pct, iv_pctile, downside_map):
    """Hedge spec. Required iff:
      (binary30d AND recommended_pct >= 0.05) OR
      (iv_pctile is not None AND iv_pctile <= 25).
    ``trigger`` names which clause fired (both, if both).
    """
    binary_clause = bool(binary30d and recommended_pct is not None
                         and recommended_pct >= _HEDGE_SIZE_THRESHOLD)
    iv_clause = iv_pctile is not None and iv_pctile <= _HEDGE_IV_PCTILE_THRESHOLD

    reasons = []
    if binary_clause:
        reasons.append(
            f"binary event within 30d with recommended size "
            f"{recommended_pct:.1%} >= {_HEDGE_SIZE_THRESHOLD:.0%}")
    if iv_clause:
        reasons.append(
            f"iv_pctile_1yr {_fmt(iv_pctile)} <= {_HEDGE_IV_PCTILE_THRESHOLD} "
            f"(cheap protection)")

    required = binary_clause or iv_clause
    if not required:
        return {"required": False, "trigger": None}

    # strikes_from = first two downside_map rows (levels).
    strikes_from = [_clean(row["level"]) for row in (downside_map or [])[:2]
                    if row.get("level") is not None]

    return {
        "required": True,
        "trigger": "; ".join(reasons),
        "structure": "put spread or collar",
        "strikes_from": strikes_from,
        "expiry_rule": "first monthly expiry after the event",
        "premium_cap_pct": _HEDGE_PREMIUM_CAP_PCT,
    }


# --------------------------------------------------------------------------- #
# Don't-chase.
# --------------------------------------------------------------------------- #

def build_dont_chase(top_entry_level):
    """Don't-chase line: 5% above the top entry."""
    return {
        "above": _clean(top_entry_level * (1 + _DONT_CHASE_PCT)),
        "convention": f"{_DONT_CHASE_PCT:.0%} above top entry",
    }


# --------------------------------------------------------------------------- #
# Expression decision table (expression-v1.0.0).
# --------------------------------------------------------------------------- #

# RULE 1 selector output: catalyst-in-sight tilts EVERY profile toward options.
_RULE1_MODES = {
    "trader": ("defined-risk directional spreads sized to full options allocation, "
               "tenor past catalyst"),
    "balanced": ("half stock core, half defined-risk options tenored past catalyst"),
    "long-term": ("stock core + small defined-risk options kicker tenored past "
                  "catalyst"),
}

# RULE 2 default: per-profile default expression when no catalyst selects options.
_RULE2_MODES = {
    "trader": "options-favored: defined-risk spreads 30-90 DTE",
    "balanced": ("mixed: stock core + CSP at entry_1; LEAPS if IV cheap vs realized"),
    "long-term": ("stock-dominant; options only as CSP entry enhancement or event "
                  "hedge"),
}


def decide_expression(days_to_catalyst, catalyst_in_thesis, profile, iv_minus_rv):
    """The expression decision (stock vs options), expression-v1.0.0.

    Selector (RULE 1): days_to_catalyst <= 60 AND catalyst_in_thesis -> options
    tilt for ALL profiles. Otherwise (RULE 2) the per-profile default.
    Modulators (appended in order): iv rich (>= +0.05) -> premium-selling;
    iv cheap (<= -0.05) -> long-premium viable; days <= 30 -> defined-risk only.
    """
    selector_catalyst = (days_to_catalyst is not None
                         and days_to_catalyst <= _CATALYST_SELECTOR_DAYS
                         and catalyst_in_thesis)

    if selector_catalyst:
        selector_fired = "catalyst"
        mode_per_profile = dict(_RULE1_MODES)
    else:
        selector_fired = "profile-default"
        mode_per_profile = dict(_RULE2_MODES)

    modulators = []
    if iv_minus_rv is not None:
        if iv_minus_rv >= _IV_RICH_THRESHOLD:
            modulators.append(
                "IV rich vs realized: prefer premium-selling structures")
        elif iv_minus_rv <= _IV_CHEAP_THRESHOLD:
            modulators.append(
                "IV cheap vs realized: long-premium structures viable")
    if days_to_catalyst is not None and days_to_catalyst <= _DEFINED_RISK_DAYS:
        modulators.append("defined-risk only into the event")

    return {
        "rule_version": EXPRESSION_RULE_VERSION,
        "selector_fired": selector_fired,
        "days_to_catalyst": days_to_catalyst,
        "catalyst_in_thesis": catalyst_in_thesis,
        "mode_per_profile": mode_per_profile,
        "modulators": modulators,
        "recommended_for_profile": mode_per_profile[profile],
    }


# --------------------------------------------------------------------------- #
# Bundle I/O.
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _load_json(path):
    """Load a JSON file, or None if absent."""
    if not os.path.isfile(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def _find_valuation_anchors(bundle):
    """Locate the coverage ``valuation_anchors.json`` for a bundle, or None.

    Optional-existence pattern (mirrors score_fundamental --anchors): the anchors
    file lives in the bundle's SIBLING ``coverage/`` dir under the ticker's
    ``trading_desk_<T>/`` root (the bundle is ``trading_desk_<T>/detail_reports_*``,
    coverage is ``trading_desk_<T>/coverage/valuation_anchors.json``). Absent -> None
    (the plan is unchanged, disclosed). This is the coverage_distilled path; on the
    web_compressed floor there is no coverage dir, so None is the correct result.
    """
    parent = os.path.dirname(os.path.abspath(bundle))
    candidate = os.path.join(parent, "coverage", "valuation_anchors.json")
    if os.path.isfile(candidate):
        return candidate
    return None


def load_valuation_anchors(bundle):
    """Return the (dcf_bull, comps_high) anchor pair for a bundle, or (None, None).

    Reads the coverage ``valuation_anchors.json`` (same shape score_fundamental
    consumes: dcf_base/dcf_bear/dcf_bull/comps_low/comps_high). A missing file, a
    malformed JSON, or a missing/non-numeric key returns (None, None) -- the plan
    stays on the raw scenario bull, disclosed. trade-plan does NOT re-validate the
    anchors (score_fundamental/score_risk are the validation backstop that exits 2
    on a malformed file upstream); here an unreadable anchors file simply means no
    triangulation, never a crash.
    """
    path = _find_valuation_anchors(bundle)
    if path is None:
        return None, None
    try:
        with open(path) as fh:
            anchors = json.load(fh)
    except (OSError, ValueError):
        return None, None
    if not isinstance(anchors, dict):
        return None, None
    dcf_bull = anchors.get("dcf_bull")
    comps_high = anchors.get("comps_high")
    dcf_bull = dcf_bull if isinstance(dcf_bull, (int, float)) else None
    comps_high = comps_high if isinstance(comps_high, (int, float)) else None
    return dcf_bull, comps_high


def build_stock_plan_module(composite, technical, risk, snapshot, profile,
                            catalyst_in_thesis, catalyst_in_thesis_justification,
                            fund_metric, fund_threshold, fund_justification,
                            dcf_bull=None, comps_high=None):
    """Assemble the full module_tradeplan.json document (pass 1).

    ``dcf_bull`` / ``comps_high`` are the optional coverage anchors (Goal B): when
    ``comps_high`` is present the bull target triangulates to min(raw, comps_high);
    both None -> the plan is unchanged (raw scenario bull), disclosed.
    """
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
    price = snapshot.get("price", {}) if isinstance(snapshot, dict) else {}
    events = snapshot.get("events", {}) if isinstance(snapshot, dict) else {}
    sentiment = snapshot.get("sentiment", {}) if isinstance(snapshot, dict) else {}
    options = snapshot.get("options") if isinstance(snapshot, dict) else None
    fundamentals = snapshot.get("fundamentals", {}) if isinstance(snapshot, dict) else {}

    last = price.get("last")
    as_of = build_snapshot._as_of_date(meta.get("as_of_utc"))

    ne = events.get("next_earnings") if isinstance(events, dict) else None
    earnings_date = ne.get("date") if isinstance(ne, dict) else None
    dtc = days_to_catalyst(as_of, earnings_date)
    binary30d = binary_event_within_30d(dtc)

    iv_pctile = sentiment.get("iv_pctile_1yr") if isinstance(sentiment, dict) else None
    iv_minus_rv = options.get("iv_minus_rv20") if isinstance(options, dict) else None
    eps_ntm = fundamentals.get("eps_ntm_consensus") if isinstance(fundamentals, dict) else None

    ev = composite.get("ev", {}) if isinstance(composite, dict) else {}
    scenarios = ev.get("scenarios") or []
    ladder = technical.get("ladder") or [] if isinstance(technical, dict) else []
    downside_map = []
    if isinstance(risk, dict):
        downside_map = (risk.get("tables", {}) or {}).get("downside_map") or []

    # -- entry ladder ------------------------------------------------------
    entries = build_entries(last, ladder, ev, downside_map)
    entry_1_level = entries[0]["level"] if entries else last

    # -- exits (bull target triangulated against coverage anchors, Goal B) --
    exits = build_exits(last, ladder, scenarios, eps_ntm,
                        dcf_bull=dcf_bull, comps_high=comps_high)

    # -- invalidation ------------------------------------------------------
    invalidation = build_invalidation(entries, ladder, fund_metric,
                                      fund_threshold, fund_justification)

    # -- sizing (at entry_1 level) -----------------------------------------
    sizing = build_sizing(scenarios, entry_1_level, profile, binary30d)

    # -- hedge -------------------------------------------------------------
    hedge = build_hedge(binary30d, sizing["recommended_pct"], iv_pctile,
                        downside_map)

    # -- don't-chase -------------------------------------------------------
    dont_chase = build_dont_chase(entry_1_level)

    # -- expression (preliminary) ------------------------------------------
    expression = decide_expression(dtc, catalyst_in_thesis, profile, iv_minus_rv)
    expression["catalyst_in_thesis_justification"] = catalyst_in_thesis_justification
    expression["synthesized"] = False

    return {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "ticker": meta.get("ticker"),
        "as_of": as_of,
        "profile": profile,
        "stock_plan": {
            "entries": entries,
            "exits": exits,
            "invalidation": invalidation,
            "sizing": sizing,
            "hedge": hedge,
            "dont_chase": dont_chase,
        },
        "expression": expression,
        "flags": {
            "catalyst_in_thesis": catalyst_in_thesis,
            "catalyst_in_thesis_justification": catalyst_in_thesis_justification,
            "fund_invalidation_metric": fund_metric,
            "fund_invalidation_threshold": fund_threshold,
            "fund_invalidation_justification": fund_justification,
        },
        "event_playbook": None,   # LLM prose slot.
        "note": _PROVISIONAL_NOTE,
        "signal": None,
    }


def synthesize(plan, options_module):
    """Pass 2: fold the options module's chosen structures into the expression.

    Selects structures from ``recommended_structures`` (each carrying strikes).
    If a recommended structure lacks strikes, the caller exits 2 (consistency).
    ``hedge_structure`` is the options module's hedge spec iff the plan's hedge is
    required. Mutates and returns the plan.
    """
    recommended = options_module.get("recommended_structures") or []
    structures_selected = []
    for st in recommended:
        strikes = st.get("strikes")
        if not strikes:
            raise ValueError(
                f"recommended structure {st.get('name')!r} has no strikes in "
                f"module_options.json (consistency: strikes must be present)")
        structures_selected.append({
            "name": st.get("name"),
            "strikes": [_clean(s) for s in strikes],
            "expiry": st.get("expiry"),
        })

    hedge_required = bool(plan.get("stock_plan", {}).get("hedge", {}).get("required"))
    # The options module's contract names this key "hedge_structure"; accept the
    # legacy "hedge" as a fallback so older module files still synthesize.
    hedge_structure = None
    if hedge_required:
        hedge_structure = options_module.get("hedge_structure",
                                             options_module.get("hedge"))

    plan["expression"]["synthesized"] = True
    plan["expression"]["structures_selected"] = structures_selected
    plan["expression"]["hedge_structure"] = hedge_structure
    # An options-tilted expression with zero executable structures is a trap for
    # the reader (Gate-3 finding, AAPL: neutral+cheap vol declined everything while
    # the expression still read "options tenored past catalyst"). Disclose it.
    plan["expression"]["executable"] = bool(structures_selected)
    if not structures_selected:
        # Preserve the original options-tilted text for the record, then LEAD the
        # recommendation with the executable (stock) leg so a reader acts on the
        # leg that is actually implementable, not the gated options tilt (Goal D).
        options_tilted = plan["expression"].get("recommended_for_profile")
        plan["expression"]["recommended_for_profile_options_tilted"] = options_tilted
        plan["expression"]["recommended_for_profile"] = (
            _stock_fallback_expression(plan)
            + " (options gated -- implement in stock)")
        plan["expression"]["executability_note"] = (
            "no options structures survived the options module's vol/liquidity/event "
            "gates (see module_options declined + liquidity_verdict) -- implement the "
            "expression in STOCK per the stock plan until conditions change")
    return plan


def _stock_fallback_expression(plan):
    """The executable stock-leg recommendation when options are gated out.

    Leads with the concrete stock action from the plan (buy the entry ladder,
    size to the recommended %) so the reader acts on the implementable leg. Reads
    only fields the plan already carries -- no arithmetic.
    """
    sp = plan.get("stock_plan", {}) or {}
    entries = sp.get("entries") or []
    sizing = sp.get("sizing", {}) or {}
    entry_1 = entries[0].get("level") if entries else None
    rec = sizing.get("recommended_pct")
    rec_txt = f"{rec:.1%}" if isinstance(rec, (int, float)) else "the recommended size"
    if entry_1 is not None:
        return (f"stock: buy the entry ladder from {_fmt(_clean(entry_1))}, "
                f"sized to {rec_txt}")
    return f"stock: buy the entry ladder, sized to {rec_txt}"


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #

def _run_stock_plan(args):
    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 2

    composite = _load_json(os.path.join(args.bundle, "module_composite.json"))
    if composite is None:
        print("ERROR: module_composite.json missing -- run composite-score first",
              file=sys.stderr)
        return 2

    # -- required judgment flags -------------------------------------------
    if not args.catalyst_in_thesis:
        print("ERROR: --catalyst-in-thesis is required (yes|no): does the thesis "
              "actually lean on the upcoming event?", file=sys.stderr)
        return 2
    if not args.catalyst_in_thesis_justification:
        print("ERROR: --catalyst-in-thesis-justification is required alongside "
              "--catalyst-in-thesis.", file=sys.stderr)
        return 2
    if not args.fund_invalidation_metric:
        print("ERROR: --fund-invalidation-metric is required (name a real "
              "thesis-pillar metric).", file=sys.stderr)
        return 2
    if not args.fund_invalidation_threshold:
        print("ERROR: --fund-invalidation-threshold is required alongside "
              "--fund-invalidation-metric.", file=sys.stderr)
        return 2
    if not args.fund_invalidation_justification:
        print("ERROR: --fund-invalidation-justification is required alongside "
              "--fund-invalidation-metric.", file=sys.stderr)
        return 2

    technical = _load_json(os.path.join(args.bundle, "module_technical.json"))
    risk = _load_json(os.path.join(args.bundle, "module_risk.json"))

    snap_path = _find_snapshot(args.bundle)
    if snap_path is None:
        print(f"ERROR: no snapshot_*.json in {args.bundle}", file=sys.stderr)
        return 2
    try:
        with open(snap_path) as fh:
            snapshot = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read snapshot {snap_path}: {exc}", file=sys.stderr)
        return 2

    if snapshot.get("price", {}).get("last") is None:
        print(f"ERROR: snapshot {snap_path} has no price.last.", file=sys.stderr)
        return 2

    # Profile: explicit flag > composite's profile > balanced.
    profile = args.profile or composite.get("profile") or "balanced"

    # The yes|no selector flag becomes a boolean for the decision table.
    catalyst_in_thesis = args.catalyst_in_thesis == "yes"

    # Goal B (PROVISIONAL): conditionally load the coverage valuation anchors from
    # the bundle's sibling coverage dir (optional-existence). Absent -> (None, None)
    # -> unchanged bull target, disclosed.
    dcf_bull, comps_high = load_valuation_anchors(args.bundle)

    doc = build_stock_plan_module(
        composite, technical, risk, snapshot, profile,
        catalyst_in_thesis, args.catalyst_in_thesis_justification,
        args.fund_invalidation_metric, args.fund_invalidation_threshold,
        args.fund_invalidation_justification,
        dcf_bull=dcf_bull, comps_high=comps_high)

    out = args.out or os.path.join(args.bundle, "module_tradeplan.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(out)
    return 0


def _run_synthesize(args):
    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 2

    plan_path = os.path.join(args.bundle, "module_tradeplan.json")
    plan = _load_json(plan_path)
    if plan is None:
        print("ERROR: module_tradeplan.json missing -- run --stock-plan first",
              file=sys.stderr)
        return 2

    options_module = _load_json(os.path.join(args.bundle, "module_options.json"))
    if options_module is None:
        print("ERROR: module_options.json missing -- run options-strategy first",
              file=sys.stderr)
        return 2

    try:
        plan = synthesize(plan, options_module)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    out = args.out or plan_path
    with open(out, "w") as fh:
        json.dump(plan, fh, indent=2, sort_keys=True)
    print(out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Trade-plan decision skill (rubric v%s): turn the composite EV "
                    "block + ladder + downside_map into an executable plan, and "
                    "decide the expression (stock vs options) via decision table %s."
                    % (RUBRIC_VERSION, EXPRESSION_RULE_VERSION))
    parser.add_argument("--bundle", required=True, help="bundle directory")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stock-plan", action="store_true",
                      help="pass 1: mint the plan + preliminary expression")
    mode.add_argument("--synthesize", action="store_true",
                      help="pass 2: fold options-strategy structures into expression")
    parser.add_argument("--profile", default=None,
                        choices=_PROFILES,
                        help="scoring profile (default: composite's profile)")
    # Expression selector flag (REQUIRED for --stock-plan).
    parser.add_argument("--catalyst-in-thesis", default=None,
                        choices=("yes", "no"),
                        help="does the thesis lean on the upcoming catalyst? "
                             "(REQUIRED for --stock-plan)")
    parser.add_argument("--catalyst-in-thesis-justification", default=None,
                        help="one-line justification (REQUIRED for --stock-plan)")
    # Fundamental invalidation leg (REQUIRED for --stock-plan).
    parser.add_argument("--fund-invalidation-metric", default=None,
                        help="thesis-pillar metric name (REQUIRED for --stock-plan)")
    parser.add_argument("--fund-invalidation-threshold", default=None,
                        help="invalidation threshold text (REQUIRED for --stock-plan)")
    parser.add_argument("--fund-invalidation-justification", default=None,
                        help="one-line justification (REQUIRED for --stock-plan)")
    parser.add_argument("--out", default=None,
                        help="output path (default <bundle>/module_tradeplan.json)")
    args = parser.parse_args(argv)

    if args.stock_plan:
        return _run_stock_plan(args)
    return _run_synthesize(args)


if __name__ == "__main__":
    sys.exit(main())
