"""Bulk-group structural adapter template — trading-desk plugin.

WHAT THIS FILE IS
-----------------
A copy-pasteable stub for writing a STRUCTURAL adapter that maps a foreign
governed-MCP source's raw output onto one of the three bulk-group shapes the
trading-desk builder accepts (daily_adjusted / spy_daily_adjusted /
options_chain).  See docs/CANONICAL_CONTRACT.md "Manifest keys → accepted raw
shapes" for the exact target shapes, and "THE ADAPTER RULE" for the
requirement that adapters be STRUCTURAL ONLY.

STRUCTURAL ONLY MEANS
---------------------
  - Field mapping and renaming: change key names, reorder, drop unneeded keys.
  - No arithmetic:    do NOT compute adjusted_close from raw close and a split
                      factor, compute mid from bid/ask, derive delta, etc.
  - No unit conversion: if the source reports volume in thousands, DISCLOSE it
                      in a note and DO NOT multiply — let the QC gate surface it.
  - No derived columns: do NOT add fields that did not exist in the source.
  - If the source shape has no acceptable equivalent for a required field, emit
    the field as null and add a comment explaining the gap.

FILE NAMING CONVENTION
----------------------
Save a completed adapter to the user's workspace (NOT inside the plugin repo):

    trading_desk_config/adapters/<source>_<group>.py

For example:
    trading_desk_config/adapters/mcp:governed_av_daily_adjusted.py
    trading_desk_config/adapters/mcp:governed_av_options_chain.py

REPRODUCIBILITY REQUIREMENT
----------------------------
Once committed, an adapter is RE-RUN VERBATIM on every subsequent fetch from
the same source.  NEVER regenerate it — a regenerated mapping could silently
drift, and a refresh delta would then misread parsing drift as real market
movement.  Adapters live with the user's data; the plugin ships none.

HOW TO VALIDATE BEFORE COMMITTING
-----------------------------------
1.  Run this adapter against a real tool-result file:
        python3 trading_desk_config/adapters/<source>_<group>.py \
            --input  <path-to-raw-tool-result-or-cache_path-file> \
            --output trading_desk_<TICKER>/detail_reports_<DATE>/raw/<group>.json
2.  Run the snapshot builder:
        python3 ${CLAUDE_PLUGIN_ROOT}/scripts/build_snapshot.py \
            --bundle trading_desk_<TICKER>/detail_reports_<DATE> --ticker <TICKER>
    Exit 0 means the shape was accepted.
3.  Run the QC gate:
        python3 ${CLAUDE_PLUGIN_ROOT}/scripts/qc_gate.py \
            trading_desk_<TICKER>/detail_reports_<DATE>/snapshot_<TICKER>_<DATE>.json
    Exit 0 means a correct adapter: the same reconciliation checks that pass on
    a native bundle also pass here (mktcap, P/E, net-cash, MA ordering, range
    sanity, options freshness, staleness).

USAGE (when completed)
-----------------------
    python3 trading_desk_config/adapters/<source>_<group>.py \
        --input  <path-to-source-file-or-cache_path>  \
        --output <bundle>/raw/<group>.json
"""

import argparse
import json
import sys


# ---------------------------------------------------------------------------
# CONFIGURATION — fill these in when you copy this template
# ---------------------------------------------------------------------------

# Human-readable label for error messages (no PII, no internal source names).
SOURCE_LABEL = "mcp:governed_av"

# The manifest key this adapter targets: "daily_adjusted", "spy_daily_adjusted",
# or "options_chain".
TARGET_GROUP = "daily_adjusted"  # CHANGE ME


# ---------------------------------------------------------------------------
# STRUCTURAL TRANSFORM — implement _transform() for your group
# ---------------------------------------------------------------------------

