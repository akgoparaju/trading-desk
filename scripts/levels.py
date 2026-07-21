"""Support/resistance ladder builder for the trading-desk plugin.

WHY THIS MODULE EXISTS: The ladder is the SHARED level vocabulary for every
Phase-2 evidence skill. technical-analysis scores price action against it,
risk-analytics builds its downside map from it, and trade-plan (Phase 3) anchors
entries and stops on it. The governing rule (spec): "no level may appear in a
report that is not in this ladder." So every level a report can cite is minted
HERE, in Python, from swing structure, moving averages, the all-time high, round
numbers, the analyst consensus target, and options open-interest structure --
each carrying an explicit ``type`` and ``basis`` provenance label. The LLM layer
selects and narrates levels; it never invents one.

All detectors are pure functions over already-parsed inputs. Reuses
scripts.indicators (sma), scripts.chain (max_pain, oi_walls, expiries) and the
build_snapshot I/O helpers for the CLI. stdlib-only.

Series convention (matching indicators.py): OHLCV rows are OLDEST-FIRST;
``adjusted_close`` is the price used for structure.
"""

import argparse
import glob
import json
import math
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation (``python3 scripts/levels.py``): ensure the repo root
# is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import build_snapshot, chain, indicators

# How recent a swing level may be to still count (one trading year).
_LOOKBACK_ROWS = 252

# Nearest-30-day options expiry target, in days.
_TARGET_EXPIRY_DAYS = 30

# Levels beyond this fraction of spot are noise for a trade horizon.
_MAX_PCT_FROM_LAST = 0.60

# Cross-type dedupe tolerance (relative).
_CROSS_DEDUPE_PCT = 0.005

# Types the market has ACTUALLY defended -> eligible as "proven" support.
_PROVEN_SUPPORT_TYPES = {"swing_low", "ma50", "ma200", "put_wall"}

# Evidence ranking for cross-type dedupe (lower index == stronger evidence,
# survives when two levels collide). Mirrors the spec ordering exactly.
_EVIDENCE_ORDER = [
    "swing_low", "swing_high",
    "ma50", "ma200",
    "ath",
    "vwap_52wk_high", "vwap_earnings",
    "max_pain", "call_wall", "put_wall", "oi_cluster",
    "analyst_pt",
    "round_number",
]


def _evidence_rank(level_type):
    """Rank of ``level_type`` (lower is stronger). Unknown types rank last."""
    try:
        return _EVIDENCE_ORDER.index(level_type)
    except ValueError:
        return len(_EVIDENCE_ORDER)


# --------------------------------------------------------------------------- #
# Swing structure
# --------------------------------------------------------------------------- #

def swing_levels(rows, window=5, dedupe_pct=0.01) -> list[dict]:
    """Local swing highs/lows over the LAST 252 rows of ``rows``.

    Index ``i`` (within the last-252 window) is a swing HIGH when its adjusted
    close equals the max of ``adj[i-window : i+window+1]`` AND has a full window
    on both sides (``window <= i <= len-1-window``); a swing LOW likewise with
    min. Levels older than a year are dropped by only ever looking at the tail
    window. Coincident levels within ``dedupe_pct`` (relative) collapse to the
    MOST RECENT one. Returns entries sorted oldest-first by date:
        {"level", "type": "swing_high"|"swing_low", "basis": "ohlcv", "date"}.
    """
    window_rows = rows[-_LOOKBACK_ROWS:]
    adj = [r.get("adjusted_close") for r in window_rows]

    raw = []
    n = len(adj)
    for i in range(n):
        if adj[i] is None:
            continue
        if i < window or i > n - 1 - window:
            continue  # no full window on one side
        segment = [v for v in adj[i - window:i + window + 1] if v is not None]
        if len(segment) < 2 * window + 1:
            continue  # a None inside the window breaks the extremum test
        if adj[i] == max(segment):
            raw.append({"level": float(adj[i]), "type": "swing_high",
                        "basis": "ohlcv", "date": window_rows[i]["date"],
                        "_i": i})
        elif adj[i] == min(segment):
            raw.append({"level": float(adj[i]), "type": "swing_low",
                        "basis": "ohlcv", "date": window_rows[i]["date"],
                        "_i": i})

    deduped = _dedupe_keep_recent(raw, dedupe_pct)
    for x in deduped:
        x.pop("_i", None)
    return deduped


