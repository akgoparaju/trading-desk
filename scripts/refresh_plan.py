"""Deterministic refresh planner for the trading-desk plugin.

WHY THIS MODULE EXISTS: A refresh re-runs an existing ticker workspace CHEAPLY.
The rule is *selective FETCHING, never selective SCORING* — one new snapshot per
refresh, all modules re-emit against it. This module is the deterministic brain
of the fetch half: given a previous bundle (manifest + snapshot) and an as-of
date, it decides per manifest group whether to REFETCH or REUSE, detects the
earnings/dividend events that fell BETWEEN the two runs (which force a
statement-set refetch AND a judgment re-affirmation downstream), and estimates
the refetch cost. It writes a ``refresh_plan.json`` the refresh-analysis skill
executes verbatim.

Two invariants make the plan honest and cheap:
  1. Staleness windows are REUSED from ``scripts.qc._STALENESS_WINDOWS`` — the
     planner and the QC gate must agree on what "in window" means, so a REUSE
     the planner authorizes is guaranteed to pass the gate's staleness check
     (the reused raw file keeps its ORIGINAL retrieved_utc — honest provenance).
  2. A group is REUSED only if it is both (a) not in the always-refetch set and
     (b) within its window and (c) not forced by an event. Anything else
     refetches — a refresh is also a chance to fill a gap that was absent last
     run.

stdlib-only; >=3.10 guard; the module is pure planning — it reads the previous
bundle and writes a plan, it builds no artifacts and fetches nothing.
"""

import argparse
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

from datetime import date

# Allow direct invocation (``python3 scripts/refresh_plan.py``): ensure the repo
# root is importable so ``from scripts import qc`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import qc

# REUSE the QC gate's own staleness table — the planner authorizes a REUSE only
# STRICTLY INSIDE the window the gate will later age it against (age < window,
# review finding: the gate ages with fractional days and strict '>', so a reuse
# at exactly integer age == window can fail when the file's time-of-day precedes
# the new as_of's time-of-day). Strict-inside keeps a planner-authorized reuse
# provably gate-passing. Bound by identity (a private copy would silently drift).
_STALENESS_WINDOWS = qc._STALENESS_WINDOWS
_DEFAULT_STALENESS_WINDOW = qc._DEFAULT_STALENESS_WINDOW

# Groups re-fetched on EVERY refresh regardless of age: the market's fast-moving
# surface (spot, daily bars, SPY, news, realtime P/C, the independent web spot
# check) plus the options chain when it was present last run. These are the
# reason a refresh exists — cheap, current, and the inputs the score turns on.
ALWAYS_REFETCH = frozenset({
    "global_quote", "daily_adjusted", "spy_daily_adjusted", "news_sentiment",
    "pc_ratio_realtime", "web_spot_check",
})

# options_chain is always-refetch WHEN present last run; when ABSENT last run it
# still refetches (fill the gap) but with a distinct reason. Handled explicitly.
_OPTIONS_CHAIN = "options_chain"

# Statement set re-fetched when earnings fell between the two runs — the print
# revises every one of these, and forces a judgment re-affirmation downstream.
EARNINGS_EVENT_GROUPS = frozenset({
    "income_statement", "balance_sheet", "cash_flow", "earnings",
    "earnings_estimates", "overview", "earnings_calendar",
    "insider_transactions",
})

# A dividend ex-date between runs refreshes only the yield/date surface.
DIVIDEND_EVENT_GROUPS = frozenset({"overview", "earnings_calendar"})

# Window-based groups the planner ages (everything that is neither always-refetch
# nor the options chain). Their windows come from _STALENESS_WINDOWS.
_WINDOW_GROUPS = (
    "overview", "income_statement", "balance_sheet", "cash_flow", "earnings",
    "earnings_estimates", "insider_transactions", "earnings_calendar",
    "treasury_yield", "short_interest",
)

# iv_history refreshes if its newest sample is older than this (matches the
# market-snapshot skill's 14-day IV-cache freshness rule).
_IV_HISTORY_WINDOW_DAYS = 14

# Sector-scale config layout (falsifier monitoring). Active scales live under
# ``trading_desk_config/scales/*.json``; drafted-but-unratified proposals under
# ``trading_desk_config/scales/proposals/*.json``. A refresh scans them so a scale
# whose pre-registered falsifier tripped surfaces (scale_review_required) and any
# unratified proposal is always visible.
_SCALES_SUBDIR = os.path.join("trading_desk_config", "scales")
_PROPOSALS_SUBDIR = os.path.join("trading_desk_config", "scales", "proposals")

