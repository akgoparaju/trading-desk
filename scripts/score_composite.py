"""Composite-score decision skill (L3) for the trading-desk plugin.

WHY THIS MODULE EXISTS: this is the DECISION layer. The four evidence modules
(technical-analysis, fundamental, sentiment-positioning, risk-analytics) each score
one dimension off the snapshot; this module does NOT re-read those snapshot facts.
It CONSUMES the four module JSONs' final scores, adds a fifth dimension it computes
in-script (THESIS CONVICTION), applies FIXED per-profile weights, and produces the
composite (0-100), a letter grade, an action, and an expected-value block. Its
arithmetic IS the composite rubric of record (composite rubric v1.1.0): every
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
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation (``python3 scripts/score_composite.py``): ensure the repo
# root is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import build_snapshot, confidence, ev_kelly

RUBRIC_VERSION = "1.1.0"
SKILL_NAME = "composite-score"

# PROVISIONAL (composite-v1.1.0, Wave 3B / Philosophy A). Stamped into the module
# note so a reader knows these thresholds are review's first numbers, subject to the
# SKILL falsifier — not calibrated defaults.
_PROVISIONAL_NOTE = (
    "composite-v1.1.0 PROVISIONAL: base-rate anchoring (+/-5% move bins, 25pp "
    "deviation flag, N>=4) and the 25-pt auto-tension spread are review's first "
    "numbers, soft/disclosed and subject to the SKILL falsifier -- not calibrated.")

# --- Base-rate anchoring (Goal A, PROVISIONAL) ------------------------------- #
# A historical earnings move is "material" (a bull/bear reaction) beyond +/-5%;
# inside that band it is a base (in-line) reaction. Provisional threshold.
_BASE_RATE_MOVE_THRESHOLD = 0.05
# Minimum history length to compute an empirical base rate at all; below this the
# check is SKIPPED (disclosed), never computed off too-thin a sample.
_BASE_RATE_MIN_HISTORY = 4
# A scenario probability that deviates from its base-rate analog by more than this
# (in probability points, i.e. 0.25 == 25pp) trips the SOFT flag. Provisional.
_BASE_RATE_DEVIATION_THRESHOLD = 0.25

# --- Auto-tension gate (Goal C, PROVISIONAL) --------------------------------- #
# When the spread (max - min) across the evidence-dimension scores (thesis_conviction
# EXCLUDED) exceeds this, tension auto-populates with a scripted string. Provisional.
_TENSION_SPREAD_THRESHOLD = 25

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

# The five weight dimensions a profile column must (and may only) carry. Used to
# reject unknown keys in a --weights-config profile (a typo'd or invented key is a
# config error, not silently ignored) and to validate the per-profile sum.
_WEIGHT_DIMENSIONS = frozenset(
    {"technical", "fundamental", "sentiment", "risk", "thesis_conviction"})

# The weight_set label stamped when no config (or no weights key) supplies custom
# weights -- the standard fixed table is the rubric of record.
STANDARD_WEIGHT_SET = "standard v1"

# Default config path (relative to the invoker's CWD). Loaded only when it exists;
# an absent default is not an error (the standard table is used).
_DEFAULT_CONFIG_PATH = "./trading_desk_config.json"


class WeightsConfigError(Exception):
    """Fatal weights-config error (maps to exit 2 with a clear message)."""


def load_weights_config(path):
    """Load + validate a trading_desk_config.json weights block.

    Returns ``(profiles, label)`` where ``profiles`` maps each configured profile
    name -> its five-dimension weight dict, and ``label`` is the ``weight_set``
    stamp string ``"CUSTOM <set_name>@<version>"``. Returns ``(None, None)`` when
    the file has no ``weights`` key (fall back entirely to the standard table).

    Validation (each raises WeightsConfigError -> exit 2):
      - each profile's provided weights sum to 1.0 (+/- 1e-6), message names the
        profile and the observed sum;
      - every weight key is one of the five known dimensions (an unknown key is a
        config typo, never silently dropped).
    A profile ABSENT from the config is NOT an error -- the resolver falls back to
    the standard table for that profile (handled by resolve_weights).
    """
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        raise WeightsConfigError(
            "cannot read weights config %s: %s" % (path, exc))
    if not isinstance(cfg, dict):
        raise WeightsConfigError(
            "weights config %s is not a JSON object" % path)

    weights_block = cfg.get("weights")
    if not isinstance(weights_block, dict):
        # No weights key (or malformed) -> no custom weights; standard table.
        return None, None

    profiles = weights_block.get("profiles")
    if not isinstance(profiles, dict):
        raise WeightsConfigError(
            "weights.profiles missing or not an object in %s" % path)

    set_name = weights_block.get("set_name", "unnamed")
    version = weights_block.get("version", "unversioned")
    label = "CUSTOM %s@%s" % (set_name, version)

    validated = {}
    for profile, weights in profiles.items():
        if not isinstance(weights, dict):
            raise WeightsConfigError(
                "weights.profiles.%s is not an object" % profile)
        unknown = set(weights) - _WEIGHT_DIMENSIONS
        if unknown:
            raise WeightsConfigError(
                "weights.profiles.%s has unknown dimension key(s): %s "
                "(known: %s)" % (profile, ", ".join(sorted(unknown)),
                                 ", ".join(sorted(_WEIGHT_DIMENSIONS))))
        total = sum(weights.values())
        if abs(total - 1.0) > 1e-6:
            raise WeightsConfigError(
                "weights.profiles.%s weights sum to %s, must sum to 1.0 "
                "(+/- 1e-6)" % (profile, _fmt(_clean(total))))
        validated[profile] = dict(weights)

    return validated, label


def resolve_weights(profile, custom_profiles, custom_label):
    """Return ``(weights, weight_set_label)`` for one profile.

    Per-profile fallback: a profile PRESENT in ``custom_profiles`` uses its custom
    weights (stamped with ``custom_label``); a profile ABSENT from the config (or
    when there is no config at all) falls back to the standard fixed table stamped
    ``STANDARD_WEIGHT_SET``. This is the "profiles absent from the config fall back
    to the standard table per-profile" rule -- fallback is decided one profile at a
    time, so a config that customizes only ``balanced`` leaves ``trader`` /
    ``long-term`` on the standard table.
    """
    if custom_profiles and profile in custom_profiles:
        return custom_profiles[profile], custom_label
    return WEIGHTS[profile], STANDARD_WEIGHT_SET

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

    ``scenario_reasoning`` is accepted for signature parity with build_ev_block
    (both take the full scenario context); the reasoning text is recorded in the
    EV block, not here — this function scores arithmetic only.

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

def score_composite(module_scores, thesis_conviction_score, profile,
                    weights=None) -> dict:
    """Weighted composite over PRESENT dimensions, weights rescaled to sum 1.

    ``module_scores`` maps present evidence-dimension names (technical/fundamental/
    sentiment/risk) to their module doc (a dict carrying a ``score``). A dimension
    absent from that mapping is EXCLUDED: its weight is dropped and the remaining
    present-dimension weights are rescaled so they sum to 1 (disclosed in
    ``renormalization_note``). thesis_conviction is ALWAYS present (computed here).

    ``weights`` overrides the standard per-profile column (custom --weights-config).
    Renormalization on missing dimensions works IDENTICALLY under custom weights:
    the same drop-and-rescale-to-1 runs whatever the weight column is.

    Returns {"score", "dimensions": [...rows...], "renormalization_note",
    "confidence"}. ``confidence`` is the roll-up (min over the PRESENT evidence
    dimensions' per-module confidence blocks -- confidence-v1.0.0); a
    renormalized-away evidence dimension contributes None and thesis-conviction is
    excluded (no data provenance).
    """
    weights = weights if weights is not None else WEIGHTS[profile]

    # Assemble (name, raw_score, weight, source, module_confidence) for present dims.
    present = []
    for name, source in _EVIDENCE_DIMENSIONS:
        if name in module_scores and module_scores[name] is not None:
            raw = module_scores[name].get("score")
            if raw is not None:
                mod_conf = module_scores[name].get("confidence")
                present.append((name, raw, weights[name], source, mod_conf))
    # thesis conviction is always present (no data provenance -> confidence n/a).
    present.append(("thesis_conviction", thesis_conviction_score,
                    weights["thesis_conviction"], "computed", None))

    excluded = [name for name, _ in _EVIDENCE_DIMENSIONS
                if name not in module_scores or module_scores[name] is None
                or module_scores[name].get("score") is None]

    weight_sum = sum(w for _, _, w, _, _ in present)

    dimensions = []
    composite = 0.0
    # Per-evidence-dimension confidence blocks for the roll-up, stamped with the
    # dimension name so the driver names the weakest dimension(s).
    dim_confidences = []
    for name, raw, weight, source, mod_conf in present:
        w_renorm = weight / weight_sum if weight_sum else 0.0
        contribution = w_renorm * raw
        composite += contribution
        row = {
            "name": name,
            "score": _clean(raw),
            "weight": _clean(weight),
            "weight_renormalized": _clean(w_renorm),
            "contribution": _clean(contribution),
            "source": source,
            # carry the per-module confidence block onto the row (n/a for
            # thesis_conviction, which carries no data provenance).
            "confidence": mod_conf if mod_conf is not None else "n/a",
        }
        dimensions.append(row)
        # thesis-conviction is EXCLUDED from the roll-up (no data provenance);
        # a renormalized-away evidence dim is simply absent from ``present`` so it
        # contributes None to the min (renormalization contract).
        if name != "thesis_conviction" and isinstance(mod_conf, dict):
            stamped = dict(mod_conf)
            stamped["dimension"] = name
            dim_confidences.append(stamped)

    note = None
    if excluded:
        note = (f"weights renormalized over present dimensions (sum {_fmt(_clean(weight_sum))}) "
                f"-- excluded missing evidence modules: {', '.join(excluded)}")

    return {
        "score": _clean(composite),
        "dimensions": dimensions,
        "renormalization_note": note,
        "confidence": confidence.rollup(dim_confidences),
    }


# --------------------------------------------------------------------------- #
# Base-rate anchoring (Goal A, composite-v1.1.0 PROVISIONAL).
# --------------------------------------------------------------------------- #

def classify_move(move_pct):
    """Classify one historical earnings move into bull/bear/base.

    move_pct > +0.05 -> "bull"; move_pct < -0.05 -> "bear"; else "base".
    The +/-5% "material move" band is the provisional threshold.
    """
    if move_pct > _BASE_RATE_MOVE_THRESHOLD:
        return "bull"
    if move_pct < -_BASE_RATE_MOVE_THRESHOLD:
        return "bear"
    return "base"


def compute_base_rates(earnings_move_history):
    """Empirical bull/base/bear base rates from a ticker's own earnings-move history.

    ``earnings_move_history`` is the snapshot ``events.earnings_move_history`` list of
    ``{"quarter_end", "move_pct"}`` (move_pct a decimal fraction). Each move is
    classified via classify_move; the base rate per class is its empirical frequency.

    Returns ``(base_rates, n)`` where ``base_rates`` is ``{"bull","base","bear"}`` of
    frequencies (rounded 4dp) and ``n`` is the count of USABLE (numeric move_pct)
    history rows. When ``n < _BASE_RATE_MIN_HISTORY`` the caller SKIPS the check
    (too-thin a sample) -- this function still returns the tallied rates for
    transparency, but callers gate on ``n``.
    """
    moves = [row.get("move_pct") for row in (earnings_move_history or [])
             if isinstance(row, dict) and isinstance(row.get("move_pct"), (int, float))]
    n = len(moves)
    if n == 0:
        return {"bull": None, "base": None, "bear": None}, 0
    counts = {"bull": 0, "base": 0, "bear": 0}
    for m in moves:
        counts[classify_move(m)] += 1
    base_rates = {k: _clean(v / n) for k, v in counts.items()}
    return base_rates, n


def build_base_rate_check(scenarios, earnings_move_history):
    """Compare each LLM scenario.prob to its empirical base-rate analog (Goal A).

    Maps a scenario to a class by NAME (a scenario named 'bull'/'base'/'bear' pairs
    with that class's base rate; any other name is not compared). Deviation =
    |LLM_prob - base_rate|. ``flagged`` is True iff any deviation exceeds the 25pp
    provisional threshold. SOFT flag -- disclosed in the report, never a hard gate.

    Returns None when there is not enough history (N < 4) to anchor at all (the
    check is SKIPPED, disclosed via the returned dict's own ``skipped`` shape). To
    keep the disclosure explicit we ALWAYS return a dict; ``flagged`` is False and
    ``deviations`` empty when skipped, with ``n_history`` carrying the (too-small)
    count so the report can say why.
    """
    base_rates, n = compute_base_rates(earnings_move_history)
    threshold_pp = int(round(_BASE_RATE_DEVIATION_THRESHOLD * 100))

    if n < _BASE_RATE_MIN_HISTORY:
        # SKIP: too thin to anchor. Disclosed, not silent.
        return {
            "base_rates": {"bull": None, "base": None, "bear": None},
            "deviations": {},
            "flagged": False,
            "n_history": n,
            "threshold_pp": threshold_pp,
            "skipped": True,
            "skip_reason": (
                f"insufficient earnings-move history (n={n} < "
                f"{_BASE_RATE_MIN_HISTORY}); base-rate check skipped"),
        }

    deviations = {}
    flagged = False
    for sc in scenarios or []:
        name = sc.get("name")
        if name not in base_rates or base_rates[name] is None:
            continue
        prob = sc.get("prob")
        if not isinstance(prob, (int, float)):
            continue
        dev = _clean(abs(prob - base_rates[name]))
        deviations[name] = dev
        if abs(prob - base_rates[name]) > _BASE_RATE_DEVIATION_THRESHOLD:
            flagged = True

    return {
        "base_rates": base_rates,
        "deviations": deviations,
        "flagged": flagged,
        "n_history": n,
        "threshold_pp": threshold_pp,
        "skipped": False,
    }


# --------------------------------------------------------------------------- #
# Auto-tension gate (Goal C, composite-v1.1.0 PROVISIONAL).
# --------------------------------------------------------------------------- #

def build_auto_tension(dimensions):
    """Auto-populate the tension string when the evidence-score spread fires.

    Over the ``dimensions`` rows (the composite doc's per-dimension rows),
    thesis_conviction EXCLUDED, compute spread = max - min of the raw scores. When
    spread > 25 (provisional) return a scripted string naming the high/low dims +
    spread (e.g. "sentiment 58.8 vs fundamental 30.8 -- 28-pt evidence spread").
    Below threshold -> None (the LLM prose slot stays open).
    """
    scores = [(d.get("name"), d.get("score")) for d in (dimensions or [])
              if d.get("name") != "thesis_conviction"
              and isinstance(d.get("score"), (int, float))]
    if len(scores) < 2:
        return None
    high_name, high_score = max(scores, key=lambda x: x[1])
    low_name, low_score = min(scores, key=lambda x: x[1])
    spread = high_score - low_score
    if spread <= _TENSION_SPREAD_THRESHOLD:
        return None
    return (f"{high_name} {_fmt(_clean(high_score))} vs {low_name} "
            f"{_fmt(_clean(low_score))} -- {_fmt(_clean(spread))}-pt evidence spread")


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
                      invalidation, invalidation_justification,
                      custom_profiles=None, custom_label=None) -> dict:
    """Recompute the full composite (INCLUDING thesis conviction with EV re-banded
    per that profile's hurdle) for all three profiles, so a reader sees how the call
    shifts with the profile lens. Each entry is {"score", "grade"}.

    When custom weights apply to a profile, that profile's entry also carries a
    ``standard_comparison`` {"score","grade"} -- the SAME call recomputed under the
    standard fixed table -- so the tuning is transparent (a reader sees exactly how
    much the custom column moved the number). The block also carries a top-level
    ``weight_set`` label (custom label when ANY profile is custom, else standard).
    """
    out = {}
    any_custom = False
    for profile in _PROFILES:
        weights, label = resolve_weights(profile, custom_profiles, custom_label)
        tc = score_thesis_conviction(
            scenarios, "", last, profile,
            variant, variant_justification,
            catalyst_clarity, catalyst_clarity_justification,
            invalidation, invalidation_justification)
        comp = score_composite(module_scores, tc["score"], profile, weights)
        grade, _ = grade_for(comp["score"])
        entry = {"score": comp["score"], "grade": grade}
        if label != STANDARD_WEIGHT_SET:
            any_custom = True
            std_comp = score_composite(
                module_scores, tc["score"], profile, WEIGHTS[profile])
            std_grade, _ = grade_for(std_comp["score"])
            entry["standard_comparison"] = {
                "score": std_comp["score"], "grade": std_grade}
        out[profile] = entry
    out["weight_set"] = custom_label if any_custom else STANDARD_WEIGHT_SET
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
                 entry_levels, custom_profiles=None, custom_label=None) -> dict:
    """Build the full module_composite.json document from parsed inputs.

    ``custom_profiles``/``custom_label`` come from a --weights-config; when the
    selected profile is customized the composite is weighted with the custom column
    and the doc's ``weight_set`` records the CUSTOM label. The ``dimensions`` rows
    carry the weights ACTUALLY used either way.
    """
    price = snapshot.get("price", {}) if isinstance(snapshot, dict) else {}
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
    events = snapshot.get("events", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(events, dict):
        events = {}
    last = price.get("last")

    weights, weight_set = resolve_weights(profile, custom_profiles, custom_label)

    tc = score_thesis_conviction(
        scenarios, scenario_reasoning, last, profile,
        variant, variant_justification,
        catalyst_clarity, catalyst_clarity_justification,
        invalidation, invalidation_justification)

    composite = score_composite(module_scores, tc["score"], profile, weights)
    grade, action = grade_for(composite["score"])

    ev = build_ev_block(scenarios, scenario_reasoning, last, profile, entry_levels)
    sensitivity = build_sensitivity(
        module_scores, scenarios, last,
        variant, variant_justification,
        catalyst_clarity, catalyst_clarity_justification,
        invalidation, invalidation_justification,
        custom_profiles=custom_profiles, custom_label=custom_label)

    # Goal A (PROVISIONAL): anchor the LLM scenario probs against the ticker's own
    # empirical earnings-move base rates; SOFT-flag any >25pp deviation (disclosed,
    # not a hard gate). Skipped + disclosed when history < 4.
    base_rate_check = build_base_rate_check(
        scenarios, events.get("earnings_move_history"))

    # Goal C (PROVISIONAL): auto-populate tension when the evidence-score spread
    # (thesis_conviction excluded) exceeds 25; else the LLM prose slot stays open.
    tension = build_auto_tension(composite["dimensions"])

    return {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "ticker": meta.get("ticker"),
        "as_of": build_snapshot._as_of_date(meta.get("as_of_utc")),
        "profile": profile,
        "weight_set": weight_set,
        "score": composite["score"],
        "grade": grade,
        # Roll-up confidence (confidence-v1.0.0): min over the PRESENT evidence
        # dimensions' per-module confidence; renormalized-away dims + thesis-
        # conviction are excluded. Disclosure-only -- does not move any number.
        "confidence": composite["confidence"],
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
            # Goal A (PROVISIONAL): base-rate-anchored scenario-probability check.
            "base_rate_check": base_rate_check,
        },
        "renormalization_note": composite["renormalization_note"],
        # Goal C (PROVISIONAL): auto-populated when the evidence spread > 25; the LLM
        # may still write a richer tension sentence on top (SKILL contract note).
        "tension": tension,
        "note": _PROVISIONAL_NOTE,
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
    parser.add_argument("--weights-config", default=None,
                        help="path to trading_desk_config.json carrying a "
                             "weights.profiles block (default ./trading_desk_config.json "
                             "if it exists); each configured profile's weights must "
                             "sum to 1.0. Absent profiles fall back to the standard "
                             "table per-profile.")
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

    # -- weights config (optional, versioned tuning transparency) -------------
    # Explicit --weights-config always loads (missing file is then an error);
    # otherwise the default ./trading_desk_config.json loads only WHEN it exists
    # (an absent default is silent -- the standard table is the rubric of record).
    custom_profiles = None
    custom_label = None
    weights_config_path = args.weights_config
    if weights_config_path is None and os.path.isfile(_DEFAULT_CONFIG_PATH):
        weights_config_path = _DEFAULT_CONFIG_PATH
    if weights_config_path is not None:
        if args.weights_config is not None and not os.path.isfile(weights_config_path):
            print(f"ERROR: weights config not found: {weights_config_path}",
                  file=sys.stderr)
            return 2
        try:
            custom_profiles, custom_label = load_weights_config(weights_config_path)
        except WeightsConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    # CONTEXT GROUNDING ENFORCEMENT (coverage-first): when a QC-stamped
    # company-context module exists, the variant and catalyst-clarity
    # justifications must cite its finding IDs (C<n>) — prose discipline
    # graduated to a script gate, per the project's required-artifact rule.
    # Invalidation is exempt (its legs cite trade-plan levels, not context).
    ctx_path = os.path.join(args.bundle, "module_context.json")
    if os.path.exists(ctx_path):
        try:
            with open(ctx_path) as fh:
                _ctx = json.load(fh)
        except (OSError, ValueError):
            _ctx = {}
        if (_ctx.get("qc") or {}).get("qc_passed"):
            import re as _re
            # Referential integrity: build the findings[] id registry once, then
            # require every cited C-ID to resolve to it (presence alone is not
            # enough — a fabricated "C99" is a broken reference, not grounding).
            _findings = _ctx.get("findings")
            _finding_ids = {
                f["id"] for f in (_findings if isinstance(_findings, list) else [])
                if isinstance(f, dict) and isinstance(f.get("id"), str)}
            for flag, just in (("--variant", args.variant_justification),
                               ("--catalyst-clarity",
                                args.catalyst_clarity_justification)):
                cited = _re.findall(r"C\d+", just or "")
                if not cited:
                    print(f"ERROR: {flag}-justification must cite context "
                          f"finding IDs (e.g. C3) — a QC-stamped "
                          f"module_context.json exists for this bundle.",
                          file=sys.stderr)
                    return 2
                _unresolved = [c for c in cited if c not in _finding_ids]
                if _unresolved:
                    _n = len(_finding_ids)
                    print(f"ERROR: cited finding {_unresolved[0]} does not exist "
                          f"in module_context.json (findings run C1..C{_n})",
                          file=sys.stderr)
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
        entry_levels, custom_profiles=custom_profiles, custom_label=custom_label)

    out = args.out or os.path.join(args.bundle, "module_composite.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
