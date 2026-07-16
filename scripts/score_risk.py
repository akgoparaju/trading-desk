"""Risk-analytics evidence module for the trade-decision plugin.

WHY THIS MODULE EXISTS: this is the SECOND scored evidence skill, and like
technical-analysis its arithmetic IS the rubric of record (risk rubric v1.0.0).
Every branch is deterministic and unit-pinned so a report can never silently
drift: the numbers a brief cites all originate here, in Python, and the version
string travels with them into the module JSON and the brief footer. The LLM layer
narrates; it does no scoring arithmetic.

CONVENTION: higher score = BETTER risk-reward conditions (a low-volatility,
shallow-drawdown, deep-discount, asymmetric, liquid, cash-rich setup scores near
100). This is the opposite polarity from a "danger meter" -- a high risk-analytics
score means the *conditions* are favorable, not that the stock is dangerous.

Scoring is over four dimensions (max 100 total):
    1. Volatility state    (25)  -- rv30 vs 10-yr percentile + benchmark beta
    2. Drawdown profile    (25)  -- max 10-yr drawdown + 30% episode count
                                    + a 20%-vs-30% episode-spread severity proxy
    3. Margin of safety    (30)  -- distance below the all-time high
                                    + ladder asymmetry (support-vs-resistance)
    4. Liquidity & solvency(20)  -- 3-month average dollar volume + net-cash ratio

HARD DEPENDENCY: this module CONSUMES ``<bundle>/module_technical.json`` for the
S/R ladder (the asymmetry component reads ``nearest_support``/``nearest_resistance``
off that ladder). If the technical module is absent, the CLI exits 2 and asks the
caller to run technical-analysis first. The ladder is minted ONLY in levels.py; no
level is invented here.

Design contract (project-wide, mirrors score_technical.py):
- The snapshot is READ-ONLY; this module never edits snapshot.json.
- ``INPUT_FIELDS`` lists exactly the snapshot fields this rubric SCORES on
  (dotted paths). ``price.last`` and the ladder are SHARED reference
  infrastructure and are deliberately excluded (a Task-13 cross-skill test
  imports INPUT_FIELDS to assert dimensions do not double-count a field).
- If a WHOLE dimension has zero evaluable inputs, it is excluded and the score is
  renormalized to 0-100 over the remaining max.

NOTE (single-mapping, deviation from design-spec s5.3): consensus-PT upside is NOT
scored here -- it is scored in sentiment-positioning's street view. The design spec
listed PT-upside in BOTH risk-analytics and sentiment-positioning, violating its
own single-mapping rule ("each snapshot fact scores in exactly one module"). We
resolve the conflict by scoring PT-upside only in sentiment; the ~10 points it
would have carried here are reallocated into the asymmetry component (18) and the
distance-from-ATH component (12), which together already express margin of safety
without double-counting the analyst target.

Reuses scripts.levels (nearest_support, nearest_resistance) on the ladder read
from module_technical.json, and the build_snapshot / chain I/O helpers for the CLI.
The scoring functions are pure over already-parsed inputs. stdlib-only.
"""

import argparse
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trade-decision requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation (``python3 scripts/score_risk.py``): ensure the repo
# root is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import build_snapshot, levels

RUBRIC_VERSION = "1.0.0"
SKILL_NAME = "risk-analytics"

# Blue-sky convention: when there is NO resistance above ``last`` on the ladder,
# treat headroom as 15% for the asymmetry ratio (labeled in the arithmetic).
_BLUE_SKY_RESIST = 0.15

# The snapshot fields this rubric SCORES on. price.last and the ladder are shared
# reference infrastructure and are intentionally NOT listed (see module docstring).
INPUT_FIELDS = {
    "technicals.rv30_vs_10yr_pctile",
    "benchmark.beta",
    "technicals.max_dd_10yr",
    "technicals.dd_episodes_20pct_10yr",
    "technicals.dd_episodes_30pct_10yr",
    "technicals.dist_from_ath_pct",
    "price.adv_dollar_3m",
    "fundamentals.net_cash_defined.net",
    "price.mktcap_computed",
}


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
# 1. Volatility state (max 25): rv percentile (20) + beta (5)
# --------------------------------------------------------------------------- #