# Default pre-registered consequence when a scale JSON omits ``on_trip``.
_DEFAULT_ON_TRIP = "flag+disclose"


class PlanError(Exception):
    """Fatal planning error (maps to exit 2 with a clear message)."""


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #

def _parse_date(text):
    """Parse a YYYY-MM-DD (or ISO timestamp) leading date, or None."""
    if not isinstance(text, str) or len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _age_days(retrieved_utc, as_of):
    """Whole days from a retrieved_utc's date to the as_of date, or None.

    Uses the DATE part only (the manifest records instants, but staleness is a
    day-granularity concept and matching qc's day math keeps REUSE decisions in
    lockstep with the gate). Negative ages clamp to 0.
    """
    retrieved = _parse_date(retrieved_utc)
    if retrieved is None:
        return None
    delta = (as_of - retrieved).days
    return delta if delta >= 0 else 0


# --------------------------------------------------------------------------- #
# Bundle discovery
# --------------------------------------------------------------------------- #

def _looks_like_bundle(path):
    """True if ``path`` is itself a bundle (manifest.json + a snapshot_*.json)."""
    if not os.path.isdir(path):
        return False
    if not os.path.isfile(os.path.join(path, "manifest.json")):
        return False
    return bool(glob.glob(os.path.join(path, "snapshot_*.json")))


def find_previous_bundle(ticker_dir):
    """Locate the newest previous bundle under ``ticker_dir``.

    Preference order:
      1. Newest ``<ticker-dir>/detail_reports_*`` by NAME (dates sort lexically).
      2. LEGACY: ``<ticker-dir>`` itself if it is a ``td_bundle_<T>_<date>``
         directory OR directly contains manifest.json + a snapshot_*.json.
    Raises PlanError (→ exit 2) if none is found.
    """
    if not os.path.isdir(ticker_dir):
        raise PlanError("ticker dir not found: %s" % ticker_dir)

    detail = sorted(glob.glob(os.path.join(ticker_dir, "detail_reports_*")))
    detail = [d for d in detail if os.path.isdir(d)]
    if detail:
        return detail[-1]  # newest by name

    # Legacy: the ticker dir is itself the bundle.
    base = os.path.basename(os.path.normpath(ticker_dir))
    if base.startswith("td_bundle_") or _looks_like_bundle(ticker_dir):
        if _looks_like_bundle(ticker_dir):
            return os.path.normpath(ticker_dir)

    raise PlanError(
        "no previous bundle found under %s — nothing to refresh; "
        "run a full analysis first" % ticker_dir)


# --------------------------------------------------------------------------- #
# Bundle reading
# --------------------------------------------------------------------------- #

def _load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _read_manifest(bundle):
    manifest = _load_json(os.path.join(bundle, "manifest.json"))
    if not isinstance(manifest, dict):
        raise PlanError("unreadable manifest.json in %s" % bundle)
    return manifest


def _read_snapshot(bundle):
    """Newest snapshot_*.json parsed, or {} if none/unreadable (non-fatal)."""
    matches = sorted(glob.glob(os.path.join(bundle, "snapshot_*.json")))
    if not matches:
        return {}
    snap = _load_json(matches[-1])
    return snap if isinstance(snap, dict) else {}


def _iv_history_newest_date(ticker_dir, bundle, manifest):
    """The newest iv_history sample date (a date object), or None.

    Resolves the cache via the manifest's top-level ``iv_history_path`` (bundle-
    relative) when present, else the conventional
    ``<ticker_dir>/iv_history_<TICKER>.json`` parent-sibling.
    """
    candidates = []
    rel = manifest.get("iv_history_path")
    if rel:
        candidates.append(rel if os.path.isabs(rel)
                          else os.path.join(bundle, rel))
    ticker = manifest.get("ticker")
    if ticker:
        candidates.append(os.path.join(ticker_dir,
                                       "iv_history_%s.json" % ticker))
    for path in candidates:
        cache = _load_json(path)
        if not isinstance(cache, dict):
            continue
        dates = [_parse_date(s.get("date"))
                 for s in (cache.get("samples") or [])
                 if isinstance(s, dict)]
        dates = [d for d in dates if d is not None]
        if dates:
            return max(dates)
    return None


# --------------------------------------------------------------------------- #
# Event detection
# --------------------------------------------------------------------------- #

