"""Sentiment-positioning evidence module for the trading-desk plugin.

WHY THIS MODULE EXISTS: this is the THIRD scored evidence skill, and like
technical-analysis and risk-analytics its arithmetic IS the rubric of record
(sentiment rubric v1.0.0). Every branch is deterministic and unit-pinned so a
report can never silently drift: the numbers a brief cites all originate here, in
Python, and the version string travels with them into the module JSON and the
brief footer. The LLM layer narrates; it does no scoring arithmetic.

Scoring is over five dimensions (max 100 total):
    1. Street view             (25) -- analyst buy% (8) + PT-vs-price (8) +
                                        rating-actions judgment flag (4) +
                                        news_heat (5). SPEC §5.2: pt_vs_price < 0
                                        caps the WHOLE dimension at 10/25.
    2. Revisions momentum      (20) -- 90-day EPS-revision band + up/down-count adj
    3. Smart money & insiders  (20) -- 13F inst-flow judgment flag (8) + insider
                                        (12): Cohen/Malloy/Pomorski routine-vs-
                                        opportunistic classification when active,
                                        else graceful fallback to net-90d + baseline.
    4. Positioning & derivatives(20) -- SI+DTC (6, with a COMPLACENCY GUARD) +
                                        OI-P/C (4) + volume-P/C (3) + skew (4) +
                                        IV percentile (3)
    5. Price momentum          (15) -- 12m + 3m relative-to-SPY + 6m absolute

WAVE 3A (sentiment-v1.1.0, PROVISIONAL): factors 1/3/4 are re-split IN PLACE to
fold in the new snapshot signals (news_heat, DTC, volume-P/C, skew, insider CMP)
WITHOUT changing any top-level weight (still 25/20/20/20/15). Band SHAPES of the
retained sub-components are held identical where possible -- only the point ceilings
scale (e.g. buy% 10->8, IV 6->3). This keeps score movement small on a provisional
wave. Positioning/news bands are unratified defaults pending B9; a falsifier is
pre-registered in the SKILL.

Design contract (project-wide, mirrors score_technical.py / score_risk.py):
- The snapshot is READ-ONLY; this module never edits snapshot.json. No market data
  is fetched here; a missing figure contributes 0 and is named "n/a".
- ``INPUT_FIELDS`` lists exactly the snapshot fields this rubric SCORES on (dotted
  paths). ``GUARD_FIELDS`` lists fields that only GATE/CAP a branch here but are
  SCORED in another module -- the single-mapping rule (each snapshot fact scores in
  exactly one module) means a guard field must NOT also appear in INPUT_FIELDS. A
  cross-skill governance test (tests/test_single_mapping.py) imports both sets and
  asserts (a) the three scorers' INPUT_FIELDS are pairwise disjoint and (b) no
  scorer scores its own guard field. ``price.last`` and the S/R ladder are shared
  reference infrastructure and are deliberately excluded from INPUT_FIELDS.
- If a WHOLE dimension has zero evaluable inputs, it is excluded and the score is
  renormalized to 0-100 over the remaining max.

SINGLE-MAPPING SPLIT (spec §2, §5.2/§5.3): options SENTIMENT fields (put/call,
IV percentile, skew) score HERE and nowhere else; options-derived LEVELS score in
technical-analysis. PT-upside scores HERE (street view), NOT in risk-analytics --
risk documents that reallocation. ``technicals.rsi14`` conditions the complacency
guard here but is SCORED only in technical-analysis: it is a GUARD_FIELD, not an
INPUT_FIELD (guard fields may gate/cap here but score elsewhere).

No dependency on other scored modules: this module consumes the snapshot only (it
does not read module_technical.json or the ladder -- it scores no levels). Reuses
the build_snapshot I/O helper for the CLI ``as_of`` date. The scoring functions are
pure over already-parsed inputs. stdlib-only.
"""

import argparse
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation (``python3 scripts/score_sentiment.py``): ensure the repo
# root is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import build_snapshot, confidence
from scripts._artifact import emit_json

RUBRIC_VERSION = "1.1.0"
SKILL_NAME = "sentiment-positioning"

# sentiment-v1.1.0 PROVISIONAL module note: positioning/news bands are unratified
# defaults pending B9 calibration; the falsifier is pre-registered in the SKILL.
MODULE_NOTE = ("sentiment-v1.1.0 PROVISIONAL -- positioning/news bands unratified "
               "pending B9; falsifier pre-registered")

# The snapshot fields this rubric SCORES on. price.last and the ladder are shared
# reference infrastructure and are intentionally NOT listed (see module docstring).
# Wave 3A additions (all null-safe): sentiment.dtc, .put_call_ratio_full_chain_volume,
# .skew_25d_30d, .news_heat, .insider_classification. si_trend is read ONLY to
# condition the DTC notch direction (a guard for the SI+DTC branch), so it is NOT an
# INPUT_FIELD.
INPUT_FIELDS = {
    "sentiment.ratings",
    "sentiment.pt_vs_price_pct",
    "sentiment.news_heat",
    "fundamentals.revisions_90d",
    "sentiment.insider_net_90d_usd",
    "sentiment.insider_classification",
    "sentiment.short_interest_pct",
    "sentiment.dtc",
    "sentiment.put_call_ratio_full_chain",
    "sentiment.put_call_ratio_full_chain_volume",
    "sentiment.skew_25d_30d",
    "sentiment.iv_pctile_1yr",
    "technicals.ret_3m",
    "technicals.ret_6m",
    "technicals.ret_12m",
    "benchmark.spy_ret_3m",
    "benchmark.spy_ret_12m",
}

