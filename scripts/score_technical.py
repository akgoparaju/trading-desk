"""Technical-analysis evidence module for the trading-desk plugin.

WHY THIS MODULE EXISTS: this is the FIRST scored evidence skill, so the arithmetic
below is not merely *an* implementation of a scoring rule -- it IS the rubric of
record (rubric v1.0.0). Every branch is deterministic and unit-pinned so that a
report can never silently drift: the numbers a brief cites all originate here, in
Python, and the version string travels with them into the module JSON and the
brief footer. The LLM layer narrates; it does no scoring arithmetic.

Scoring is over four dimensions (max 100 total):
    1. Trend structure    (30)  -- price/MA stack + MA slopes
    2. Momentum           (25)  -- RSI band (+ optional divergence adj) + MACD state
    3. Structure & levels (25)  -- proven support proximity + resistance headroom
                                   + confluence, all read off the shared S/R ladder
    4. Volume & extension (20)  -- distance above MA200 + volume regime, minus a
                                   vertical-rally penalty

Design contract (project-wide):
- The snapshot is READ-ONLY; this module never edits snapshot.json.
- ``INPUT_FIELDS`` lists exactly the snapshot fields this rubric SCORES on
  (dotted paths). ``price.last`` and the ladder are SHARED reference
  infrastructure and are deliberately excluded (a Task-13 cross-skill test
  imports INPUT_FIELDS to assert dimensions do not double-count a field).
- ``trend_claim`` is a mechanical label emitted for later report-level QC.
- If a WHOLE dimension has zero evaluable inputs, it is excluded and the score is
  renormalized to 0-100 over the remaining max.

Reuses scripts.levels (build_ladder, nearest_support, nearest_resistance) and the
build_snapshot / chain I/O helpers for the CLI, mirroring levels.py's CLI. The
scoring functions are pure over already-parsed inputs. stdlib-only.
"""

import argparse
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation (``python3 scripts/score_technical.py``): ensure the
# repo root is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import build_snapshot, chain, levels

RUBRIC_VERSION = "1.0.0"
SKILL_NAME = "technical-analysis"

# The snapshot fields this rubric SCORES on. price.last and the ladder are shared
# reference infrastructure and are intentionally NOT listed (see module docstring).
INPUT_FIELDS = {
    "technicals.ma50",
    "technicals.ma200",
    "technicals.ma50_slope_20d",
    "technicals.ma200_slope_20d",
    "technicals.rsi14",
    "technicals.macd",
    "technicals.macd_signal",
    "technicals.vol_20d_vs_90d",
    "technicals.ret_15d",
}

# Types the market has actually defended -> eligible as proven support (mirrors
# levels._PROVEN_SUPPORT_TYPES; nearest_support(proven_only=True) enforces this).
_DIVERGENCE_CHOICES = ("none", "bullish", "bearish")


def _fmt(x):
    """Compact number formatting for arithmetic strings (stable across runs)."""
    if x is None:
        return "n/a"
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return f"{x:g}"


# --------------------------------------------------------------------------- #
# 1. Trend structure (max 30)
# --------------------------------------------------------------------------- #

