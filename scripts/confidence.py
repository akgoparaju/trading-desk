"""Confidence / provenance layer for the trading-desk plugin (confidence-v1.0.0).

WHY THIS MODULE EXISTS: every report -- premium or degraded -- must ship a
per-module confidence badge plus a composite roll-up, computed DETERMINISTICALLY
by script (never LLM judgment). This module is the versioned rubric of record for
that computation: the SINGLE SOURCE OF TRUTH for how the three axes (source, depth,
staleness) map onto LOW/MEDIUM/HIGH and how they combine (the weakest link -- an
ordinal ``min``). It turns "no MCP -> medium/low" into a first-class, versioned
artifact and turns rubric maturity into an honest, visible signal ("this dimension
is still shallow" until its R-wave lands).

Design contract (project-wide):
- DETERMINISTIC. The ONLY arithmetic here is the ordinal ``min`` over levels; there
  is no scoring, no weighting, no float math. The LLM layer narrates; it does no
  confidence arithmetic.
- READ-ONLY over existing fields. This layer reads existing snapshot / module-doc
  fields only; it never fetches, never edits the snapshot, and drives no schema
  change (spec scope decision 1: no snapshot schema change).
- DISCLOSURE, NOT GOVERNOR (v1.0.0). Confidence is displayed; it does NOT change any
  score, weight, EV, or size (spec scope decision 2).
- WORD-ONLY tags. Every ``why`` string is digit-free -- a report_qc
  ``number_provenance`` constraint (report_qc.py:70 ``_NUM_RE`` matches any
  leading-digit token). Spell counts as words or omit them. Asserted in tests.

The model (per spec ``confidence-v1.0.0``): each module (and the composite) gains a
``confidence`` block::

    {
      "level": "HIGH|MEDIUM|LOW",
      "source":    {"level": "...", "why": "<digit-free tag>"},
      "depth":     {"level": "...", "why": "<digit-free tag>"},
      "staleness": {"level": "...", "why": "<digit-free tag>"},
      "rule": "min(source, depth, staleness)",
      "version": "1.0.0"
    }

``level = min(source.level, depth.level, staleness.level)`` with ordering
``LOW < MEDIUM < HIGH``.

stdlib-only.
"""

import json
import os

CONFIDENCE_VERSION = "1.0.0"

RULE = "min(source, depth, staleness)"

# --------------------------------------------------------------------------- #
# Ordinal level machinery. LOW < MEDIUM < HIGH; the ONLY arithmetic is min.
# --------------------------------------------------------------------------- #

LOW = "LOW"
MEDIUM = "MEDIUM"
HIGH = "HIGH"

_LEVELS = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
_LEVELS_INV = {0: "LOW", 1: "MEDIUM", 2: "HIGH"}


def _min_level(*levels):
    """Ordinal min over LOW/MEDIUM/HIGH (the weakest link). None inputs are
    ignored; an all-None call returns None (no evidence to combine)."""
    ordinals = [_LEVELS[lv] for lv in levels if lv is not None]
    if not ordinals:
        return None
    return _LEVELS_INV[min(ordinals)]


# --------------------------------------------------------------------------- #
# SOURCE axis inputs.
# --------------------------------------------------------------------------- #

# run-level data_mode enum (build_snapshot.py:1158 default "alpha_vantage").
_MODE_PREMIUM = "alpha_vantage"
_MODE_DEGRADED = "av_free_degraded"
_MODE_WEB_FALLBACK = "web_fallback"

# The ONE by-design web-scored input per module: sentiment SCORES short_interest,
# which is a web-transcribed input by design (build_snapshot.py:65 routes
# ``short_interest`` into the sentiment section). Because a scored input is a web
# transcription, sentiment source can never be HIGH -- it is MEDIUM at best. This
# is a reviewable, easily-edited map: add a module -> [fields] row if a future
# rubric scores another by-design web input.
_SOURCE_BY_DESIGN_WEB = {"sentiment": ["short_interest"]}