# Fields that CONDITION (gate/cap) a branch here but are SCORED in another module.
# rsi14 conditions the complacency guard in positioning but is scored ONLY in
# technical-analysis. Single-mapping rule: a guard field may gate/cap here but must
# score elsewhere, so it must NOT appear in INPUT_FIELDS (governance test enforces).
GUARD_FIELDS = {"technicals.rsi14"}

# Judgment-flag choices.
_RATING_ACTIONS_CHOICES = ("positive", "neutral", "negative")
_INST_FLOW_CHOICES = ("accumulating", "neutral", "distributing", "unknown")
_INSIDER_BASELINE_CHOICES = ("normal", "unusual")


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
# 1. Street view (max 25): buy% (8) + PT (8) + rating-actions flag (4) +
#    news_heat (5).   [v1.0.0 was buy% 10 / PT 10 / rating-actions 5.]
#    SPEC §5.2 CAP: pt_vs_price_pct < 0 caps the WHOLE dimension at 10/25.
#    v1.1.0 re-split: buy% ceiling 10->8, PT 10->8, rating-actions 5->4 (LLM
#    judgment trimmed), 5 pts of ALGORITHMIC news_heat added. Band SHAPES held
#    identical -- only the ceilings scale. news_heat is INTERNALLY renormalized: a
#    null news_heat drops the sub-component and the dimension renormalizes over its
#    OTHER available sub-components (do NOT zero -- a missing feed is not a bearish
#    read).
# --------------------------------------------------------------------------- #

_RATING_ACTIONS_POINTS = {"positive": 4, "neutral": 2, "negative": 0}


def score_street(sentiment, rating_actions, rating_actions_justification) -> dict:
    """Analyst buy% (max 8) + PT-vs-price (max 8) + rating-actions (max 4) +
    news_heat (max 5).

    buy_pct = (strong_buy + buy)/n: >=0.70 -> 8; [0.50,0.70) -> 6 (0.7*8~5.6->6);
        [0.30,0.50) -> 3; <0.30 -> 2; null ratings or zero n -> "n/a" (sub dropped).
    PT (pt_vs_price_pct): >0.15 -> 8; (0.05,0.15] -> 6; [0,0.05] -> 3; <0 -> 0.
    rating-actions judgment: positive +4 / neutral +2 / negative +0 (always
        evaluable -- a flag is always supplied).
    news_heat (news_heat.ewma): >+0.15 -> 5 (bullish heat); [-0.15,0.15] -> 3;
        <-0.15 -> 1 (bearish heat); then -1 notch (floor 1) when news_heat.volume_z
        > 2 (attention spike). Null news_heat -> sub dropped (renormalize, not zero).

    SPEC §5.2: pt_vs_price_pct < 0 -> the WHOLE street-view dimension is capped at
    10/25 (a below-price consensus target overrides an otherwise-bullish street).
    A NULL PT is "n/a" (0 pts) and does NOT trigger the cap.

    RENORMALIZATION (v1.1.0): the street dimension's four sub-components carry a
    per-sub max; a sub whose input is null (buy_pct null ratings; news_heat null
    feed) is DROPPED and the dimension is rescaled to /25 over the sub-maxes that
    remain. rating-actions and (present) PT are always in the denominator.
    """
    ratings = sentiment.get("ratings")
    pt = sentiment.get("pt_vs_price_pct")
    news_heat = sentiment.get("news_heat")

    parts = []
    # Each entry: (points, sub_max, present). A sub with present=False is dropped
    # from BOTH numerator and denominator (renormalize, not zero).
    subs = []

    # -- analyst buy% (max 8) ----------------------------------------------
    n = ratings.get("n") if isinstance(ratings, dict) else None
    if isinstance(ratings, dict) and n:
        buy_pct = (ratings.get("strong_buy", 0) + ratings.get("buy", 0)) / n
        if buy_pct >= 0.70:
            buy_pct_pts = 8
        elif buy_pct >= 0.50:
            buy_pct_pts = 6
        elif buy_pct >= 0.30:
            buy_pct_pts = 3
        else:  # < 0.30
            buy_pct_pts = 2
        subs.append((buy_pct_pts, 8, True))
        parts.append(f"buy_pct {buy_pct*100:.1f}% (n {_fmt(n)}) -> {buy_pct_pts}/8")
    else:
        buy_pct_pts = 0
        subs.append((0, 8, False))
        parts.append("buy_pct: n/a (null ratings or zero n) (dropped)")

    # -- PT vs price (max 8) -----------------------------------------------
    pt_below_price = False
    if pt is not None:
        if pt > 0.15:
            pt_pts = 8
        elif pt > 0.05:
            pt_pts = 6
        elif pt >= 0:
            pt_pts = 3
        else:  # < 0
            pt_pts = 0
            pt_below_price = True
        subs.append((pt_pts, 8, True))
        parts.append(f"pt_vs_price_pct {_fmt(pt)} -> {pt_pts}/8")
    else:
        pt_pts = 0
        subs.append((0, 8, False))
        parts.append("pt_vs_price_pct: n/a (dropped)")

    # -- rating-actions judgment flag (max 4) ------------------------------
    ra_pts = _RATING_ACTIONS_POINTS[rating_actions]
    subs.append((ra_pts, 4, True))
    parts.append(f"rating_actions {rating_actions} -> +{ra_pts}/4")

    # -- news_heat (max 5) -------------------------------------------------
    ewma = news_heat.get("ewma") if isinstance(news_heat, dict) else None
    if ewma is not None:
        if ewma > 0.15:
            nh_pts = 5
            nh_label = "bullish heat"
        elif ewma >= -0.15:
            nh_pts = 3
            nh_label = "neutral heat"
        else:  # < -0.15
            nh_pts = 1
            nh_label = "bearish heat"
        vz = news_heat.get("volume_z")
        if vz is not None and vz > 2:
            nh_pts = max(1, nh_pts - 1)
            parts.append(
                f"news_heat.ewma {_fmt(_clean(ewma))} -> {nh_label}, "
                f"volume_z {_fmt(_clean(vz))} > 2 (attention spike) -1 -> {nh_pts}/5")
        else:
            parts.append(
                f"news_heat.ewma {_fmt(_clean(ewma))} -> {nh_pts}/5 ({nh_label})")
        subs.append((nh_pts, 5, True))
    else:
        nh_pts = 0
        subs.append((0, 5, False))
        parts.append("news_heat: n/a (null feed -> dropped, renormalized)")

    # -- assemble with internal renormalization over PRESENT sub-maxes ------
    present = [(p, m) for (p, m, ok) in subs if ok]
    raw_pts = sum(p for p, _ in present)
    raw_max = sum(m for _, m in present)
    evaluable = raw_max > 0
    if raw_max <= 0:
        total = 0
    elif raw_max == 25:
        total = raw_pts
    else:
        total = _clean(raw_pts / raw_max * 25)
        parts.append(f"street renormalized over sub-max {raw_max} -> {_fmt(total)}/25")

    # -- SPEC §5.2 dimension cap -------------------------------------------
    if pt_below_price and total > 10:
        total = 10
        parts.append("PT below price: dimension capped at 10/25")

    return {
        "name": "street_view",
        "points": _clean(min(25, total)),
        "max": 25,
        "arithmetic": "; ".join(parts),
        "inputs": {"buy_pct_points": buy_pct_pts, "pt_points": pt_pts,
                   "rating_actions_points": ra_pts, "news_heat_points": nh_pts,
                   "ratings": ratings, "pt_vs_price_pct": pt,
                   "news_heat": news_heat,
                   "rating_actions": rating_actions,
                   "rating_actions_justification": rating_actions_justification},
        "evaluable": evaluable,
    }


