"""Compressed-pass fundamental scorer for the trading-desk plugin.

WHY THIS MODULE EXISTS: this is the ALWAYS-AVAILABLE fundamental path (design spec
§8.1 "FSI absent" branch). The plugin's deep fundamental read is the FSI
initiation / model reuse; when that is not applied, the composite report still
needs a disclosed, snapshot-only fundamental score so a ticker never lands with a
blank fundamental dimension. Like technical-analysis, risk-analytics and
sentiment-positioning, this module's arithmetic IS the rubric of record
(fundamental rubric v1.1.0, "compressed_snapshot_pass"): every branch is
deterministic and unit-pinned so a report can never silently drift, and the mode
is disclosed at the module top level (``fundamental_mode`` + ``mode_note``) so a
reader always knows this was the snapshot-only pass, not the deep model. The LLM
layer narrates; it does no scoring arithmetic. There is NO SKILL.md for this pass
-- the composite-score skill invokes this CLI directly when ``module_fundamental``
is absent.

Scoring is over two dimensions (max 100 total):
    1. Quality    (50) -- revenue growth (12) + margins gm/om (7+5) +
                          returns on capital/roe (8) + FCF margin (8) +
                          moat/positioning judgment flag (10)
    2. Valuation  (50) -- TWO MODES (see RUBRIC v1.2.0 below):
       - snapshot mode (v1.1 floor): fwd P/E vs own 5-yr median (20) + PEG (15)
                          + FCF yield (15)
       - anchored mode (v1.2, --anchors): DCF-band position (17) + comps-range
                          position (13) + own-history multiple (8) + FCF yield (7)
                          + justified sector-band position (5) = 50; PEG is
                          DISPLAY-ONLY (excluded from scoring).

RUBRIC v1.0.0 -> v1.1.0 (coverage-first spec, Task C1 -- "context in scoring"):
the Quality dimension is rebalanced from a five-component mechanical 50 to a
SIX-component 50 so that MOAT/POSITIONING enters the score. The design intent of
coverage-first is that the qualitative context a report gathers must actually move
a number, not just narrate alongside it; a moat read is the natural quality lever.
The mechanical components shrink to make room (rev growth 15->12, gm 8->7, om 7->5,
roe 10->8, fcf 10->8, summing to 40) and a new moat component (max 10) is added.
Moat is a JUDGMENT FLAG (like sentiment's rating_actions / inst_flow): the CLI takes
``--moat wide|narrow|none`` with a REQUIRED ``--moat-justification``, and -- because
the read must be grounded in the gathered context -- the justification MUST cite at
least one context finding ID (a ``C\\d+`` token, e.g. "C3"). Omitting ``--moat``
entirely scores 0 ("moat: n/a (no context assessment)") and, like sentiment's
inst_flow "unknown", does NOT count toward the dimension's evaluable inputs; a
PRESENT flag is always evaluable. Valuation (50) is unchanged.

RUBRIC v1.1.0 -> v1.2.0 (sector-scales batch, Task V1 -- "anchored valuation"):
QUALITY/MOAT ARE UNCHANGED; only Valuation is reworked, and only when the caller
supplies real valuation anchors. The compressed snapshot pass bands a name against
its OWN price history, which is honest but blind to a fundamentals-derived fair
value. When a ``--anchors <valuation_anchors.json>`` is provided the Valuation 50
switches to ANCHORED MODE, decomposing into five disclosed components:
    1. DCF-band position       (max 17) -- last vs a DCF base/bear/bull.
    2. Comps-range position    (max 13) -- last vs a comps low/high.
    3. Own-history multiple    (max 8)  -- the v1.1 pe_fwd/pe_5yr_median band,
                                           rescaled from 20 to 8 (sanity band kept).
    4. FCF yield               (max 7)  -- the v1.1 fcf_yield band, rescaled to 7.
    5. Justified sector-band   (max 5)  -- last multiple vs a sector_scales band
                                           (via ``--scale``); n/a (0) when absent.
PEG is REMOVED from anchored scoring (institutional practice; PEG is unreliable
for cyclicals) and re-emitted at the module top level as ``peg_display`` --
display-only, never a subscore. The component maxima (17/13/8/7/5) were sized so
no single component exceeds 35% of the 50 (17/50 = 34%): the design cap is honored
by construction rather than enforced at runtime.
DCF disagreement rule: comps_mid = (comps_low+comps_high)/2; disagreement =
|dcf_base - comps_mid| / ((dcf_base+comps_mid)/2). When disagreement > 0.25 the
methods materially conflict, so the DCF band is WIDENED to
[min(dcf_bear,comps_low), max(dcf_bull,comps_high)] AND a CONFIDENCE HAIRCUT scales
the DCF component's max by 0.75 (17 -> 12.75); both are disclosed in the arithmetic.
Absent ``--anchors`` the SNAPSHOT MODE (v1.1 valuation: pe 20 / peg 15 / fcf 15) is
preserved EXACTLY as the FSI-absent floor (PEG stays scored there). The active mode
is disclosed on the valuation subscore as ``valuation_mode``
("anchored_v1.2" | "snapshot_v1.1"). The sector scale (name@version) and the CLI
flags are recorded in the module JSON.

Design contract (project-wide, mirrors score_sentiment.py / score_risk.py):
- The snapshot is READ-ONLY; this module never edits snapshot.json. No market data
  is fetched here; a missing figure contributes 0 and is named "n/a".
- ``INPUT_FIELDS`` lists exactly the snapshot fields this rubric SCORES on (dotted
  paths). A cross-skill governance test (tests/test_single_mapping.py) imports the
  set and asserts the scorers' INPUT_FIELDS are pairwise disjoint.
- If a WHOLE dimension has zero evaluable inputs, it is excluded and the score is
  renormalized to 0-100 over the remaining max.

SINGLE-MAPPING SPLIT (spec §2): balance-sheet SOLVENCY
(``fundamentals.net_cash_defined.net``) is OWNED by risk-analytics and is NOT
scored here; EPS-REVISIONS (``fundamentals.revisions_90d``) are OWNED by
sentiment-positioning and are NOT scored here. ``valuation.pe_5yr_median`` is
scored HERE (multiple-vs-own-history) -- risk-analytics consumes it only as an
unscored downside-map level (its valuation_floor), so there is no collision.

The pe-vs-history component MUST carry the snapshot's ``valuation.pe_median_method``
label ("approx_current_eps") into its arithmetic string so the approximation used
to build the median is disclosed wherever this scores. That method back-projects
TODAY's EPS across the 5-yr price history, so for a name whose EPS regime shifted
(real MU: pe_5yr_median 1.82) the baseline is garbage. The component therefore
carries a SANITY BAND on the ratio ``pe_fwd / pe_5yr_median``: a ratio outside
[0.2, 5.0] is treated as the method having broken down under an EPS regime change --
the component scores 0 and is treated as n/a (like a null input for the evaluable /
renormalization accounting), so the dimension renormalizes over the components that
remain rather than banding on a bogus multiple.

No dependency on other scored modules: this module consumes the snapshot only.
Reuses the build_snapshot I/O helper for the CLI ``as_of`` date. The scoring
functions are pure over already-parsed inputs. stdlib-only.
"""

