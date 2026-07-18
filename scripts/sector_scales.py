"""Sector valuation-scale library for the trading-desk plugin.

WHY THIS MODULE EXISTS: a single fwd-P/E band cannot fairly value a memory-semis
cyclical, an E&P driller and a REIT on the same axis. The compressed fundamental
pass (score_fundamental v1.1.0) bands a name against its OWN price history, which
is honest but blind to what a SECTOR's structure implies a fair multiple should
be. This module supplies that missing anchor: a small, auditable, VERSIONED
"sector scale" -- a JSON contract that declares (a) how to compute a fair-value
BAND for the sector from first-principles fundamentals (a justified P/B from
Gordon residual income, a justified forward P/E, or a pass-through NAV multiple),
and (b) the FALSIFIERS that would break the scale's thesis (dotted snapshot
metrics with comparison operators). The scale is DATA, not code: the sector
librarian writes the JSON, this module validates + evaluates it deterministically,
and the fundamental scorer's justified-band component (v1.2.0) positions the
name's current multiple inside the band this module returns.

Design contract (project-wide, mirrors qc.py / the scorers):
- Pure + deterministic: no I/O beyond ``load_scale`` reading one JSON file; the
  band math and falsifier evaluation are total functions over already-parsed
  dicts. No market data is fetched.
- A scale is only usable once ``validate_scale`` returns [] -- ``load_scale``
  runs it and raises ValueError naming every issue, so a malformed scale can
  never silently drive a number.
- Band math is unit-pinned (see the module tests): the Gordon residual-income
  justified P/B, the justified forward P/E, and the NAV pass-through each have a
  hand-computed reference case so a report can never silently drift.
- Falsifier evaluation is SINGLE-SNAPSHOT: it resolves a dotted metric path,
  compares, and reports tripped True/False (or None when the metric is absent).
  ``consecutive_quarters`` is metadata for the CALLER -- one snapshot cannot count
  quarters -- and is passed through in the result untouched.

stdlib-only; Python >= 3.10.
"""

import json
import os
import re

# Justified-band formula names this library dispatches on. A scale's "formula"
# MUST be one of these; validate_scale rejects anything else.
FORMULAS = {"justified_pb", "justified_pe", "nav_based"}

# Falsifier comparison operators. A falsifier "trips" (thesis-breaking condition
# met) when ``observed <op> value`` is True.
_OPS = {
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
}

# Default half-width of the band envelope around the justified mid (fraction).
# low = mid * (1 - spread), high = mid * (1 + spread).
_DEFAULT_BAND_SPREAD = 0.30

# Evidence citations look like C3 / C12 (same convention as the context findings
# registry the fundamental moat flag cites).
_CITATION_RE = re.compile(r"^C\d+$")

# The effective date is part of the forward-only versioning contract; a scale
# whose effective date can't be ordered can't be governed.
_EFFECTIVE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

# Required parameter keys per formula (all must be present + numeric).
_FORMULA_REQUIRED_PARAMS = {
    "justified_pb": ("roe_normalized", "r", "g"),
    "justified_pe": ("roe_normalized", "r", "g"),
    "nav_based": ("nav_multiple_low", "nav_multiple_mid", "nav_multiple_high"),
}