def _dedupe_keep_recent(entries, dedupe_pct):
    """Collapse entries within ``dedupe_pct`` (relative) keeping the most recent.

    ``entries`` carry an ``_i`` index into the source window (higher == more
    recent). Within each near-cluster we keep the highest ``_i``. Same-type
    clustering only -- a swing high and a swing low at the same price are two
    distinct facts and are NOT merged here (cross-type collapse is a separate,
    later step in build_ladder).
    """
    kept = []
    for cand in sorted(entries, key=lambda e: e["_i"]):  # oldest -> newest
        collapsed = False
        for k in kept:
            if k["type"] != cand["type"]:
                continue
            if k["level"] == 0:
                continue
            if abs(cand["level"] - k["level"]) / abs(k["level"]) <= dedupe_pct:
                # cand is more recent (higher _i by iteration order) -> replace.
                k.update(cand)
                collapsed = True
                break
        if not collapsed:
            kept.append(dict(cand))
    kept.sort(key=lambda e: e["_i"])
    return kept


# --------------------------------------------------------------------------- #
# Round numbers
# --------------------------------------------------------------------------- #

def round_numbers(spot, count=2) -> list[dict]:
    """The ``count`` nearest round levels strictly ABOVE and BELOW ``spot``.

    Grid step = ``10 ** floor(log10(spot)) / 2`` (e.g. spot 327 -> step 50 ->
    250/300 below, 350/400 above; spot 85 -> step 5 -> 75/80 below, 90/95
    above). A ``spot`` sitting exactly on a grid line is skipped on both sides
    (strictly-above / strictly-below). Returns
        {"level", "type": "round_number", "basis": "psychological"}.
    """
    if spot is None or spot <= 0 or count <= 0:
        return []
    step = 10 ** math.floor(math.log10(spot)) / 2

    below = []
    k = math.floor(spot / step)
    line = k * step
    if line >= spot:          # strictly below
        line -= step
    while len(below) < count and line > 0:
        below.append(round(line, 10))
        line -= step

    above = []
    k = math.ceil(spot / step)
    line = k * step
    if line <= spot:          # strictly above
        line += step
    while len(above) < count:
        above.append(round(line, 10))
        line += step

    out = []
    for lvl in below + above:
        out.append({"level": float(lvl), "type": "round_number",
                    "basis": "psychological"})
    return out


# --------------------------------------------------------------------------- #
# Options-derived structure
# --------------------------------------------------------------------------- #

def options_levels(contracts, spot, as_of_date) -> list[dict]:
    """Max-pain, OI walls and near-money OI clusters at the ~30d expiry.

    Uses chain.py on the already-loaded ``contracts`` (never re-reads the file).
    Selects the expiry whose day-distance from ``as_of_date`` is closest to 30
    and emits, when available:
        max_pain, call_wall, put_wall, and up to 3 oi_cluster strikes.
    Each entry: {"level", "type", "basis": "options-derived"}.
    """
    if not contracts or spot is None:
        return []
    exps = chain.expiries(contracts)
    exp30 = build_snapshot._nearest_expiry(exps, as_of_date, _TARGET_EXPIRY_DAYS)
    if exp30 is None:
        return []

    out = []

    mp = chain.max_pain(contracts, exp30)
    if mp is not None:
        out.append({"level": float(mp), "type": "max_pain",
                    "basis": "options-derived"})

    walls = chain.oi_walls(contracts, exp30, spot)
    if walls:
        cw = walls.get("call_wall")
        if cw and cw.get("strike") is not None:
            out.append({"level": float(cw["strike"]), "type": "call_wall",
                        "basis": "options-derived"})
        pw = walls.get("put_wall")
        if pw and pw.get("strike") is not None:
            out.append({"level": float(pw["strike"]), "type": "put_wall",
                        "basis": "options-derived"})
        for cluster in walls.get("near_money_clusters", [])[:3]:
            if cluster.get("strike") is not None:
                out.append({"level": float(cluster["strike"]),
                            "type": "oi_cluster", "basis": "options-derived"})
    return out