import argparse
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation (``python3 scripts/score_fundamental.py``): ensure the
# repo root is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import build_snapshot
from scripts import sector_scales

RUBRIC_VERSION = "1.2.0"
SKILL_NAME = "fundamental"
# Top-level mode disclosure tracks the valuation mode actually used (ORCL live
# finding: the static "not applied" note shipped on runs where FSI initiation
# HAD run and anchors scored the valuation — a false disclosure).
FUNDAMENTAL_MODE = "compressed_snapshot_pass"
MODE_NOTE = ("snapshot-only fundamental pass; deep FSI initiation/model reuse "
            "not applied")
FUNDAMENTAL_MODE_ANCHORED = "coverage_anchored_pass"
MODE_NOTE_ANCHORED = ("coverage-anchored pass; valuation scored against the "
                      "coverage DCF/comps anchors (quality reads the snapshot "
                      "per single-mapping; coverage enters via anchors + the "
                      "cited moat flag)")

# Moat/positioning judgment-flag choices + point table (v1.1.0). Mirrors the
# score_sentiment judgment-flag pattern: OMITTING the flag (moat is None) is the
# "not assessed" state -- it scores 0 and does not count toward evaluable; any of
# the three PRESENT choices is always evaluable.
_MOAT_CHOICES = ("wide", "narrow", "none")
_MOAT_POINTS = {"wide": 10, "narrow": 6, "none": 2}

# Context finding IDs look like C3 / C12; a moat justification must cite at least one
# (coverage-first: the moat read is grounded in the gathered context, not free-form).
import re as _re
_CITATION_RE = _re.compile(r"C\d+")