# --------------------------------------------------------------------------- #
# DEPTH axis -- GOVERNED BELIEF, cited, reviewable (spec Section "DEPTH axis").
# --------------------------------------------------------------------------- #
#
# Depth answers: "has this module received its institutional-depth pass?" It is a
# BELIEF, not a measurement -- so it lives here as an explicit, commented, versioned
# constant that is trivial to review and edit (a one-line change per R-wave).
#
# RATIONALE (cited): docs/reviews/2026-07-19-analysis-quality-review.md, Part 1
# "depth-asymmetry" finding -- ONLY the fundamental dimension is deep (v1.2 anchored
# is two rubric versions ahead); technical / sentiment / risk are still mechanical
# snapshot-band scorers at rubric 1.0.0, awaiting their R-waves (R5/R3/R1). This map
# encodes that asymmetry honestly: fundamental-anchored reads HIGH, everything else
# reads MEDIUM until its rubric bumps to 1.1.0.
#
# Each row PROMOTES TO HIGH at rubric 1.1.0 (one disclosed, one-line table edit per
# R-wave):
#   - technical  -> HIGH at rubric 1.1.0  (R5 / B28: regime-conditional scoring)
#   - sentiment  -> HIGH at rubric 1.1.0  (R3 / B25: positioning dynamics)
#   - risk       -> HIGH at rubric 1.1.0  (R1 / B24: event-aware scoring)
# Fundamental depth is keyed on MODE, not rubric version:
#   - coverage_anchored_pass  -> HIGH  (already deep; the v1.2 anchored pass)
#   - compressed_snapshot_pass-> MEDIUM (snapshot-only floor; promotes when
#                                         anchored coverage is present)
#
# Depth NEVER returns LOW -- a scored module is at least MEDIUM (a shallow rubric is
# still a real, versioned scorer). This is THE ONE reviewable judgment in the layer:
# a different initial assignment is a one-line edit to the rows below.
#
# Keyed by module name. For evidence scorers the key is (module, rubric_version);
# for fundamental the key is (module, fundamental_mode). Read via _depth_level.
DEPTH_TABLE = {
    # module -> { discriminator -> (level, why-tag) }
    "fundamental": {
        # keyed on fundamental_mode
        "coverage_anchored_pass": (HIGH, "anchored coverage"),
        "compressed_snapshot_pass": (MEDIUM, "snapshot pass"),
    },
    "technical": {
        # keyed on rubric_version.
        # v1.0.0: pre-regime mechanical band scorer -> MEDIUM.
        # v1.1.0: regime-conditional (adx/stage guard + A/D + upvol + anchored-VWAP
        #   levels) -> HIGH. This IS a promotion. Unlike sentiment (source capped at
        #   MEDIUM by web-transcribed short_interest) and risk (depth held MEDIUM
        #   while its 1.1.0 is provisional-pending-B9), technical's SOURCE is
        #   AV-premium and NOT web-dependent, so HIGH is honest: source HIGH + depth
        #   HIGH + staleness HIGH -> overall HIGH on a fresh premium build. The
        #   1.1.0 rubric itself is PROVISIONAL (thresholds unratified pending B9),
        #   but the DEPTH axis here answers "has the regime-depth pass landed?" --
        #   it has (R5/B28) -- which is a distinct question from threshold
        #   calibration; the provisional status travels in the module_note + SKILL
        #   falsifier, not by suppressing the depth badge.
        "1.0.0": (MEDIUM, "pre-regime"),
        "1.1.0": (HIGH, "regime-conditional depth"),
        # v1.2.0 (Track O4): adds a PROVISIONAL sector-relative RS factor. The
        # DEPTH TIER is UNCHANGED from 1.1.0 -- still HIGH (the regime-depth pass
        # already landed; adding one more provisional factor does not lower the
        # depth badge, and promoting anything is a SEPARATE gated task). This
        # explicit row exists only to DISCLOSE the new provisional factor in the
        # why-tag (it overrides the generic "rubric past 1.0.0 -> HIGH" fallthrough
        # so the disclosure travels). The sector-RS bands are unratified pending B9;
        # that provisional status travels in the module_note + SKILL falsifier, not
        # by suppressing this depth badge (same reasoning as the 1.1.0 row).
        "1.2.0": (HIGH, "regime-conditional depth; sector-RS provisional"),
    },
    "sentiment": {
        # keyed on rubric_version.
        # v1.0.0: pre-positioning-dynamics mechanical band scorer -> MEDIUM.
        # v1.1.0: positioning-aware (news_heat/skew/DTC/volume-P/C + insider CMP)
        #   BUT PROVISIONAL (unratified pending B9 calibration; falsifier pre-
        #   registered). sentiment 1.1.0 stays MEDIUM while provisional -- it does
        #   NOT auto-promote to HIGH via the generic "rubric past 1.0.0 -> HIGH"
        #   fallthrough (this explicit row overrides it). But note this is moot for
        #   the OVERALL badge: sentiment SOURCE is STRUCTURALLY CAPPED AT MEDIUM
        #   (it scores short_interest, a web-transcribed input BY DESIGN -- see
        #   _source_axis / _SOURCE_BY_DESIGN_WEB), so min(source, depth, staleness)
        #   is MEDIUM at best REGARDLESS of depth. Do NOT promote sentiment to HIGH.
        "1.0.0": (MEDIUM, "pre-positioning-dynamics"),
        "1.1.0": (MEDIUM, "provisional positioning-aware"),
    },
    "risk": {
        # keyed on rubric_version.
        # v1.0.0: pre-event-aware mechanical scorer -> MEDIUM.
        # v1.1.0: event-aware BUT PROVISIONAL (unratified pending B9 calibration;
        #   falsifier pre-registered). risk 1.1.0 stays MEDIUM while provisional;
        #   promote to HIGH only on B9 ratification. An event-aware score that
        #   reads HIGH before its calibration set has confirmed the weights/bands
        #   would be exactly the dishonesty the confidence layer exists to prevent.
        #   (The generic "rubric past 1.0.0 -> HIGH" fallthrough is DELIBERATELY
        #   overridden by this explicit row so 1.1.0 does not auto-promote.)
        "1.0.0": (MEDIUM, "pre-event-aware"),
        "1.1.0": (MEDIUM, "provisional event-aware"),
    },
}