# --------------------------------------------------------------------------- #
# 2. Revisions momentum (max 20): band + up/down count adjustment
# --------------------------------------------------------------------------- #

def score_revisions(revisions) -> dict:
    """90-day EPS-revision band (max 20) + up_30d/down_30d count adjustment.

    rev (revisions_90d.pct): >0.03 -> 20; (0.005,0.03] -> 14; [-0.005,0.005] -> 10;
        [-0.03,-0.005) -> 5; <-0.03 -> 0; null -> 0 ("n/a").
    adjustment (same block's up_30d/down_30d): up>down -> +2 (cap 20);
        down>up -> -2 (floor 0); ties/nulls -> 0.
    """
    rev = revisions.get("pct") if isinstance(revisions, dict) else None
    up = revisions.get("up_30d") if isinstance(revisions, dict) else None
    down = revisions.get("down_30d") if isinstance(revisions, dict) else None

    parts = []
    evaluable = 0

    if rev is not None:
        evaluable += 1
        if rev > 0.03:
            band_pts = 20
        elif rev > 0.005:
            band_pts = 14
        elif rev >= -0.005:
            band_pts = 10
        elif rev >= -0.03:
            band_pts = 5
        else:  # < -0.03
            band_pts = 0
        parts.append(f"revisions_90d.pct {_fmt(rev)} -> {band_pts}/20")
    else:
        band_pts = 0
        parts.append("revisions_90d.pct: n/a (+0)")

    # -- up/down count adjustment ------------------------------------------
    if up is not None and down is not None:
        if up > down:
            adj_pts = 2
            parts.append(f"up_30d {_fmt(up)} > down_30d {_fmt(down)} -> +2 (cap 20)")
        elif down > up:
            adj_pts = -2
            parts.append(f"down_30d {_fmt(down)} > up_30d {_fmt(up)} -> -2 (floor 0)")
        else:
            adj_pts = 0
            parts.append(f"up_30d == down_30d {_fmt(up)} -> +0")
    else:
        adj_pts = 0
        parts.append("up/down counts: n/a (+0)")

    total = max(0, min(20, band_pts + adj_pts))
    return {
        "name": "revisions_momentum",
        "points": total,
        "max": 20,
        "arithmetic": "; ".join(parts),
        "inputs": {"band_points": band_pts, "adjustment_points": adj_pts,
                   "pct": rev, "up_30d": up, "down_30d": down},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 3. Smart money & insiders (max 20): inst-flow flag (8) + insider net (12)
# --------------------------------------------------------------------------- #

_INST_FLOW_POINTS = {"accumulating": 8, "neutral": 5, "distributing": 2,
                     "unknown": 0}


def _score_insider(sentiment, insider_baseline, insider_baseline_justification):
    """Insider sub-score (max 12) with a Cohen/Malloy/Pomorski (CMP) classifier
    that ACTIVATES ONLY when the snapshot carries enough per-insider history, else
    a graceful fall-back to the v1.0.0 net-90d + baseline logic (UNCHANGED).

    Returns (insider_pts, present, arithmetic_str, insider_net).

    CMP path (sentiment.insider_classification.classifier_active is True):
      - opportunistic net-SELLING cluster (opportunistic_cluster True AND
        opportunistic_net_usd < 0) -> 2/12  (the review's "insider cluster at the
        highs" signal).
      - opportunistic net-BUYING (opportunistic_net_usd > 0) -> 12/12.
      - routine-only / no opportunistic signal -> 8/12 (neutral).

    GRACEFUL FALLBACK (classifier_active False OR insider_classification null): the
    EXACT v1.0.0 rule -- insider_net_90d_usd >0 -> 12; <=0 baseline normal -> 8
    (routine selling); <=0 baseline unusual -> 2; null -> "n/a" (sub dropped).
    """
    classification = sentiment.get("insider_classification")
    insider = sentiment.get("insider_net_90d_usd")

    # -- CMP classifier path (only when the data justifies it) -------------
    if isinstance(classification, dict) and classification.get("classifier_active"):
        opp_net = classification.get("opportunistic_net_usd")
        cluster = classification.get("opportunistic_cluster")
        if cluster and opp_net is not None and opp_net < 0:
            return (2, True,
                    f"insider CMP: opportunistic net-selling cluster "
                    f"(opportunistic_net_usd {_fmt(_clean(opp_net))} < 0) -> 2/12",
                    insider)
        if opp_net is not None and opp_net > 0:
            return (12, True,
                    f"insider CMP: opportunistic net-buying "
                    f"(opportunistic_net_usd {_fmt(_clean(opp_net))} > 0) -> 12/12",
                    insider)
        return (8, True,
                "insider CMP: routine-only / no opportunistic signal -> 8/12",
                insider)

    # -- GRACEFUL FALLBACK: v1.0.0 net-90d + baseline logic, UNCHANGED ------
    if insider is not None:
        if insider > 0:
            return (12, True,
                    f"insider_net_90d_usd {_fmt(_clean(insider))} > 0 -> 12 "
                    f"(classifier inactive: graceful net-90d fallback)",
                    insider)
        if insider_baseline == "unusual":
            return (2, True,
                    f"insider_net_90d_usd {_fmt(_clean(insider))} <= 0, baseline "
                    f"unusual -> 2 (graceful net-90d fallback)",
                    insider)
        return (8, True,
                f"insider_net_90d_usd {_fmt(_clean(insider))} <= 0, baseline "
                f"normal -> 8 (routine selling; graceful net-90d fallback)",
                insider)
    return (0, False, "insider_net_90d_usd: n/a (+0)", insider)


def score_smart_money(sentiment, inst_flow, inst_flow_justification,
                      insider_baseline, insider_baseline_justification) -> dict:
    """13F inst-flow judgment flag (max 8) + insider (max 12).

    inst-flow: accumulating 8 / neutral 5 / distributing 2 / unknown 0
        ("n/a -- 13F not assessed; lag disclosed").
    insider (max 12): CMP routine-vs-opportunistic classification when
        insider_classification.classifier_active; else graceful fallback to the
        v1.0.0 insider_net_90d_usd + baseline logic (see _score_insider).

    unknown inst-flow AND no insider signal -> whole dimension not evaluable
    (renormalized out), since neither component carries a real signal.
    """
    parts = []
    evaluable = 0

    # -- 13F institutional flow (judgment flag) ----------------------------
    inst_pts = _INST_FLOW_POINTS[inst_flow]
    if inst_flow == "unknown":
        parts.append("inst_flow unknown -> +0 (n/a -- 13F not assessed; lag disclosed)")
    else:
        evaluable += 1
        parts.append(f"inst_flow {inst_flow} -> +{inst_pts}")

    # -- insider (CMP with graceful degrade) -------------------------------
    insider_pts, insider_present, insider_str, insider = _score_insider(
        sentiment, insider_baseline, insider_baseline_justification)
    if insider_present:
        evaluable += 1
    parts.append(insider_str)

    classification = sentiment.get("insider_classification")
    total = inst_pts + insider_pts
    return {
        "name": "smart_money_insiders",
        "points": _clean(total),
        "max": 20,
        "arithmetic": "; ".join(parts),
        "inputs": {"inst_flow_points": inst_pts, "insider_points": insider_pts,
                   "insider_net_90d_usd": insider,
                   "insider_classification": classification,
                   "inst_flow": inst_flow,
                   "inst_flow_justification": inst_flow_justification,
                   "insider_baseline": insider_baseline,
                   "insider_baseline_justification": insider_baseline_justification},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 4. Positioning & derivatives (max 20): SI+DTC (6) + OI-P/C (4) + volume-P/C (3)
#    + skew (4) + IV pctile (3).   [v1.0.0 was SI 8 / OI-P/C 6 / IV 6.]
#    COMPLACENCY GUARD evaluated FIRST (uses technicals.rsi14 -- a GUARD field).
#    v1.1.0 re-split: SI ceiling 8->6 (plus a DTC notch), OI-P/C 6->4, IV 6->3;
#    ADDS volume-P/C (3) and skew (4). Retained band SHAPES held identical -- only
#    the ceilings scale; the DTC notch and the two new signals are additive.
# --------------------------------------------------------------------------- #

def score_positioning(sentiment, rsi14) -> dict:
    """SI+DTC (max 6) + OI-P/C (max 4) + volume-P/C (max 3) + skew (max 4) +
    IV percentile (max 3).

    SI+DTC (6): COMPLACENCY GUARD FIRST -- si < 1.5 AND rsi14 > 70 -> 2
        ("complacency guard: SI <1.5% with RSI >70"). Else the v1.0.0 SI% band
        scaled 8->6 (si<2 -> 4 (was 6); [2,8] -> 6 (was 8); (8,15] -> 4 (was 5);
        >15 -> 2 (was 3)), THEN a DTC notch: -1 when sentiment.dtc > 10 AND si_trend
        rising (squeeze/crowded-short risk); +1 when dtc < 2. Notch floors at 1,
        caps at 6. si_trend only CONDITIONS the notch direction.
    OI-P/C (4): the v1.0.0 put_call_ratio_full_chain band scaled 6->4: [0.7,1.1] ->
        4; <0.7 -> 2 ("call-heavy froth"); >1.1 -> 3 ("hedged/bearish tilt").
    volume-P/C (3): put_call_ratio_full_chain_volume (FLOW vs OI=structural):
        [0.7,1.3] -> 3; extreme (<0.5 call-froth or >2.0 hedged) -> 1; else 2.
    skew (4): skew_25d_30d (25Delta RR = IV(25d put) - IV(25d call); POSITIVE = put
        IV richer = fear/hedging demand, NEGATIVE = call chase): |skew| < 0.03 -> 4
        (balanced); [0.03,0.08] -> 2 (moderate hedging demand); > 0.08 -> 1 (extreme
        put bid = fear, or negative = call chase).
    IV pctile (iv_pctile_1yr): <25 -> 3 (note "hedges cheap"); [25,75] -> 2; >75 ->
        1. [v1.0.0 was /6: 6/4/2 -> now /3: 3/2/1.]

    Each sub-component null -> "n/a" (+0); rsi14 CONDITIONS the guard but is SCORED
    only in technical-analysis (GUARD_FIELD, not INPUT_FIELD).
    """
    si = sentiment.get("short_interest_pct")
    dtc = sentiment.get("dtc")
    si_trend = sentiment.get("si_trend")
    pc = sentiment.get("put_call_ratio_full_chain")
    pcv = sentiment.get("put_call_ratio_full_chain_volume")
    skew = sentiment.get("skew_25d_30d")
    iv = sentiment.get("iv_pctile_1yr")

    parts = []
    evaluable = 0

    # -- SI + DTC (max 6; complacency guard first) -------------------------
    if si is not None:
        evaluable += 1
        if si < 1.5 and rsi14 is not None and rsi14 > 70:
            si_pts = 2
            parts.append(
                f"short_interest_pct {_fmt(si)} -> 2 (complacency guard: "
                f"SI <1.5% with RSI >70, rsi14 {_fmt(rsi14)})")
        else:
            if si < 2:
                si_pts = 4
                parts.append(f"short_interest_pct {_fmt(si)} < 2 -> 4/6")
            elif si <= 8:
                si_pts = 6
                parts.append(f"short_interest_pct {_fmt(si)} in [2,8] -> 6/6")
            elif si <= 15:
                si_pts = 4
                parts.append(f"short_interest_pct {_fmt(si)} in (8,15] -> 4/6")
            else:  # > 15
                si_pts = 2
                parts.append(f"short_interest_pct {_fmt(si)} > 15 -> 2/6")
            # Crowded-short notch (only when the complacency guard did NOT fire).
            # -1 when SI is RISING AND (very high days-to-cover, OR a heavy short book
            # still elevated on DTC). Re-based 2026-07-23 (O1): DTC>10 rarely fires on
            # liquid US names -- high-SI names are high-volume, so days-to-cover stays
            # moderate (a 29.6%-short name read dtc ~6-7) -- so a crowded, still-covered
            # book (si_pct > 15 AND dtc > 5) also trips it. Never SI% alone or DTC>5
            # alone; the rising-trend condition is required in every path.
            if dtc is not None:
                si_rising = (si_trend or "").lower() == "rising"
                crowded = si_rising and (
                    dtc > 10 or (si is not None and si > 15 and dtc > 5))
                if crowded:
                    si_pts = max(1, si_pts - 1)
                    reason = ("dtc > 10" if dtc > 10
                              else f"si {_fmt(si)}>15 & dtc {_fmt(_clean(dtc))}>5")
                    parts.append(
                        f"crowded-short ({reason}, si_trend rising) -> -1 notch "
                        f"-> {si_pts}/6")
                elif dtc < 2:
                    si_pts = min(6, si_pts + 1)
                    parts.append(
                        f"dtc {_fmt(_clean(dtc))} < 2 -> +1 notch -> {si_pts}/6")
    else:
        si_pts = 0
        parts.append("short_interest_pct: n/a (+0)")

    # -- OI-based full-chain put/call ratio (max 4) ------------------------
    if pc is not None:
        evaluable += 1
        if 0.7 <= pc <= 1.1:
            pc_pts = 4
            parts.append(f"put_call_ratio_full_chain {_fmt(pc)} in [0.7,1.1] -> 4/4")
        elif pc < 0.7:
            pc_pts = 2
            parts.append(
                f"put_call_ratio_full_chain {_fmt(pc)} < 0.7 -> 2/4 (call-heavy froth)")
        else:  # > 1.1
            pc_pts = 3
            parts.append(
                f"put_call_ratio_full_chain {_fmt(pc)} > 1.1 -> 3/4 "
                f"(hedged/bearish tilt)")
    else:
        pc_pts = 0
        parts.append("put_call_ratio_full_chain: n/a (+0)")

    # -- volume-based full-chain put/call ratio (FLOW; max 3) --------------
    if pcv is not None:
        evaluable += 1
        if 0.7 <= pcv <= 1.3:
            pcv_pts = 3
            parts.append(
                f"put_call_ratio_full_chain_volume {_fmt(pcv)} in [0.7,1.3] -> 3/3")
        elif pcv < 0.5 or pcv > 2.0:
            pcv_pts = 1
            parts.append(
                f"put_call_ratio_full_chain_volume {_fmt(pcv)} extreme "
                f"(<0.5 call-froth or >2.0 hedged) -> 1/3")
        else:
            pcv_pts = 2
            parts.append(
                f"put_call_ratio_full_chain_volume {_fmt(pcv)} -> 2/3")
    else:
        pcv_pts = 0
        parts.append("put_call_ratio_full_chain_volume: n/a (+0)")

    # -- 25d/30d skew (25Delta RR = IV(25d put) - IV(25d call); max 4) -----
    if skew is not None:
        evaluable += 1
        askew = abs(skew)
        if askew < 0.03:
            skew_pts = 4
            parts.append(f"skew_25d_30d {_fmt(_clean(skew))} |.|<0.03 -> 4/4 (balanced)")
        elif askew <= 0.08:
            skew_pts = 2
            parts.append(
                f"skew_25d_30d {_fmt(_clean(skew))} |.| in [0.03,0.08] -> 2/4 "
                f"(moderate hedging demand)")
        else:  # |skew| > 0.08
            skew_pts = 1
            parts.append(
                f"skew_25d_30d {_fmt(_clean(skew))} |.|>0.08 -> 1/4 "
                f"(extreme put bid = fear, or negative = call chase)")
    else:
        skew_pts = 0
        parts.append("skew_25d_30d: n/a (+0)")

    # -- IV percentile (max 3) ---------------------------------------------
    if iv is not None:
        evaluable += 1
        if iv < 25:
            iv_pts = 3
            parts.append(
                f"iv_pctile_1yr {_fmt(iv)} < 25 -> 3/3 "
                f"(hedges cheap -- cross-ref options-strategy)")
        elif iv <= 75:
            iv_pts = 2
            parts.append(f"iv_pctile_1yr {_fmt(iv)} in [25,75] -> 2/3")
        else:  # > 75
            iv_pts = 1
            parts.append(f"iv_pctile_1yr {_fmt(iv)} > 75 -> 1/3")
    else:
        iv_pts = 0
        parts.append("iv_pctile_1yr: n/a (+0)")

    total = si_pts + pc_pts + pcv_pts + skew_pts + iv_pts
    return {
        "name": "positioning_derivatives",
        "points": _clean(total),
        "max": 20,
        "arithmetic": "; ".join(parts),
        "inputs": {"si_points": si_pts, "pc_points": pc_pts,
                   "pcv_points": pcv_pts, "skew_points": skew_pts,
                   "iv_points": iv_pts,
                   "short_interest_pct": si, "dtc": dtc, "si_trend": si_trend,
                   "put_call_ratio_full_chain": pc,
                   "put_call_ratio_full_chain_volume": pcv,
                   "skew_25d_30d": skew,
                   "iv_pctile_1yr": iv, "rsi14_guard": rsi14},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 5. Price momentum (max 15): rel12 (7) + rel3 (5) + abs6 (3)
# --------------------------------------------------------------------------- #

def score_momentum(tech, bench) -> dict:
    """Relative-to-SPY 12m (max 7) + 3m (max 5) + absolute 6m (max 3).

    rel12 = ret_12m - spy_ret_12m: >0.15 -> 7; (0,0.15] -> 5; [-0.15,0] -> 2;
        <-0.15 -> 0.
    rel3  = ret_3m - spy_ret_3m: >0.10 -> 5; (0,0.10] -> 4; <=0 -> 1.
    abs6  = ret_6m > 0 -> 3; else 0.
    Any null in a component -> that component 0 ("n/a").
    """
    ret3 = tech.get("ret_3m")
    ret6 = tech.get("ret_6m")
    ret12 = tech.get("ret_12m")
    spy3 = bench.get("spy_ret_3m")
    spy12 = bench.get("spy_ret_12m")

    parts = []
    evaluable = 0

    # -- 12-month relative -------------------------------------------------
    if ret12 is not None and spy12 is not None:
        evaluable += 1
        rel12 = ret12 - spy12
        if rel12 > 0.15:
            rel12_pts = 7
        elif rel12 > 0:
            rel12_pts = 5
        elif rel12 >= -0.15:
            rel12_pts = 2
        else:  # < -0.15
            rel12_pts = 0
        parts.append(
            f"rel12 (ret_12m {_fmt(ret12)} - spy_ret_12m {_fmt(spy12)} = "
            f"{_fmt(_clean(rel12))}) -> {rel12_pts}/7")
    else:
        rel12_pts = 0
        parts.append("rel12: n/a (+0)")

    # -- 3-month relative --------------------------------------------------
    if ret3 is not None and spy3 is not None:
        evaluable += 1
        rel3 = ret3 - spy3
        if rel3 > 0.10:
            rel3_pts = 5
        elif rel3 > 0:
            rel3_pts = 4
        else:  # <= 0
            rel3_pts = 1
        parts.append(
            f"rel3 (ret_3m {_fmt(ret3)} - spy_ret_3m {_fmt(spy3)} = "
            f"{_fmt(_clean(rel3))}) -> {rel3_pts}/5")
    else:
        rel3_pts = 0
        parts.append("rel3: n/a (+0)")

    # -- 6-month absolute --------------------------------------------------
    if ret6 is not None:
        evaluable += 1
        abs6_pts = 3 if ret6 > 0 else 0
        parts.append(f"abs6 (ret_6m {_fmt(ret6)} > 0) -> {abs6_pts}/3")
    else:
        abs6_pts = 0
        parts.append("abs6: n/a (+0)")

    total = rel12_pts + rel3_pts + abs6_pts
    return {
        "name": "price_momentum",
        "points": _clean(total),
        "max": 15,
        "arithmetic": "; ".join(parts),
        "inputs": {"rel12_points": rel12_pts, "rel3_points": rel3_pts,
                   "abs6_points": abs6_pts, "ret_3m": ret3, "ret_6m": ret6,
                   "ret_12m": ret12, "spy_ret_3m": spy3, "spy_ret_12m": spy12},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# Tables: positioning + momentum_vs_spy + hedging_cost_note
# --------------------------------------------------------------------------- #

def build_positioning_table(sentiment) -> dict:
    """Verbatim positioning context block (realtime P/C, iv30, implied move are
    UNSCORED context; SI/full-chain P/C/IV pctile are scored elsewhere in this
    module and repeated here for the brief's mini-table)."""
    return {
        "short_interest_pct": sentiment.get("short_interest_pct"),
        "si_trend": sentiment.get("si_trend"),
        "si_as_of": sentiment.get("si_as_of"),
        "put_call_ratio_full_chain": sentiment.get("put_call_ratio_full_chain"),
        "put_call_ratio_realtime": sentiment.get("put_call_ratio_realtime"),
        "iv30": sentiment.get("iv30"),
        "iv_pctile_1yr": sentiment.get("iv_pctile_1yr"),
        "implied_move_next_earnings_pct":
            sentiment.get("implied_move_next_earnings_pct"),
    }


def build_momentum_table(tech, bench) -> dict:
    """Momentum-vs-SPY table. rel_3m / rel_12m are computed in-script (never in
    prose); a null in either leg leaves that rel value None."""
    ret3 = tech.get("ret_3m")
    ret6 = tech.get("ret_6m")
    ret12 = tech.get("ret_12m")
    spy3 = bench.get("spy_ret_3m")
    spy12 = bench.get("spy_ret_12m")
    rel3 = _clean(ret3 - spy3) if (ret3 is not None and spy3 is not None) else None
    rel12 = (_clean(ret12 - spy12)
             if (ret12 is not None and spy12 is not None) else None)
    return {
        "ret_3m": ret3,
        "spy_ret_3m": spy3,
        "rel_3m": rel3,
        "ret_6m": ret6,
        "ret_12m": ret12,
        "spy_ret_12m": spy12,
        "rel_12m": rel12,
    }


def hedging_cost_note(iv_pctile_1yr):
    """A hedging-cost note when the 1-yr IV percentile is < 25 (protective
    structures historically cheap), else None."""
    if iv_pctile_1yr is not None and iv_pctile_1yr < 25:
        return ("IV percentile <25 -- protective structures historically cheap; "
                "see options-strategy")
    return None


# --------------------------------------------------------------------------- #
# Composite scoring + renormalization (identical pattern to the other scorers)
# --------------------------------------------------------------------------- #

def score(sentiment, revisions, tech, bench,
          rating_actions, rating_actions_justification,
          inst_flow, inst_flow_justification,
          insider_baseline, insider_baseline_justification) -> dict:
    """Assemble the five subscores and the (possibly renormalized) 0-100 score.

    A dimension whose ``evaluable`` is False (all its scored inputs null) is
    EXCLUDED from the max total and the score is rescaled to 0-100 over the
    remaining max, with ``renormalized: true`` recorded.
    """
    rsi14 = tech.get("rsi14")
    subs = [
        score_street(sentiment, rating_actions, rating_actions_justification),
        score_revisions(revisions if isinstance(revisions, dict) else {}),
        score_smart_money(sentiment, inst_flow, inst_flow_justification,
                          insider_baseline, insider_baseline_justification),
        score_positioning(sentiment, rsi14),
        score_momentum(tech, bench),
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
# CLI (mirrors score_technical.py / score_risk.py; snapshot-only, no ladder)
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _days_to_earnings(snapshot):
    """Return calendar days from the snapshot as_of date to the next earnings
    date, or None if either date is absent/unparseable."""
    from datetime import date
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
    events = snapshot.get("events", {}) if isinstance(snapshot, dict) else {}
    as_of_raw = meta.get("as_of_utc") or ""
    as_of_str = as_of_raw[:10] if isinstance(as_of_raw, str) else ""
    ne = events.get("next_earnings") if isinstance(events, dict) else None
    ne_date_str = (ne.get("date") if isinstance(ne, dict) else None) or ""
    if not as_of_str or not ne_date_str:
        return None
    try:
        as_of = date.fromisoformat(as_of_str[:10])
        ne_date = date.fromisoformat(ne_date_str[:10])
    except ValueError:
        return None
    return (ne_date - as_of).days


def build_module(snapshot, rating_actions, rating_actions_justification,
                 inst_flow, inst_flow_justification,
                 insider_baseline, insider_baseline_justification,
                 bundle_dir=None) -> dict:
    """Build the full module_sentiment.json document from parsed inputs.

    ``bundle_dir`` is threaded to the confidence layer so the staleness axis can
    read a ``refresh_plan.json`` reuse signal when present (absent on fresh runs).
    """
    sentiment = snapshot.get("sentiment", {}) if isinstance(snapshot, dict) else {}
    tech = snapshot.get("technicals", {}) if isinstance(snapshot, dict) else {}
    bench = snapshot.get("benchmark", {}) if isinstance(snapshot, dict) else {}
    fund = snapshot.get("fundamentals", {}) if isinstance(snapshot, dict) else {}
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}

    revisions = fund.get("revisions_90d")

    scored = score(sentiment, revisions, tech, bench,
                   rating_actions, rating_actions_justification,
                   inst_flow, inst_flow_justification,
                   insider_baseline, insider_baseline_justification)

    iv = sentiment.get("iv_pctile_1yr")

    # QF5: when revisions_90d is null and the snapshot is within ~14 days of
    # next_earnings, the renormalization silently removes the most forward-looking
    # signal at exactly the moment it matters most -- surface this loudly.
    revisions_null_reason = fund.get("revisions_null_reason")
    days_to_earnings = _days_to_earnings(snapshot)
    pre_earnings_revisions_warning = None
    if revisions is None and days_to_earnings is not None and 0 <= days_to_earnings <= 14:
        pre_earnings_revisions_warning = (
            f"WARNING: revisions_90d is null within {days_to_earnings}d of next earnings "
            f"-- the 20-pt revisions dimension has been renormalized away at the most "
            f"critical signal window. Null reason: {revisions_null_reason or 'unknown'}. "
            f"Treat the sentiment score as incomplete; do NOT interpret a high score "
            f"as confirmation of positive revision momentum."
        )

    doc = {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "module_note": MODULE_NOTE,
        "ticker": meta.get("ticker"),
        "as_of": build_snapshot._as_of_date(meta.get("as_of_utc")),
        "score": scored["score"],
        "subscores": scored["subscores"],
        "tables": {
            "positioning": build_positioning_table(sentiment),
            "momentum_vs_spy": build_momentum_table(tech, bench),
            "hedging_cost_note": hedging_cost_note(iv),
        },
        "flags": {
            "rating_actions": rating_actions,
            "rating_actions_justification": rating_actions_justification,
            "inst_flow": inst_flow,
            "inst_flow_justification": inst_flow_justification,
            "insider_baseline": insider_baseline,
            "insider_baseline_justification": insider_baseline_justification,
            # QF5: loud disclosure when revisions null near earnings.
            "revisions_null_pre_earnings_warning": pre_earnings_revisions_warning,
        },
        "renormalized": scored["renormalized"],
        "signal": None,
    }
    if scored["renormalization_note"]:
        doc["renormalization_note"] = scored["renormalization_note"]
    # QF5: promote the warning into renormalization_note when it fires.
    if pre_earnings_revisions_warning:
        existing = doc.get("renormalization_note") or ""
        doc["renormalization_note"] = (
            pre_earnings_revisions_warning
            + (" | " + existing if existing else "")
        )
    # Confidence / provenance layer (confidence-v1.0.0): deterministic, disclosure-
    # only. Sentiment scores short_interest (a by-design web input) so its SOURCE
    # axis is MEDIUM at best -- the confidence layer encodes that honestly.
    doc["confidence"] = confidence.compute_module(doc, snapshot, bundle_dir)
    return doc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score the sentiment-positioning dimension for a snapshot "
                    "bundle (rubric v%s)." % RUBRIC_VERSION)
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--rating-actions", default="neutral",
                        choices=_RATING_ACTIONS_CHOICES,
                        help="recent analyst rating-actions judgment "
                             "(non-neutral requires --rating-actions-justification)")
    parser.add_argument("--rating-actions-justification", default=None,
                        help="required whenever --rating-actions is not 'neutral'")
    parser.add_argument("--inst-flow", default="unknown",
                        choices=_INST_FLOW_CHOICES,
                        help="13F institutional-flow judgment "
                             "(non-default values require --inst-flow-justification)")
    parser.add_argument("--inst-flow-justification", default=None,
                        help="required whenever --inst-flow is not 'unknown'")
    parser.add_argument("--insider-baseline", default="normal",
                        choices=_INSIDER_BASELINE_CHOICES,
                        help="baseline read of non-positive insider net "
                             "('unusual' requires --insider-baseline-justification)")
    parser.add_argument("--insider-baseline-justification", default=None,
                        help="required whenever --insider-baseline is 'unusual'")
    parser.add_argument("--out", default=None,
                        help="output path (default <bundle>/module_sentiment.json)")
    args = parser.parse_args(argv)

    if args.rating_actions != "neutral" and not args.rating_actions_justification:
        print("ERROR: --rating-actions-justification is required when "
              "--rating-actions is not 'neutral'", file=sys.stderr)
        return 2
    if args.inst_flow != "unknown" and not args.inst_flow_justification:
        print("ERROR: --inst-flow-justification is required when "
              "--inst-flow is not 'unknown'", file=sys.stderr)
        return 2
    if args.insider_baseline == "unusual" and not args.insider_baseline_justification:
        print("ERROR: --insider-baseline-justification is required when "
              "--insider-baseline is 'unusual'", file=sys.stderr)
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

    doc = build_module(snapshot, args.rating_actions,
                       args.rating_actions_justification,
                       args.inst_flow, args.inst_flow_justification,
                       args.insider_baseline, args.insider_baseline_justification,
                       bundle_dir=args.bundle)

    out = args.out or os.path.join(args.bundle, "module_sentiment.json")
    emit_json(doc, out)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