def score_volatility(tech, beta) -> dict:
    """rv30_vs_10yr_pctile band (max 20) + benchmark beta band (max 5).

    pctile: <30 -> 20; 30-60 -> 14; 60-80 -> 8; >=80 -> 3; null -> 0 ("n/a").
    beta: <1.2 -> +5; 1.2-1.8 -> +3; >1.8 -> +0; null -> 0.
    Lower volatility and lower beta = better conditions = more points.
    """
    pctile = tech.get("rv30_vs_10yr_pctile")

    parts = []
    evaluable = 0

    if pctile is not None:
        evaluable += 1
        if pctile < 30:
            pctile_pts = 20
        elif pctile < 60:
            pctile_pts = 14
        elif pctile < 80:
            pctile_pts = 8
        else:  # >= 80
            pctile_pts = 3
        parts.append(f"rv30_vs_10yr_pctile {_fmt(pctile)} -> {pctile_pts}/20")
    else:
        pctile_pts = 0
        parts.append("rv30_vs_10yr_pctile: n/a (+0)")

    if beta is not None:
        evaluable += 1
        if beta < 1.2:
            beta_pts = 5
        elif beta <= 1.8:
            beta_pts = 3
        else:  # > 1.8
            beta_pts = 0
        parts.append(f"beta {_fmt(beta)} -> +{beta_pts}")
    else:
        beta_pts = 0
        parts.append("beta: n/a (+0)")

    total = pctile_pts + beta_pts
    return {
        "name": "volatility_state",
        "points": _clean(total),
        "max": 25,
        "arithmetic": "; ".join(parts),
        "inputs": {"pctile_points": pctile_pts, "beta_points": beta_pts,
                   "rv30_vs_10yr_pctile": pctile, "beta": beta},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 2. Drawdown profile (max 25): max_dd (12) + episodes (8) + spread proxy (5)
# --------------------------------------------------------------------------- #

def score_drawdown(tech) -> dict:
    """max_dd_10yr (max 12) + dd_episodes_30pct_10yr (max 8) + severity-spread
    proxy (max 5).

    max_dd (negative fraction): >= -0.35 -> 12; [-0.50,-0.35) -> 8;
        [-0.65,-0.50) -> 4; < -0.65 -> 0.
    episodes (30% count): <=1 -> 8; 2-3 -> 5; >=4 -> 2.
    spread proxy (method "episode_spread_proxy"): (dd20 - dd30) <= 2 -> 5; else 2.
    Shallower drawdowns and fewer/less-severe episodes = better = more points.
    """
    max_dd = tech.get("max_dd_10yr")
    dd20 = tech.get("dd_episodes_20pct_10yr")
    dd30 = tech.get("dd_episodes_30pct_10yr")

    parts = []
    evaluable = 0

    # -- max drawdown ------------------------------------------------------
    if max_dd is not None:
        evaluable += 1
        if max_dd >= -0.35:
            maxdd_pts = 12
        elif max_dd >= -0.50:
            maxdd_pts = 8
        elif max_dd >= -0.65:
            maxdd_pts = 4
        else:  # < -0.65
            maxdd_pts = 0
        parts.append(f"max_dd_10yr {_fmt(max_dd)} -> {maxdd_pts}/12")
    else:
        maxdd_pts = 0
        parts.append("max_dd_10yr: n/a (+0)")

    # -- 30% episode count -------------------------------------------------
    if dd30 is not None:
        evaluable += 1
        if dd30 <= 1:
            episodes_pts = 8
        elif dd30 <= 3:
            episodes_pts = 5
        else:  # >= 4
            episodes_pts = 2
        parts.append(f"dd_episodes_30pct_10yr {_fmt(dd30)} -> {episodes_pts}/8")
    else:
        episodes_pts = 0
        parts.append("dd_episodes_30pct_10yr: n/a (+0)")

    # -- severity-spread proxy (20% count - 30% count) ---------------------
    if dd20 is not None and dd30 is not None:
        evaluable += 1
        spread = dd20 - dd30
        spread_pts = 5 if spread <= 2 else 2
        parts.append(
            f"episode_spread_proxy (dd20 {_fmt(dd20)} - dd30 {_fmt(dd30)} = "
            f"{_fmt(spread)}) -> {spread_pts}/5")
    else:
        spread_pts = 0
        parts.append("episode_spread_proxy: n/a (+0)")

    total = maxdd_pts + episodes_pts + spread_pts
    return {
        "name": "drawdown_profile",
        "points": _clean(total),
        "max": 25,
        "arithmetic": "; ".join(parts),
        "inputs": {"maxdd_points": maxdd_pts, "episodes_points": episodes_pts,
                   "spread_points": spread_pts, "max_dd_10yr": max_dd,
                   "dd_episodes_20pct_10yr": dd20,
                   "dd_episodes_30pct_10yr": dd30},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 3. Margin of safety (max 30): dist_from_ath (12) + asymmetry (18)
# --------------------------------------------------------------------------- #

def score_margin(tech, ladder, last) -> dict:
    """dist_from_ath_pct (max 12) + ladder asymmetry (max 18).

    dist_from_ath (negative fraction): <= -0.15 -> 12; (-0.15,-0.05] -> 7;
        > -0.05 -> 3; null -> 0 ("n/a").
    asymmetry: d_support = |pct_from_last| of the nearest PROVEN support below
        ``last`` (levels.nearest_support); d_resist = pct_from_last of the nearest
        resistance above (levels.nearest_resistance), or 0.15 blue-sky convention
        when none above. ratio = d_support / d_resist:
            <=0.5 -> 18; (0.5,1.0] -> 12; (1.0,2.0] -> 6; >2.0 -> 2.
        NO proven support below -> 2 ("no proven floor" -- cannot anchor risk).
    Deeper discount and tighter downside relative to upside = better = more points.
    """
    dist = tech.get("dist_from_ath_pct")

    parts = []

    # -- distance from all-time high ---------------------------------------
    if dist is not None:
        if dist <= -0.15:
            dist_pts = 12
        elif dist <= -0.05:
            dist_pts = 7
        else:  # > -0.05
            dist_pts = 3
        parts.append(f"dist_from_ath_pct {_fmt(dist)} -> {dist_pts}/12")
    else:
        dist_pts = 0
        parts.append("dist_from_ath_pct: n/a (+0)")

    # -- ladder asymmetry --------------------------------------------------
    sup = levels.nearest_support(ladder, last, proven_only=True)
    res = levels.nearest_resistance(ladder, last)

    if sup is None or not last:
        asymmetry_pts = 2
        parts.append("no proven floor (no proven support below) -> asymmetry +2")
    else:
        d_support = abs(sup["level"] / last - 1)
        if res is None:
            d_resist = _BLUE_SKY_RESIST
            resist_note = f"blue_sky_convention_15pct (d_resist {_BLUE_SKY_RESIST})"
        else:
            d_resist = res["level"] / last - 1
            resist_note = (f"resistance {res['type']} {_fmt(res['level'])} "
                           f"(d_resist {d_resist*100:.1f}%)")
        # d_resist is always positive here (resistance is above last, or the 15%
        # convention). Ratio compares downside distance to upside distance.
        ratio = d_support / d_resist if d_resist else float("inf")
        if ratio <= 0.5:
            asymmetry_pts = 18
        elif ratio <= 1.0:
            asymmetry_pts = 12
        elif ratio <= 2.0:
            asymmetry_pts = 6
        else:  # > 2.0
            asymmetry_pts = 2
        parts.append(
            f"asymmetry: support {sup['type']} {_fmt(sup['level'])} "
            f"(d_support {d_support*100:.1f}%) / {resist_note} -> ratio "
            f"{_fmt(_clean(ratio))} -> +{asymmetry_pts}")

    total = dist_pts + asymmetry_pts
    return {
        "name": "margin_of_safety",
        "points": min(30, total),
        "max": 30,
        "arithmetic": "; ".join(parts),
        "inputs": {"dist_ath_points": dist_pts,
                   "asymmetry_points": asymmetry_pts,
                   "dist_from_ath_pct": dist,
                   "nearest_support": sup["level"] if sup else None,
                   "nearest_resistance": res["level"] if res else None},
        "evaluable": True,
    }


# --------------------------------------------------------------------------- #
# 4. Liquidity & solvency (max 20): ADV (10) + net-cash ratio (10)
# --------------------------------------------------------------------------- #

def score_liquidity(adv, net, mktcap) -> dict:
    """adv_dollar_3m (max 10) + net-cash ratio (max 10).

    adv: >=500e6 -> 10; [50e6,500e6) -> 7; [10e6,50e6) -> 4; <10e6 -> 1; null -> 0.
    net_ratio = net / mktcap: >0.05 -> 10; [0,0.05] -> 7; [-0.10,0) -> 4;
        < -0.10 -> 1; null (net or mktcap missing) -> 0.
    Deeper liquidity and a stronger balance sheet = better = more points.
    """
    parts = []
    evaluable = 0

    # -- average dollar volume ---------------------------------------------
    if adv is not None:
        evaluable += 1
        if adv >= 500e6:
            adv_pts = 10
        elif adv >= 50e6:
            adv_pts = 7
        elif adv >= 10e6:
            adv_pts = 4
        else:  # < 10e6
            adv_pts = 1
        parts.append(f"adv_dollar_3m {_fmt(_clean(adv))} -> {adv_pts}/10")
    else:
        adv_pts = 0
        parts.append("adv_dollar_3m: n/a (+0)")

    # -- net-cash ratio ----------------------------------------------------
    if net is not None and mktcap not in (None, 0):
        evaluable += 1
        net_ratio = net / mktcap
        if net_ratio > 0.05:
            net_pts = 10
        elif net_ratio >= 0:
            net_pts = 7
        elif net_ratio >= -0.10:
            net_pts = 4
        else:  # < -0.10
            net_pts = 1
        parts.append(
            f"net_ratio (net {_fmt(_clean(net))} / mktcap {_fmt(_clean(mktcap))} "
            f"= {net_ratio*100:.1f}%) -> {net_pts}/10")
    else:
        net_pts = 0
        parts.append("net_ratio: n/a (+0)")

    total = adv_pts + net_pts
    return {
        "name": "liquidity_solvency",
        "points": _clean(total),
        "max": 20,
        "arithmetic": "; ".join(parts),
        "inputs": {"adv_points": adv_pts, "net_ratio_points": net_pts,
                   "adv_dollar_3m": adv, "net": net, "mktcap": mktcap},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# Tables: downside_map + vol_profile
# --------------------------------------------------------------------------- #

def valuation_floor(pe_5yr_median, eps_ntm):
    """A valuation-floor level from pe_5yr_median x eps_ntm_consensus, or None.

    Both inputs must be present. The level is a judgment anchor (where a 5-yr
    median multiple on forward EPS would put the stock), NOT a proven support.
    """
    if pe_5yr_median is None or eps_ntm is None:
        return None
    return {"level": _clean(pe_5yr_median * eps_ntm), "type": "valuation_floor",
            "basis": "valuation", "method": "pe_5yr_median x eps_ntm"}


def build_downside_map(ladder, last, val_floor, stress_pct, top_risk) -> list:
    """Ordered list of downside anchors BELOW ``last``, NEAREST-FIRST.

    Rows sort DESCENDING by level: the first row is the first support price
    falls through (Gate-2 finding: ascending order made "top 5 rows" read as
    the deepest anchors instead of the nearest). Ladder entries below ``last``
    plus the valuation-floor row (if computable) in sorted position; the
    stress-scenario row (if ``stress_pct`` given) appends last, labeled.
    """
    rows = []
    for e in ladder:
        lvl = e.get("level")
        if lvl is None or last is None or lvl >= last:
            continue
        rows.append({"level": _clean(lvl), "type": e.get("type"),
                     "basis": e.get("basis"),
                     "pct_from_last": _clean(e.get("pct_from_last"))})

    if val_floor is not None and last is not None and val_floor["level"] < last:
        pct = _clean(val_floor["level"] / last - 1) if last else None
        rows.append({"level": val_floor["level"], "type": val_floor["type"],
                     "basis": val_floor["basis"], "method": val_floor["method"],
                     "pct_from_last": pct})

    rows.sort(key=lambda r: r["level"], reverse=True)

    if stress_pct is not None and last is not None:
        stress_level = _clean(last * (1 + stress_pct))
        rows.append({"level": stress_level, "type": "stress_scenario",
                     "basis": "judgment", "risk": top_risk,
                     "pct_from_last": _clean(stress_pct)})

    return rows


def build_vol_profile(tech, bench) -> dict:
    """Verbatim volatility/drawdown context block (correlation is context,
    unscored)."""
    return {
        "rv20_ann": tech.get("rv20_ann"),
        "rv30_ann": tech.get("rv30_ann"),
        "rv30_vs_10yr_pctile": tech.get("rv30_vs_10yr_pctile"),
        "beta": bench.get("beta"),
        "corr": bench.get("corr"),
        "beta_n_days": bench.get("beta_n_days"),
        "max_dd_10yr": tech.get("max_dd_10yr"),
        "dd_episodes_20pct_10yr": tech.get("dd_episodes_20pct_10yr"),
        "dd_episodes_30pct_10yr": tech.get("dd_episodes_30pct_10yr"),
    }


# --------------------------------------------------------------------------- #
# Composite scoring + renormalization (identical pattern to score_technical)
# --------------------------------------------------------------------------- #

def score(tech, beta, ladder, last, adv, net, mktcap) -> dict:
    """Assemble the four subscores and the (possibly renormalized) 0-100 score.

    A dimension whose ``evaluable`` is False (all its scored inputs null) is
    EXCLUDED from the max total and the score is rescaled to 0-100 over the
    remaining max, with ``renormalized: true`` recorded.
    """
    subs = [
        score_volatility(tech, beta),
        score_drawdown(tech),
        score_margin(tech, ladder, last),
        score_liquidity(adv, net, mktcap),
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

    # Strip the internal "evaluable" flag from the published subscores. A
    # dimension excluded from renormalization keeps its row (the arithmetic trail
    # stays visible) but its ``max`` is zeroed so the published subscores' max
    # total equals the renormalization denominator.
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
# CLI (mirrors score_technical.py; additionally requires module_technical.json)
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def build_module(snapshot, ladder, stress_pct, top_risk) -> dict:
    """Build the full module_risk.json document from parsed inputs + ladder."""
    price = snapshot.get("price", {}) if isinstance(snapshot, dict) else {}
    tech = snapshot.get("technicals", {}) if isinstance(snapshot, dict) else {}
    bench = snapshot.get("benchmark", {}) if isinstance(snapshot, dict) else {}
    fund = snapshot.get("fundamentals", {}) if isinstance(snapshot, dict) else {}
    val = snapshot.get("valuation", {}) if isinstance(snapshot, dict) else {}
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}

    last = price.get("last")
    beta = bench.get("beta")
    adv = price.get("adv_dollar_3m")
    mktcap = price.get("mktcap_computed")
    net_cash = fund.get("net_cash_defined") or {}
    net = net_cash.get("net") if isinstance(net_cash, dict) else None

    scored = score(tech, beta, ladder, last, adv, net, mktcap)

    vf = valuation_floor(val.get("pe_5yr_median"),
                         fund.get("eps_ntm_consensus"))
    downside_map = build_downside_map(ladder, last, vf, stress_pct, top_risk)
    vol_profile = build_vol_profile(tech, bench)

    doc = {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "ticker": meta.get("ticker"),
        "as_of": build_snapshot._as_of_date(meta.get("as_of_utc")),
        "score": scored["score"],
        "subscores": scored["subscores"],
        "tables": {
            "downside_map": downside_map,
            "vol_profile": vol_profile,
        },
        "flags": {
            "stress_pct": stress_pct,
            "top_risk": top_risk,
        },
        "renormalized": scored["renormalized"],
        "signal": None,
    }
    if scored["renormalization_note"]:
        doc["renormalization_note"] = scored["renormalization_note"]
    return doc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score the risk-analytics dimension for a snapshot bundle "
                    "(rubric v%s). Higher score = better risk-reward conditions."
                    % RUBRIC_VERSION)
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--stress-pct", type=float, default=None,
                        help="stress scenario as a signed fraction (e.g. -0.30); "
                             "requires --top-risk")
    parser.add_argument("--top-risk", default=None,
                        help="single named top risk for the stress row "
                             "(required whenever --stress-pct is given)")
    parser.add_argument("--out", default=None,
                        help="output path (default <bundle>/module_risk.json)")
    args = parser.parse_args(argv)

    if args.stress_pct is not None and not args.top_risk:
        print("ERROR: --top-risk is required when --stress-pct is given",
              file=sys.stderr)
        return 2

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

    # Hard dependency: the S/R ladder is minted by technical-analysis. If its
    # module is missing, the asymmetry component has no ladder -- fail loudly.
    module_tech_path = os.path.join(args.bundle, "module_technical.json")
    if not os.path.exists(module_tech_path):
        print("ERROR: run technical-analysis first (module_technical.json missing)",
              file=sys.stderr)
        return 2
    try:
        with open(module_tech_path) as fh:
            module_tech = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read {module_tech_path}: {exc}", file=sys.stderr)
        return 2
    ladder = module_tech.get("ladder") or []

    doc = build_module(snapshot, ladder, args.stress_pct, args.top_risk)

    out = args.out or os.path.join(args.bundle, "module_risk.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
