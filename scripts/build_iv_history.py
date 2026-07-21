"""IV-history batch builder for the trading-desk plugin (spec B18).

WHY THIS MODULE EXISTS: the market-snapshot IV-history refresh used to run as
~54 serial LLM tool round-trips -- 27 HISTORICAL_OPTIONS fetches interleaved with
27 per-sample inline python one-liners (each computing one ATM IV via chain.py,
then ``rm``-ing the temp chain). That serialized the compute through the LLM and
amplified cache-read load turn over turn. This script collapses the compute half
into ONE call: the LLM batches the ~26 fetches (parallel tool-use blocks) into a
manifest ``raw/iv_samples.json``, then invokes this script once to compute every
sample's ATM IV from the offloaded chains on disk.

CORRECTNESS (spec B18 "Determinism gain"): the legacy one-liner took spot as an
LLM-supplied argument with NO specified source, so two runs could pick different
ATM strikes near a boundary. This script derives spot DETERMINISTICALLY from the
daily raw file's nominal ``"4. close"`` for each sample date -- NOT
``"5. adjusted close"``: historical option strikes are nominal, so ATM selection
must use the nominal price. IV history runs only in premium ``alpha_vantage``
mode where the daily file is AV JSON carrying ``"4. close"``.

Self-contained: imports only ``scripts.chain`` + stdlib. Never loads a chain into
context -- ``scripts.chain`` is the only reader; this script only records/appends
compact ``{date, atm_iv}`` samples. On success it DELETES each consumed chain
file (the ``rm`` moved out of the SKILL loop and into the script).

stdlib-only otherwise. Exit 0 on success; exit 2 on a fatal input error
(unreadable manifest/daily file, or daily file lacking ``"4. close"``).
"""

import argparse
import datetime
import json
import os
import sys

# Allow direct invocation (``python3 scripts/build_iv_history.py``): ensure the
# repo root is importable so ``from scripts import chain`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import chain


# Target DTE for the sampled ATM expiry: the expiry closest to date + 30 days,
# matching the rule the legacy Step-4 one-liner used.
_TARGET_DTE = 30


class IVHistoryError(Exception):
    """Fatal input error (bad manifest / daily file). Maps to exit code 2."""


def _parse_date(text):
    """Parse a leading ``YYYY-MM-DD`` to a date, or None on any failure."""
    if not isinstance(text, str) or len(text) < 10:
        return None
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        return None


def load_daily_closes(daily_path):
    """Map ``date -> nominal raw close`` from an AV daily JSON file.

    Reads ``"Time Series (Daily)"`` and takes each bar's ``"4. close"`` -- the
    NOMINAL (unadjusted) close, because historical option strikes are nominal.
    Deliberately does NOT read ``"5. adjusted close"``.

    Raises IVHistoryError if the file is unreadable, is not an AV JSON daily
    series, or its bars carry no ``"4. close"`` key at all (a clean error rather
    than silently falling back to the adjusted close -- spec B18).
    """
    try:
        with open(daily_path, "r") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        raise IVHistoryError(f"cannot read daily file {daily_path}: {exc}")

    if not isinstance(payload, dict):
        raise IVHistoryError(
            f"daily file {daily_path} is not an AV JSON object "
            "(IV history requires premium alpha_vantage mode)"
        )
    ts = payload.get("Time Series (Daily)")
    if not isinstance(ts, dict) or not ts:
        raise IVHistoryError(
            f"daily file {daily_path} missing 'Time Series (Daily)' "
            "(IV history requires premium alpha_vantage mode)"
        )

    closes = {}
    saw_close_key = False
    for date_key, bar in ts.items():
        if not isinstance(bar, dict):
            continue
        if "4. close" in bar:
            saw_close_key = True
            try:
                closes[date_key[:10]] = float(bar["4. close"])
            except (ValueError, TypeError):
                continue

    if not saw_close_key:
        raise IVHistoryError(
            f"daily file {daily_path} has no '4. close' (nominal close) column; "
            "refusing to fall back to '5. adjusted close' -- historical option "
            "strikes are nominal, so ATM selection needs the nominal price"
        )
    return closes


def _nearest_expiry(expiries, sample_date):
    """Expiry closest to ``sample_date + 30 days`` (ties -> earlier expiry).

    ``expiries`` is the sorted unique expiration list from ``chain.expiries``.
    Returns None if the list is empty or none of its entries parse as a date.
    """
    best = None
    best_gap = None
    for exp in expiries:
        exp_date = _parse_date(exp)
        if exp_date is None:
            continue
        gap = abs((exp_date - sample_date).days - _TARGET_DTE)
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = exp
    return best