# --------------------------------------------------------------------------- #
# Ladder assembly
# --------------------------------------------------------------------------- #

def build_ladder(snapshot, rows, contracts=None) -> list[dict]:
    """Assemble the full S/R ladder from a snapshot + daily rows (+ chain).

    Sources: swing_levels(rows), ma50/ma200, all-time high (max adjusted close
    over FULL history), round_numbers(spot), analyst consensus PT (if present),
    and options_levels (if ``contracts`` given). Every entry gets
    ``pct_from_last = level/last - 1``; entries beyond +/-60% of spot are
    dropped as noise. The list is sorted ascending by level, then a final
    cross-type dedupe at 0.5% (relative) keeps the entry whose ``type`` ranks
    higher in the evidence order. Returns the ladder list.
    """
    price = snapshot.get("price", {}) if isinstance(snapshot, dict) else {}
    tech = snapshot.get("technicals", {}) if isinstance(snapshot, dict) else {}
    sent = snapshot.get("sentiment", {}) if isinstance(snapshot, dict) else {}

    last = price.get("last")
    spot = last
    entries = []

    # -- swing structure ---------------------------------------------------
    for sw in swing_levels(rows):
        entries.append({"level": sw["level"], "type": sw["type"],
                        "basis": sw["basis"], "date": sw.get("date")})

    # -- moving averages ---------------------------------------------------
    ma50 = tech.get("ma50")
    if ma50 is not None:
        entries.append({"level": float(ma50), "type": "ma50", "basis": "ohlcv"})
    ma200 = tech.get("ma200")
    if ma200 is not None:
        entries.append({"level": float(ma200), "type": "ma200", "basis": "ohlcv"})

    # -- all-time high (full history) --------------------------------------
    adj_full = [r.get("adjusted_close") for r in rows
                if r.get("adjusted_close") is not None]
    if adj_full:
        entries.append({"level": float(max(adj_full)), "type": "ath",
                        "basis": "ohlcv"})

    # -- Wave 4A: anchored VWAPs (institutional cost basis) ----------------
    # These are minted in build_technicals (Python, deterministic) and surfaced
    # here as candidate S/R level TYPES so structure scoring can register an
    # institutional cost-basis line. Positioned by price like every other level;
    # null when the anchor produced no VWAP (e.g. a future earnings anchor).
    vwap_hi = tech.get("vwap_52wk_high")
    if vwap_hi is not None:
        entries.append({"level": float(vwap_hi), "type": "vwap_52wk_high",
                        "basis": "anchored-vwap"})
    vwap_earn = tech.get("vwap_earnings")
    if vwap_earn is not None:
        entries.append({"level": float(vwap_earn), "type": "vwap_earnings",
                        "basis": "anchored-vwap"})

    # -- round numbers -----------------------------------------------------
    if spot is not None:
        entries.extend(round_numbers(spot))

    # -- analyst consensus PT ---------------------------------------------
    pt = sent.get("consensus_pt")
    if pt is not None:
        entries.append({"level": float(pt), "type": "analyst_pt",
                        "basis": "consensus"})

    # -- options-derived ---------------------------------------------------
    if contracts:
        as_of = None
        meta = snapshot.get("meta") if isinstance(snapshot, dict) else None
        if isinstance(meta, dict):
            as_of = build_snapshot._as_of_date(meta.get("as_of_utc"))
        if as_of is None:
            as_of = tech.get("last_ohlcv_date")
        entries.extend(options_levels(contracts, spot, as_of))

    # -- pct_from_last + noise cutoff --------------------------------------
    scored = []
    for e in entries:
        if last in (None, 0):
            e["pct_from_last"] = None
            scored.append(e)
            continue
        pct = e["level"] / last - 1
        if abs(pct) > _MAX_PCT_FROM_LAST:
            continue
        e["pct_from_last"] = pct
        scored.append(e)

    scored.sort(key=lambda e: e["level"])
    return _cross_type_dedupe(scored)