# Skill-name -> DEPTH_TABLE module key. The scorers stamp doc["skill"] with their
# skill name; map it onto the depth-table module key.
_SKILL_TO_MODULE = {
    "technical-analysis": "technical",
    "risk-analytics": "risk",
    "sentiment-positioning": "sentiment",
    "fundamental": "fundamental",
}

# The price-sensitive modules the STALENESS axis applies to (technical, risk,
# sentiment). Fundamental staleness = coverage freshness (handled separately).
_PRICE_SENSITIVE = {"technical", "risk", "sentiment"}

# Refresh-plan groups that feed the price-sensitive modules. A reused (in-window)
# price group -> MEDIUM staleness; a reused at/over-window group -> LOW. Windows are
# reused from qc._STALENESS_WINDOWS (single source of truth) via _price_group_window.
_PRICE_GROUPS = {
    "global_quote", "daily_adjusted", "spy_daily_adjusted",
    "options_chain", "pc_ratio_realtime",
}


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #

def _module_key(module_doc):
    """The DEPTH_TABLE / axis module key for a module doc (via doc['skill'])."""
    skill = module_doc.get("skill") if isinstance(module_doc, dict) else None
    return _SKILL_TO_MODULE.get(skill)


def _as_of_date(meta):
    """The YYYY-MM-DD date of the snapshot as_of instant (meta.as_of_utc)."""
    au = meta.get("as_of_utc") if isinstance(meta, dict) else None
    return au[:10] if isinstance(au, str) and len(au) >= 10 else None