def _event_between(event_date, prev_as_of, as_of):
    """True if ``event_date`` falls in the half-open-left interval
    (prev_as_of, as_of] — strictly after the previous run, up to and including
    the new as_of. A date on the previous run was already reflected; a future
    date has not happened yet."""
    d = _parse_date(event_date)
    if d is None or prev_as_of is None:
        return False
    return prev_as_of < d <= as_of


# --------------------------------------------------------------------------- #
# Group planning
# --------------------------------------------------------------------------- #

def _group_decision(group, present, age, window, forced_by_event):
    """Return a {action, reason, age_days} decision for one manifest group."""
    if forced_by_event:
        return {"action": "refetch", "reason": "event forces refetch",
                "age_days": age}
    if group in ALWAYS_REFETCH:
        return {"action": "refetch", "reason": "always-refetch group",
                "age_days": age}
    if group == _OPTIONS_CHAIN:
        if not present:
            return {"action": "refetch", "reason": "absent last run",
                    "age_days": None}
        return {"action": "refetch", "reason": "always-refetch group",
                "age_days": age}
    if not present:
        return {"action": "refetch", "reason": "absent last run",
                "age_days": None}
    if age is None:
        # Present but no parseable retrieved_utc — refetch rather than trust it.
        return {"action": "refetch", "reason": "retrieved_utc unparseable",
                "age_days": None}
    if age < window:  # strict: at age == window the gate's fractional aging can exceed it
        return {"action": "reuse",
                "reason": "age %dd vs window %dd" % (age, window),
                "age_days": age}
    return {"action": "refetch",
            "reason": "age %dd vs window %dd" % (age, window),
            "age_days": age}


# --------------------------------------------------------------------------- #
# Sector-scale falsifier monitoring
# --------------------------------------------------------------------------- #

def _walk_up_for(start, subdir, max_levels=3):
    """First ancestor of ``start`` (inclusive) whose ``<dir>/<subdir>`` is an existing
    directory, or None.

    Walks UP at most ``max_levels`` so scale/proposal discovery is FLATTEN-transparent:
    under ``--output-dir`` (v1.2.0 flat layout) the ``--ticker-dir`` IS the workspace
    root, so ``trading_desk_config/…`` sits directly under it (0 up); under the nested
    ``<WORKROOT>/trading_desk_<T>`` layout it is one level up; and for an un-redirected
    (human) run ``ticker_dir`` is CWD-relative so the parent resolves to the CWD.
    Byte-identical to the pre-1.2.0 ``dirname(ticker_dir)`` derivation in the nested /
    human cases (a ``trading_desk_config`` never lives INSIDE the ticker dir, so the
    inclusive first hop is skipped there), and additionally correct when flat.
    """
    try:
        d = os.path.abspath(start) if start else None
    except (TypeError, ValueError):
        d = None
    seen = set()
    for _ in range(max_levels):
        if not d or d in seen:
            break
        seen.add(d)
        cand = os.path.join(d, subdir)
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _scales_dirs(ticker_dir):
    """The active-scales dir for a ticker workspace (0 or 1 entry, realpath'd).

    Resolves ``trading_desk_config/scales`` by walking up from ``ticker_dir`` (see
    ``_walk_up_for``) — flatten-transparent and byte-identical to the pre-1.2.0
    ``dirname(ticker_dir)`` derivation for the nested/human layouts. Only an existing
    directory is returned; realpath so a symlinked workspace resolves canonically.
    """
    found = _walk_up_for(ticker_dir, _SCALES_SUBDIR)
    return [os.path.realpath(found)] if found else []


def _evaluate_scale_falsifiers(scale, snapshot):
    """Run scripts.sector_scales.evaluate_falsifiers, degrading gracefully.

    Lazy import (per the module contract, sector_scales is authored concurrently):
    an ImportError -- or any AttributeError if the symbol is not yet present --
    yields ``(results=[], note=<why skipped>)`` rather than raising, so the refresh
    planner never hard-depends on that module's timeline. A normal run returns
    ``(results, None)``.
    """
    try:
        from scripts import sector_scales  # lazy: authored by a concurrent agent
    except ImportError:
        return [], "sector_scales module unavailable (falsifiers not evaluated)"
    evaluate = getattr(sector_scales, "evaluate_falsifiers", None)
    if evaluate is None:
        return [], "sector_scales.evaluate_falsifiers unavailable"
    try:
        results = evaluate(scale, snapshot)
    except Exception as exc:  # a scale-eval error must not sink the whole refresh
        return [], "falsifier evaluation error: %s" % exc
    return (results if isinstance(results, list) else []), None


