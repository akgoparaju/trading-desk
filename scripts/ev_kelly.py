"""Expected-value and Kelly position-sizing library for the trading-desk plugin.

WHY THIS MODULE EXISTS: Position sizing must be arithmetic, not vibes. The LLM
layer proposes probability-weighted price-target scenarios; this module turns
those scenarios into an expected return, a full-Kelly fraction, and a capped
recommendation. The LLM never does the math -- it lives here as pure functions.

A scenario is a dict: {"name": str, "prob": float, "price_target": float}.
Probabilities across the scenario set must sum to 1.0 (+-1e-6). A scenario's
return relative to entry is r_i = price_target_i / entry - 1.

stdlib-only. All functions are pure (no I/O, no globals mutated).
"""

# Tolerance for probability-sum validation.
_PROB_TOL = 1e-6

# Max fraction-of-portfolio caps per risk profile (full-Kelly is never used raw).
_CAPS = {"trader": 0.05, "balanced": 0.08, "long-term": 0.10}


def _validate_probs(scenarios) -> None:
    """Raise ValueError unless the scenario probabilities sum to 1.0 (+-1e-6)."""
    total = sum(sc["prob"] for sc in scenarios)
    if abs(total - 1.0) > _PROB_TOL:
        raise ValueError(f"scenario probabilities sum to {total}, expected 1.0")


def ev_at(scenarios, entry_price) -> float:
    """Probability-weighted expected return at ``entry_price``.

    Formula: sum(prob_i * (price_target_i / entry_price - 1)) over all scenarios.
    Does NOT validate probability sum (use scenario_ev for that gate).
    """
    return sum(
        sc["prob"] * (sc["price_target"] / entry_price - 1) for sc in scenarios
    )


def scenario_ev(scenarios) -> dict:
    """Validate the scenario set and echo it back.

    Raises ValueError if probabilities do not sum to 1.0 (+-1e-6). On success
    returns {"scenarios": scenarios, "valid": True}.
    """
    _validate_probs(scenarios)
    return {"scenarios": scenarios, "valid": True}


def kelly(scenarios, entry_price) -> dict:
    """Full-Kelly fraction for a probability-weighted scenario set.

    Validates the probability sum (ValueError otherwise). For each scenario,
    r_i = price_target_i / entry_price - 1. Winners have r_i > 0.

        p    = sum(prob_i) over winners
        win  = sum(prob_i * r_i over winners) / p      (prob-weighted avg win)
        loss = |sum(prob_i * r_i over losers) / (1 - p)|  (prob-weighted avg loss)
        b    = win / loss
        f*   = p - (1 - p) / b, clamped to >= 0

    Edge cases (b is None in both):
        no losing scenario  -> f* = 1.0
        no winning scenario -> f* = 0.0

    Returns {"f_star", "quarter", "half", "p_win", "b_odds"}.
    """
    _validate_probs(scenarios)

    winners = []
    losers = []
    for sc in scenarios:
        r = sc["price_target"] / entry_price - 1
        if r > 0:
            winners.append((sc["prob"], r))
        elif r < 0:
            losers.append((sc["prob"], r))
        # r == 0 (break-even) contributes to neither win nor loss weighting.

    p = sum(prob for prob, _ in winners)

    if not losers:
        # Nothing can lose -> bet everything (full Kelly = 1.0), no odds defined.
        f_star, b = 1.0, None
    elif not winners:
        # Nothing can win -> bet nothing, no odds defined.
        f_star, b = 0.0, None
    else:
        win = sum(prob * r for prob, r in winners) / p
        loss = abs(sum(prob * r for prob, r in losers) / (1 - p))
        b = win / loss
        f_star = p - (1 - p) / b
        if f_star < 0:
            f_star = 0.0

    return {
        "f_star": f_star,
        "quarter": f_star / 4,
        "half": f_star / 2,
        "p_win": p,
        "b_odds": b,
    }


# --------------------------------------------------------------------------- #
# QC18: adapt coverage's own DCF scenario fan into the canonical scenario shape.
# --------------------------------------------------------------------------- #

