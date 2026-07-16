"""Composite-score decision skill (L3) for the trade-decision plugin.

WHY THIS MODULE EXISTS: this is the DECISION layer. The four evidence modules
(technical-analysis, fundamental, sentiment-positioning, risk-analytics) each score
one dimension off the snapshot; this module does NOT re-read those snapshot facts.
It CONSUMES the four module JSONs' final scores, adds a fifth dimension it computes
in-script (THESIS CONVICTION), applies FIXED per-profile weights, and produces the
composite (0-100), a letter grade, an action, and an expected-value block. Its
arithmetic IS the composite rubric of record (composite rubric v1.0.0): every
weight, band edge, hurdle, and EV formula is deterministic and unit-pinned so a
report can never silently drift. The LLM layer supplies judgment (the four
conviction flags and the scenario set, each with mandatory reasoning) and writes
prose; it does no scoring arithmetic and it invents no numbers.

Design contract (project-wide, mirrors the four scorers):
- ``INPUT_FIELDS`` is EMPTY. This module scores no snapshot field directly -- it
  consumes the four module scores and reads ``price.last`` only as an EV reference
  (never scored). The single-mapping rule (each snapshot fact scores in exactly one
  module) is therefore preserved BY CONSTRUCTION: an empty scored set collides with
  nothing. The governance test (tests/test_single_mapping.py) includes this module
  and its checks stay green trivially.
- A missing evidence module excludes that dimension, the remaining weights are
  rescaled to sum 1, and the exclusion is disclosed in ``renormalization_note``.
  If >= 3 of the 5 dimensions are missing, there is not enough evidence to render a
  composite and the CLI exits 2.
- Thesis conviction is asserted, never assumed: all four judgment flags are REQUIRED
  (no defaults) and each carries a mandatory one-line justification; the scenario
  set (with mandatory reasoning) is REQUIRED too. A missing flag, missing
  justification, or missing scenario set is a hard error (exit 2).

THESIS CONVICTION (5th dimension, 0-100, computed here):
    EV asymmetry        (max 40, mechanical) -- ev / hurdle ratio banded.
    Variant perception  (max 20) -- --variant strong|some|none -> 20/12/4.
    Catalyst clarity    (max 20) -- --catalyst-clarity clear|partial|vague -> 20/12/4.
    Invalidation quality(max 20) -- --invalidation both-legs|one-leg|none -> 20/10/0.

All EV math is delegated to scripts.ev_kelly (ev_at, scenario_ev) -- this module
never re-derives an expected value; it calls the library and bands the result.

stdlib-only; the scoring functions are pure over already-parsed inputs.
"""

import argparse
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trade-decision requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation (``python3 scripts/score_composite.py``): ensure the repo
# root is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import build_snapshot, ev_kelly

RUBRIC_VERSION = "1.0.0"
SKILL_NAME = "composite-score"

# This module scores NO snapshot field directly (it consumes module scores and reads
# price.last only as an EV reference). Empty by construction -> single-mapping safe.
INPUT_FIELDS = set()

# No fields gate/cap a branch here (there are no snapshot inputs), so GUARD_FIELDS is
# empty (declared for parity with the four scorers and the governance test's
# getattr(mod, "GUARD_FIELDS", set())).
GUARD_FIELDS = set()

# The four evidence dimensions, in report order, mapped to their module file.
_EVIDENCE_DIMENSIONS = [
    ("technical", "module_technical.json"),
    ("fundamental", "module_fundamental.json"),
    ("sentiment", "module_sentiment.json"),
    ("risk", "module_risk.json"),
]

# FIXED weight table per profile (spec table; never hand-tuned). Each column sums to
# 1.0 across the five dimensions. Comparability across names > per-name
# personalization is the reason these are fixed (spec Section 9.3).
WEIGHTS = {
    "balanced":  {"technical": .25, "fundamental": .25, "sentiment": .20,
                  "risk": .15, "thesis_conviction": .15},
    "trader":    {"technical": .35, "fundamental": .10, "sentiment": .25,
                  "risk": .15, "thesis_conviction": .15},
    "long-term": {"technical": .10, "fundamental": .40, "sentiment": .15,
                  "risk": .15, "thesis_conviction": .20},
}
_PROFILES = tuple(WEIGHTS)