def _load_refresh_plan(bundle_dir):
    """Load ``refresh_plan.json`` from a bundle dir, or None (fresh runs have none)."""
    if not bundle_dir:
        return None
    path = os.path.join(bundle_dir, "refresh_plan.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _price_group_window(group):
    """Staleness window (days) for a refresh-plan group, from qc._STALENESS_WINDOWS.

    Imported lazily so confidence.py stays import-light for pure-unit use and does
    not create an import cycle at module load."""
    try:
        from scripts import qc
    except ImportError:  # pragma: no cover - defensive; qc always present in-repo
        return None
    return qc._STALENESS_WINDOWS.get(group, qc._DEFAULT_STALENESS_WINDOW)


# --------------------------------------------------------------------------- #
# SOURCE axis.
# --------------------------------------------------------------------------- #

def _source_axis(module_doc, snapshot):
    """Return {"level","why"} for the SOURCE axis of one module (spec SOURCE rules).

    HIGH   -- data_mode == alpha_vantage AND no web-by-design / web-transcribed
              scored input AND (technical) series_source is not stooq.
    MEDIUM -- data_mode == av_free_degraded, OR a web-by-design scored input
              (sentiment always, via short_interest), OR fundamental
              web_transcribed_fields non-empty, OR technical series_source == stooq.
    LOW    -- data_mode == web_fallback, OR core scored inputs absent/stood-aside.
    """
    module = _module_key(module_doc)
    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
    data_mode = meta.get("data_mode", _MODE_PREMIUM)

    # LOW dominates: a web_fallback run has web-transcribed everything.
    if data_mode == _MODE_WEB_FALLBACK:
        return {"level": LOW, "why": "web fallback"}

    # Module-specific MEDIUM signals (in addition to the run-level degraded signal).
    if module == "sentiment":
        # sentiment ALWAYS scores short_interest (a by-design web input) -> MEDIUM
        # at best, regardless of data_mode (short-interest is web-transcribed even on
        # a premium run).
        return {"level": MEDIUM, "why": "AV premium; web short-interest"}

    if module == "fundamental":
        fund = snapshot.get("fundamentals", {}) if isinstance(snapshot, dict) else {}
        transcribed = fund.get("web_transcribed_fields") or []
        if transcribed:
            return {"level": MEDIUM, "why": "web-transcribed fields present"}
        if data_mode == _MODE_DEGRADED:
            return {"level": MEDIUM, "why": "AV free degraded"}
        return {"level": HIGH, "why": "coverage + AV"}

    if module == "technical":
        tech = snapshot.get("technicals", {}) if isinstance(snapshot, dict) else {}
        if tech.get("series_source") == "stooq":
            return {"level": MEDIUM, "why": "stooq series"}
        if data_mode == _MODE_DEGRADED:
            return {"level": MEDIUM, "why": "AV free degraded"}
        return {"level": HIGH, "why": "AV premium"}

    # risk (and any future non-web-by-design module).
    if data_mode == _MODE_DEGRADED:
        return {"level": MEDIUM, "why": "AV free degraded"}
    return {"level": HIGH, "why": "AV premium"}


# --------------------------------------------------------------------------- #
# DEPTH axis.
# --------------------------------------------------------------------------- #

def _depth_axis(module_doc):
    """Return {"level","why"} for the DEPTH axis of one module (DEPTH_TABLE lookup)."""
    module = _module_key(module_doc)
    rows = DEPTH_TABLE.get(module, {})
    if module == "fundamental":
        # keyed on fundamental_mode (anchored vs compressed).
        mode = module_doc.get("fundamental_mode", "compressed_snapshot_pass")
        level, why = rows.get(mode, rows["compressed_snapshot_pass"])
        return {"level": level, "why": why}
    # keyed on rubric_version; a version not yet in the table means it bumped past
    # 1.0.0 without a table edit -> promote to HIGH (the R-wave landed).
    rubric = module_doc.get("rubric_version", "1.0.0")
    if rubric in rows:
        level, why = rows[rubric]
        return {"level": level, "why": why}
    # rubric bumped beyond the shallow 1.0.0 row -> the depth pass landed.
    return {"level": HIGH, "why": "depth pass landed"}


# --------------------------------------------------------------------------- #
# STALENESS axis.
# --------------------------------------------------------------------------- #

def _staleness_axis(module_doc, snapshot, refresh_plan):
    """Return {"level","why"} for the STALENESS axis (spec STALENESS rules).

    Applied to price-sensitive modules (technical/risk/sentiment):
      HIGH   -- latest_trading_day present AND == as_of date AND (no refresh, or no
                in-window reuse of a price group).
      MEDIUM -- weekend/stale print (latest_trading_day != as_of date), OR a refresh
                reused a price group within its window.
      LOW    -- latest_trading_day null (freshness unverifiable), OR a reused price
                group is at/over its staleness window.
    Fundamental staleness = coverage freshness: HIGH when coverage current, else
    MEDIUM (never LOW here; coverage staleness is gated upstream).
    """
    module = _module_key(module_doc)

    if module == "fundamental":
        # v1.0.0: anchored coverage -> current -> HIGH; compressed snapshot pass ->
        # MEDIUM (coverage not confirmed current here).
        mode = module_doc.get("fundamental_mode", "compressed_snapshot_pass")
        if mode == "coverage_anchored_pass":
            return {"level": HIGH, "why": "coverage current"}
        return {"level": MEDIUM, "why": "snapshot pass"}

    if module not in _PRICE_SENSITIVE:
        # Non-price-sensitive module with no coverage notion -> freshness from print.
        pass

    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
    latest = meta.get("latest_trading_day")
    as_of_date = _as_of_date(meta)

    # LOW: freshness unverifiable (no vendor stamp).
    if not latest:
        return {"level": LOW, "why": "freshness unverifiable"}

    # Refresh-plan reuse signal (absent on fresh runs).
    if refresh_plan:
        groups = refresh_plan.get("groups", {}) if isinstance(refresh_plan, dict) else {}
        reused_over = False
        reused_in_window = False
        for group, decision in groups.items():
            if group not in _PRICE_GROUPS or not isinstance(decision, dict):
                continue
            if decision.get("action") != "reuse":
                continue
            age = decision.get("age_days")
            window = _price_group_window(group)
            if isinstance(age, (int, float)) and window is not None and age >= window:
                reused_over = True
            else:
                reused_in_window = True
        if reused_over:
            return {"level": LOW, "why": "reused over window"}
        if reused_in_window:
            return {"level": MEDIUM, "why": "reused in-window"}

    # No reuse (or no plan): freshness from the print itself.
    if as_of_date is not None and latest[:10] == as_of_date:
        return {"level": HIGH, "why": "fresh print"}
    # stale / weekend print.
    return {"level": MEDIUM, "why": "weekend print"}


# --------------------------------------------------------------------------- #
# Public API.
# --------------------------------------------------------------------------- #

def compute_module(module_doc, snapshot, bundle_dir=None):
    """Compute the ``confidence`` block for one module doc (pure + deterministic).

    Reads ``module_doc`` provenance (skill / rubric_version / fundamental_mode),
    ``snapshot.meta.*``, ``snapshot.fundamentals.web_transcribed_fields``,
    ``snapshot.technicals.series_source``, and -- when ``bundle_dir`` is given and a
    ``refresh_plan.json`` is present -- the refresh reuse signal. Returns the block::

        {"level","source","depth","staleness","rule","version"}

    ``level = min(source, depth, staleness)``. The ONLY arithmetic is the ordinal
    min. Absent refresh plan -> staleness from print freshness only.
    """
    refresh_plan = _load_refresh_plan(bundle_dir)

    source = _source_axis(module_doc, snapshot)
    depth = _depth_axis(module_doc)
    staleness = _staleness_axis(module_doc, snapshot, refresh_plan)

    level = _min_level(source["level"], depth["level"], staleness["level"])

    return {
        "level": level,
        "source": source,
        "depth": depth,
        "staleness": staleness,
        "rule": RULE,
        "version": CONFIDENCE_VERSION,
    }


def rollup(dimension_confidences):
    """Roll up per-dimension confidence blocks into the composite block.

    ``dimension_confidences`` is a list of confidence blocks (dicts) OR None (a
    dimension the composite excluded via renormalization contributes None and is
    skipped). Thesis-conviction carries no data provenance and is NOT passed in.

    ``level = min`` over the non-None dimensions' levels; ``why`` names the weakest
    dimension(s). Returns the same block shape; a source/depth/staleness axis is not
    meaningful at the roll-up so those are omitted -- the block carries ``level``,
    ``why``, ``rule``, ``version``.
    """
    present = [c for c in dimension_confidences
               if isinstance(c, dict) and c.get("level") is not None]

    if not present:
        return {
            "level": None,
            "why": "no evidence dimensions",
            "rule": "min over evidence dimensions",
            "version": CONFIDENCE_VERSION,
        }

    level = _min_level(*[c["level"] for c in present])

    # Name the weakest dimension(s): those AT the roll-up level. Use each block's
    # own weakest-axis why tag when available (from _weakest_why), else the module
    # tag we stamp below.
    weakest = [c for c in present if c.get("level") == level]
    parts = []
    for c in weakest:
        tag = _weakest_why(c)
        name = c.get("dimension")
        if name and tag:
            parts.append("%s %s" % (name, tag))
        elif tag:
            parts.append(tag)
        elif name:
            parts.append(name)
    driver = "; ".join(parts) if parts else "evidence"
    why = "%s -- %s" % (level, driver)

    return {
        "level": level,
        "why": why,
        "rule": "min over evidence dimensions",
        "version": CONFIDENCE_VERSION,
    }


def _weakest_why(confidence_block):
    """The ``why`` tag of the axis that pins a module's confidence level (its
    weakest link) -- used to name the driver in a roll-up. Ties resolve
    source > depth > staleness (deterministic)."""
    level = confidence_block.get("level")
    for axis in ("source", "depth", "staleness"):
        ax = confidence_block.get(axis)
        if isinstance(ax, dict) and ax.get("level") == level:
            return ax.get("why")
    return None