def _context_finding_ids(bundle):
    """The set of findings[] ids in <bundle>/module_context.json, or None.

    Returns None when the context module is absent or unparseable (the
    compressed / FSI-absent floor: no context registry to check against, so the
    moat gate stays presence-only). Returns a (possibly empty) set of id strings
    when it exists and parses -- the referential-integrity check then verifies
    every cited C-ID resolves to one of these.
    """
    path = os.path.join(bundle, "module_context.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            module = json.load(fh)
    except (OSError, ValueError):
        return None
    findings = module.get("findings") if isinstance(module, dict) else None
    if not isinstance(findings, list):
        return set()
    ids = set()
    for f in findings:
        if isinstance(f, dict) and isinstance(f.get("id"), str):
            ids.add(f["id"])
    return ids


def _unresolved_citations(justification, finding_ids):
    """Cited C-IDs (in order, de-duped) that are NOT in ``finding_ids``."""
    out = []
    seen = set()
    for cid in _CITATION_RE.findall(justification or ""):
        if cid not in finding_ids and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


# The snapshot fields this rubric SCORES on. Solvency (net_cash_defined.net) is
# owned by risk-analytics; revisions_90d is owned by sentiment-positioning; both
# are intentionally NOT listed (see module docstring / single-mapping test).
INPUT_FIELDS = {
    "fundamentals.rev_growth_latest_q",
    "fundamentals.gm_ttm",
    "fundamentals.om_ttm",
    "fundamentals.roe",
    "fundamentals.fcf_ttm",
    "fundamentals.rev_ttm",
    "valuation.pe_fwd",
    "valuation.pe_5yr_median",
    "valuation.peg",
    "valuation.fcf_yield",
}

# No fields gate/cap a branch here without being scored -- this pass is fully
# mechanical -- so GUARD_FIELDS is empty (declared for parity with the other
# scorers and the governance test's ``getattr(mod, "GUARD_FIELDS", set())``).
GUARD_FIELDS = set()


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
# 1. Quality (max 50, v1.1.0): rev growth (12) + margins gm(7)/om(5) + roe (8) +
#    fcf (8) + moat/positioning judgment flag (10)
# --------------------------------------------------------------------------- #

def score_quality(fund, moat=None, moat_justification=None) -> dict:
    """Revenue growth (12) + margins (gm 7 + om 5) + roe (8) + FCF margin (8) +
    moat/positioning judgment flag (10).

    rev growth (rev_growth_latest_q, YoY fraction): >0.20 -> 12; (0.10,0.20] -> 9;
        (0.03,0.10] -> 6; [0,0.03] -> 4; <0 -> 2; null -> 0 ("n/a").
    gm (gm_ttm): >=0.50 -> 7; [0.35,0.50) -> 5; [0.20,0.35) -> 3; <0.20 -> 1;
        null -> 0.
    om (om_ttm): >=0.25 -> 5; [0.15,0.25) -> 4; [0.05,0.15) -> 2; <0.05 -> 1;
        null -> 0.
    roe (percent OR fraction): value > 3 is treated as a percent and divided by
        100 (the normalization is labeled in the arithmetic); then >=0.30 -> 8;
        [0.15,0.30) -> 6; [0.05,0.15) -> 3; <0.05 -> 1; null -> 0.
    FCF margin = fcf_ttm / rev_ttm: >=0.20 -> 8; [0.10,0.20) -> 6; [0,0.10) -> 3;
        <0 -> 1; either null (or rev_ttm 0) -> 0.
    moat/positioning (JUDGMENT FLAG, v1.1.0): wide -> 10; narrow -> 6; none -> 2;
        flag OMITTED (moat is None) -> 0 "moat: n/a (no context assessment)" and
        does NOT count toward evaluable (mirrors sentiment's inst_flow "unknown");
        any PRESENT choice always counts toward evaluable.
    """
    rg = fund.get("rev_growth_latest_q")
    gm = fund.get("gm_ttm")
    om = fund.get("om_ttm")
    roe = fund.get("roe")
    fcf = fund.get("fcf_ttm")
    rev = fund.get("rev_ttm")

    parts = []
    evaluable = 0

    # -- revenue growth ----------------------------------------------------
    if rg is not None:
        evaluable += 1
        if rg > 0.20:
            rg_pts = 12
        elif rg > 0.10:
            rg_pts = 9
        elif rg > 0.03:
            rg_pts = 6
        elif rg >= 0:
            rg_pts = 4
        else:  # < 0
            rg_pts = 2
        parts.append(f"rev_growth_latest_q {_fmt(rg)} -> {rg_pts}/12")
    else:
        rg_pts = 0
        parts.append("rev_growth_latest_q: n/a (+0)")

    # -- gross margin ------------------------------------------------------
    if gm is not None:
        evaluable += 1
        if gm >= 0.50:
            gm_pts = 7
        elif gm >= 0.35:
            gm_pts = 5
        elif gm >= 0.20:
            gm_pts = 3
        else:  # < 0.20
            gm_pts = 1
        parts.append(f"gm_ttm {_fmt(gm)} -> {gm_pts}/7")
    else:
        gm_pts = 0
        parts.append("gm_ttm: n/a (+0)")

    # -- operating margin --------------------------------------------------
    if om is not None:
        evaluable += 1
        if om >= 0.25:
            om_pts = 5
        elif om >= 0.15:
            om_pts = 4
        elif om >= 0.05:
            om_pts = 2
        else:  # < 0.05
            om_pts = 1
        parts.append(f"om_ttm {_fmt(om)} -> {om_pts}/5")
    else:
        om_pts = 0
        parts.append("om_ttm: n/a (+0)")

    # -- returns on capital / roe (percent-vs-fraction normalization) ------
    roe_norm = None
    if roe is not None:
        evaluable += 1
        if roe > 3:
            roe_norm = _clean(roe / 100.0)
            norm_label = (f"roe {_fmt(roe)} > 3 treated as percent -> "
                          f"{_fmt(roe_norm)}")
        else:
            roe_norm = _clean(roe)
            norm_label = f"roe {_fmt(roe_norm)} (fraction)"
        if roe_norm >= 0.30:
            roe_pts = 8
        elif roe_norm >= 0.15:
            roe_pts = 6
        elif roe_norm >= 0.05:
            roe_pts = 3
        else:  # < 0.05
            roe_pts = 1
        parts.append(f"{norm_label} -> {roe_pts}/8")
    else:
        roe_pts = 0
        parts.append("roe: n/a (+0)")

    # -- FCF margin (fcf_ttm / rev_ttm) ------------------------------------
    if fcf is not None and rev not in (None, 0):
        evaluable += 1
        fcf_margin = fcf / rev
        if fcf_margin >= 0.20:
            fcf_pts = 8
        elif fcf_margin >= 0.10:
            fcf_pts = 6
        elif fcf_margin >= 0:
            fcf_pts = 3
        else:  # < 0
            fcf_pts = 1
        parts.append(
            f"fcf_margin (fcf_ttm {_fmt(_clean(fcf))} / rev_ttm {_fmt(_clean(rev))} "
            f"= {_fmt(_clean(fcf_margin))}) -> {fcf_pts}/8")
    else:
        fcf_pts = 0
        parts.append("fcf_margin: n/a (fcf_ttm or rev_ttm null/zero) (+0)")

    # -- moat/positioning judgment flag (NEW v1.1.0) -----------------------
    # OMITTED flag (moat is None) mirrors sentiment inst_flow "unknown": +0 and NOT
    # counted toward evaluable. Any PRESENT choice is always evaluable.
    if moat is None:
        moat_pts = 0
        parts.append("moat: n/a (no context assessment) (+0)")
    else:
        evaluable += 1
        moat_pts = _MOAT_POINTS[moat]
        parts.append(f"moat {moat} -> +{moat_pts}/10")

    total = rg_pts + gm_pts + om_pts + roe_pts + fcf_pts + moat_pts
    return {
        "name": "quality",
        "points": _clean(total),
        "max": 50,
        "arithmetic": "; ".join(parts),
        "inputs": {"rev_growth_points": rg_pts, "gm_points": gm_pts,
                   "om_points": om_pts, "roe_points": roe_pts,
                   "fcf_margin_points": fcf_pts, "moat_points": moat_pts,
                   "rev_growth_latest_q": rg, "gm_ttm": gm, "om_ttm": om,
                   "roe": roe, "roe_normalized": roe_norm,
                   "fcf_ttm": fcf, "rev_ttm": rev,
                   "moat": moat, "moat_justification": moat_justification},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 2. Valuation (max 50): pe-vs-history (20) + PEG (15) + FCF yield (15)
# --------------------------------------------------------------------------- #

def score_valuation(val) -> dict:
    """Fwd P/E vs own 5-yr median (20) + PEG (15) + FCF yield (15).

    multiple vs history: ratio = pe_fwd / pe_5yr_median (both > 0 required, else
        the component is 0 "n/a"): a ratio OUTSIDE the sanity band [0.2, 5.0] is
        scored 0 and treated as n/a (approx_current_eps method breakdown under an
        EPS regime change) -- not counted toward ``evaluable``; else <=0.75 -> 20
        (discount to own history); (0.75,1.0] -> 14; (1.0,1.25] -> 8; >1.25 -> 3.
        The arithmetic string carries the ``pe_median_method`` label so the
        approximation is disclosed.
    PEG (peg): (0,1.0] -> 15; (1.0,2.0] -> 10; (2.0,3.0] -> 5; >3.0 -> 2;
        null or <=0 -> 0.
    FCF yield (fcf_yield): >=0.05 -> 15; [0.03,0.05) -> 11; [0.015,0.03) -> 7;
        (0,0.015) -> 3; <=0 -> 1; null -> 0.
    """
    pe_fwd = val.get("pe_fwd")
    pe_median = val.get("pe_5yr_median")
    pe_method = val.get("pe_median_method")
    peg = val.get("peg")
    fcf_yield = val.get("fcf_yield")

    parts = []
    evaluable = 0

    # -- multiple vs own 5-yr history --------------------------------------
    # The ratio only means something when pe_5yr_median is a real earnings-history
    # baseline. The snapshot builds that median with the "approx_current_eps"
    # method, which back-projects TODAY's EPS across the 5-yr price history -- for a
    # name whose EPS exploded (real MU: pe_5yr_median 1.82) the baseline is garbage,
    # producing a huge ratio that would band as a "rich premium" on noise. So we
    # gate on a SANITY BAND [0.2, 5.0]: a ratio outside it means the approx method
    # broke down under an EPS regime change, and the component is scored 0 and
    # treated as n/a (NOT counted toward ``evaluable``, exactly like a null input),
    # so the dimension renormalizes over the remaining components instead of banding
    # on a bogus number.
    if (pe_fwd is not None and pe_fwd > 0
            and pe_median is not None and pe_median > 0):
        ratio = pe_fwd / pe_median
        if ratio < 0.2 or ratio > 5.0:
            pe_pts = 0
            parts.append(
                f"pe_fwd/pe_5yr_median ratio {_fmt(_clean(ratio))} outside "
                f"sanity band [0.2,5] -- approx_current_eps method breakdown "
                f"under EPS regime change; component n/a")
        else:
            evaluable += 1
            if ratio <= 0.75:
                pe_pts = 20
                band = "discount to own history"
            elif ratio <= 1.0:
                pe_pts = 14
                band = "in line with own history"
            elif ratio <= 1.25:
                pe_pts = 8
                band = "modest premium to own history"
            else:  # > 1.25
                pe_pts = 3
                band = "rich premium to own history"
            parts.append(
                f"pe_fwd {_fmt(_clean(pe_fwd))} / pe_5yr_median "
                f"{_fmt(_clean(pe_median))} (method {pe_method}) = "
                f"{_fmt(_clean(ratio))} -> {pe_pts}/20 ({band})")
    else:
        pe_pts = 0
        parts.append(
            f"pe_vs_history: n/a (pe_fwd or pe_5yr_median null/non-positive; "
            f"method {pe_method}) (+0)")

    # -- PEG ---------------------------------------------------------------
    if peg is not None and peg > 0:
        evaluable += 1
        if peg <= 1.0:
            peg_pts = 15
        elif peg <= 2.0:
            peg_pts = 10
        elif peg <= 3.0:
            peg_pts = 5
        else:  # > 3.0
            peg_pts = 2
        parts.append(f"peg {_fmt(peg)} -> {peg_pts}/15")
    else:
        peg_pts = 0
        parts.append("peg: n/a (null or <=0) (+0)")

    # -- FCF yield ---------------------------------------------------------
    if fcf_yield is not None:
        evaluable += 1
        if fcf_yield >= 0.05:
            fcfy_pts = 15
        elif fcf_yield >= 0.03:
            fcfy_pts = 11
        elif fcf_yield >= 0.015:
            fcfy_pts = 7
        elif fcf_yield > 0:
            fcfy_pts = 3
        else:  # <= 0
            fcfy_pts = 1
        parts.append(f"fcf_yield {_fmt(fcf_yield)} -> {fcfy_pts}/15")
    else:
        fcfy_pts = 0
        parts.append("fcf_yield: n/a (+0)")

    total = pe_pts + peg_pts + fcfy_pts
    return {
        "name": "valuation",
        "points": _clean(total),
        "max": 50,
        "arithmetic": "; ".join(parts),
        "valuation_mode": "snapshot_v1.1",
        "inputs": {"pe_ratio_points": pe_pts, "peg_points": peg_pts,
                   "fcf_yield_points": fcfy_pts,
                   "pe_fwd": pe_fwd, "pe_5yr_median": pe_median,
                   "pe_median_method": pe_method, "peg": peg,
                   "fcf_yield": fcf_yield},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 2b. Valuation -- ANCHORED MODE (max 50, v1.2.0): DCF-band (17) + comps-range
#     (13) + own-history multiple (8) + FCF yield (7) + justified sector-band (5).
#     PEG is REMOVED from scoring (emitted separately as peg_display).
# --------------------------------------------------------------------------- #

# Anchored-mode component maxima. Sized so no single component exceeds 35% of 50
# (17/50 = 34%); the design cap is honored by construction, not runtime-enforced.
_DCF_MAX = 17
_COMPS_MAX = 13
_OWNHIST_MAX = 8
_FCFY_ANCHORED_MAX = 7
_JUSTIFIED_MAX = 5

# Disagreement threshold above which DCF and comps materially conflict, triggering
# a widened DCF band + a confidence haircut on the DCF component's max.
_DISAGREE_THRESHOLD = 0.25
_CONFIDENCE_HAIRCUT = 0.75

# Required numeric anchor keys (all must be present + positive) in anchors.json.
_ANCHOR_REQUIRED = ("dcf_base", "dcf_bear", "dcf_bull", "comps_low", "comps_high")


def validate_anchors(anchors) -> list:
    """Return a list of named issues for a valuation_anchors.json dict ([] valid).

    Requires dcf_base/dcf_bear/dcf_bull/comps_low/comps_high present + positive.
    current_pb (optional) must be positive when present. Every problem is
    reported so a malformed anchors file names all its issues at once.
    """
    issues = []
    if not isinstance(anchors, dict):
        return ["anchors is not a JSON object"]
    for key in _ANCHOR_REQUIRED:
        v = anchors.get(key)
        if v is None:
            issues.append(f"missing required anchor: {key}")
        elif not isinstance(v, (int, float)) or isinstance(v, bool):
            issues.append(f"anchor {key} must be numeric")
        elif v <= 0:
            issues.append(f"anchor {key} must be positive (got {v})")
    if "current_pb" in anchors and anchors["current_pb"] is not None:
        cpb = anchors["current_pb"]
        if not isinstance(cpb, (int, float)) or isinstance(cpb, bool) or cpb <= 0:
            issues.append("anchor current_pb must be positive when present")
    return issues


def _dcf_band_position(last, anchors):
    """DCF-band component (max 17, or 12.75 haircut when methods disagree).

    Disagreement: comps_mid = (comps_low+comps_high)/2 ;
    disagreement = |dcf_base - comps_mid| / ((dcf_base+comps_mid)/2). When
    disagreement > 0.25 the DCF band is WIDENED to
    [min(dcf_bear,comps_low), max(dcf_bull,comps_high)] and the component's max is
    scaled by 0.75 (17 -> 12.75); both are disclosed in the arithmetic. Banding is
    on r = last/dcf_base: r <= 0.8 -> full; (0.8,1.1] -> 14; (1.1,1.5] -> 9;
    (1.5,2.5] -> 4; > 2.5 -> 1 (absolute tiers against the 17 max; haircut scales
    all tiers by 0.75). Returns (points, arithmetic_str).
    """
    dcf_base = anchors["dcf_base"]
    dcf_bear = anchors["dcf_bear"]
    dcf_bull = anchors["dcf_bull"]
    comps_low = anchors["comps_low"]
    comps_high = anchors["comps_high"]

    comps_mid = (comps_low + comps_high) / 2.0
    denom = (dcf_base + comps_mid) / 2.0
    disagreement = abs(dcf_base - comps_mid) / denom if denom else 0.0
    widened = disagreement > _DISAGREE_THRESHOLD

    haircut = _CONFIDENCE_HAIRCUT if widened else 1.0
    # Absolute tiers against max 17; haircut scales every tier by 0.75.
    r = last / dcf_base
    if r <= 0.8:
        base_pts = 17
        band = "deep discount to DCF base"
    elif r <= 1.1:
        base_pts = 14
        band = "at/below DCF base"
    elif r <= 1.5:
        base_pts = 9
        band = "modest premium to DCF base"
    elif r <= 2.5:
        base_pts = 4
        band = "rich premium to DCF base"
    else:  # > 2.5
        base_pts = 1
        band = "far above DCF base"
    pts = _clean(base_pts * haircut)

    if widened:
        eff_low = min(dcf_bear, comps_low)
        eff_high = max(dcf_bull, comps_high)
        arithmetic = (
            f"disagreement |dcf_base {_fmt(_clean(dcf_base))} - comps_mid "
            f"{_fmt(_clean(comps_mid))}| / mid {_fmt(_clean(denom))} = "
            f"{_fmt(_clean(disagreement))} > {_DISAGREE_THRESHOLD} -> WIDEN band to "
            f"[{_fmt(_clean(eff_low))},{_fmt(_clean(eff_high))}] + confidence haircut "
            f"x{_CONFIDENCE_HAIRCUT} (max {_DCF_MAX} -> {_fmt(_clean(_DCF_MAX * _CONFIDENCE_HAIRCUT))}); "
            f"r = last {_fmt(_clean(last))} / dcf_base {_fmt(_clean(dcf_base))} = "
            f"{_fmt(_clean(r))} -> {base_pts} x {_CONFIDENCE_HAIRCUT} = {_fmt(pts)}/"
            f"{_fmt(_clean(_DCF_MAX * _CONFIDENCE_HAIRCUT))} ({band})")
    else:
        arithmetic = (
            f"disagreement {_fmt(_clean(disagreement))} <= {_DISAGREE_THRESHOLD} "
            f"(no widen); r = last {_fmt(_clean(last))} / dcf_base "
            f"{_fmt(_clean(dcf_base))} = {_fmt(_clean(r))} -> {_fmt(pts)}/{_DCF_MAX} "
            f"({band})")
    return pts, arithmetic


def _comps_range_position(last, anchors):
    """Comps-range component (max 13). last vs [comps_low, comps_high]:
    below low -> 13; in lower half -> 9; upper half -> 6; above high (<=1.5x
    high) -> 3; > 1.5x high -> 1. Returns (points, arithmetic_str)."""
    lo = anchors["comps_low"]
    hi = anchors["comps_high"]
    mid = (lo + hi) / 2.0
    if last < lo:
        pts = 13
        band = "below comps low"
    elif last <= mid:
        pts = 9
        band = "lower half of comps range"
    elif last <= hi:
        pts = 6
        band = "upper half of comps range"
    elif last <= 1.5 * hi:
        pts = 3
        band = "above comps high (<=1.5x)"
    else:  # > 1.5x high
        pts = 1
        band = "far above comps high (>1.5x)"
    arithmetic = (
        f"last {_fmt(_clean(last))} vs comps [{_fmt(_clean(lo))},"
        f"{_fmt(_clean(hi))}] (mid {_fmt(_clean(mid))}) -> {pts}/{_COMPS_MAX} "
        f"({band})")
    return pts, arithmetic


def _own_history_position(pe_fwd, pe_median, pe_method):
    """Own-history multiple component (max 8). The v1.1 pe_fwd/pe_5yr_median band
    rescaled from 20 to 8, keeping the [0.2,5.0] sanity band (outside -> n/a).

    Rescaled tiers (8 x v1.1_fraction): <=0.75 -> 8; (0.75,1.0] -> 5.6;
    (1.0,1.25] -> 3.2; > 1.25 -> 1.2. Returns (points, arithmetic_str,
    evaluable_bool)."""
    if not (pe_fwd is not None and pe_fwd > 0
            and pe_median is not None and pe_median > 0):
        return 0, (f"own_history: n/a (pe_fwd or pe_5yr_median null/non-positive; "
                   f"method {pe_method}) (+0)"), False
    ratio = pe_fwd / pe_median
    if ratio < 0.2 or ratio > 5.0:
        return 0, (
            f"own_history: pe_fwd/pe_5yr_median ratio {_fmt(_clean(ratio))} outside "
            f"sanity band [0.2,5] -- approx_current_eps method breakdown under EPS "
            f"regime change; component n/a"), False
    if ratio <= 0.75:
        pts = 8
        band = "discount to own history"
    elif ratio <= 1.0:
        pts = _clean(20 * 8 / 20 * 14 / 20)  # 5.6
        band = "in line with own history"
    elif ratio <= 1.25:
        pts = _clean(8 * 8 / 20)  # 3.2
        band = "modest premium to own history"
    else:  # > 1.25
        pts = _clean(3 * 8 / 20)  # 1.2
        band = "rich premium to own history"
    arithmetic = (
        f"own_history: pe_fwd {_fmt(_clean(pe_fwd))} / pe_5yr_median "
        f"{_fmt(_clean(pe_median))} (method {pe_method}) = {_fmt(_clean(ratio))} "
        f"-> {_fmt(pts)}/{_OWNHIST_MAX} ({band})")
    return pts, arithmetic, True


def _fcf_yield_anchored(fcf_yield):
    """FCF-yield component (max 7). v1.1 bands rescaled to 7:
    >=.05 -> 7; [.03,.05) -> 5; [.015,.03) -> 3; (0,.015) -> 2; <=0 -> 1;
    null -> 0 n/a. Returns (points, arithmetic_str, evaluable_bool)."""
    if fcf_yield is None:
        return 0, "fcf_yield: n/a (+0)", False
    if fcf_yield >= 0.05:
        pts = 7
    elif fcf_yield >= 0.03:
        pts = 5
    elif fcf_yield >= 0.015:
        pts = 3
    elif fcf_yield > 0:
        pts = 2
    else:  # <= 0
        pts = 1
    return pts, f"fcf_yield {_fmt(fcf_yield)} -> {pts}/{_FCFY_ANCHORED_MAX}", True


def _justified_band_position(scale, snapshot, anchors):
    """Justified sector-band component (max 5).

    The scale (from --scale) declares ``metric_source``: a dotted snapshot path OR
    "anchors:<key>" (e.g. "anchors:current_pb"). Resolve the current multiple, then
    position it vs the sector_scales band: below low -> 5; low..mid -> 4;
    mid..high -> 2; above high -> 1; unresolvable (or no scale) -> 0 n/a (not
    evaluable). Returns (points, arithmetic_str, evaluable_bool)."""
    if scale is None:
        return 0, "justified_band: n/a (no --scale provided) (+0)", False
    src = scale.get("metric_source")
    if not isinstance(src, str) or not src.strip():
        return 0, "justified_band: n/a (scale has no metric_source) (+0)", False

    # Resolve the current multiple from the declared source.
    metric_val = None
    if src.startswith("anchors:"):
        key = src[len("anchors:"):]
        metric_val = anchors.get(key) if isinstance(anchors, dict) else None
    else:
        cur = snapshot if isinstance(snapshot, dict) else {}
        ok = True
        for seg in src.split("."):
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                ok = False
                break
        metric_val = cur if ok else None

    if not isinstance(metric_val, (int, float)) or isinstance(metric_val, bool):
        return 0, (f"justified_band: n/a (metric_source {src!r} unresolvable) "
                   f"(+0)"), False

    band = sector_scales.compute_band(scale)
    lo, mid, hi = band["low"], band["mid"], band["high"]
    if metric_val < lo:
        pts = 5
        pos = "below band low"
    elif metric_val <= mid:
        pts = 4
        pos = "low..mid of band"
    elif metric_val <= hi:
        pts = 2
        pos = "mid..high of band"
    else:  # > high
        pts = 1
        pos = "above band high"
    arithmetic = (
        f"justified_band: metric ({src}) {_fmt(_clean(metric_val))} vs band "
        f"[{_fmt(_clean(lo))},{_fmt(_clean(mid))},{_fmt(_clean(hi))}] -> "
        f"{pts}/{_JUSTIFIED_MAX} ({pos})")
    return pts, arithmetic, True


def score_valuation_anchored(val, anchors, last, scale=None,
                             snapshot=None) -> dict:
    """Anchored-mode valuation (max 50, v1.2.0). See module docstring for the
    component design. ``val`` is the snapshot valuation block (for own-history +
    fcf_yield + PEG display); ``anchors`` is the validated anchors dict; ``last``
    is the current price; ``scale`` (optional) is a validated sector scale;
    ``snapshot`` (optional) is the full snapshot (for a dotted metric_source).

    PEG is NOT scored here -- it is surfaced separately by build_module as
    ``peg_display``. Every component that resolves to n/a is treated like a null
    input (not counted toward evaluable), so a valuation with no resolvable
    component renormalizes away exactly like snapshot mode.
    """
    pe_fwd = val.get("pe_fwd")
    pe_median = val.get("pe_5yr_median")
    pe_method = val.get("pe_median_method")
    fcf_yield = val.get("fcf_yield")

    parts = []
    evaluable = 0

    # 1. DCF-band position (always evaluable: anchors are validated present).
    dcf_pts, dcf_str = _dcf_band_position(last, anchors)
    evaluable += 1
    parts.append(dcf_str)

    # 2. Comps-range position (always evaluable).
    comps_pts, comps_str = _comps_range_position(last, anchors)
    evaluable += 1
    parts.append(comps_str)

    # 3. Own-history multiple.
    own_pts, own_str, own_ok = _own_history_position(pe_fwd, pe_median, pe_method)
    if own_ok:
        evaluable += 1
    parts.append(own_str)

    # 4. FCF yield.
    fcfy_pts, fcfy_str, fcfy_ok = _fcf_yield_anchored(fcf_yield)
    if fcfy_ok:
        evaluable += 1
    parts.append(fcfy_str)

    # 5. Justified sector-band position.
    just_pts, just_str, just_ok = _justified_band_position(scale, snapshot, anchors)
    if just_ok:
        evaluable += 1
    parts.append(just_str)

    total = dcf_pts + comps_pts + own_pts + fcfy_pts + just_pts
    return {
        "name": "valuation",
        "points": _clean(total),
        "max": 50,
        "arithmetic": "; ".join(parts),
        "valuation_mode": "anchored_v1.2",
        "inputs": {"dcf_band_points": _clean(dcf_pts),
                   "comps_range_points": comps_pts,
                   "own_history_points": _clean(own_pts),
                   "fcf_yield_points": fcfy_pts,
                   "justified_band_points": just_pts,
                   "pe_fwd": pe_fwd, "pe_5yr_median": pe_median,
                   "pe_median_method": pe_method, "fcf_yield": fcf_yield,
                   "last": last,
                   "anchors": anchors},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# Tables: verbatim quality + valuation inputs (for the brief's mini-tables)
# --------------------------------------------------------------------------- #

def build_quality_table(fund) -> dict:
    """Verbatim quality inputs (the scored figures repeated for the brief)."""
    return {
        "rev_growth_latest_q": fund.get("rev_growth_latest_q"),
        "gm_ttm": fund.get("gm_ttm"),
        "om_ttm": fund.get("om_ttm"),
        "roe": fund.get("roe"),
        "fcf_ttm": fund.get("fcf_ttm"),
        "rev_ttm": fund.get("rev_ttm"),
    }


def build_valuation_table(val) -> dict:
    """Verbatim valuation inputs, carrying the pe_median_method disclosure."""
    return {
        "pe_fwd": val.get("pe_fwd"),
        "pe_5yr_median": val.get("pe_5yr_median"),
        "pe_median_method": val.get("pe_median_method"),
        "peg": val.get("peg"),
        "fcf_yield": val.get("fcf_yield"),
    }


# --------------------------------------------------------------------------- #
# Composite scoring + renormalization (identical pattern to the other scorers)
# --------------------------------------------------------------------------- #

def score(fund, val, moat=None, moat_justification=None,
          anchors=None, last=None, scale=None, snapshot=None) -> dict:
    """Assemble the two subscores and the (possibly renormalized) 0-100 score.

    A dimension whose ``evaluable`` is False (all its scored inputs null) is
    EXCLUDED from the max total and the score is rescaled to 0-100 over the
    remaining max, with ``renormalized: true`` recorded. The moat judgment flag
    (v1.1.0) is threaded into the quality dimension.

    Valuation MODE (v1.2.0): when ``anchors`` is provided (and ``last`` is a
    number) the valuation dimension uses ANCHORED MODE
    (score_valuation_anchored); otherwise it uses SNAPSHOT MODE
    (score_valuation, the v1.1 floor). Quality/moat are identical in both modes.
    """
    val_block = val if isinstance(val, dict) else {}
    if anchors is not None and isinstance(last, (int, float)) \
            and not isinstance(last, bool):
        val_sub = score_valuation_anchored(val_block, anchors, last, scale,
                                           snapshot)
    else:
        val_sub = score_valuation(val_block)
    subs = [
        score_quality(fund if isinstance(fund, dict) else {},
                      moat, moat_justification),
        val_sub,
    ]

    included = [s for s in subs if s.get("evaluable", True)]
    raw_max = sum(s["max"] for s in included)
    raw_pts = sum(s["points"] for s in included)
    renormalized = raw_max != 100

    if raw_max <= 0:
        final = 0
    else:
        final = _clean(raw_pts / raw_max * 100)

    note = None
    if renormalized:
        excluded = [s["name"] for s in subs if not s.get("evaluable", True)]
        note = (f"renormalized over max {raw_max} "
                f"(excluded dimensions with no evaluable inputs: "
                f"{', '.join(excluded)})")

    # Strip the internal "evaluable" flag from the published subscores. A dimension
    # excluded from renormalization keeps its row (the arithmetic trail stays
    # visible) but its ``max`` is zeroed so the published subscores' max total
    # equals the renormalization denominator.
    published = []
    for s in subs:
        row = {k: v for k, v in s.items() if k != "evaluable"}
        if not s.get("evaluable", True):
            row["max"] = 0
            row["points"] = 0
            row["excluded"] = True
        published.append(row)

    return {
        "score": final,
        "subscores": published,
        "renormalized": renormalized,
        "renormalization_note": note,
    }


# --------------------------------------------------------------------------- #
# CLI (mirrors score_sentiment.py; snapshot-only + the v1.1.0 moat judgment flag)
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def build_module(snapshot, moat=None, moat_justification=None,
                 anchors=None, scale=None) -> dict:
    """Build the full module_fundamental.json document from a parsed snapshot.

    The moat judgment flag (v1.1.0) is threaded into the quality scoring and
    recorded in ``flags`` (mirroring score_sentiment's flag disclosure).

    Valuation MODE (v1.2.0): when ``anchors`` (a validated valuation_anchors dict)
    is provided the valuation dimension uses ANCHORED MODE, positioning the
    current price against the DCF/comps/own-history/fcf/justified-band components;
    PEG is emitted top-level as ``peg_display`` (excluded from scoring). ``scale``
    (a validated sector scale dict) supplies the justified-band component and is
    recorded as ``sector_scale`` (name@version). Absent ``anchors`` the SNAPSHOT
    MODE (v1.1 floor) is used and PEG stays scored.
    """
    fund = snapshot.get("fundamentals", {}) if isinstance(snapshot, dict) else {}
    val = snapshot.get("valuation", {}) if isinstance(snapshot, dict) else {}
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
    price = snapshot.get("price", {}) if isinstance(snapshot, dict) else {}
    last = price.get("last") if isinstance(price, dict) else None

    scored = score(fund, val, moat, moat_justification,
                   anchors=anchors, last=last, scale=scale, snapshot=snapshot)

    anchored = anchors is not None and isinstance(last, (int, float)) \
        and not isinstance(last, bool)

    doc = {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "fundamental_mode": (FUNDAMENTAL_MODE_ANCHORED if anchored
                             else FUNDAMENTAL_MODE),
        "mode_note": MODE_NOTE_ANCHORED if anchored else MODE_NOTE,
        "ticker": meta.get("ticker"),
        "as_of": build_snapshot._as_of_date(meta.get("as_of_utc")),
        "score": scored["score"],
        "subscores": scored["subscores"],
        "tables": {
            "quality": build_quality_table(fund),
            "valuation": build_valuation_table(val),
        },
        "flags": {
            "moat": moat,
            "moat_justification": moat_justification,
        },
        "sector_scale": (f"{scale['scale']}@{scale['version']}"
                         if anchored and isinstance(scale, dict) else None),
        "renormalized": scored["renormalized"],
        "signal": None,
    }
    # PEG is display-only in anchored mode (institutional practice; unreliable for
    # cyclicals). In snapshot mode PEG stays SCORED, so no display block is added.
    if anchored:
        doc["peg_display"] = {
            "value": val.get("peg"),
            "note": ("display-only; excluded from scoring (institutional "
                     "practice; unreliable for cyclicals)"),
        }
    if scored["renormalization_note"]:
        doc["renormalization_note"] = scored["renormalization_note"]
    return doc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score the compressed-pass fundamental dimension for a "
                    "snapshot bundle (rubric v%s, snapshot-only)." % RUBRIC_VERSION)
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--moat", default=None, choices=_MOAT_CHOICES,
                        help="moat/positioning judgment from the gathered context "
                             "(wide|narrow|none). REQUIRES --moat-justification, and "
                             "that justification must cite >=1 context finding ID "
                             "(e.g. C3). Omit entirely to score 0 'n/a'.")
    parser.add_argument("--moat-justification", default=None,
                        help="required whenever --moat is given; must reference at "
                             "least one context finding ID (regex C\\d+, e.g. C3)")
    parser.add_argument("--anchors", default=None,
                        help="path to valuation_anchors.json; switches the "
                             "valuation dimension to ANCHORED MODE (v1.2 -- "
                             "DCF/comps/own-history/fcf/justified-band, PEG "
                             "display-only). Absent -> snapshot mode (v1.1 floor).")
    parser.add_argument("--scale", default=None,
                        help="path to a sector-scale JSON (sector_scales format); "
                             "supplies the anchored justified-band component. "
                             "Ignored in snapshot mode.")
    parser.add_argument("--out", default=None,
                        help="output path (default <bundle>/module_fundamental.json)")
    args = parser.parse_args(argv)

    # Moat judgment-flag validation (mirrors score_sentiment's flag+justification
    # gate, plus the coverage-first citation requirement).
    if args.moat is not None:
        if not args.moat_justification:
            print("ERROR: --moat-justification is required when --moat is given",
                  file=sys.stderr)
            return 2
        if not _CITATION_RE.search(args.moat_justification):
            print("ERROR: moat justification must cite context finding IDs "
                  "(e.g. C3)", file=sys.stderr)
            return 2
        # Referential integrity: when a context module exists, every cited C-ID
        # must resolve to a real findings[] id (a present-but-fabricated citation
        # is worse than an absent one). No context module (compressed / FSI-absent
        # floor) -> presence-only, unchanged.
        finding_ids = _context_finding_ids(args.bundle)
        if finding_ids is not None:
            unresolved = _unresolved_citations(args.moat_justification, finding_ids)
            if unresolved:
                n = len(finding_ids)
                print(f"ERROR: cited finding {unresolved[0]} does not exist in "
                      f"module_context.json (findings run C1..C{n})",
                      file=sys.stderr)
                return 2

    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 2

    # Anchored-mode inputs: a malformed/absent anchors file is a hard error (the
    # caller asked for anchored mode explicitly). The scale is optional even in
    # anchored mode (the justified-band component scores n/a without it).
    anchors = None
    if args.anchors is not None:
        try:
            with open(args.anchors) as fh:
                anchors = json.load(fh)
        except OSError as exc:
            print(f"ERROR: cannot read anchors {args.anchors}: {exc}",
                  file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"ERROR: anchors {args.anchors} is not valid JSON: {exc}",
                  file=sys.stderr)
            return 2
        issues = validate_anchors(anchors)
        if issues:
            print("ERROR: invalid anchors " + args.anchors + ": "
                  + "; ".join(issues), file=sys.stderr)
            return 2

    scale = None
    if args.scale is not None:
        try:
            scale = sector_scales.load_scale(args.scale)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

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

    doc = build_module(snapshot, args.moat, args.moat_justification,
                       anchors=anchors, scale=scale)

    out = args.out or os.path.join(args.bundle, "module_fundamental.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