# Horizon convention (in years) per profile -- the EV hurdle is 0.08 * horizon_years,
# so a longer horizon demands a proportionally larger expected value to clear.
HORIZON_YEARS = {"trader": 0.5, "balanced": 1.5, "long-term": 4.0}

# Annualized-equivalent hurdle rate: 8% per horizon-year.
_HURDLE_RATE = 0.08

# Judgment-flag point tables (asserted, never defaulted).
_VARIANT_POINTS = {"strong": 20, "some": 12, "none": 4}
_CATALYST_POINTS = {"clear": 20, "partial": 12, "vague": 4}
_INVALIDATION_POINTS = {"both-legs": 20, "one-leg": 10, "none": 0}

_VARIANT_CHOICES = tuple(_VARIANT_POINTS)
_CATALYST_CHOICES = tuple(_CATALYST_POINTS)
_INVALIDATION_CHOICES = tuple(_INVALIDATION_POINTS)


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
# Thesis conviction (5th dimension, computed in-script).
# --------------------------------------------------------------------------- #

def ev_asymmetry_points(ratio) -> int:
    """Band ev/hurdle ratio -> EV-asymmetry points (max 40).

    ratio >= 2      -> 40   (expected value is >= 2x the return hurdle)
    [1.5, 2)        -> 32
    [1.0, 1.5)      -> 24   (just clears the hurdle)
    [0.5, 1.0)      -> 12   (below hurdle but positive)
    [0, 0.5)        -> 6    (barely positive)
    < 0             -> 0    (negative expected value)
    """
    if ratio >= 2:
        return 40
    if ratio >= 1.5:
        return 32
    if ratio >= 1.0:
        return 24
    if ratio >= 0.5:
        return 12
    if ratio >= 0:
        return 6
    return 0


def score_thesis_conviction(scenarios, scenario_reasoning, last, profile,
                            variant, variant_justification,
                            catalyst_clarity, catalyst_clarity_justification,
                            invalidation, invalidation_justification) -> dict:
    """Compute the thesis-conviction dimension (0-100) for the given profile.

    EV asymmetry (max 40, mechanical):
        ev = ev_kelly.ev_at(scenarios, last)  -- prob-weighted expected return.
        hurdle_total = 0.08 * horizon_years[profile].
        ratio = ev / hurdle_total, banded by ev_asymmetry_points().
    Variant perception (max 20), catalyst clarity (max 20), invalidation quality
    (max 20) are the three judgment flags -- asserted, never defaulted.

    Returns {"score", "subscore_points": {...}, "subscores": [arithmetic strings]}.
    The EV-asymmetry band uses THIS profile's hurdle; the sensitivity row recomputes
    the whole thing per each profile's hurdle.
    """
    ev = ev_kelly.ev_at(scenarios, last)
    horizon_years = HORIZON_YEARS[profile]
    hurdle_total = _HURDLE_RATE * horizon_years
    ratio = ev / hurdle_total
    ev_pts = ev_asymmetry_points(ratio)

    variant_pts = _VARIANT_POINTS[variant]
    catalyst_pts = _CATALYST_POINTS[catalyst_clarity]
    invalidation_pts = _INVALIDATION_POINTS[invalidation]

    total = ev_pts + variant_pts + catalyst_pts + invalidation_pts

    subscores = [
        (f"ev_asymmetry: ev {_fmt(_clean(ev))} / hurdle_total "
         f"{_fmt(_clean(hurdle_total))} (0.08 x horizon_years {_fmt(horizon_years)}, "
         f"{profile}) = ratio {_fmt(_clean(ratio))} -> {ev_pts}/40"),
        f"variant {variant} -> {variant_pts}/20 ({variant_justification})",
        (f"catalyst_clarity {catalyst_clarity} -> {catalyst_pts}/20 "
         f"({catalyst_clarity_justification})"),
        (f"invalidation {invalidation} -> {invalidation_pts}/20 "
         f"({invalidation_justification})"),
    ]

    return {
        "score": total,
        "subscore_points": {
            "ev_asymmetry": ev_pts,
            "variant": variant_pts,
            "catalyst_clarity": catalyst_pts,
            "invalidation": invalidation_pts,
        },
        "subscores": subscores,
    }


