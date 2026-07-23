"""Risk-analytics evidence module for the trading-desk plugin.

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

Scoring is over SIX dimensions (max 100 total) as of risk-v1.1.0 (PROVISIONAL --
see the "risk-v1.1.0" note below). Rubric v1.0.0 scored only the first four (at
maxes 25/25/30/20); v1.1.0 trims each of the four by a symmetric -5 to free 20 pts
for the two event/tail factors that make the near-term binary REAL evidence:
    1. Volatility state    (20)  -- rv30 vs 10-yr percentile (16) + benchmark beta (4)
                                    [v1.0.0: 25 = pctile 20 + beta 5]
    2. Drawdown profile    (20)  -- max 10-yr drawdown (10) + 30% episode count (6)
                                    + a 20%-vs-30% episode-spread severity proxy (4)
                                    [v1.0.0: 25 = maxdd 12 + episodes 8 + spread 5]
    3. Margin of safety    (25)  -- distance below the all-time high (10)
                                    + ladder asymmetry (15)
                                    [v1.0.0: 30 = dist 12 + asymmetry 18]
    4. Liquidity & solvency(15)  -- 3-month average dollar volume (8) + net-cash ratio (7)
                                    [v1.0.0: 20 = ADV 10 + net 10]
    5. Event risk          (12)  -- days-to-event x implied-move-vs-own-history (NEW v1.1.0)
    6. Tail risk           (8)   -- overnight-gap kurtosis + p95 magnitude (NEW v1.1.0)

RISK-v1.1.0 IS PROVISIONAL. The event/tail weights + bands are a versioned,
falsifiable DEFAULT (user philosophy "A"): shipped loudly disclosed and UNRATIFIED
pending the B9 calibration set (5-10 anchored names). The re-weight band SHAPES
(thresholds) of the four pre-existing factors are IDENTICAL to v1.0.0 -- only the
point ceilings scale down proportionally, so the ORDERING a name earns within each
factor is unchanged; only its contribution to the composite shifts. A pre-registered
falsifier (recorded in skills/risk-analytics/SKILL.md + here) governs the re-set.

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

DOWNSIDE FLOOR MODE (sector-scales batch, spec A2 -- "dcf_bear downside floor"):
the valuation-floor row in the downside map has TWO modes, disclosed at the module
top level as ``downside_floor_mode`` ("dcf_bear" | "pe_median"):
  - SNAPSHOT mode (``--anchors`` absent, the default / FSI-absent floor): the floor
    is the v1.0 pe_5yr_median x eps_ntm level, with the suspect-flag machinery
    intact (approx_current_eps breakdown detection). Byte-identical to prior runs.
  - ANCHORED mode (``--anchors <valuation_anchors.json>`` present + valid): the
    floor becomes the coverage-derived ``dcf_bear`` level (labeled
    ``basis: "dcf_bear (coverage anchors)"``), REPLACING the pe-median floor
    entirely. An anchored floor is a validated fundamentals-derived level, so the
    suspect machinery does NOT apply -- there is nothing to "suspect" about a DCF
    bear case a covered model produced. The anchors file is the SAME
    valuation_anchors.json score_fundamental consumes; malformed anchors exit 2.
Nearest-first (descending) ordering is unchanged -- the dcf_bear floor simply
interleaves among the ladder / stress levels by its own level. ``validate_anchors``
here is a small LOCAL copy of score_fundamental's validator (same required keys +
positivity): the floor only reads ``dcf_bear``, but validating the full anchor set
keeps the two consumers' exit-2 contract identical without importing across modules
(the coupling is not worth it for one shared numeric key).

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
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation (``python3 scripts/score_risk.py``): ensure the repo
# root is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import build_snapshot, confidence, levels
from scripts._artifact import emit_json

RUBRIC_VERSION = "1.1.0"
SKILL_NAME = "risk-analytics"

# risk-v1.1.0 PROVISIONAL disclosure. Stamped into the module note so every brief
# footer + the report methodology page carry the unratified flag verbatim. Promotes
# out of PROVISIONAL only when the B9 calibration set ratifies the weights/bands.
_PROVISIONAL_NOTE = (
    "risk-v1.1.0 PROVISIONAL — event/tail weights unratified pending B9 "
    "calibration; falsifier pre-registered")

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
    "price.mktcap",
    # Confidence-gating inputs (short-history bug): the beta component is gated
    # on the number of return-days behind the estimate, and the rv30 regime
    # percentile on the number of ohlcv rows behind the percentile. They do not
    # carry points of their own -- they can only zero a component and disclose
    # why -- but they ARE scored inputs (they change the score), so single-mapping
    # governance must see them here and nowhere else.
    "benchmark.beta_n_days",
    "technicals.ohlcv_rows",
    # risk-v1.1.0 SCORED event/tail fields (Part B). PROMOTED from CONTEXT_FIELDS:
    # these carry points now. days_to_event x implied_move_vs_own_history_pctile
    # feed event_risk; overnight_gap (its excess_kurtosis + p95_abs) feeds
    # tail_risk. They are single-mapped here and nowhere else (checked by the
    # single-mapping governance test -- no other module scores these paths).
    "events.days_to_event",
    "events.implied_move_vs_own_history_pctile",
    "technicals.overnight_gap",
}

# Snapshot fields consumed by this module as CONTEXT-ONLY (unscored, carry zero
# points, excluded from INPUT_FIELDS so the single-mapping governance test is not
# confused into treating them as scored). These are surfaced verbatim from the
# snapshot into tables.event_context and tables.tail_context for disclosure.
#
# A2 originally listed FIVE fields here (all unscored, scoring gated on Part B).
# risk-v1.1.0 (Part B) PROMOTED three of them to SCORED (moved to INPUT_FIELDS):
# days_to_event + implied_move_vs_own_history_pctile (-> event_risk) and
# overnight_gap (-> tail_risk). The two that remain are still pure context: the
# raw implied_move fraction (implied_move_vs_own_history_pctile is what scores)
# and the earnings_move_history LIST (surfaced for the reader; the percentile
# distilled from it is what scores).
CONTEXT_FIELDS = {
    "events.implied_move",
    "events.earnings_move_history",
}

# Minimum history for a component to be trusted (see the module docstring / the
# real-world bug that motivated this: a beta of 3.61 from 100 unadjusted days).
_MIN_BETA_N_DAYS = 150     # ~half a trading year of return-days for a stable beta
_MIN_OHLCV_ROWS = 500      # ~2 trading years for a meaningful regime percentile


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
# 1. Volatility state (max 20): rv percentile (16) + beta (4)
# --------------------------------------------------------------------------- #

def score_volatility(tech, beta, beta_n_days=None, ohlcv_rows=None) -> dict:
    """rv30_vs_10yr_pctile band (max 16) + benchmark beta band (max 4).

    RE-WEIGHT (risk-v1.1.0): factor max 25->20 (pctile 20->16, beta 5->4) to free
    room for event_risk/tail_risk. Band SHAPES (thresholds) are UNCHANGED from
    v1.0.0 -- only the point ceilings scale (pctile ~x0.8, beta ~x0.8):
      pctile: <30 -> 16; 30-60 -> 11; 60-80 -> 6; >=80 -> 2; null -> 0 ("n/a").
      beta:   <1.2 -> +4; 1.2-1.8 -> +2; >1.8 -> +0; null -> 0.
    (v1.0.0 was pctile 20/14/8/3 + beta 5/3/0.)
    Lower volatility and lower beta = better conditions = more points.

    Short-history confidence gating (REAL-WORLD BUG: a beta of 3.61 computed from
    100 unadjusted return-days was fed the score as a plain number). A component
    is only trusted with enough history behind it:
      - beta needs ``beta_n_days`` >= 150 return-days; below -> 0 with an explicit
        "beta n/a (only {n} return-days; needs >=150 ...)" disclosure.
      - the rv30 regime percentile needs ``ohlcv_rows`` >= 500 (~2yr); below -> 0
        with "rv30 percentile n/a ({rows} rows; needs >=500 (~2yr) ...)".
    Gating trips ONLY when the gating count is PRESENT and below threshold; when
    it is None (not supplied) the gate does not fire, so the pure-branch tests
    that never pass a count keep their existing behavior. A gated component is
    NOT counted toward ``evaluable`` (an untrustworthy input is no input).
    """
    pctile = tech.get("rv30_vs_10yr_pctile")

    parts = []
    evaluable = 0

    pctile_gated = ohlcv_rows is not None and ohlcv_rows < _MIN_OHLCV_ROWS
    if pctile is not None and pctile_gated:
        pctile_pts = 0
        parts.append(f"rv30 percentile n/a ({_fmt(ohlcv_rows)} rows; needs "
                     f">={_MIN_OHLCV_ROWS} (~2yr) for a regime percentile)")
    elif pctile is not None:
        evaluable += 1
        if pctile < 30:
            pctile_pts = 16
        elif pctile < 60:
            pctile_pts = 11
        elif pctile < 80:
            pctile_pts = 6
        else:  # >= 80
            pctile_pts = 2
        parts.append(f"rv30_vs_10yr_pctile {_fmt(pctile)} -> {pctile_pts}/16")
    else:
        pctile_pts = 0
        parts.append("rv30_vs_10yr_pctile: n/a (+0)")

    beta_gated = beta_n_days is not None and beta_n_days < _MIN_BETA_N_DAYS
    if beta is not None and beta_gated:
        beta_pts = 0
        parts.append(f"beta n/a (only {_fmt(beta_n_days)} return-days; needs "
                     f">={_MIN_BETA_N_DAYS} for a stable estimate)")
    elif beta is not None:
        evaluable += 1
        if beta < 1.2:
            beta_pts = 4
        elif beta <= 1.8:
            beta_pts = 2
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
        "max": 20,
        "arithmetic": "; ".join(parts),
        "inputs": {"pctile_points": pctile_pts, "beta_points": beta_pts,
                   "rv30_vs_10yr_pctile": pctile, "beta": beta,
                   "beta_n_days": beta_n_days, "ohlcv_rows": ohlcv_rows},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 2. Drawdown profile (max 20): max_dd (10) + episodes (6) + spread proxy (4)
# --------------------------------------------------------------------------- #

def score_drawdown(tech) -> dict:
    """max_dd_10yr (max 10) + dd_episodes_30pct_10yr (max 6) + severity-spread
    proxy (max 4).

    RE-WEIGHT (risk-v1.1.0): factor max 25->20, split as maxdd 12->10, episodes
    8->6, spread 5->4 (10+6+4 = 20). Band SHAPES (thresholds) are UNCHANGED from
    v1.0.0 -- only the ceilings scale:
      max_dd (negative fraction): >= -0.35 -> 10; [-0.50,-0.35) -> 7;
          [-0.65,-0.50) -> 3; < -0.65 -> 0.  (v1.0.0: 12/8/4/0)
      episodes (30% count): <=1 -> 6; 2-3 -> 4; >=4 -> 2.  (v1.0.0: 8/5/2)
      spread proxy ("episode_spread_proxy"): (dd20-dd30) <= 2 -> 4; else 2. (v1.0.0: 5/2)
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
            maxdd_pts = 10
        elif max_dd >= -0.50:
            maxdd_pts = 7
        elif max_dd >= -0.65:
            maxdd_pts = 3
        else:  # < -0.65
            maxdd_pts = 0
        parts.append(f"max_dd_10yr {_fmt(max_dd)} -> {maxdd_pts}/10")
    else:
        maxdd_pts = 0
        parts.append("max_dd_10yr: n/a (+0)")

    # -- 30% episode count -------------------------------------------------
    if dd30 is not None:
        evaluable += 1
        if dd30 <= 1:
            episodes_pts = 6
        elif dd30 <= 3:
            episodes_pts = 4
        else:  # >= 4
            episodes_pts = 2
        parts.append(f"dd_episodes_30pct_10yr {_fmt(dd30)} -> {episodes_pts}/6")
    else:
        episodes_pts = 0
        parts.append("dd_episodes_30pct_10yr: n/a (+0)")

    # -- severity-spread proxy (20% count - 30% count) ---------------------
    if dd20 is not None and dd30 is not None:
        evaluable += 1
        spread = dd20 - dd30
        spread_pts = 4 if spread <= 2 else 2
        parts.append(
            f"episode_spread_proxy (dd20 {_fmt(dd20)} - dd30 {_fmt(dd30)} = "
            f"{_fmt(spread)}) -> {spread_pts}/4")
    else:
        spread_pts = 0
        parts.append("episode_spread_proxy: n/a (+0)")

    total = maxdd_pts + episodes_pts + spread_pts
    return {
        "name": "drawdown_profile",
        "points": _clean(total),
        "max": 20,
        "arithmetic": "; ".join(parts),
        "inputs": {"maxdd_points": maxdd_pts, "episodes_points": episodes_pts,
                   "spread_points": spread_pts, "max_dd_10yr": max_dd,
                   "dd_episodes_20pct_10yr": dd20,
                   "dd_episodes_30pct_10yr": dd30},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 3. Margin of safety (max 25): dist_from_ath (10) + asymmetry (15)
# --------------------------------------------------------------------------- #

def score_margin(tech, ladder, last) -> dict:
    """dist_from_ath_pct (max 10) + ladder asymmetry (max 15).

    RE-WEIGHT (risk-v1.1.0): factor max 30->25, split as dist 12->10, asymmetry
    18->15 (10+15 = 25). Band SHAPES (thresholds) are UNCHANGED from v1.0.0 --
    only the ceilings scale:
      dist_from_ath (negative fraction): <= -0.15 -> 10; (-0.15,-0.05] -> 6;
          > -0.05 -> 3; null -> 0 ("n/a").  (v1.0.0: 12/7/3)
      asymmetry: d_support = |pct_from_last| of the nearest PROVEN support below
        ``last`` (levels.nearest_support); d_resist = pct_from_last of the nearest
        resistance above (levels.nearest_resistance), or 0.15 blue-sky convention
        when none above. ratio = d_support / d_resist:
            <=0.5 -> 15; (0.5,1.0] -> 10; (1.0,2.0] -> 5; >2.0 -> 2.  (v1.0.0: 18/12/6/2)
        NO proven support below -> 2 ("no proven floor" -- cannot anchor risk).
    Deeper discount and tighter downside relative to upside = better = more points.
    """
    dist = tech.get("dist_from_ath_pct")

    parts = []

    # -- distance from all-time high ---------------------------------------
    if dist is not None:
        if dist <= -0.15:
            dist_pts = 10
        elif dist <= -0.05:
            dist_pts = 6
        else:  # > -0.05
            dist_pts = 3
        parts.append(f"dist_from_ath_pct {_fmt(dist)} -> {dist_pts}/10")
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
            asymmetry_pts = 15
        elif ratio <= 1.0:
            asymmetry_pts = 10
        elif ratio <= 2.0:
            asymmetry_pts = 5
        else:  # > 2.0
            asymmetry_pts = 2
        parts.append(
            f"asymmetry: support {sup['type']} {_fmt(sup['level'])} "
            f"(d_support {d_support*100:.1f}%) / {resist_note} -> ratio "
            f"{_fmt(_clean(ratio))} -> +{asymmetry_pts}")

    total = dist_pts + asymmetry_pts
    return {
        "name": "margin_of_safety",
        "points": min(25, total),
        "max": 25,
        "arithmetic": "; ".join(parts),
        "inputs": {"dist_ath_points": dist_pts,
                   "asymmetry_points": asymmetry_pts,
                   "dist_from_ath_pct": dist,
                   "nearest_support": sup["level"] if sup else None,
                   "nearest_resistance": res["level"] if res else None},
        "evaluable": True,
    }


# --------------------------------------------------------------------------- #
# 4. Liquidity & solvency (max 15): ADV (8) + net-cash ratio (7)
# --------------------------------------------------------------------------- #

def score_liquidity(adv, net, mktcap) -> dict:
    """adv_dollar_3m (max 8) + net-cash ratio (max 7).

    RE-WEIGHT (risk-v1.1.0): factor max 20->15, split as ADV 10->8, net 10->7
    (8+7 = 15). Band SHAPES (thresholds) are UNCHANGED from v1.0.0 -- only the
    ceilings scale:
      adv: >=500e6 -> 8; [50e6,500e6) -> 6; [10e6,50e6) -> 3; <10e6 -> 1; null -> 0.
        (v1.0.0: 10/7/4/1)
      net_ratio = net / mktcap: >0.05 -> 7; [0,0.05] -> 5; [-0.10,0) -> 3;
        < -0.10 -> 1; null (net or mktcap missing) -> 0.  (v1.0.0: 10/7/4/1)
    Deeper liquidity and a stronger balance sheet = better = more points.
    """
    parts = []
    evaluable = 0

    # -- average dollar volume ---------------------------------------------
    if adv is not None:
        evaluable += 1
        if adv >= 500e6:
            adv_pts = 8
        elif adv >= 50e6:
            adv_pts = 6
        elif adv >= 10e6:
            adv_pts = 3
        else:  # < 10e6
            adv_pts = 1
        parts.append(f"adv_dollar_3m {_fmt(_clean(adv))} -> {adv_pts}/8")
    else:
        adv_pts = 0
        parts.append("adv_dollar_3m: n/a (+0)")

    # -- net-cash ratio ----------------------------------------------------
    if net is not None and mktcap not in (None, 0):
        evaluable += 1
        net_ratio = net / mktcap
        if net_ratio > 0.05:
            net_pts = 7
        elif net_ratio >= 0:
            net_pts = 5
        elif net_ratio >= -0.10:
            net_pts = 3
        else:  # < -0.10
            net_pts = 1
        parts.append(
            f"net_ratio (net {_fmt(_clean(net))} / mktcap {_fmt(_clean(mktcap))} "
            f"= {net_ratio*100:.1f}%) -> {net_pts}/7")
    else:
        net_pts = 0
        parts.append("net_ratio: n/a (+0)")

    total = adv_pts + net_pts
    return {
        "name": "liquidity_solvency",
        "points": _clean(total),
        "max": 15,
        "arithmetic": "; ".join(parts),
        "inputs": {"adv_points": adv_pts, "net_ratio_points": net_pts,
                   "adv_dollar_3m": adv, "net": net, "mktcap": mktcap},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 5. Event risk (max 12): days-to-event x implied-move-vs-own-history (v1.1.0)
# --------------------------------------------------------------------------- #

def score_event_risk(events) -> dict:
    """days_to_event (d) x implied_move_vs_own_history_pctile (p) -> event_risk (max 12).

    NEW in risk-v1.1.0 (PROVISIONAL). Higher = better (calmer) conditions: a name
    with NO near-term binary earns the full 12; a name days out from earnings the
    market is pricing an above-its-own-history move earns as little as 2. The two
    inputs are Part-A deterministic snapshot fields (build_snapshot):
      d = events.days_to_event (int days; null when no dated event)
      p = events.implied_move_vs_own_history_pctile (0-100; null with no chain/history)

    Bands (from the spec, verbatim):
      - d null OR d > 30                    -> 12  (no near-term event risk)
      - d <= 30, p null (proximity only)    -> d<=7: 6 ; d<=14: 8 ; else(<=30): 10
      - d <= 30, p >= 90                    -> d<=7: 2 ; d<=14: 3 ; else: 5
      - d <= 30, 60 <= p < 90               -> d<=7: 4 ; d<=14: 6 ; else: 8
      - d <= 30, p < 60                     -> d<=7: 6 ; d<=14: 8 ; else: 10

    ALWAYS evaluable: even with no dated event (d null) the factor makes a
    deterministic claim ("no near-term event risk -> 12"), so it carries points
    and stays in the renormalization denominator. (Contrast tail_risk, which is
    NOT evaluable when its kurtosis input is null -- an unmeasurable tail is no
    input.)
    """
    ev = events if isinstance(events, dict) else {}
    d = ev.get("days_to_event")
    p = ev.get("implied_move_vs_own_history_pctile")

    # No dated event, or the event is beyond the 30-day near-term window: no
    # near-term event risk -> full points.
    if d is None or d > 30:
        pts = 12
        arithmetic = (f"days_to_event {_fmt(d)} (null or >30) -> no near-term "
                      f"event risk -> 12/12")
    else:
        # A near-term event within 30d. Proximity buckets are the same in every
        # implied-move branch; only the point ceiling per bucket changes with p.
        if d <= 7:
            bucket = "<=7d"
        elif d <= 14:
            bucket = "<=14d"
        else:  # <= 30
            bucket = "<=30d"

        if p is None:
            # Proximity only (no chain/history to price the move against).
            table = {"<=7d": 6, "<=14d": 8, "<=30d": 10}
            pband = "proximity only (implied_move_vs_own_history_pctile n/a)"
        elif p >= 90:
            # Market pricing a move at/above the 90th pctile of this name's own
            # history: the binary is loud -> steepest discount.
            table = {"<=7d": 2, "<=14d": 3, "<=30d": 5}
            pband = "implied_pctile >= 90 (loud binary)"
        elif p >= 60:
            table = {"<=7d": 4, "<=14d": 6, "<=30d": 8}
            pband = "60 <= implied_pctile < 90"
        else:  # p < 60
            table = {"<=7d": 6, "<=14d": 8, "<=30d": 10}
            pband = "implied_pctile < 60 (below-average priced move)"

        pts = table[bucket]
        arithmetic = (f"event {_fmt(d)}d out ({bucket}), {pband} "
                      f"[p={_fmt(p)}] -> {pts}/12")

    return {
        "name": "event_risk",
        "points": _clean(pts),
        "max": 12,
        "arithmetic": arithmetic,
        "inputs": {"days_to_event": d,
                   "implied_move_vs_own_history_pctile": p,
                   "event_points": pts},
        "evaluable": True,
    }


# --------------------------------------------------------------------------- #
# 6. Tail risk (max 8): overnight-gap kurtosis + p95 magnitude (v1.1.0)
# --------------------------------------------------------------------------- #

def score_tail_risk(overnight_gap) -> dict:
    """overnight-gap excess_kurtosis + p95_abs -> tail_risk (max 8).

    NEW in risk-v1.1.0 (PROVISIONAL). Higher = better (calmer) conditions. Both
    inputs are Part-A deterministic snapshot fields on technicals.overnight_gap:
      k = excess_kurtosis (4th standardized moment - 3; null when n<4 or degenerate)
      p95 = p95_abs (95th-pctile ABSOLUTE overnight gap, a positive fraction)

    Bands (from the spec, verbatim):
      - excess_kurtosis null (n < 4) -> NOT evaluable (renormalize; do NOT zero --
        an unmeasurable tail is no input, exactly like a null dimension elsewhere).
      - k < 8  AND p95 < 0.04 -> 8  (calm tails)
      - k < 20 AND p95 < 0.06 -> 5  (moderate)
      - else                  -> 2  (violent tails)

    The overnight_gap block may be absent entirely (None) on a degraded/web run
    with no daily series -- that is also NOT evaluable (renormalize), same as a
    null kurtosis.
    """
    og = overnight_gap if isinstance(overnight_gap, dict) else {}
    k = og.get("excess_kurtosis")          # full-history -- DIAGNOSTIC only (not scored)
    p95 = og.get("p95_abs_3y")             # trailing-3y scoring inputs (O1 2026-07-23):
    tmean = og.get("tail_mean_95_3y")      # recent-regime MAGNITUDE, not shape

    # NOT evaluable when the trailing-3y tail cannot be measured: no overnight_gap
    # block, or the 3y window had < 4 gaps (p95_abs_3y/tail_mean_95_3y null).
    # Renormalize -- never zero. (A pre-O1 snapshot without the 3y fields also lands
    # here and renormalizes, until it is rebuilt.)
    if not isinstance(overnight_gap, dict) or p95 is None or tmean is None:
        why = ("no overnight_gap block" if not isinstance(overnight_gap, dict)
               else "3y tail window n/a (n<4 or pre-O1 snapshot)")
        return {
            "name": "tail_risk",
            "points": 0,
            "max": 8,
            "arithmetic": f"tail_risk: n/a ({why}) -> renormalized (not zeroed)",
            "inputs": {"p95_abs_3y": p95, "tail_mean_95_3y": tmean,
                       "excess_kurtosis_fullhist": k, "tail_points": 0},
            "evaluable": False,
        }

    # Bands score the RECENT (3y) gap regime by MAGNITUDE -- both p95 and the worst-5%
    # mean must clear the cut (AND). Calibrated on the 2026-07-23 10-name set so the
    # calm tier is reachable: KO (calmest staple) reads calm, and a lone decades-old
    # crash gap no longer forces "violent" via full-history kurtosis.
    if p95 < 0.015 and tmean < 0.03:
        pts = 8
        band = "calm (3y p95 < 1.5% & worst-5% mean < 3%)"
    elif p95 < 0.045 and tmean < 0.07:
        pts = 5
        band = "moderate (3y p95 < 4.5% & worst-5% mean < 7%)"
    else:
        pts = 2
        band = "violent"

    return {
        "name": "tail_risk",
        "points": _clean(pts),
        "max": 8,
        "arithmetic": (f"3y p95_abs {_fmt(_clean(p95))}, worst-5% mean "
                       f"{_fmt(_clean(tmean))} -> {band} -> {pts}/8 "
                       f"(full-hist kurtosis {_fmt(_clean(k))} diagnostic)"),
        "inputs": {"p95_abs_3y": p95, "tail_mean_95_3y": tmean,
                   "excess_kurtosis_fullhist": k, "tail_points": pts},
        "evaluable": True,
    }


# --------------------------------------------------------------------------- #
# Tables: downside_map + vol_profile
# --------------------------------------------------------------------------- #

# Suspect-floor detection thresholds (see valuation_floor). The 0.25 floor/last
# ratio catches a collapsed floor directly; the [0.2, 5.0] pe_fwd/pe_5yr_median
# band MIRRORS score_fundamental.score_valuation's sanity band so the SCORING and
# DISPLAY paths agree on when the approx_current_eps method has broken down.
_SUSPECT_FLOOR_RATIO = 0.25          # floor/last below this => suspect
_SUSPECT_PE_BAND = (0.2, 5.0)        # pe_fwd/pe_5yr_median outside this => suspect
_SUSPECT_REASON = "approx_current_eps method breakdown"

# Anchored-floor basis label (spec A2). Distinct, self-describing string so a
# DISPLAY consumer can tell an anchored floor from a pe-median floor at a glance.
_DCF_BEAR_BASIS = "dcf_bear (coverage anchors)"

# Required numeric anchor keys for a valuation_anchors.json (all must be present +
# positive). A LOCAL copy of score_fundamental._ANCHOR_REQUIRED / validate_anchors:
# the floor only reads dcf_bear, but validating the full set keeps the two
# consumers' exit-2 contract identical without importing across modules.
_ANCHOR_REQUIRED = ("dcf_base", "dcf_bear", "dcf_bull", "comps_low", "comps_high")


def validate_anchors(anchors) -> list:
    """Return a list of named issues for a valuation_anchors.json dict ([] valid).

    Mirrors score_fundamental.validate_anchors EXACTLY (a local copy, not an
    import -- one shared numeric key does not justify cross-module coupling):
    requires dcf_base/dcf_bear/dcf_bull/comps_low/comps_high present + positive;
    current_pb (optional) must be positive when present. Every problem is reported
    so a malformed anchors file names all its issues at once.
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


def valuation_floor(pe_5yr_median, eps_ntm, last=None, pe_fwd=None, anchors=None):
    """A valuation-floor level for the downside map, or None.

    TWO MODES (spec A2):
      - ANCHORED (``anchors`` is a validated valuation_anchors dict): the floor is
        the coverage-derived ``dcf_bear`` level, labeled
        ``basis: "dcf_bear (coverage anchors)"`` / ``method: "dcf_bear"``, REPLACING
        the pe-median path entirely. The pe_5yr_median / eps_ntm / pe_fwd inputs are
        IGNORED. An anchored floor is a validated fundamentals-derived level, so the
        suspect machinery below does NOT run -- it is always trusted.
      - SNAPSHOT (``anchors`` is None, the default): the v1.0 pe_5yr_median x
        eps_ntm level with the suspect-flag machinery intact (byte-identical to
        prior runs). Both pe inputs must be present or the floor is None.

    In snapshot mode the level is a judgment anchor (where a 5-yr median multiple on
    forward EPS would put the stock), NOT a proven support.

    SUSPECT SUPPRESSION (fix 3, snapshot mode only): the snapshot builds
    ``pe_5yr_median`` with the
    "approx_current_eps" method, which back-projects TODAY's EPS across the 5-yr
    price history. For a name whose EPS regime changed (real MU: the median
    collapses to 1.82), that baseline is garbage and the floor lands absurdly low
    (~$134 on a ~$850 stock), yet the DISPLAY paths (football-field anchors,
    downside ladder) were still drawing it. score_fundamental already GATES the
    SCORING side on a sanity band; here we mirror that on the DISPLAY side. Rather
    than DROP the row (which would break downside-map continuity), we RETURN it
    flagged ``suspect: true`` so consumers can gray it / omit it from anchors while
    the row still exists for the map.

    The rule (documented, kept simple): the floor is SUSPECT when EITHER
      (a) a reference ``last`` is given and floor/last < 0.25 (the floor has
          collapsed to under a quarter of the current price -- an impossible
          "value" anchor), OR
      (b) both ``pe_fwd`` and ``pe_5yr_median`` are given (>0) and their ratio
          pe_fwd/pe_5yr_median falls OUTSIDE [0.2, 5.0] -- the SAME band
          score_fundamental uses to declare the approx method broken.
    A non-suspect floor is returned exactly as before (``suspect`` absent).
    """
    # ANCHORED mode: the coverage dcf_bear REPLACES the pe-median floor entirely,
    # and is always trusted (no suspect machinery). The caller validated the
    # anchors dict already (CLI exit 2 on malformed), so dcf_bear is a positive
    # number here.
    if isinstance(anchors, dict):
        return {"level": _clean(anchors["dcf_bear"]),
                "type": "valuation_floor",
                "basis": _DCF_BEAR_BASIS,
                "method": "dcf_bear"}

    if pe_5yr_median is None or eps_ntm is None:
        return None
    level = _clean(pe_5yr_median * eps_ntm)
    row = {"level": level, "type": "valuation_floor",
           "basis": "valuation", "method": "pe_5yr_median x eps_ntm"}

    suspect = False
    if last not in (None, 0) and level is not None:
        if level / last < _SUSPECT_FLOOR_RATIO:
            suspect = True
    if (pe_fwd is not None and pe_fwd > 0
            and pe_5yr_median is not None and pe_5yr_median > 0):
        ratio = pe_fwd / pe_5yr_median
        if ratio < _SUSPECT_PE_BAND[0] or ratio > _SUSPECT_PE_BAND[1]:
            suspect = True
    if suspect:
        row["suspect"] = True
        row["suspect_reason"] = _SUSPECT_REASON
    return row


# A3: the long-horizon anchor basis label, applied in the downside map when the
# floor is a dcf_bear (anchored mode) or a suspect snapshot floor far below the
# nearest proven swing support. PRESENTATION ONLY -- the numeric level is
# unchanged. The relabeled basis clearly segregates long-horizon anchors from
# actionable swing levels in the display (spec A3).
_LONG_HORIZON_ANCHOR_BASIS = "long-horizon anchor (not a swing level)"


def build_downside_map(ladder, last, val_floor, stress_pct, top_risk) -> list:
    """Ordered list of downside anchors BELOW ``last``, NEAREST-FIRST.

    Rows sort DESCENDING by level: the first row is the first support price
    falls through (Gate-2 finding: ascending order made "top 5 rows" read as
    the deepest anchors instead of the nearest). Ladder entries below ``last``
    plus the valuation-floor row (if computable) in sorted position; the
    stress-scenario row (if ``stress_pct`` given) appends last, labeled.

    A3 (valuation_floor relabel): a dcf_bear (anchored) floor OR a suspect
    snapshot floor are long-horizon anchors -- not actionable swing levels.
    Their ``basis`` is relabeled in this map so a reader cannot mistake them
    for short-term structure. The numeric level is unchanged.
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
        # A3: determine whether this floor should carry the long-horizon-anchor
        # relabel. Two triggers (spec A3): (a) anchored dcf_bear mode, or (b)
        # suspect snapshot floor (approx_current_eps breakdown) which already
        # signals the floor is far below any proven swing level.
        is_long_horizon = (val_floor.get("method") == "dcf_bear"
                           or val_floor.get("suspect"))
        display_basis = (_LONG_HORIZON_ANCHOR_BASIS if is_long_horizon
                         else val_floor["basis"])
        vf_row = {"level": val_floor["level"], "type": val_floor["type"],
                  "basis": display_basis, "method": val_floor["method"],
                  "pct_from_last": pct}
        # Carry the suspect flag so DISPLAY consumers can gray/omit the row while
        # keeping it in the map for continuity (fix 3).
        if val_floor.get("suspect"):
            vf_row["suspect"] = True
            vf_row["suspect_reason"] = val_floor.get("suspect_reason")
        rows.append(vf_row)

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

def score(tech, beta, ladder, last, adv, net, mktcap,
          beta_n_days=None, ohlcv_rows=None,
          events=None, overnight_gap=None) -> dict:
    """Assemble the SIX subscores and the (possibly renormalized) 0-100 score.

    A dimension whose ``evaluable`` is False (all its scored inputs null) is
    EXCLUDED from the max total and the score is rescaled to 0-100 over the
    remaining max, with ``renormalized: true`` recorded.

    ``beta_n_days``/``ohlcv_rows`` gate the volatility-state components on
    sufficient history (see score_volatility).

    risk-v1.1.0 adds ``events`` (the snapshot events block -> event_risk) and
    ``overnight_gap`` (technicals.overnight_gap -> tail_risk). event_risk is
    ALWAYS evaluable (a null event is a real "no near-term risk" claim);
    tail_risk is NOT evaluable when its kurtosis input is null (renormalize).
    The six maxes sum to 100 (20+20+25+15+12+8); a renormalization over a
    non-evaluable tail_risk rescales over the remaining 92.
    """
    subs = [
        score_volatility(tech, beta, beta_n_days=beta_n_days,
                         ohlcv_rows=ohlcv_rows),
        score_drawdown(tech),
        score_margin(tech, ladder, last),
        score_liquidity(adv, net, mktcap),
        score_event_risk(events),
        score_tail_risk(overnight_gap),
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


def build_event_context(snapshot) -> dict:
    """Surface the event-context fields verbatim from the snapshot (A2, unscored).

    Reads events.{days_to_event, implied_move, implied_move_vs_own_history_pctile,
    earnings_move_history} from the snapshot without any arithmetic. The
    earnings_move_history list and its count are passed through unchanged.
    Returns a dict suitable for tables.event_context. Null-safe: any absent
    field is returned as None.
    """
    ev = snapshot.get("events") if isinstance(snapshot, dict) else None
    if not isinstance(ev, dict):
        ev = {}
    history = ev.get("earnings_move_history")
    history_count = len(history) if isinstance(history, list) else None
    return {
        "days_to_event": ev.get("days_to_event"),
        "implied_move": ev.get("implied_move"),
        "implied_move_vs_own_history_pctile": ev.get("implied_move_vs_own_history_pctile"),
        "earnings_move_history_summary": {
            "history": history if isinstance(history, list) else [],
            "count": history_count,
        },
    }


def build_module(snapshot, ladder, stress_pct, top_risk, anchors=None,
                 bundle_dir=None) -> dict:
    """Build the full module_risk.json document from parsed inputs + ladder.

    ``anchors`` (a validated valuation_anchors dict) switches the downside floor
    to ANCHORED mode (dcf_bear); absent, the snapshot pe-median floor is used. The
    active mode is disclosed at the top level as ``downside_floor_mode``.

    ``bundle_dir`` is threaded to the confidence layer so the staleness axis can
    read a ``refresh_plan.json`` reuse signal when present (absent on fresh runs).
    """
    price = snapshot.get("price", {}) if isinstance(snapshot, dict) else {}
    tech = snapshot.get("technicals", {}) if isinstance(snapshot, dict) else {}
    bench = snapshot.get("benchmark", {}) if isinstance(snapshot, dict) else {}
    fund = snapshot.get("fundamentals", {}) if isinstance(snapshot, dict) else {}
    val = snapshot.get("valuation", {}) if isinstance(snapshot, dict) else {}
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}

    last = price.get("last")
    beta = bench.get("beta")
    beta_n_days = bench.get("beta_n_days")
    ohlcv_rows = tech.get("ohlcv_rows")
    adv = price.get("adv_dollar_3m")
    mktcap = price.get("mktcap") or price.get("mktcap_computed")
    net_cash = fund.get("net_cash_defined") or {}
    net = net_cash.get("net") if isinstance(net_cash, dict) else None
    events = snapshot.get("events", {}) if isinstance(snapshot, dict) else {}
    overnight_gap = tech.get("overnight_gap")  # may be None

    scored = score(tech, beta, ladder, last, adv, net, mktcap,
                   beta_n_days=beta_n_days, ohlcv_rows=ohlcv_rows,
                   events=events, overnight_gap=overnight_gap)

    vf = valuation_floor(val.get("pe_5yr_median"),
                         fund.get("eps_ntm_consensus"),
                         last=last, pe_fwd=val.get("pe_fwd"),
                         anchors=anchors)
    downside_map = build_downside_map(ladder, last, vf, stress_pct, top_risk)
    vol_profile = build_vol_profile(tech, bench)

    # A2: surface context-only (unscored) event and tail blocks verbatim from the
    # snapshot. Zero arithmetic here -- all computation was done in build_snapshot.
    event_context = build_event_context(snapshot)
    tail_context = tech.get("overnight_gap")  # verbatim passthrough; may be None

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
            "event_context": event_context,
            "tail_context": tail_context,
        },
        "flags": {
            "stress_pct": stress_pct,
            "top_risk": top_risk,
        },
        "downside_floor_mode": "dcf_bear" if isinstance(anchors, dict)
                               else "pe_median",
        "renormalized": scored["renormalized"],
        # risk-v1.1.0 (Part B): event/tail are now SCORED factors, shipped
        # PROVISIONAL and loudly disclosed (unratified pending B9 calibration).
        # The report renderer surfaces this note verbatim -> the provisional flag
        # travels into every brief/report footer.
        "note": _PROVISIONAL_NOTE,
        "signal": None,
    }
    if scored["renormalization_note"]:
        doc["renormalization_note"] = scored["renormalization_note"]
    # Confidence / provenance layer (confidence-v1.0.0): deterministic, disclosure-
    # only, computed from source/depth/staleness of THIS module's own doc + snapshot.
    doc["confidence"] = confidence.compute_module(doc, snapshot, bundle_dir)
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
    parser.add_argument("--anchors", default=None,
                        help="path to valuation_anchors.json (same file "
                             "score_fundamental consumes); switches the downside "
                             "valuation floor to ANCHORED MODE (dcf_bear, labeled "
                             "'dcf_bear (coverage anchors)'), replacing the "
                             "pe-median floor. Absent -> snapshot pe-median floor.")
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

    # Anchored-mode input: a malformed/absent anchors file is a hard error (the
    # caller asked for anchored mode explicitly). Validation mirrors
    # score_fundamental so the two consumers' exit-2 contract is identical.
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

    doc = build_module(snapshot, ladder, args.stress_pct, args.top_risk,
                       anchors=anchors, bundle_dir=args.bundle)

    out = args.out or os.path.join(args.bundle, "module_risk.json")
    emit_json(doc, out)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
