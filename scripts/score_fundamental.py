"""Compressed-pass fundamental scorer for the trading-desk plugin.

WHY THIS MODULE EXISTS: this is the ALWAYS-AVAILABLE fundamental path (design spec
§8.1 "FSI absent" branch). The plugin's deep fundamental read is the FSI
initiation / model reuse; when that is not applied, the composite report still
needs a disclosed, snapshot-only fundamental score so a ticker never lands with a
blank fundamental dimension. Like technical-analysis, risk-analytics and
sentiment-positioning, this module's arithmetic IS the rubric of record
(fundamental rubric v1.0.0, "compressed_snapshot_pass"): every branch is
deterministic and unit-pinned so a report can never silently drift, and the mode
is disclosed at the module top level (``fundamental_mode`` + ``mode_note``) so a
reader always knows this was the snapshot-only pass, not the deep model. The LLM
layer narrates; it does no scoring arithmetic. There is NO SKILL.md for this pass
-- the composite-score skill invokes this CLI directly when ``module_fundamental``
is absent.

Scoring is over two dimensions (max 100 total):
    1. Quality    (50) -- revenue growth (15) + margins gm/om (8+7) +
                          returns on capital/roe (10) + FCF margin (10)
    2. Valuation  (50) -- fwd P/E vs own 5-yr median (20) + PEG (15) +
                          FCF yield (15)

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
to build the median is disclosed wherever this scores.

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

RUBRIC_VERSION = "1.0.0"
SKILL_NAME = "fundamental"
FUNDAMENTAL_MODE = "compressed_snapshot_pass"
MODE_NOTE = ("snapshot-only fundamental pass; deep FSI initiation/model reuse "
            "not applied")

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
# 1. Quality (max 50): rev growth (15) + margins gm(8)/om(7) + roe (10) + fcf (10)
# --------------------------------------------------------------------------- #

def score_quality(fund) -> dict:
    """Revenue growth (15) + margins (gm 8 + om 7) + roe (10) + FCF margin (10).

    rev growth (rev_growth_latest_q, YoY fraction): >0.20 -> 15; (0.10,0.20] -> 11;
        (0.03,0.10] -> 8; [0,0.03] -> 5; <0 -> 2; null -> 0 ("n/a").
    gm (gm_ttm): >=0.50 -> 8; [0.35,0.50) -> 6; [0.20,0.35) -> 4; <0.20 -> 2;
        null -> 0.
    om (om_ttm): >=0.25 -> 7; [0.15,0.25) -> 5; [0.05,0.15) -> 3; <0.05 -> 1;
        null -> 0.
    roe (percent OR fraction): value > 3 is treated as a percent and divided by
        100 (the normalization is labeled in the arithmetic); then >=0.30 -> 10;
        [0.15,0.30) -> 7; [0.05,0.15) -> 4; <0.05 -> 1; null -> 0.
    FCF margin = fcf_ttm / rev_ttm: >=0.20 -> 10; [0.10,0.20) -> 7; [0,0.10) -> 4;
        <0 -> 1; either null (or rev_ttm 0) -> 0.
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
            rg_pts = 15
        elif rg > 0.10:
            rg_pts = 11
        elif rg > 0.03:
            rg_pts = 8
        elif rg >= 0:
            rg_pts = 5
        else:  # < 0
            rg_pts = 2
        parts.append(f"rev_growth_latest_q {_fmt(rg)} -> {rg_pts}/15")
    else:
        rg_pts = 0
        parts.append("rev_growth_latest_q: n/a (+0)")

    # -- gross margin ------------------------------------------------------
    if gm is not None:
        evaluable += 1
        if gm >= 0.50:
            gm_pts = 8
        elif gm >= 0.35:
            gm_pts = 6
        elif gm >= 0.20:
            gm_pts = 4
        else:  # < 0.20
            gm_pts = 2
        parts.append(f"gm_ttm {_fmt(gm)} -> {gm_pts}/8")
    else:
        gm_pts = 0
        parts.append("gm_ttm: n/a (+0)")

    # -- operating margin --------------------------------------------------
    if om is not None:
        evaluable += 1
        if om >= 0.25:
            om_pts = 7
        elif om >= 0.15:
            om_pts = 5
        elif om >= 0.05:
            om_pts = 3
        else:  # < 0.05
            om_pts = 1
        parts.append(f"om_ttm {_fmt(om)} -> {om_pts}/7")
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
            roe_pts = 10
        elif roe_norm >= 0.15:
            roe_pts = 7
        elif roe_norm >= 0.05:
            roe_pts = 4
        else:  # < 0.05
            roe_pts = 1
        parts.append(f"{norm_label} -> {roe_pts}/10")
    else:
        roe_pts = 0
        parts.append("roe: n/a (+0)")

    # -- FCF margin (fcf_ttm / rev_ttm) ------------------------------------
    if fcf is not None and rev not in (None, 0):
        evaluable += 1
        fcf_margin = fcf / rev
        if fcf_margin >= 0.20:
            fcf_pts = 10
        elif fcf_margin >= 0.10:
            fcf_pts = 7
        elif fcf_margin >= 0:
            fcf_pts = 4
        else:  # < 0
            fcf_pts = 1
        parts.append(
            f"fcf_margin (fcf_ttm {_fmt(_clean(fcf))} / rev_ttm {_fmt(_clean(rev))} "
            f"= {_fmt(_clean(fcf_margin))}) -> {fcf_pts}/10")
    else:
        fcf_pts = 0
        parts.append("fcf_margin: n/a (fcf_ttm or rev_ttm null/zero) (+0)")

    total = rg_pts + gm_pts + om_pts + roe_pts + fcf_pts
    return {
        "name": "quality",
        "points": _clean(total),
        "max": 50,
        "arithmetic": "; ".join(parts),
        "inputs": {"rev_growth_points": rg_pts, "gm_points": gm_pts,
                   "om_points": om_pts, "roe_points": roe_pts,
                   "fcf_margin_points": fcf_pts,
                   "rev_growth_latest_q": rg, "gm_ttm": gm, "om_ttm": om,
                   "roe": roe, "roe_normalized": roe_norm,
                   "fcf_ttm": fcf, "rev_ttm": rev},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 2. Valuation (max 50): pe-vs-history (20) + PEG (15) + FCF yield (15)
# --------------------------------------------------------------------------- #

def score_valuation(val) -> dict:
    """Fwd P/E vs own 5-yr median (20) + PEG (15) + FCF yield (15).

    multiple vs history: ratio = pe_fwd / pe_5yr_median (both > 0 required, else
        the component is 0 "n/a"): <=0.75 -> 20 (discount to own history);
        (0.75,1.0] -> 14; (1.0,1.25] -> 8; >1.25 -> 3. The arithmetic string carries
        the ``pe_median_method`` label so the approximation is disclosed.
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
    if (pe_fwd is not None and pe_fwd > 0
            and pe_median is not None and pe_median > 0):
        evaluable += 1
        ratio = pe_fwd / pe_median
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
        "inputs": {"pe_ratio_points": pe_pts, "peg_points": peg_pts,
                   "fcf_yield_points": fcfy_pts,
                   "pe_fwd": pe_fwd, "pe_5yr_median": pe_median,
                   "pe_median_method": pe_method, "peg": peg,
                   "fcf_yield": fcf_yield},
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

def score(fund, val) -> dict:
    """Assemble the two subscores and the (possibly renormalized) 0-100 score.

    A dimension whose ``evaluable`` is False (all its scored inputs null) is
    EXCLUDED from the max total and the score is rescaled to 0-100 over the
    remaining max, with ``renormalized: true`` recorded.
    """
    subs = [
        score_quality(fund if isinstance(fund, dict) else {}),
        score_valuation(val if isinstance(val, dict) else {}),
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
# CLI (mirrors score_sentiment.py; snapshot-only, no judgment flags)
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def build_module(snapshot) -> dict:
    """Build the full module_fundamental.json document from a parsed snapshot."""
    fund = snapshot.get("fundamentals", {}) if isinstance(snapshot, dict) else {}
    val = snapshot.get("valuation", {}) if isinstance(snapshot, dict) else {}
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}

    scored = score(fund, val)

    doc = {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "fundamental_mode": FUNDAMENTAL_MODE,
        "mode_note": MODE_NOTE,
        "ticker": meta.get("ticker"),
        "as_of": build_snapshot._as_of_date(meta.get("as_of_utc")),
        "score": scored["score"],
        "subscores": scored["subscores"],
        "tables": {
            "quality": build_quality_table(fund),
            "valuation": build_valuation_table(val),
        },
        "flags": {},
        "renormalized": scored["renormalized"],
        "signal": None,
    }
    if scored["renormalization_note"]:
        doc["renormalization_note"] = scored["renormalization_note"]
    return doc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score the compressed-pass fundamental dimension for a "
                    "snapshot bundle (rubric v%s, snapshot-only)." % RUBRIC_VERSION)
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--out", default=None,
                        help="output path (default <bundle>/module_fundamental.json)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
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

    doc = build_module(snapshot)

    out = args.out or os.path.join(args.bundle, "module_fundamental.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