# --------------------------------------------------------------------------- #
# Composite weighting (fixed per-profile weights, renormalized over present dims).
# --------------------------------------------------------------------------- #

def score_composite(module_scores, thesis_conviction_score, profile) -> dict:
    """Weighted composite over PRESENT dimensions, weights rescaled to sum 1.

    ``module_scores`` maps present evidence-dimension names (technical/fundamental/
    sentiment/risk) to their module doc (a dict carrying a ``score``). A dimension
    absent from that mapping is EXCLUDED: its weight is dropped and the remaining
    present-dimension weights are rescaled so they sum to 1 (disclosed in
    ``renormalization_note``). thesis_conviction is ALWAYS present (computed here).

    Returns {"score", "dimensions": [...rows...], "renormalization_note"}.
    """
    weights = WEIGHTS[profile]

    # Assemble (name, raw_score, weight, source) for present dimensions.
    present = []
    for name, source in _EVIDENCE_DIMENSIONS:
        if name in module_scores and module_scores[name] is not None:
            raw = module_scores[name].get("score")
            if raw is not None:
                present.append((name, raw, weights[name], source))
    # thesis conviction is always present.
    present.append(("thesis_conviction", thesis_conviction_score,
                    weights["thesis_conviction"], "computed"))

    excluded = [name for name, _ in _EVIDENCE_DIMENSIONS
                if name not in module_scores or module_scores[name] is None
                or module_scores[name].get("score") is None]

    weight_sum = sum(w for _, _, w, _ in present)

    dimensions = []
    composite = 0.0
    for name, raw, weight, source in present:
        w_renorm = weight / weight_sum if weight_sum else 0.0
        contribution = w_renorm * raw
        composite += contribution
        dimensions.append({
            "name": name,
            "score": _clean(raw),
            "weight": _clean(weight),
            "weight_renormalized": _clean(w_renorm),
            "contribution": _clean(contribution),
            "source": source,
        })

    note = None
    if excluded:
        note = (f"weights renormalized over present dimensions (sum {_fmt(_clean(weight_sum))}) "
                f"-- excluded missing evidence modules: {', '.join(excluded)}")

    return {
        "score": _clean(composite),
        "dimensions": dimensions,
        "renormalization_note": note,
    }


# --------------------------------------------------------------------------- #
# Grade bands (fixed).
# --------------------------------------------------------------------------- #

def grade_for(score):
    """Fixed grade + action bands: A >=80 Buy/Add; B 60-79
    Hold/Accumulate-on-weakness; C 45-59 Hold/Trim; D <45 Reduce/Avoid."""
    if score >= 80:
        return "A", "Buy/Add"
    if score >= 60:
        return "B", "Hold/Accumulate-on-weakness"
    if score >= 45:
        return "C", "Hold/Trim"
    return "D", "Reduce/Avoid"


# --------------------------------------------------------------------------- #
# EV block.
# --------------------------------------------------------------------------- #

def build_ev_block(scenarios, scenario_reasoning, last, profile,
                   entry_levels) -> dict:
    """Assemble the EV block for the chosen profile.

    ev_at_current   = ev_kelly.ev_at(scenarios, last).
    hurdle_total    = 0.08 * horizon_years[profile].
    ev_breakeven_entry = sum(p_i * target_i) / (1 + hurdle_total).
        DERIVATION: EV at an entry E is sum(p_i*(target_i/E - 1)) = mean_target/E - 1
        where mean_target = sum(p_i*target_i). Setting that EQUAL to hurdle_total and
        solving for E gives E = mean_target / (1 + hurdle_total) -- the entry price at
        which the probability-weighted expected value exactly clears the hurdle.
    ev_at_levels    = one {level, ev} row per --entry-level (trade-plan feeds these
        later; optional now).
    """
    ev_at_current = ev_kelly.ev_at(scenarios, last)
    horizon_years = HORIZON_YEARS[profile]
    hurdle_total = _HURDLE_RATE * horizon_years

    mean_target = sum(sc["prob"] * sc["price_target"] for sc in scenarios)
    ev_breakeven_entry = mean_target / (1 + hurdle_total)

    ev_at_levels = [
        {"level": _clean(level), "ev": _clean(ev_kelly.ev_at(scenarios, level))}
        for level in entry_levels
    ]

    return {
        "scenarios": scenarios,
        "scenario_reasoning": scenario_reasoning,
        "ev_at_current": _clean(ev_at_current),
        "hurdle_total": _clean(hurdle_total),
        "horizon_years_convention": _clean(horizon_years),
        "ev_breakeven_entry": _clean(ev_breakeven_entry),
        "ev_at_levels": ev_at_levels,
    }