def _scale_label(scale, path):
    """`<name>@<version>` for a scale.

    The scale's identifier is its ``scale`` field (the sector_scales contract);
    ``name`` is accepted as an alias, and the filename stem is the last-resort
    default. ``version`` completes the ``@`` label when present.
    """
    name = None
    if isinstance(scale, dict):
        name = scale.get("scale") or scale.get("name")
    if not name:
        name = os.path.splitext(os.path.basename(path))[0]
    version = scale.get("version") if isinstance(scale, dict) else None
    return "%s@%s" % (name, version) if version else name


def _scan_scales(ticker_dir, snapshot):
    """Build the ``scales`` block and the ``scale_review_required`` flag.

    For each active scale JSON found, evaluate its pre-registered falsifiers
    against the PREVIOUS bundle's snapshot (the newest data the refresh has in
    hand before it fetches). A falsifier whose ``tripped`` is True flips
    ``any_tripped`` for that scale and ``scale_review_required`` overall; the
    action string names the pre-registered consequence from the scale's
    ``on_trip`` field (default ``flag+disclose``). A falsifier whose ``tripped``
    is None (unresolvable -- the metric was absent from the snapshot) does NOT
    trip review; it is reported as-is so the gap is visible.
    """
    scales = []
    review_required = False
    for scales_dir in _scales_dirs(ticker_dir):
        for path in sorted(glob.glob(os.path.join(scales_dir, "*.json"))):
            scale = _load_json(path)
            if not isinstance(scale, dict):
                continue
            results, note = _evaluate_scale_falsifiers(scale, snapshot)
            any_tripped = any(
                isinstance(r, dict) and r.get("tripped") is True
                for r in results)
            on_trip = scale.get("on_trip") or _DEFAULT_ON_TRIP
            if any_tripped:
                review_required = True
                action = ("re-affirm or re-base (pre-registered consequence: %s)"
                          % on_trip)
            else:
                action = "none"
            entry = {
                "scale": _scale_label(scale, path),
                "falsifiers": results,
                "any_tripped": any_tripped,
                "action_required": action,
            }
            if note:
                entry["note"] = note
            scales.append(entry)
    return scales, review_required


def _pending_proposals(ticker_dir):
    """Filenames of drafted-but-unratified scale proposals (sorted).

    Scans ``trading_desk_config/scales/proposals/`` at the workspace root, resolved by
    walking up from ``ticker_dir`` (same ``_walk_up_for`` derivation as ``_scales_dirs``
    — flatten-transparent, byte-identical to pre-1.2.0 for the nested/human layouts).
    A refresh surfaces these so an unratified proposal is never silently pending.
    """
    names = set()
    props_dir = _walk_up_for(ticker_dir, _PROPOSALS_SUBDIR)
    if props_dir:
        for f in glob.glob(os.path.join(props_dir, "*.json")):
            names.add(os.path.basename(f))
    return sorted(names)