# The three roles coverage's DCF fan is keyed by (module_valuation_reconcile.json's
# "scenarios" block -- itself a verbatim copy of coverage/scenario_drivers.json's
# "scenarios" -- a DIFFERENT on-disk shape than the ev_kelly scenario contract:
# {"bear"|"base"|"bull": {"probability", "dcf_value_per_share"}} rather than a
# list of {"name","prob","price_target"}).
_COVERAGE_DCF_ROLES = ("bear", "base", "bull")


def _is_num(x) -> bool:
    """True for a real (non-bool) int/float."""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def coverage_dcf_scenarios(reconcile_scenarios) -> list:
    """Reshape coverage's own DCF scenario fan into the canonical ev_kelly
    scenario list ``[{"name","prob","price_target"}, ...]``.

    ``reconcile_scenarios`` is ``module_valuation_reconcile.json["scenarios"]``
    (or the equivalent already-parsed dict): keyed "bear"/"base"/"bull" ->
    {"probability", "dcf_value_per_share"}. This is the ONE place that performs
    this reshape -- score_composite's scenario_derivation disclosure and
    report_qc's ev_scenario_agreement gate (QC18) both call it, so they read the
    SAME coverage EV off the SAME conversion.

    A role is skipped (never guessed) when it is absent, non-dict, or either
    field is missing/non-numeric. Output names are prefixed ``dcf_<role>``
    (``dcf_bear``/``dcf_base``/``dcf_bull``) so they read unambiguously next to
    a comps anchor. Returns ``[]`` (not an error) when nothing usable is
    present, when ``reconcile_scenarios`` is not a dict, or when it is empty --
    callers treat an empty list as "no usable coverage scenarios".

    Does NOT validate that the extracted probabilities sum to 1.0 -- callers
    that need that gate (e.g. an EV computation) check it themselves, exactly
    like ``ev_at`` doesn't validate probs either.
    """
    if not isinstance(reconcile_scenarios, dict):
        return []
    out = []
    for role in _COVERAGE_DCF_ROLES:
        node = reconcile_scenarios.get(role)
        if not isinstance(node, dict):
            continue
        prob = node.get("probability")
        value = node.get("dcf_value_per_share")
        if _is_num(prob) and _is_num(value):
            out.append({"name": f"dcf_{role}", "prob": prob, "price_target": value})
    return out


def size_recommendation(f_star, profile, binary_event_within_30d: bool) -> dict:
    """Cap a full-Kelly fraction into a recommended position size.

    ``profile`` must be one of trader (cap 5%), balanced (8%), long-term (10%);
    an unknown profile raises ValueError.

    Normal:              recommended = min(f_star / 2, cap)      (half-Kelly)
    Binary event <= 30d: recommended = min(f_star / 4, cap / 2)  (extra haircut)

    Returns {"recommended_pct", "cap_pct", "rationale"} where rationale is one
    sentence naming the binding constraint.
    """
    if profile not in _CAPS:
        raise ValueError(f"unknown profile {profile!r}; expected one of {sorted(_CAPS)}")

    cap = _CAPS[profile]
    if binary_event_within_30d:
        kelly_frac = f_star / 4
        effective_cap = cap / 2
        kelly_label = "quarter-Kelly"
        cap_label = f"half the {profile} cap ({effective_cap:.1%}) due to a binary event within 30d"
    else:
        kelly_frac = f_star / 2
        effective_cap = cap
        kelly_label = "half-Kelly"
        cap_label = f"the {profile} cap ({effective_cap:.1%})"

    recommended = min(kelly_frac, effective_cap)
    if kelly_frac <= effective_cap:
        rationale = f"{kelly_label} ({kelly_frac:.1%}) is the binding constraint."
    else:
        rationale = f"{cap_label} is the binding constraint, capping {kelly_label} ({kelly_frac:.1%})."

    return {
        "recommended_pct": recommended,
        "cap_pct": effective_cap,
        "rationale": rationale,
    }