def _transform(source_payload: dict | list) -> dict | list:
    """Map source_payload onto the accepted canonical shape for TARGET_GROUP.

    STRUCTURAL ONLY: rename/reorder fields; no arithmetic, no unit conversion,
    no derived columns.  See docs/CANONICAL_CONTRACT.md for the exact target
    shape.

    Parameters
    ----------
    source_payload:
        The parsed JSON body read from the file at cache_path (for a bulk
        file-offload source) or directly from the tool result dict/list.

    Returns
    -------
    dict | list
        The canonical shape for TARGET_GROUP, ready to write to raw/<key>.json.

    Raises
    ------
    ValueError
        If source_payload does not match the expected source shape, so the
        caller can surface a clear error rather than writing a silently
        wrong file.
    """
    # ------------------------------------------------------------------
    # EXAMPLE: daily_adjusted target shape
    #
    # Target (AV JSON, the shape build_snapshot.parse_daily_rows accepts):
    #   {
    #     "Time Series (Daily)": {
    #       "YYYY-MM-DD": {
    #         "1. open":           "<string-or-number>",
    #         "2. high":           "<string-or-number>",
    #         "3. low":            "<string-or-number>",
    #         "4. close":          "<string-or-number>",
    #         "5. adjusted close": "<string-or-number>",  # REQUIRED
    #         "6. volume":         "<string-or-number>"
    #       },
    #       ...  (one entry per trading day, any order — builder sorts ascending)
    #     }
    #   }
    #
    # Replace the body below with your source's actual field names.
    # ------------------------------------------------------------------

    # Shape-check: raise early with a clear message if the source changed.
    if not isinstance(source_payload, dict):
        raise ValueError(
            f"{SOURCE_LABEL} {TARGET_GROUP}: expected a dict payload, "
            f"got {type(source_payload).__name__}"
        )

    # REPLACE THIS with your source's top-level key for the time-series map.
    source_series_key = "Time Series (Daily)"  # CHANGE ME if different
    source_series = source_payload.get(source_series_key)
    if not isinstance(source_series, dict):
        raise ValueError(
            f"{SOURCE_LABEL} {TARGET_GROUP}: missing or non-dict "
            f"'{source_series_key}' in source payload"
        )

    # Field-name mapping: source key → canonical key.
    # Edit the LEFT side (source key) to match your source; keep the RIGHT
    # side (canonical key) exactly as shown — the builder reads these names.
    FIELD_MAP = {
        # source field name  : canonical field name
        "1. open":            "1. open",            # CHANGE LEFT if needed
        "2. high":            "2. high",
        "3. low":             "3. low",
        "4. close":           "4. close",
        "5. adjusted close":  "5. adjusted close",  # REQUIRED; map to your source's adj-close field
        "6. volume":          "6. volume",
    }

    canonical_series = {}
    for date_str, bar in source_series.items():
        if not isinstance(bar, dict):
            continue  # skip malformed bars
        canonical_bar = {}
        for src_key, dst_key in FIELD_MAP.items():
            if src_key in bar:
                canonical_bar[dst_key] = bar[src_key]  # value copied verbatim
        canonical_series[date_str] = canonical_bar

    return {"Time Series (Daily)": canonical_series}

    # ------------------------------------------------------------------
    # ALTERNATIVE EXAMPLE: options_chain target shape
    #
    # Target (raw JSON list of contracts, shape chain.load_contracts accepts):
    #   [
    #     {
    #       "expiration": "YYYY-MM-DD",  # REQUIRED
    #       "type":       "call"|"put",  # REQUIRED (normalized to lower-case)
    #       "strike":     <number>,      # REQUIRED (must coerce to float)
    #       "mark":       <number>,      # optional; falls back to (bid+ask)/2, then "last"
    #       "bid":        <number>,
    #       "ask":        <number>,
    #       "iv":         <number>,      # alias: implied_volatility
    #       "delta":      <number>,
    #       "oi":         <number>,      # alias: open_interest
    #       "volume":     <number>,
    #       "last":       <number>
    #     },
    #     ...
    #   ]
    #
    # For a file-offload source: read the file at cache_path, parse JSON,
    # then map each contract to the canonical field names above.
    # STRUCTURAL ONLY — do not compute mark from bid/ask here; the builder
    # already does that fallback.
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FILE-OFFLOAD HELPER
# ---------------------------------------------------------------------------

def _read_offload(cache_path: str) -> dict | list:
    """Read the file offloaded by a bulk-group MCP tool result.

    Governed-source bulk tools return {"cache_path": "...", "bytes": ...,
    "summary": "..."} — the actual body is ON DISK at cache_path.  This helper
    reads and parses that file.  It does NOT handle the MCP envelope (the tool
    result itself); the caller should have already extracted cache_path from the
    tool result before calling this.
    """
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ValueError(
            f"{SOURCE_LABEL}: offloaded file not found at cache_path={cache_path!r}. "
            "Temp files may have been reaped — re-run the fetch step."
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{SOURCE_LABEL}: could not parse JSON from cache_path={cache_path!r}: {exc}"
        )


# ---------------------------------------------------------------------------
# CLI ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            f"Structural adapter: {SOURCE_LABEL} → canonical {TARGET_GROUP} shape.\n"
            "Reads the source file (the offloaded cache_path body or a direct\n"
            "tool-result JSON file) and writes the canonical shape."
        )
    )
    parser.add_argument(
        "--input", required=True,
        help=(
            "Path to the source JSON file to transform.  For file-offload sources, "
            "this is the value of cache_path from the tool result."
        ),
    )
    parser.add_argument(
        "--output", required=True,
        help="Destination path for the canonical raw/<key>.json file.",
    )
    args = parser.parse_args()

    # Read source file.
    try:
        with open(args.input, "r", encoding="utf-8") as fh:
            source_payload = json.load(fh)
    except FileNotFoundError:
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: could not parse JSON from {args.input}: {exc}", file=sys.stderr)
        sys.exit(1)

    # If the source payload is the MCP tool result (not the offloaded body),
    # extract cache_path and read the actual body from disk.
    if isinstance(source_payload, dict) and "cache_path" in source_payload:
        cache_path = source_payload["cache_path"]
        print(
            f"Detected file-offload contract: reading body from cache_path={cache_path!r}",
            file=sys.stderr,
        )
        try:
            source_payload = _read_offload(cache_path)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    # Apply the structural transform.
    try:
        canonical = _transform(source_payload)
    except ValueError as exc:
        print(f"ERROR: transform failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Write canonical output.
    try:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(canonical, fh, separators=(",", ":"))
        print(f"OK: wrote {args.output}", file=sys.stderr)
    except OSError as exc:
        print(f"ERROR: could not write output: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