def score_trend(last, tech) -> dict:
    """Price/MA stack (+8/+8/+4) and MA slopes (+5/+5). Null input -> that
    component contributes 0 and is named "n/a" in the arithmetic string.

    An input is "evaluable" if the specific comparison can be made; a dimension
    with NO evaluable comparisons at all is flagged (``evaluable=False``) so the
    caller can renormalize. For trend, ``last`` plus any of ma50/ma200/slopes
    being present makes at least one comparison possible.
    """
    ma50 = tech.get("ma50")
    ma200 = tech.get("ma200")
    s50 = tech.get("ma50_slope_20d")
    s200 = tech.get("ma200_slope_20d")

    pts = 0.0
    parts = []
    evaluable = 0

    # price > ma50 -> +8
    if last is not None and ma50 is not None:
        evaluable += 1
        if last > ma50:
            pts += 8
            parts.append(f"price {_fmt(last)} > ma50 {_fmt(ma50)}: +8")
        else:
            parts.append(f"price {_fmt(last)} <= ma50 {_fmt(ma50)}: +0")
    else:
        parts.append("price>ma50: n/a (+0)")

    # ma50 > ma200 -> +8
    if ma50 is not None and ma200 is not None:
        evaluable += 1
        if ma50 > ma200:
            pts += 8
            parts.append(f"ma50 {_fmt(ma50)} > ma200 {_fmt(ma200)}: +8")
        else:
            parts.append(f"ma50 {_fmt(ma50)} <= ma200 {_fmt(ma200)}: +0")
    else:
        parts.append("ma50>ma200: n/a (+0)")

    # price > ma200 -> +4
    if last is not None and ma200 is not None:
        evaluable += 1
        if last > ma200:
            pts += 4
            parts.append(f"price {_fmt(last)} > ma200 {_fmt(ma200)}: +4")
        else:
            parts.append(f"price {_fmt(last)} <= ma200 {_fmt(ma200)}: +0")
    else:
        parts.append("price>ma200: n/a (+0)")

    # ma50_slope_20d > 0 -> +5
    if s50 is not None:
        evaluable += 1
        if s50 > 0:
            pts += 5
            parts.append(f"ma50_slope_20d {_fmt(s50)} > 0: +5")
        else:
            parts.append(f"ma50_slope_20d {_fmt(s50)} <= 0: +0")
    else:
        parts.append("ma50_slope_20d: n/a (+0)")

    # ma200_slope_20d > 0 -> +5
    if s200 is not None:
        evaluable += 1
        if s200 > 0:
            pts += 5
            parts.append(f"ma200_slope_20d {_fmt(s200)} > 0: +5")
        else:
            parts.append(f"ma200_slope_20d {_fmt(s200)} <= 0: +0")
    else:
        parts.append("ma200_slope_20d: n/a (+0)")

    return {
        "name": "trend_structure",
        "points": _clean(pts),
        "max": 30,
        "arithmetic": "; ".join(parts),
        "inputs": {"ma50": ma50, "ma200": ma200, "ma50_slope_20d": s50,
                   "ma200_slope_20d": s200, "last": last},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 2. Momentum (max 25): RSI (15) + MACD (10)
# --------------------------------------------------------------------------- #

def _rsi_component(rsi, divergence) -> float:
    """RSI sub-score (max 15) with optional divergence adjustment.

    Bands: 45<=rsi<=65 -> 15; 40<=rsi<45 or 65<rsi<=70 -> 12; rsi>70 ->
    max(0, 12-(rsi-70)*0.75); rsi<40 -> max(0, 12-(40-rsi)*0.75). Then:
    bearish divergence AND rsi>65 -> additional -3 (floor 0); bullish divergence
    AND rsi<45 -> +3 (cap 15).
    """
    if 45 <= rsi <= 65:
        base = 15.0
    elif (40 <= rsi < 45) or (65 < rsi <= 70):
        base = 12.0
    elif rsi > 70:
        base = max(0.0, 12 - (rsi - 70) * 0.75)
    else:  # rsi < 40
        base = max(0.0, 12 - (40 - rsi) * 0.75)

    if divergence == "bearish" and rsi > 65:
        base = max(0.0, base - 3)
    elif divergence == "bullish" and rsi < 45:
        base = min(15.0, base + 3)
    return _clean(base)


def _macd_component(macd, signal) -> float:
    """MACD sub-score (max 10): >signal & >0 ->10; >signal & <=0 ->7;
    <=signal & >0 ->4; else 0."""
    if macd > signal and macd > 0:
        return 10.0
    if macd > signal and macd <= 0:
        return 7.0
    if macd <= signal and macd > 0:
        return 4.0
    return 0.0


def score_momentum(tech, divergence, justification) -> dict:
    """Momentum dimension (max 25): RSI band + MACD state.

    Null RSI or null MACD inputs contribute 0 and are named "n/a". The divergence
    flag + justification are recorded in the subscore inputs (also surfaced at the
    module-JSON ``flags`` level by the caller).
    """
    rsi = tech.get("rsi14")
    macd = tech.get("macd")
    signal = tech.get("macd_signal")

    parts = []
    pts = 0.0
    evaluable = 0

    if rsi is not None:
        evaluable += 1
        rsi_pts = _rsi_component(rsi, divergence)
        pts += rsi_pts
        div_note = ""
        if divergence == "bearish" and rsi > 65:
            div_note = " (bearish divergence -3)"
        elif divergence == "bullish" and rsi < 45:
            div_note = " (bullish divergence +3)"
        parts.append(f"rsi {_fmt(rsi)} -> {_fmt(rsi_pts)}/15{div_note}")
    else:
        parts.append("rsi: n/a (+0)")

    if macd is not None and signal is not None:
        evaluable += 1
        macd_pts = _macd_component(macd, signal)
        pts += macd_pts
        parts.append(
            f"macd {_fmt(macd)} vs signal {_fmt(signal)} -> {_fmt(macd_pts)}/10")
    else:
        parts.append("macd: n/a (+0)")

    return {
        "name": "momentum",
        "points": _clean(pts),
        "max": 25,
        "arithmetic": "; ".join(parts),
        "inputs": {"rsi14": rsi, "macd": macd, "macd_signal": signal,
                   "divergence": divergence,
                   "divergence_justification": justification},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 3. Structure & levels (max 25)
# --------------------------------------------------------------------------- #

def score_structure(ladder, last) -> dict:
    """Support proximity (12) + resistance headroom (8) + confluence (5).

    Support: nearest PROVEN support below ``last`` (levels.nearest_support,
    proven_only=True). |pct|<=5% -> 12; 5-10% -> 8; else/none -> 0.
    Resistance: nearest resistance above (levels.nearest_resistance). headroom
    >=5% -> 8; 2-5% -> 4; <2% -> 0; NONE above (ATH blue sky) -> 8.
    Confluence: >=2 ladder entries below ``last`` within 2% (relative) of each
    other -> +5.

    The ladder is always available (it is built even from a bare price series),
    so this dimension is always evaluable.
    """
    parts = []

    # -- support -----------------------------------------------------------
    sup = levels.nearest_support(ladder, last, proven_only=True)
    if sup is not None and last:
        pct = abs(sup["level"] / last - 1)
        if pct <= 0.05:
            support_pts = 12
        elif pct <= 0.10:
            support_pts = 8
        else:
            support_pts = 0
        parts.append(
            f"support {sup['type']} {_fmt(sup['level'])} ({pct*100:.1f}% below)"
            f" -> +{support_pts}")
    else:
        support_pts = 0
        parts.append("no proven support below -> +0")

    # -- resistance --------------------------------------------------------
    res = levels.nearest_resistance(ladder, last)
    if res is None:
        resistance_pts = 8
        parts.append("no resistance above (ATH blue sky) -> +8")
    elif last:
        headroom = res["level"] / last - 1
        if headroom >= 0.05:
            resistance_pts = 8
        elif headroom >= 0.02:
            resistance_pts = 4
        else:
            resistance_pts = 0
        parts.append(
            f"resistance {res['type']} {_fmt(res['level'])}"
            f" ({headroom*100:.1f}% above) -> +{resistance_pts}")
    else:
        resistance_pts = 0
        parts.append("resistance: n/a (+0)")

    # -- confluence --------------------------------------------------------
    below = sorted((e["level"] for e in ladder
                    if last and e["level"] < last), reverse=True)
    confluence_pts = 0
    for i in range(len(below) - 1):
        hi, lo = below[i], below[i + 1]
        if hi and abs(hi - lo) / abs(hi) <= 0.02:
            confluence_pts = 5
            parts.append(
                f"confluence {_fmt(lo)}/{_fmt(hi)} within 2% -> +5")
            break
    if confluence_pts == 0:
        parts.append("no confluence below within 2% -> +0")

    total = support_pts + resistance_pts + confluence_pts
    return {
        "name": "structure_levels",
        "points": min(25, total),
        "max": 25,
        "arithmetic": "; ".join(parts),
        "inputs": {"support_points": support_pts,
                   "resistance_points": resistance_pts,
                   "confluence_points": confluence_pts,
                   "nearest_support": sup["level"] if sup else None,
                   "nearest_resistance": res["level"] if res else None},
        "evaluable": True,
    }


# --------------------------------------------------------------------------- #
# 4. Volume & extension (max 20)
# --------------------------------------------------------------------------- #

def score_volume(last, tech) -> dict:
    """Extension (12) + volume regime (8) minus a vertical-rally penalty.

    Extension: ext = last/ma200 - 1; penalty = max(0, (ext-0.12)*100) points
    (1 pt / 1% above 12%); component = max(0, 12-penalty). Volume:
    0.8<=vol_20d_vs_90d<=1.5 -> 8; >1.5 -> 5; <0.8 -> 4; null -> 0 ("n/a").
    Vertical-rally: ret_15d > 0.12 -> -4 off this dimension's total (floor 0).
    """
    ma200 = tech.get("ma200")
    vol = tech.get("vol_20d_vs_90d")
    ret15 = tech.get("ret_15d")

    parts = []
    evaluable = 0

    # -- extension ---------------------------------------------------------
    if last is not None and ma200 not in (None, 0):
        evaluable += 1
        ext = last / ma200 - 1
        penalty = max(0.0, (ext - 0.12) * 100)
        extension_pts = _clean(max(0.0, 12 - penalty))
        parts.append(
            f"ext {ext*100:.1f}% (last/ma200 {_fmt(last / ma200)}) "
            f"-> {_fmt(extension_pts)}/12")
    else:
        extension_pts = 0
        parts.append("extension: n/a (+0)")

    # -- volume ------------------------------------------------------------
    if vol is not None:
        evaluable += 1
        if 0.8 <= vol <= 1.5:
            volume_pts = 8
        elif vol > 1.5:
            volume_pts = 5
        else:  # < 0.8
            volume_pts = 4
        parts.append(f"vol_20d_vs_90d {_fmt(vol)} -> {volume_pts}/8")
    else:
        volume_pts = 0
        parts.append("volume: n/a (+0)")

    total = extension_pts + volume_pts

    # -- vertical-rally penalty (off the dimension total) ------------------
    vertical_penalty = 0
    if ret15 is not None and ret15 > 0.12:
        vertical_penalty = -4
        parts.append(f"ret_15d {_fmt(ret15)} > 0.12 -> -4 (vertical rally)")
    total = max(0.0, total + vertical_penalty)

    return {
        "name": "volume_extension",
        "points": _clean(total),
        "max": 20,
        "arithmetic": "; ".join(parts),
        "inputs": {"extension_points": extension_pts,
                   "volume_points": volume_pts,
                   "vertical_rally_penalty": vertical_penalty,
                   "ma200": ma200, "vol_20d_vs_90d": vol, "ret_15d": ret15},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# trend_claim (mechanical label)
# --------------------------------------------------------------------------- #

def trend_claim(last, tech) -> str:
    """uptrend if last>ma50>ma200; downtrend if last<ma50<ma200; else sideways.
    Any null in the chain -> sideways (the strict chain cannot be established)."""
    ma50 = tech.get("ma50")
    ma200 = tech.get("ma200")
    if None in (last, ma50, ma200):
        return "sideways"
    if last > ma50 > ma200:
        return "uptrend"
    if last < ma50 < ma200:
        return "downtrend"
    return "sideways"


# --------------------------------------------------------------------------- #
# Composite scoring + renormalization
# --------------------------------------------------------------------------- #

def _clean(x):
    """Normalize a numeric to int when integral, else round to 4 dp for stable
    JSON. Keeps 0.75, 6.0->6, 13.5 exact while avoiding float noise."""
    if x is None:
        return None
    xf = float(x)
    if xf.is_integer():
        return int(xf)
    return round(xf, 4)


def score(last, tech, ladder, divergence, justification) -> dict:
    """Assemble the four subscores and the (possibly renormalized) 0-100 score.

    A dimension whose ``evaluable`` is False (all its scored inputs null) is
    EXCLUDED from the max total and the score is rescaled to 0-100 over the
    remaining max, with ``renormalized: true`` recorded.
    """
    subs = [
        score_trend(last, tech),
        score_momentum(tech, divergence, justification),
        score_structure(ladder, last),
        score_volume(last, tech),
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
        "trend_claim": trend_claim(last, tech),
        "renormalized": renormalized,
        "renormalization_note": note,
    }


# --------------------------------------------------------------------------- #
# CLI (mirrors levels.py: newest snapshot, manifest-loaded rows + chain)
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def build_module(snapshot, rows, contracts, divergence, justification) -> dict:
    """Build the full module_technical.json document from parsed inputs."""
    price = snapshot.get("price", {}) if isinstance(snapshot, dict) else {}
    tech = snapshot.get("technicals", {}) if isinstance(snapshot, dict) else {}
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
    last = price.get("last")

    ladder = levels.build_ladder(snapshot, rows, contracts=contracts)
    scored = score(last, tech, ladder, divergence, justification)

    doc = {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "ticker": meta.get("ticker"),
        "as_of": build_snapshot._as_of_date(meta.get("as_of_utc")),
        "score": scored["score"],
        "subscores": scored["subscores"],
        "trend_claim": scored["trend_claim"],
        "ladder": ladder,
        "flags": {
            "divergence": divergence,
            "divergence_justification": justification,
        },
        "renormalized": scored["renormalized"],
        "signal": None,
    }
    if scored["renormalization_note"]:
        doc["renormalization_note"] = scored["renormalization_note"]
    return doc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score the technical-analysis dimension for a snapshot "
                    "bundle (rubric v%s)." % RUBRIC_VERSION)
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--divergence", default="none",
                        choices=_DIVERGENCE_CHOICES,
                        help="RSI divergence flag (requires justification if set)")
    parser.add_argument("--divergence-justification", default=None,
                        help="required whenever --divergence != none")
    parser.add_argument("--out", default=None,
                        help="output path (default <bundle>/module_technical.json)")
    args = parser.parse_args(argv)

    if args.divergence != "none" and not args.divergence_justification:
        print("ERROR: --divergence-justification is required when "
              "--divergence is not 'none'", file=sys.stderr)
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

    try:
        rows, contracts = levels._load_rows_and_contracts(args.bundle)
    except (build_snapshot.BuildError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    doc = build_module(snapshot, rows, contracts,
                       args.divergence, args.divergence_justification)

    out = args.out or os.path.join(args.bundle, "module_technical.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