def build_plan(ticker_dir, bundle, as_of):
    """Build the refresh-plan dict from a previous bundle + an as_of date."""
    manifest = _read_manifest(bundle)
    snapshot = _read_snapshot(bundle)

    ticker = manifest.get("ticker")
    prev_as_of_raw = manifest.get("as_of_utc")
    prev_as_of = _parse_date(prev_as_of_raw)
    files = manifest.get("files") or {}

    # -- event detection ---------------------------------------------------
    events_block = snapshot.get("events") if isinstance(snapshot, dict) else None
    events_block = events_block if isinstance(events_block, dict) else {}
    ne = events_block.get("next_earnings")
    earnings_date = ne.get("date") if isinstance(ne, dict) else None
    dividends = events_block.get("dividends")
    ex_date = dividends.get("ex_date") if isinstance(dividends, dict) else None

    earnings_between = _event_between(earnings_date, prev_as_of, as_of)
    dividend_between = _event_between(ex_date, prev_as_of, as_of)
    judgment_review = earnings_between or dividend_between

    forced = set()
    if earnings_between:
        forced |= EARNINGS_EVENT_GROUPS
    if dividend_between:
        forced |= DIVIDEND_EVENT_GROUPS

    # -- per-group decisions ----------------------------------------------
    # The universe of groups to plan: everything present in the previous
    # manifest, plus options_chain (which we plan even when absent — a refresh
    # is a chance to fill it).
    planned_groups = set(files) | {_OPTIONS_CHAIN} | ALWAYS_REFETCH | forced

    groups = {}
    for group in sorted(planned_groups):
        entry = files.get(group)
        present = isinstance(entry, dict)
        age = None
        if present:
            age = _age_days(entry.get("retrieved_utc"), as_of)
        window = _STALENESS_WINDOWS.get(group, _DEFAULT_STALENESS_WINDOW)
        groups[group] = _group_decision(group, present, age, window,
                                        forced_by_event=(group in forced))

    estimated_refetch_calls = sum(1 for v in groups.values()
                                  if v["action"] == "refetch")

    # -- iv_history --------------------------------------------------------
    iv_newest = _iv_history_newest_date(ticker_dir, bundle, manifest)
    if iv_newest is None:
        iv_plan = {"action": "refresh",
                   "reason": "no iv_history cache to reuse"}
    else:
        iv_age = (as_of - iv_newest).days
        iv_age = iv_age if iv_age >= 0 else 0
        if iv_age <= _IV_HISTORY_WINDOW_DAYS:
            iv_plan = {"action": "reuse",
                       "reason": "newest sample %dd old vs %dd window"
                                 % (iv_age, _IV_HISTORY_WINDOW_DAYS)}
        else:
            iv_plan = {"action": "refresh",
                       "reason": "newest sample %dd old vs %dd window"
                                 % (iv_age, _IV_HISTORY_WINDOW_DAYS)}

    # -- scale falsifier monitoring (parallel to judgment_review) ----------
    # Evaluated against the PREVIOUS snapshot (the newest data in hand pre-fetch).
    # scale_review_required is a PARALLEL signal to judgment_review_required --
    # neither influences the other (judgment_review logic is UNCHANGED above).
    scales, scale_review_required = _scan_scales(ticker_dir, snapshot)

    return {
        "ticker": ticker,
        "as_of": as_of.isoformat(),
        "previous_bundle": os.path.normpath(bundle),
        "previous_as_of": prev_as_of.isoformat() if prev_as_of else None,
        "new_bundle": "detail_reports_%s" % as_of.isoformat(),
        "events": {
            "earnings_between_runs": earnings_between,
            "earnings_date": earnings_date,
            "dividend_ex_date_between_runs": dividend_between,
            "judgment_review_required": judgment_review,
        },
        "groups": groups,
        "estimated_refetch_calls": estimated_refetch_calls,
        "iv_history": iv_plan,
        "scales": scales,
        "scale_review_required": scale_review_required,
        "pending_proposals": _pending_proposals(ticker_dir),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Plan a cheap refresh of an existing ticker workspace: "
                    "selective refetch vs reuse per group, event detection, and "
                    "a refetch-cost estimate. Writes refresh_plan.json.")
    parser.add_argument("--ticker-dir", required=True,
                        help="the NEW ticker workspace (where the plan + new bundle "
                             "go), e.g. ./trading_desk_MU or a flat --output-dir root")
    parser.add_argument("--prev-dir", default=None,
                        help="the PRIOR workspace root to read the previous bundle "
                             "from (the dir whose immediate children are "
                             "detail_reports_*). Default: resolve the prior under "
                             "--ticker-dir (v1.1.0 behavior).")
    parser.add_argument("--as-of", default=None,
                        help="planning date YYYY-MM-DD (default: today)")
    parser.add_argument("--out", default=None,
                        help="output path (default <ticker-dir>/refresh_plan.json)")
    args = parser.parse_args(argv)

    if args.as_of:
        as_of = _parse_date(args.as_of)
        if as_of is None:
            print("ERROR: --as-of must be YYYY-MM-DD, got %r" % args.as_of,
                  file=sys.stderr)
            return 2
    else:
        as_of = date.today()

    try:
        # The PRIOR bundle is read from --prev-dir when given (a fresh, empty
        # --output-dir refresh points here at the previous run's workspace), else
        # from --ticker-dir (v1.1.0: the prior lives in the same workspace). Scale/
        # proposal discovery + the plan output stay rooted at --ticker-dir (the NEW
        # workspace), so current scales govern and the plan lands in the new dir.
        prev_root = args.prev_dir or args.ticker_dir
        bundle = find_previous_bundle(prev_root)
        plan = build_plan(args.ticker_dir, bundle, as_of)
    except PlanError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    out = args.out or os.path.join(args.ticker_dir, "refresh_plan.json")
    try:
        with open(out, "w") as fh:
            json.dump(plan, fh, indent=2)
    except OSError as exc:
        print("ERROR: cannot write plan to %s: %s" % (out, exc), file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