# --------------------------------------------------------------------------- #
# Sensitivity: recompute the FULL composite per profile (EV re-banded per hurdle).
# --------------------------------------------------------------------------- #

def build_sensitivity(module_scores, scenarios, last,
                      variant, variant_justification,
                      catalyst_clarity, catalyst_clarity_justification,
                      invalidation, invalidation_justification) -> dict:
    """Recompute the full composite (INCLUDING thesis conviction with EV re-banded
    per that profile's hurdle) for all three profiles, so a reader sees how the call
    shifts with the profile lens. Each entry is {"score", "grade"}."""
    out = {}
    for profile in _PROFILES:
        tc = score_thesis_conviction(
            scenarios, "", last, profile,
            variant, variant_justification,
            catalyst_clarity, catalyst_clarity_justification,
            invalidation, invalidation_justification)
        comp = score_composite(module_scores, tc["score"], profile)
        grade, _ = grade_for(comp["score"])
        out[profile] = {"score": comp["score"], "grade": grade}
    return out


# --------------------------------------------------------------------------- #
# Bundle I/O + module assembly.
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _load_module(bundle, filename):
    """Load a module JSON if present, else None (missing dimension)."""
    path = os.path.join(bundle, filename)
    if not os.path.isfile(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def build_module(snapshot, module_scores, scenarios, scenario_reasoning, profile,
                 variant, variant_justification,
                 catalyst_clarity, catalyst_clarity_justification,
                 invalidation, invalidation_justification,
                 entry_levels) -> dict:
    """Build the full module_composite.json document from parsed inputs."""
    price = snapshot.get("price", {}) if isinstance(snapshot, dict) else {}
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
    last = price.get("last")

    tc = score_thesis_conviction(
        scenarios, scenario_reasoning, last, profile,
        variant, variant_justification,
        catalyst_clarity, catalyst_clarity_justification,
        invalidation, invalidation_justification)

    composite = score_composite(module_scores, tc["score"], profile)
    grade, action = grade_for(composite["score"])

    ev = build_ev_block(scenarios, scenario_reasoning, last, profile, entry_levels)
    sensitivity = build_sensitivity(
        module_scores, scenarios, last,
        variant, variant_justification,
        catalyst_clarity, catalyst_clarity_justification,
        invalidation, invalidation_justification)

    return {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "ticker": meta.get("ticker"),
        "as_of": build_snapshot._as_of_date(meta.get("as_of_utc")),
        "profile": profile,
        "score": composite["score"],
        "grade": grade,
        "action": action,
        "dimensions": composite["dimensions"],
        "thesis_conviction": {
            "score": tc["score"],
            "subscores": tc["subscores"],
        },
        "ev": ev,
        "sensitivity": sensitivity,
        "flags": {
            "variant": variant,
            "variant_justification": variant_justification,
            "catalyst_clarity": catalyst_clarity,
            "catalyst_clarity_justification": catalyst_clarity_justification,
            "invalidation": invalidation,
            "invalidation_justification": invalidation_justification,
        },
        "renormalization_note": composite["renormalization_note"],
        "tension": None,   # LLM prose slot for the one-line tension sentence.
        "signal": None,
    }


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score the composite decision layer for a snapshot bundle "
                    "(rubric v%s): consumes the four evidence module JSONs + an "
                    "in-script thesis-conviction dimension." % RUBRIC_VERSION)
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--scenarios", default=None,
                        help="path to scenario JSON "
                             "[{\"name\",\"prob\",\"price_target\"}] (REQUIRED)")
    parser.add_argument("--scenario-reasoning", default=None,
                        help="one-line reasoning for the scenario probabilities "
                             "(REQUIRED alongside --scenarios)")
    parser.add_argument("--variant", default=None, choices=_VARIANT_CHOICES,
                        help="variant-perception strength (REQUIRED)")
    parser.add_argument("--variant-justification", default=None,
                        help="one-line justification (REQUIRED)")
    parser.add_argument("--catalyst-clarity", default=None,
                        choices=_CATALYST_CHOICES,
                        help="catalyst clarity (REQUIRED)")
    parser.add_argument("--catalyst-clarity-justification", default=None,
                        help="one-line justification (REQUIRED)")
    parser.add_argument("--invalidation", default=None,
                        choices=_INVALIDATION_CHOICES,
                        help="invalidation quality (REQUIRED)")
    parser.add_argument("--invalidation-justification", default=None,
                        help="one-line justification (REQUIRED)")
    parser.add_argument("--profile", default="balanced", choices=_PROFILES,
                        help="scoring profile (default balanced)")
    parser.add_argument("--entry-level", type=float, action="append", default=None,
                        help="repeatable entry price for ev_at_levels (optional)")
    parser.add_argument("--out", default=None,
                        help="output path (default <bundle>/module_composite.json)")
    args = parser.parse_args(argv)

    # -- required-conviction gates (all flags asserted; never assumed) --------
    if not args.scenarios:
        print("ERROR: --scenarios is required. The SKILL must construct the "
              "bull/base/bear scenario set (with a 25/50/25 disclosed fallback "
              "only when no differentiated view exists) and pass it here.",
              file=sys.stderr)
        return 2
    if not args.scenario_reasoning:
        print("ERROR: --scenario-reasoning is required alongside --scenarios "
              "(scenario probabilities must carry stated reasoning).",
              file=sys.stderr)
        return 2
    for flag, val, just in (
        ("--variant", args.variant, args.variant_justification),
        ("--catalyst-clarity", args.catalyst_clarity,
         args.catalyst_clarity_justification),
        ("--invalidation", args.invalidation, args.invalidation_justification),
    ):
        if not val:
            print(f"ERROR: {flag} is required (conviction must be asserted, "
                  f"never assumed).", file=sys.stderr)
            return 2
        if not just:
            print(f"ERROR: {flag}-justification is required whenever {flag} is set.",
                  file=sys.stderr)
            return 2

    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 2

    # -- scenario file -------------------------------------------------------
    if not os.path.isfile(args.scenarios):
        print(f"ERROR: scenario file not found: {args.scenarios}", file=sys.stderr)
        return 2
    try:
        with open(args.scenarios) as fh:
            scenarios = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read scenario file {args.scenarios}: {exc}",
              file=sys.stderr)
        return 2

    # Validate the scenario probability set via the shared EV library. A bad
    # probability sum is ev_kelly.scenario_ev's ValueError -> exit 2.
    try:
        ev_kelly.scenario_ev(scenarios)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: invalid scenario set: {exc}", file=sys.stderr)
        return 2

    # -- snapshot (price.last EV reference; meta ticker/as_of) ----------------
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

    last = snapshot.get("price", {}).get("last")
    if last is None:
        print(f"ERROR: snapshot {snap_path} has no price.last (EV reference).",
              file=sys.stderr)
        return 2

    # -- evidence modules (each missing -> excluded + renormalized) -----------
    module_scores = {}
    for name, filename in _EVIDENCE_DIMENSIONS:
        doc = _load_module(args.bundle, filename)
        if doc is not None:
            module_scores[name] = doc

    missing = len(_EVIDENCE_DIMENSIONS) - len(module_scores)
    # >= 3 of the 5 dimensions missing == >= 2 of the 4 evidence modules missing
    # (thesis conviction is always present). Not enough evidence to render.
    if missing >= 2:
        present_names = sorted(module_scores)
        print("ERROR: insufficient evidence modules -- %d of 4 evidence modules "
              "present (%s); >= 3 of 5 dimensions missing. Run the missing "
              "evidence skills first." % (len(module_scores),
                                          ", ".join(present_names) or "none"),
              file=sys.stderr)
        return 2

    entry_levels = args.entry_level or []

    doc = build_module(
        snapshot, module_scores, scenarios, args.scenario_reasoning, args.profile,
        args.variant, args.variant_justification,
        args.catalyst_clarity, args.catalyst_clarity_justification,
        args.invalidation, args.invalidation_justification,
        entry_levels)

    out = args.out or os.path.join(args.bundle, "module_composite.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