def _is_num(value):
    """True if ``value`` is a real (non-bool) number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_scale(scale) -> list:
    """Return a list of named validation issues for ``scale`` ([] when valid).

    A scale is a dict declaring:
      - ``scale``      (str): the scale's identifier / slug (e.g. "memory_semis").
      - ``name``       (str, OPTIONAL): human label.
      - ``version``    (str): a version tag (e.g. "2026.1").
      - ``effective``  (str): the date the scale takes effect (YYYY-MM-DD).
      - ``basis``      (str): free-text rationale for the chosen fair-value logic.
      - ``formula``    (str in FORMULAS): which justified-band math to apply.
      - ``parameters`` (dict): formula-specific numeric inputs (see
        _FORMULA_REQUIRED_PARAMS); for justified_pb/pe, r > g is required.
      - ``evidence``   (list of str): C-IDs (regex ``C\\d+``) grounding the scale.
      - ``falsifiers`` (list of dict): each {metric (dotted snapshot path), op in
        <,>,<=,>=, value (number), consecutive_quarters? (int >= 1), meaning
        (str)} -- the conditions that would break the scale's thesis.
      - ``prior``      (dict | None): optional Bayesian prior metadata.

    Every problem is reported (the list is not short-circuited) so a librarian
    fixing a scale sees all issues at once.
    """
    issues = []
    if not isinstance(scale, dict):
        return ["scale is not a JSON object"]

    # -- required string fields --------------------------------------------
    for field in ("scale", "version", "effective", "basis"):
        v = scale.get(field)
        if v is None:
            issues.append(f"missing required field: {field}")
        elif not isinstance(v, str) or not v.strip():
            issues.append(f"field {field} must be a non-empty string")
        elif field == "effective" and not _EFFECTIVE_RE.match(v.strip()):
            issues.append("field effective must be a YYYY-MM-DD date")

    # -- optional name -----------------------------------------------------
    if "name" in scale and not isinstance(scale["name"], str):
        issues.append("field name must be a string when present")

    # -- formula -----------------------------------------------------------
    formula = scale.get("formula")
    if formula is None:
        issues.append("missing required field: formula")
    elif formula not in FORMULAS:
        issues.append(
            f"formula {formula!r} not in {sorted(FORMULAS)}")

    # -- parameters (formula-specific) -------------------------------------
    params = scale.get("parameters")
    if not isinstance(params, dict):
        issues.append("missing required field: parameters (must be an object)")
        params = {}
    if formula in _FORMULA_REQUIRED_PARAMS:
        for key in _FORMULA_REQUIRED_PARAMS[formula]:
            if key not in params:
                issues.append(f"parameters missing required key for "
                              f"{formula}: {key}")
            elif not _is_num(params[key]):
                issues.append(f"parameters.{key} must be numeric")
        # r > g economic constraint for the Gordon-style formulas.
        if formula in ("justified_pb", "justified_pe"):
            r = params.get("r")
            g = params.get("g")
            if _is_num(r) and _is_num(g) and not (r > g):
                issues.append(
                    f"parameters require r > g (cost of equity above growth); "
                    f"got r={r}, g={g}")

    # -- evidence ----------------------------------------------------------
    evidence = scale.get("evidence")
    if not isinstance(evidence, list):
        issues.append("missing required field: evidence (must be a list of "
                      "context finding C-IDs)")
    else:
        for i, cid in enumerate(evidence):
            if not isinstance(cid, str) or not _CITATION_RE.match(cid):
                issues.append(f"evidence[{i}] {cid!r} is not a C-ID (regex "
                              f"C\\d+)")

    # -- falsifiers --------------------------------------------------------
    falsifiers = scale.get("falsifiers")
    if not isinstance(falsifiers, list):
        issues.append("missing required field: falsifiers (must be a list)")
    else:
        for i, f in enumerate(falsifiers):
            issues.extend(_validate_falsifier(f, i))

    # -- prior (optional; dict or None) ------------------------------------
    if "prior" not in scale:
        issues.append("missing required field: prior (use null when absent)")
    else:
        prior = scale["prior"]
        if prior is not None and not isinstance(prior, dict):
            issues.append("field prior must be an object or null")

    return issues


def _validate_falsifier(f, i):
    """Validate a single falsifier dict; return its list of issues."""
    issues = []
    if not isinstance(f, dict):
        return [f"falsifiers[{i}] is not an object"]
    metric = f.get("metric")
    if not isinstance(metric, str) or not metric.strip():
        issues.append(f"falsifiers[{i}].metric must be a non-empty dotted path")
    op = f.get("op")
    if op not in _OPS:
        issues.append(f"falsifiers[{i}].op {op!r} not in {sorted(_OPS)}")
    if not _is_num(f.get("value")):
        issues.append(f"falsifiers[{i}].value must be numeric")
    if "consecutive_quarters" in f:
        cq = f["consecutive_quarters"]
        if not isinstance(cq, int) or isinstance(cq, bool) or cq < 1:
            issues.append(f"falsifiers[{i}].consecutive_quarters must be an "
                          f"integer >= 1")
    if not isinstance(f.get("meaning"), str) or not f.get("meaning", "").strip():
        issues.append(f"falsifiers[{i}].meaning must be a non-empty string")
    return issues


def load_scale(path) -> dict:
    """Read + validate the scale JSON at ``path``; return the parsed dict.

    Raises ValueError (naming every issue) when the file is unreadable, is not
    valid JSON, or fails ``validate_scale``. A returned scale is guaranteed
    usable by ``compute_band`` / ``evaluate_falsifiers``.
    """
    try:
        with open(path) as fh:
            scale = json.load(fh)
    except OSError as exc:
        raise ValueError(f"cannot read scale {path}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"scale {path} is not valid JSON: {exc}") from exc

    issues = validate_scale(scale)
    if issues:
        raise ValueError(
            f"invalid scale {path}: " + "; ".join(issues))
    return scale


# --------------------------------------------------------------------------- #
# Band math
# --------------------------------------------------------------------------- #

def _band_from_mid(mid, band_spread):
    """Envelope a justified ``mid`` into {low, mid, high} at +-band_spread."""
    return {
        "low": mid * (1 - band_spread),
        "mid": mid,
        "high": mid * (1 + band_spread),
    }


def justified_pb(params) -> dict:
    """Gordon residual-income justified price-to-book band.

    A no-growth-adjusted residual-income model reduces the justified P/B to
        mid = (roe_normalized - g) / (r - g)
    where roe_normalized is the sustainable return on equity (fraction), r is the
    cost of equity (fraction), and g is the sustainable growth (fraction). The
    band envelopes mid at +- ``band_spread`` (default 0.30):
        low  = mid * (1 - band_spread)
        high = mid * (1 + band_spread)

    Reference (pinned in the tests): roe .35, r .12, g .04 ->
        mid = (.35 - .04)/(.12 - .04) = 3.875 ; spread .30 -> low 2.7125, high 5.0375.

    Requires r > g (validated upstream). Returns {"low","mid","high"}.
    """
    roe = params["roe_normalized"]
    r = params["r"]
    g = params["g"]
    band_spread = params.get("band_spread", _DEFAULT_BAND_SPREAD)
    mid = (roe - g) / (r - g)
    return _band_from_mid(mid, band_spread)


def justified_pe(params) -> dict:
    """Justified forward P/E from fundamentals (Gordon dividend-growth form).

    Retaining the plowback that funds growth g at return roe_normalized, the
    payout ratio implied is (1 - g/roe_normalized), and the justified forward P/E
    (dividend-discount, dividing through by forward earnings) is
        mid = (1 - g/roe_normalized) / (r - g).
    Same +- ``band_spread`` envelope as justified_pb.

    Requires r > g (validated upstream). Returns {"low","mid","high"}.
    """
    roe = params["roe_normalized"]
    r = params["r"]
    g = params["g"]
    band_spread = params.get("band_spread", _DEFAULT_BAND_SPREAD)
    payout_adjusted = 1 - (g / roe)
    mid = payout_adjusted / (r - g)
    return _band_from_mid(mid, band_spread)


def nav_based(params) -> dict:
    """Pass-through of appraised NAV multiples for E&P / REIT-style scales.

    The sector librarian appraises fair-value NAV multiples directly (from PV-10,
    strip decks, cap-rate NAV etc.) rather than deriving them from roe/r/g; this
    formula simply surfaces them as the band. Returns {"low","mid","high"} =
    {nav_multiple_low, nav_multiple_mid, nav_multiple_high}.
    """
    return {
        "low": params["nav_multiple_low"],
        "mid": params["nav_multiple_mid"],
        "high": params["nav_multiple_high"],
    }


_DISPATCH = {
    "justified_pb": justified_pb,
    "justified_pe": justified_pe,
    "nav_based": nav_based,
}


def compute_band(scale) -> dict:
    """Dispatch on ``scale["formula"]`` and return the {low,mid,high} band."""
    formula = scale["formula"]
    fn = _DISPATCH[formula]
    return fn(scale["parameters"])


# --------------------------------------------------------------------------- #
# Falsifier evaluation
# --------------------------------------------------------------------------- #

def _resolve_dotted(obj, path):
    """Resolve a dotted path (e.g. "fundamentals.roe") in a nested dict.

    Returns (value, True) on success; (None, False) when any segment is missing
    or an intermediate node is not a dict.
    """
    cur = obj
    for seg in path.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return None, False
        cur = cur[seg]
    return cur, True


def evaluate_falsifiers(scale, snapshot) -> list:
    """Evaluate every falsifier in ``scale`` against ``snapshot``.

    Per falsifier, resolve the dotted ``metric`` from the snapshot and compare
    ``observed <op> value``. Returns one result dict per falsifier:

      {"metric","op","value","observed","tripped": bool, "meaning",
       "consecutive_quarters"?}

    when the metric resolves to a number; and

      {"metric","op","value","observed": None, "tripped": None,
       "reason": "metric not in snapshot", "meaning", "consecutive_quarters"?}

    when the metric is absent or non-numeric (a single snapshot cannot evaluate
    it). ``consecutive_quarters`` (when present on the falsifier) is passed
    through untouched: it is metadata for the caller, which must count quarters
    across snapshots -- a single-snapshot check reports only whether THIS
    snapshot trips the condition.
    """
    results = []
    falsifiers = scale.get("falsifiers") or []
    for f in falsifiers:
        metric = f.get("metric")
        op = f.get("op")
        value = f.get("value")
        meaning = f.get("meaning")
        observed, ok = _resolve_dotted(snapshot, metric) if isinstance(
            snapshot, dict) and isinstance(metric, str) else (None, False)

        row = {
            "metric": metric,
            "op": op,
            "value": value,
            "meaning": meaning,
        }
        if "consecutive_quarters" in f:
            row["consecutive_quarters"] = f["consecutive_quarters"]

        if not ok or not _is_num(observed):
            row["observed"] = None
            row["tripped"] = None
            row["reason"] = "metric not in snapshot"
        else:
            row["observed"] = observed
            row["tripped"] = _OPS[op](observed, value)
        results.append(row)
    return results


# --------------------------------------------------------------------------- #
# Locating a scale on disk
# --------------------------------------------------------------------------- #

def find_scale_for(ticker_dir_or_cwd, scale_name) -> str:
    """Return the path to ``<name>.json`` under a scales config dir, or None.

    Looks for ``trading_desk_config/scales/<scale_name>.json`` beneath the given
    directory (a ticker bundle dir or the workspace CWD). Returns the path when
    it exists as a file, else None -- callers treat a missing scale as "no
    sector anchor", scoring the justified-band component n/a rather than failing.
    """
    if not scale_name:
        return None
    path = os.path.join(ticker_dir_or_cwd, "trading_desk_config", "scales",
                        f"{scale_name}.json")
    return path if os.path.isfile(path) else None