def compute_sample(entry, closes):
    """Compute one ``{date, atm_iv}`` sample from a manifest entry.

    Returns ``(sample_dict, chain_file, None)`` on success, or
    ``(None, chain_file, reason)`` when the sample must be skipped (with the
    reason recorded). ``chain_file`` is echoed so the caller can delete it only
    on success.
    """
    sample_date_str = entry.get("date")
    chain_file = entry.get("chain_file")

    sample_date = _parse_date(sample_date_str)
    if sample_date is None:
        return None, chain_file, f"unparseable sample date {sample_date_str!r}"
    if not chain_file:
        return None, None, f"missing chain_file for {sample_date_str}"

    if not os.path.exists(chain_file):
        return None, chain_file, f"chain file not found: {chain_file}"

    # chain.load_contracts raises ValueError on an empty/holiday chain -- treat
    # that as a skip with a recorded reason (the LLM handles the step-back retry
    # before ever calling this script).
    try:
        contracts = chain.load_contracts(chain_file)
    except (ValueError, OSError) as exc:
        return None, chain_file, f"empty/unparseable chain ({exc})"
    if not contracts:
        return None, chain_file, "empty chain (holiday?)"

    spot = closes.get(sample_date_str[:10])
    if spot is None:
        return None, chain_file, f"no nominal close for {sample_date_str} in daily file"

    expiry = _nearest_expiry(chain.expiries(contracts), sample_date)
    if expiry is None:
        return None, chain_file, f"no usable expiry for {sample_date_str}"

    atm_iv = chain.atm_iv(contracts, spot, expiry)
    if atm_iv is None:
        return None, chain_file, f"atm_iv is None for {sample_date_str} (sparse chain)"

    return {"date": sample_date_str[:10], "atm_iv": atm_iv}, chain_file, None


def _load_existing_cache(out_path):
    """Return the existing ``samples`` list from ``out_path``, or [] if absent."""
    if not out_path or not os.path.exists(out_path):
        return []
    try:
        with open(out_path, "r") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        return []
    samples = cache.get("samples") if isinstance(cache, dict) else None
    return samples if isinstance(samples, list) else []


def merge_samples(existing, new_samples):
    """Merge two sample lists: dedupe by date (new wins), sort ascending."""
    by_date = {}
    for sample in existing:
        if isinstance(sample, dict) and sample.get("date"):
            by_date[sample["date"][:10]] = sample
    for sample in new_samples:
        by_date[sample["date"]] = sample  # new samples override same-date old ones
    return [by_date[d] for d in sorted(by_date)]


def build_iv_history(samples_path, daily_path, out_path, ticker=None):
    """Build/merge the IV-history cache from a fetch manifest + daily file.

    Returns a summary dict ``{ticker, written, computed, skipped, deleted}``
    where ``skipped`` is a list of ``{date, reason}`` records. Raises
    IVHistoryError on a fatal input error (unreadable manifest/daily).
    """
    try:
        with open(samples_path, "r") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        raise IVHistoryError(f"cannot read samples manifest {samples_path}: {exc}")

    if not isinstance(manifest, list):
        raise IVHistoryError(
            f"samples manifest {samples_path} must be a JSON list of "
            '{"date", "chain_file"} entries'
        )

    closes = load_daily_closes(daily_path)

    new_samples = []
    skipped = []
    consumed_chain_files = []
    for entry in manifest:
        if not isinstance(entry, dict):
            skipped.append({"date": None, "reason": f"non-dict manifest entry {entry!r}"})
            continue
        sample, chain_file, reason = compute_sample(entry, closes)
        if sample is None:
            skipped.append({"date": entry.get("date"), "reason": reason})
            continue
        new_samples.append(sample)
        if chain_file:
            consumed_chain_files.append(chain_file)

    existing = _load_existing_cache(out_path)
    merged = merge_samples(existing, new_samples)

    # Resolve the ticker: explicit flag wins, else an existing cache's ticker,
    # else derive from the out filename (iv_history_<T>.json), else null.
    resolved_ticker = ticker
    if resolved_ticker is None and out_path and os.path.exists(out_path):
        try:
            with open(out_path, "r") as fh:
                old = json.load(fh)
            if isinstance(old, dict):
                resolved_ticker = old.get("ticker")
        except (OSError, ValueError):
            resolved_ticker = None
    if resolved_ticker is None and out_path:
        base = os.path.basename(out_path)
        if base.startswith("iv_history_") and base.endswith(".json"):
            resolved_ticker = base[len("iv_history_"):-len(".json")] or None

    out_obj = {"ticker": resolved_ticker, "samples": merged}
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(out_obj, fh, indent=2)
        fh.write("\n")

    # Only after a successful write: delete each consumed chain file (the ``rm``
    # moved out of the SKILL loop). Missing files are ignored.
    deleted = []
    for chain_file in consumed_chain_files:
        try:
            os.remove(chain_file)
            deleted.append(chain_file)
        except OSError:
            continue

    return {
        "ticker": resolved_ticker,
        "written": out_path,
        "computed": len(new_samples),
        "skipped": skipped,
        "deleted": deleted,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Batch-compute the IV-history cache from a fetch manifest of "
        "offloaded HISTORICAL_OPTIONS chains + the daily raw file (spec B18)."
    )
    parser.add_argument(
        "--samples", required=True,
        help="path to raw/iv_samples.json (list of {date, chain_file})",
    )
    parser.add_argument(
        "--daily", required=True,
        help="path to raw/daily_adjusted.json (AV JSON; nominal '4. close' used)",
    )
    parser.add_argument(
        "--out", required=True,
        help="path to iv_history_<T>.json (merged/deduped/sorted in place)",
    )
    parser.add_argument(
        "--ticker", default=None,
        help="ticker for the cache header (else inferred from the out filename)",
    )
    args = parser.parse_args(argv)

    try:
        summary = build_iv_history(args.samples, args.daily, args.out, args.ticker)
    except IVHistoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"iv_history: wrote {summary['written']} "
        f"({summary['computed']} computed, {len(summary['skipped'])} skipped, "
        f"{len(summary['deleted'])} chains deleted)"
    )
    for skip in summary["skipped"]:
        print(f"  skipped {skip.get('date')}: {skip.get('reason')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