def _cross_type_dedupe(entries):
    """Collapse levels within 0.5% keeping the stronger-evidence type.

    ``entries`` are pre-sorted ascending by level. Walk in order; for each
    candidate, if it is within _CROSS_DEDUPE_PCT (relative) of an already-kept
    entry, keep whichever ranks HIGHER in the evidence order (lower rank index).
    """
    kept = []
    for cand in entries:
        merged = False
        for idx, k in enumerate(kept):
            base = k["level"]
            if base == 0:
                continue
            if abs(cand["level"] - base) / abs(base) <= _CROSS_DEDUPE_PCT:
                if _evidence_rank(cand["type"]) < _evidence_rank(k["type"]):
                    kept[idx] = cand
                merged = True
                break
        if not merged:
            kept.append(cand)
    kept.sort(key=lambda e: e["level"])
    return kept


# --------------------------------------------------------------------------- #
# Nearest support / resistance
# --------------------------------------------------------------------------- #

def nearest_support(ladder, last, proven_only=True) -> dict | None:
    """Highest-level ladder entry strictly BELOW ``last``.

    ``proven_only`` restricts candidates to types the market has actually
    defended ({"swing_low","ma50","ma200","put_wall"}); levels merely projected
    (analyst_pt, round_number, resistance-style types) are skipped so a stop is
    never anchored on a line price has not held.
    """
    if last is None:
        return None
    candidates = [e for e in ladder if e["level"] < last]
    if proven_only:
        candidates = [e for e in candidates
                      if e["type"] in _PROVEN_SUPPORT_TYPES]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e["level"])


def nearest_resistance(ladder, last) -> dict | None:
    """Lowest-level ladder entry strictly ABOVE ``last`` (any type)."""
    if last is None:
        return None
    candidates = [e for e in ladder if e["level"] > last]
    if not candidates:
        return None
    return min(candidates, key=lambda e: e["level"])


# --------------------------------------------------------------------------- #
# CLI (thin wrapper)
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _load_rows_and_contracts(bundle):
    """Load daily OHLCV rows and (optionally) options contracts from a bundle.

    Reuses build_snapshot manifest + raw loaders so the parsing is identical to
    the snapshot build. Returns (rows, contracts) where contracts may be None.
    """
    manifest_path = os.path.join(bundle, "manifest.json")
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    files = manifest.get("files", {})

    daily_entry = files.get("daily_adjusted")
    if not daily_entry:
        raise build_snapshot.BuildError("manifest has no daily_adjusted file")
    daily_path = build_snapshot._resolve(bundle, daily_entry["path"])
    # CSV-aware: the daily series may be AV JSON or a bare stooq CSV export.
    daily = build_snapshot.load_daily_raw(daily_path)
    rows = build_snapshot.parse_daily_rows(daily)

    contracts = None
    chain_entry = files.get("options_chain")
    if chain_entry:
        chain_path = build_snapshot._resolve(bundle, chain_entry["path"])
        if os.path.exists(chain_path):
            try:
                contracts = chain.load_contracts(chain_path)
            except ValueError:
                contracts = None
    return rows, contracts


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build the S/R ladder for a snapshot bundle.")
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--out", default=None,
                        help="output path (default <bundle>/ladder.json)")
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

    try:
        rows, contracts = _load_rows_and_contracts(args.bundle)
    except (build_snapshot.BuildError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    ladder = build_ladder(snapshot, rows, contracts=contracts)

    meta = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
    doc = {
        "ticker": meta.get("ticker"),
        "as_of": build_snapshot._as_of_date(meta.get("as_of_utc")),
        "ladder": ladder,
    }

    out = args.out or os.path.join(args.bundle, "ladder.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
