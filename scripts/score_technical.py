"""Technical-analysis evidence module for the trading-desk plugin.

WHY THIS MODULE EXISTS: this is the FIRST scored evidence skill, so the arithmetic
below is not merely *an* implementation of a scoring rule -- it IS the rubric of
record (rubric v1.0.0). Every branch is deterministic and unit-pinned so that a
report can never silently drift: the numbers a brief cites all originate here, in
Python, and the version string travels with them into the module JSON and the
brief footer. The LLM layer narrates; it does no scoring arithmetic.

Scoring is over four dimensions (max 100 total). TOP-LEVEL WEIGHTS ARE UNCHANGED
from v1.0.0 (Trend 30 / Momentum 25 / Structure 25 / Volume 20); rubric v1.1.0
(Wave 4A, R5/B28) enriches the SUB-splits and adds a regime GUARD -- it does NOT
re-weight the four dimensions:
    1. Trend structure    (30)  -- price/MA stack + MA slopes
    2. Momentum           (25)  -- RSI band (+ optional divergence adj) + MACD
                                   state, REGIME-CONDITIONED by adx14 + stage
                                   (v1.1.0): choppy regime (adx<20) halves the
                                   MACD sub; a declining stage-4 caps the RSI
                                   healthy-band bonus. Band SHAPES unchanged --
                                   the guard modulates the points.
    3. Structure & levels (25)  -- proven support proximity + resistance headroom
                                   + confluence, all read off the shared S/R
                                   ladder. v1.1.0: the ladder now carries
                                   anchored-VWAP level candidates (institutional
                                   cost basis); support proximity accepts them as
                                   defended levels. No new points -- more level
                                   candidates.
    4. Volume & extension (20)  -- v1.1.0 RE-SPLIT: extension (10) + volume regime
                                   (5) + A/D-line slope (3) + up/down volume (2),
                                   minus a vertical-rally penalty. Band SHAPES of
                                   the retained extension + vol-regime sub-scores
                                   are unchanged, only scaled; A/D + upvol add
                                   accumulation/distribution quality. Null sub-
                                   components renormalize WITHIN the factor (the
                                   factor is NOT zeroed).

REGIME AS GUARD (v1.1.0, PROVISIONAL): adx14 + stage are GUARD/modulator fields
(``GUARD_FIELDS``) -- they change how many points an existing momentum sub-score
earns, they do NOT add a new scored factor and they earn no points themselves. The
thresholds (ADX<20 = no-trend; stage-4 = declining) are documented Philosophy-A
defaults, unratified pending B9 calibration; a falsifier is pre-registered in the
SKILL. Null adx/stage -> NO modulation (graceful): a v1.0.0 snapshot scores
byte-for-byte as before on the momentum guard.

Design contract (project-wide):
- The snapshot is READ-ONLY; this module never edits snapshot.json.
- ``INPUT_FIELDS`` lists exactly the snapshot fields this rubric SCORES on (earns
  points from) as dotted paths. ``GUARD_FIELDS`` lists fields that only MODULATE a
  scoring branch (adx14/stage) and earn no points -- they must be DISJOINT from
  ``INPUT_FIELDS`` (a governance test asserts this) and mirror the sentiment
  scorer's rsi14-guard precedent. ``price.last`` and the ladder are SHARED
  reference infrastructure and are deliberately excluded from both. A Task-13
  cross-skill test imports INPUT_FIELDS to assert dimensions do not double-count a
  field. The anchored-VWAP fields feed the LADDER (built in levels.py), not
  INPUT_FIELDS directly.
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

from scripts import build_snapshot, chain, confidence, levels

RUBRIC_VERSION = "1.1.0"
SKILL_NAME = "technical-analysis"

# Module note stamped on every doc: rubric v1.1.0 is PROVISIONAL (Wave 4A / R5 /
# B28) -- the regime guard thresholds (adx/stage) and the A/D + upvol volume-
# quality bands are unratified pending B9 calibration; a falsifier is pre-
# registered in the SKILL.
MODULE_NOTE = ("technical-v1.1.0 PROVISIONAL -- regime guard + volume-quality "
               "bands unratified pending B9; falsifier pre-registered")

# The snapshot fields this rubric SCORES on (earns points from). price.last and
# the ladder are shared reference infrastructure and are intentionally NOT listed
# (see module docstring). v1.1.0 adds ad_line_slope + upvol_ratio (the new SCORED
# volume-quality sub-components). The anchored-VWAP fields (vwap_52wk_high /
# vwap_earnings) are NOT here: they feed the shared ladder (levels.py), not a
# scored branch directly.
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
    "technicals.ad_line_slope",
    "technicals.upvol_ratio",
}

# Fields that only MODULATE (guard/cap) a scoring branch -- they earn NO points
# themselves, so they are NOT scored inputs. v1.1.0's momentum guard reads
# ``adx14`` (choppy-regime MACD discount) and ``stage`` (stage-4 RSI cap). A
# governance test (test_single_mapping) asserts GUARD_FIELDS is DISJOINT from
# INPUT_FIELDS (a field is either scored xor a pure guard, never both here). This
# mirrors the sentiment scorer's ``GUARD_FIELDS = {"technicals.rsi14"}``.
GUARD_FIELDS = {
    "technicals.adx14",
    "technicals.stage",
}

# Regime-guard thresholds (v1.1.0, PROVISIONAL -- documented Philosophy-A
# defaults, unratified pending B9). ADX below this reads as no-trend/choppy (the
# widely-cited ADX<20 = no-trend default); the declining Weinstein stage.
_ADX_CHOPPY = 20.0
_STAGE_DECLINING = 4
# In a choppy regime the MACD sub-component is DISCOUNTED (multiplied) by this.
_MACD_CHOPPY_FACTOR = 0.5
# In a declining stage-4 regime the RSI sub-component is CAPPED at this (the
# healthy-band bonus is not rewarded in a downtrend; the full 15 falls to 12,
# readings already <=12 are unaffected).
_RSI_STAGE4_CAP = 12.0

_DIVERGENCE_CHOICES = ("none", "bullish", "bearish")

# Anchored-VWAP level types (v1.1.0): institutional cost-basis lines the market has
# transacted at -> eligible as DEFENDED support alongside levels._PROVEN_SUPPORT_TYPES.
# The ladder (levels.build_ladder) already MINTS these when the snapshot carries
# vwap_52wk_high / vwap_earnings; the structure scorer just accepts them as proven
# support candidates. Resistance + confluence already read the ladder generically,
# so a VWAP above price registers as resistance with no change.
_VWAP_SUPPORT_TYPES = {"vwap_52wk_high", "vwap_earnings"}
# The proven-support set the STRUCTURE scorer accepts (base proven + anchored VWAP).
_STRUCTURE_PROVEN_SUPPORT = set(levels._PROVEN_SUPPORT_TYPES) | _VWAP_SUPPORT_TYPES


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
    """Momentum dimension (max 25): RSI band + MACD state, REGIME-CONDITIONED.

    Band SHAPES are unchanged from v1.0.0; a v1.1.0 GUARD (adx14 + stage) modulates
    how many points the sub-scores earn:
      - ``adx14`` < 20 (choppy / no-trend): the MACD sub-component is DISCOUNTED
        BY HALF (momentum signals are noise in a rangebound regime).
      - ``stage`` == 4 (Weinstein declining): the RSI sub-component is CAPPED at 12
        (a "healthy" RSI is not rewarded in a downtrend; the full-band 15 falls to
        12, readings already <=12 are unaffected).
    Both guards are PROVISIONAL (documented thresholds, unratified pending B9).
    Null adx14 / null stage -> NO modulation (graceful): identical to v1.0.0.

    Null RSI or null MACD inputs contribute 0 and are named "n/a". The divergence
    flag + justification are recorded in the subscore inputs (also surfaced at the
    module-JSON ``flags`` level by the caller).
    """
    rsi = tech.get("rsi14")
    macd = tech.get("macd")
    signal = tech.get("macd_signal")
    adx = tech.get("adx14")
    stage = tech.get("stage")

    # Regime-guard predicates (null -> guard inactive -> no modulation).
    choppy = adx is not None and adx < _ADX_CHOPPY
    declining = stage == _STAGE_DECLINING

    parts = []
    pts = 0.0
    evaluable = 0

    if rsi is not None:
        evaluable += 1
        rsi_pts = _rsi_component(rsi, divergence)
        div_note = ""
        if divergence == "bearish" and rsi > 65:
            div_note = " (bearish divergence -3)"
        elif divergence == "bullish" and rsi < 45:
            div_note = " (bullish divergence +3)"
        # stage-4 guard: cap the RSI healthy-band bonus (does not reward a healthy
        # RSI in a downtrend). Only bites when the earned points exceed the cap.
        stage_note = ""
        if declining and rsi_pts > _RSI_STAGE4_CAP:
            rsi_pts = _clean(_RSI_STAGE4_CAP)
            stage_note = f" (stage-4 cap -> {_fmt(_RSI_STAGE4_CAP)})"
        pts += rsi_pts
        parts.append(f"rsi {_fmt(rsi)} -> {_fmt(rsi_pts)}/15{div_note}{stage_note}")
    else:
        parts.append("rsi: n/a (+0)")

    if macd is not None and signal is not None:
        evaluable += 1
        macd_pts = _macd_component(macd, signal)
        # choppy guard: discount the MACD sub-component by half (unreliable in a
        # rangebound, no-trend regime -- cited ADX<20 = no-trend default).
        chop_note = ""
        if choppy:
            macd_pts = _clean(macd_pts * _MACD_CHOPPY_FACTOR)
            chop_note = f" (adx {_fmt(adx)} < {_fmt(_ADX_CHOPPY)} choppy: x{_fmt(_MACD_CHOPPY_FACTOR)})"
        pts += macd_pts
        parts.append(
            f"macd {_fmt(macd)} vs signal {_fmt(signal)} -> {_fmt(macd_pts)}/10"
            f"{chop_note}")
    else:
        parts.append("macd: n/a (+0)")

    return {
        "name": "momentum",
        "points": _clean(pts),
        "max": 25,
        "arithmetic": "; ".join(parts),
        "inputs": {"rsi14": rsi, "macd": macd, "macd_signal": signal,
                   "adx14": adx, "stage": stage,
                   "regime_choppy": choppy, "regime_declining": declining,
                   "divergence": divergence,
                   "divergence_justification": justification},
        "evaluable": evaluable > 0,
    }


# --------------------------------------------------------------------------- #
# 3. Structure & levels (max 25)
# --------------------------------------------------------------------------- #

def _nearest_structure_support(ladder, last):
    """Highest ladder entry strictly BELOW ``last`` whose type is a
    STRUCTURE-proven support (base proven types + anchored-VWAP cost-basis lines).

    v1.1.0 widens levels.nearest_support's proven set to accept anchored VWAPs as
    defended support without editing levels.py (institutional cost basis is a
    genuinely-transacted level, not a merely-projected one). No new points -- it
    just lets a VWAP register in the SAME support-proximity band the ma/swing
    levels already use.
    """
    if last is None:
        return None
    candidates = [e for e in ladder
                  if e["level"] < last and e["type"] in _STRUCTURE_PROVEN_SUPPORT]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e["level"])


def score_structure(ladder, last) -> dict:
    """Support proximity (12) + resistance headroom (8) + confluence (5).

    Support: nearest STRUCTURE-proven support below ``last`` -- the base proven
    types PLUS anchored-VWAP cost-basis lines (v1.1.0). |pct|<=5% -> 12; 5-10% ->
    8; else/none -> 0.
    Resistance: nearest resistance above (levels.nearest_resistance, any type --
    so a VWAP above price registers). headroom >=5% -> 8; 2-5% -> 4; <2% -> 0;
    NONE above (ATH blue sky) -> 8.
    Confluence: >=2 ladder entries below ``last`` within 2% (relative) of each
    other -> +5 (reads the whole ladder generically -- VWAP levels can contribute).

    The ladder is always available (it is built even from a bare price series),
    so this dimension is always evaluable.
    """
    parts = []

    # -- support -----------------------------------------------------------
    sup = _nearest_structure_support(ladder, last)
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

# v1.1.0 volume-factor sub-splits (sum to 20). Extension + vol-regime keep their
# v1.0.0 BAND SHAPES, scaled to the new maxes; A/D-line + upvol are new quality
# bands. The full-mark maxes:
_EXT_MAX = 10.0        # was 12; scaled band shape
_VOLREG_MAX = 5.0      # was 8;  scaled band shape
_AD_MAX = 3.0          # new: accumulation/distribution from A/D-line slope
_UPVOL_MAX = 2.0       # new: up-day volume share


def score_volume(last, tech) -> dict:
    """v1.1.0 re-split (extension 10 + vol-regime 5 + A/D 3 + upvol 2 = 20), minus a
    vertical-rally penalty.

    - Extension (10): ext = last/ma200 - 1; penalty = max(0, (ext-0.12)*100) points
      (1 pt / 1% above 12%, v1.0.0 shape); component = max(0, 10 - penalty*10/12)
      (the v1.0.0 0-12 band scaled to 0-10).
    - Vol-regime (5): 0.8<=vol<=1.5 -> 5; >1.5 -> 3.125; <0.8 -> 2.5 (the v1.0.0
      8/5/4 bands scaled by 5/8, same ordering/shape).
    - A/D-line (3): ad_line_slope > 0 -> 3 (accumulation); == 0 (flat) -> 2;
      < 0 -> 0 (distribution).
    - Upvol (2): upvol_ratio > 0.55 -> 2; in [0.45, 0.55] -> 1; < 0.45 -> 0.
    - Null sub-component -> "n/a", EXCLUDED and the factor is RENORMALIZED over the
      present sub-maxes back to 20 (the factor is NOT zeroed -- a missing A/D slope
      does not blank the whole volume read).
    - Vertical-rally: ret_15d > 0.12 -> -4 off the (renormalized) factor (floor 0).
    """
    ma200 = tech.get("ma200")
    vol = tech.get("vol_20d_vs_90d")
    ret15 = tech.get("ret_15d")
    ad_slope = tech.get("ad_line_slope")
    upvol = tech.get("upvol_ratio")

    parts = []
    evaluable = 0
    present_pts = 0.0   # points earned over PRESENT sub-components
    present_max = 0.0   # summed max over PRESENT sub-components

    # -- extension (max 10) ------------------------------------------------
    if last is not None and ma200 not in (None, 0):
        evaluable += 1
        ext = last / ma200 - 1
        penalty = max(0.0, (ext - 0.12) * 100)
        extension_pts = _clean(max(0.0, _EXT_MAX - penalty * (_EXT_MAX / 12.0)))
        present_pts += extension_pts
        present_max += _EXT_MAX
        parts.append(
            f"ext {ext*100:.1f}% (last/ma200 {_fmt(last / ma200)}) "
            f"-> {_fmt(extension_pts)}/{_fmt(_EXT_MAX)}")
    else:
        extension_pts = None
        parts.append("extension: n/a")

    # -- volume regime (max 5) ---------------------------------------------
    if vol is not None:
        evaluable += 1
        if 0.8 <= vol <= 1.5:
            volume_pts = _clean(_VOLREG_MAX)
        elif vol > 1.5:
            volume_pts = _clean(5.0 * (_VOLREG_MAX / 8.0))   # was 5/8 band
        else:  # < 0.8
            volume_pts = _clean(4.0 * (_VOLREG_MAX / 8.0))   # was 4/8 band
        present_pts += volume_pts
        present_max += _VOLREG_MAX
        parts.append(f"vol_20d_vs_90d {_fmt(vol)} -> {_fmt(volume_pts)}/{_fmt(_VOLREG_MAX)}")
    else:
        volume_pts = None
        parts.append("volume: n/a")

    # -- A/D-line slope (max 3): accumulation vs distribution --------------
    if ad_slope is not None:
        evaluable += 1
        if ad_slope > 0:
            ad_pts = 3
        elif ad_slope == 0:
            ad_pts = 2
        else:  # < 0
            ad_pts = 0
        present_pts += ad_pts
        present_max += _AD_MAX
        parts.append(f"ad_line_slope {_fmt(ad_slope)} -> {ad_pts}/{_fmt(_AD_MAX)}")
    else:
        ad_pts = None
        parts.append("ad_line: n/a")

    # -- up/down volume (max 2) --------------------------------------------
    if upvol is not None:
        evaluable += 1
        if upvol > 0.55:
            upvol_pts = 2
        elif upvol >= 0.45:   # [0.45, 0.55]
            upvol_pts = 1
        else:  # < 0.45
            upvol_pts = 0
        present_pts += upvol_pts
        present_max += _UPVOL_MAX
        parts.append(f"upvol_ratio {_fmt(upvol)} -> {upvol_pts}/{_fmt(_UPVOL_MAX)}")
    else:
        upvol_pts = None
        parts.append("upvol: n/a")

    # -- renormalize present sub-components back to the factor max (20) -----
    factor_max = _EXT_MAX + _VOLREG_MAX + _AD_MAX + _UPVOL_MAX  # 20
    renorm = present_max not in (0.0, factor_max)
    if present_max <= 0:
        total = 0.0
    elif renorm:
        total = _clean(present_pts / present_max * factor_max)
        parts.append(
            f"renormalized over present max {_fmt(present_max)} -> "
            f"{_fmt(total)}/{_fmt(factor_max)}")
    else:
        total = _clean(present_pts)

    # -- vertical-rally penalty (off the factor total) ---------------------
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
                   "ad_line_points": ad_pts,
                   "upvol_points": upvol_pts,
                   "vertical_rally_penalty": vertical_penalty,
                   "renormalized": renorm,
                   "ma200": ma200, "vol_20d_vs_90d": vol, "ret_15d": ret15,
                   "ad_line_slope": ad_slope, "upvol_ratio": upvol},
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


def build_module(snapshot, rows, contracts, divergence, justification,
                 bundle_dir=None) -> dict:
    """Build the full module_technical.json document from parsed inputs.

    ``bundle_dir`` is threaded to the confidence layer so the staleness axis can
    read a ``refresh_plan.json`` reuse signal when present (absent on fresh runs).
    """
    price = snapshot.get("price", {}) if isinstance(snapshot, dict) else {}
    tech = snapshot.get("technicals", {}) if isinstance(snapshot, dict) else {}
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
    last = price.get("last")

    ladder = levels.build_ladder(snapshot, rows, contracts=contracts)
    scored = score(last, tech, ladder, divergence, justification)

    doc = {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "module_note": MODULE_NOTE,
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
    # Confidence / provenance layer (confidence-v1.0.0): deterministic, disclosure-
    # only, computed from source/depth/staleness of THIS module's own doc + snapshot.
    doc["confidence"] = confidence.compute_module(doc, snapshot, bundle_dir)
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
                       args.divergence, args.divergence_justification,
                       bundle_dir=args.bundle)

    out = args.out or os.path.join(args.bundle, "module_technical.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
