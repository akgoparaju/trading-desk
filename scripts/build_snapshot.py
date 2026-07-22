"""Snapshot builder CLI for the trading-desk plugin.

WHY THIS MODULE EXISTS: This is the ONLY path from raw Alpha Vantage response
files to the numeric fields the LLM later reasons over. Every price, ratio,
return, drawdown, and valuation multiple in the snapshot is computed here in
Python; the LLM layer fills only qualitative TEXT slots (news summary, catalysts,
institutional-flow notes) and NEVER any number. If the arithmetic here is wrong,
every downstream trade decision inherits the error silently, so each field is a
pure, unit-testable builder function over already-parsed inputs and ``main()``
merely orchestrates I/O.

Input is a manifest-described bundle of raw JSON files (see the manifest schema
in the task spec). Every raw file is passed through TWO unwrappers in order:
``chain.unwrap`` (MCP content envelope) then ``unpreview`` (Alpha Vantage MCP
oversized-result envelope). Missing REQUIRED files are fatal (exit 2); missing
optional files leave their fields null and are disclosed in ``meta.missing``.

stdlib-only. Reuses scripts.indicators and scripts.chain for all math.
"""

import argparse
import csv
import io
import json
import math
import os
import sys

if sys.version_info < (3, 10):  # statistics.covariance/correlation need 3.10
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

from datetime import date, datetime, timedelta, timezone

# Allow direct invocation (``python3 scripts/build_snapshot.py``): ensure the
# repo root is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import chain, indicators

SCHEMA_VERSION = "0.4.0"

# Files that MUST be present; their absence aborts the build.
REQUIRED = ("global_quote", "overview", "daily_adjusted", "spy_daily_adjusted")

# Which snapshot blocks each manifest source vouches for (provenance). Only
# emitted for files actually present in the bundle.
COVERS = {
    "global_quote": ["price"],
    "overview": ["price", "fundamentals", "valuation", "sentiment", "events"],
    "daily_adjusted": ["technicals"],
    "spy_daily_adjusted": ["benchmark"],
    "income_statement": ["fundamentals"],
    "balance_sheet": ["fundamentals"],
    "cash_flow": ["fundamentals"],
    "earnings": ["fundamentals"],
    "earnings_estimates": ["fundamentals"],
    "news_sentiment": ["sentiment"],
    "insider_transactions": ["sentiment"],
    "options_chain": ["options", "sentiment"],
    "pc_ratio_realtime": ["sentiment"],
    "earnings_calendar": ["events"],
    "treasury_yield": ["macro"],
    "web_spot_check": ["price"],
    "short_interest": ["sentiment"],
    "web_fundamentals": ["fundamentals", "valuation"],
    # OPTIONAL (Track O4): the ticker's GICS-sector SPDR Select Sector ETF daily
    # series, for sector-relative RS. NOT in REQUIRED -- missing sector data must
    # never fail a snapshot (disclosed absence: the sector benchmark drops out).
    "sector_daily_adjusted": ["benchmark"],
}

# --------------------------------------------------------------------------- #
# Track O4 -- GICS sector -> SPDR Select Sector ETF (for sector-relative RS).
# --------------------------------------------------------------------------- #
#
# VERIFIED: AV COMPANY_OVERVIEW's ``Sector`` field is GICS-aligned (live bundles
# returned "COMMUNICATION SERVICES", "TECHNOLOGY", "INDUSTRIALS"). This map is the
# canonical GICS -> SPDR Select Sector correspondence, with the documented alias
# rows (GICS renames / common vendor variants) folded in. It is intentionally
# EXHAUSTIVE over the eleven Select-Sector SPDRs plus verified aliases; an
# UNKNOWN sector (or None) resolves to None -- the sector benchmark is OMITTED
# (disclosed absence), NEVER guessed. Keys are uppercased.
SECTOR_ETF = {
    "TECHNOLOGY": "XLK",
    "INFORMATION TECHNOLOGY": "XLK",
    "COMMUNICATION SERVICES": "XLC",
    "CONSUMER DISCRETIONARY": "XLY",
    "CONSUMER CYCLICAL": "XLY",
    "CONSUMER STAPLES": "XLP",
    "CONSUMER DEFENSIVE": "XLP",
    "ENERGY": "XLE",
    "FINANCIALS": "XLF",
    "FINANCIAL": "XLF",
    "FINANCE": "XLF",
    "HEALTH CARE": "XLV",
    "HEALTHCARE": "XLV",
    "INDUSTRIALS": "XLI",
    "MATERIALS": "XLB",
    "BASIC MATERIALS": "XLB",
    "REAL ESTATE": "XLRE",
    "UTILITIES": "XLU",
}


def resolve_sector_etf(sector):
    """Map an overview ``Sector`` string to its SPDR Select Sector ETF, or None.

    Uppercases + strips the input. Returns the mapped ETF symbol, or None for an
    unknown/None sector (the sector benchmark is then omitted -- a DISCLOSED
    absence; this function NEVER guesses an ETF for an unrecognized sector).
    """
    if not sector or not isinstance(sector, str):
        return None
    return SECTOR_ETF.get(sector.strip().upper())


# Trading-day windows.
_W1M, _W3M, _W6M, _W12M = 21, 63, 126, 252
_TEN_YR_ROWS = 2520
_FIVE_YR_ROWS = 1260


class BuildError(Exception):
    """Fatal build error (maps to exit 2 with a clear message)."""


# --------------------------------------------------------------------------- #
# Envelope handling + coercion helpers
# --------------------------------------------------------------------------- #

def unpreview(obj):
    """Unwrap an Alpha Vantage MCP oversized-result preview envelope.

    The MCP server wraps large results as
    ``{"preview": true, "sample_data": "<json-string>", ...}``. If ``obj`` is
    such a dict, parse ``sample_data`` as JSON and return it (it may itself be
    truncated with ``"data_truncated": true`` -- that is fine, we always take
    latest-first entries). Otherwise return ``obj`` unchanged.
    """
    if isinstance(obj, dict) and obj.get("preview") is True:
        sample = obj.get("sample_data")
        if isinstance(sample, str):
            try:
                return json.loads(sample)
            except ValueError:
                return obj
    return obj


def _unwrap_all(obj):
    """Apply both envelope unwrappers in order: chain.unwrap then unpreview."""
    return unpreview(chain.unwrap(obj))


def load_raw(path):
    """Read a raw JSON file and strip both envelopes. None if unreadable."""
    try:
        with open(path, "r") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    return _unwrap_all(obj)


def load_daily_raw(path):
    """Load a daily-series file that may be AV JSON or a bare stooq CSV.

    Tries JSON first (AV shape, or a {"result": csv} envelope). If the file is not
    JSON (a bare .csv/.txt stooq export), falls back to reading it as text so
    parse_daily_rows can take the CSV path. None only if the file is unreadable.
    """
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except OSError:
        return None
    try:
        return _unwrap_all(json.loads(text))
    except ValueError:
        return text  # bare CSV (or other non-JSON text); CSV detector handles it


_NULLISH = {"none", "-", "", "null", "n/a", "nan"}


def num(value):
    """Coerce a raw value to float, or None for null-ish / uncoercible input."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if value.strip().lower() in _NULLISH:
            return None
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_of_date(as_of_utc):
    """Return the YYYY-MM-DD date string of the as_of instant."""
    return as_of_utc[:10] if isinstance(as_of_utc, str) else None


def _parse_date(text):
    """Parse a YYYY-MM-DD string to a date, or None."""
    if not isinstance(text, str) or len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _mean(seq):
    """Arithmetic mean of a sequence, or None if empty."""
    seq = list(seq)
    return sum(seq) / len(seq) if seq else None


# --------------------------------------------------------------------------- #
# Raw-shape parsers
# --------------------------------------------------------------------------- #

# Disclosure label carried in technicals.series_source when the daily series was
# parsed from a stooq CSV export (close used as adjusted_close -- stooq closes are
# already split-adjusted, so the label discloses the convention explicitly).
SERIES_SOURCE_STOOQ = "stooq_csv_close_as_adjusted"


def _extract_daily_csv_text(payload):
    """Return the stooq-CSV text if ``payload`` is one, else None.

    Accepts a bare CSV string OR a {"result": "<csv>"} CSV-in-JSON envelope (the
    same envelope other CSV-bearing AV MCP responses use). Detection is by the
    header shape: the first non-empty line must start with ``Date,Open`` (case-
    insensitive). An AV JSON dict (which carries "Time Series (Daily)") is NOT a
    CSV and returns None so the JSON path is taken.
    """
    text = None
    if isinstance(payload, str):
        text = payload
    elif isinstance(payload, dict) and isinstance(payload.get("result"), str):
        text = payload["result"]
    if text is None:
        return None
    stripped = text.lstrip("﻿ \t\r\n")
    if stripped[:9].lower().startswith("date,open"):
        return text
    return None


def _parse_stooq_csv_rows(csv_text):
    """Parse a stooq daily CSV into ASCENDING rows (adjusted_close == close).

    Header: ``Date,Open,High,Low,Close,Volume``. stooq exports are already
    ascending, but we sort defensively. Rows with an unparseable/absent date are
    skipped. Raises BuildError if no valid row is produced.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    # Normalize header keys case-insensitively (stooq uses TitleCase).
    rows = []
    for raw in reader:
        row = {(k or "").strip().lower(): v for k, v in raw.items()}
        d = (row.get("date") or "").strip()
        if not d:
            continue
        close = num(row.get("close"))
        rows.append({
            "date": d,
            "open": num(row.get("open")),
            "high": num(row.get("high")),
            "low": num(row.get("low")),
            "close": close,
            "adjusted_close": close,  # stooq close IS split-adjusted
            "volume": num(row.get("volume")),
        })
    if not rows:
        raise BuildError("stooq CSV daily series parsed to zero rows")
    rows.sort(key=lambda r: r["date"])  # ascending
    return rows


def is_stooq_csv_daily(payload):
    """True if ``payload`` is a stooq-CSV daily series (bare or {"result": csv})."""
    return _extract_daily_csv_text(payload) is not None


def parse_daily_rows(payload):
    """Parse a daily series into ASCENDING (oldest-first) standard rows.

    Two accepted shapes:
      - AV JSON: ``{"Time Series (Daily)": {...}}`` (NEWEST-first keys) with the
        adjusted-close column -> parsed as before.
      - stooq CSV: a bare CSV string or a {"result": "<csv>"} envelope whose
        first line is ``Date,Open,...`` -> parsed with adjusted_close == close.

    Returns a list of dicts with keys date/open/high/low/close/adjusted_close/
    volume. Raises BuildError if neither shape yields rows.
    """
    csv_text = _extract_daily_csv_text(payload)
    if csv_text is not None:
        return _parse_stooq_csv_rows(csv_text)

    if not isinstance(payload, dict):
        raise BuildError("daily series is not a JSON object")
    ts = payload.get("Time Series (Daily)")
    if not isinstance(ts, dict) or not ts:
        raise BuildError("daily series missing 'Time Series (Daily)'")
    rows = []
    for d in sorted(ts):  # ascending
        bar = ts[d]
        rows.append({
            "date": d,
            "open": num(bar.get("1. open")),
            "high": num(bar.get("2. high")),
            "low": num(bar.get("3. low")),
            "close": num(bar.get("4. close")),
            "adjusted_close": num(bar.get("5. adjusted close")),
            "volume": num(bar.get("6. volume")),
        })
    return rows


def _quarterly(payload, key="quarterlyReports"):
    """Return the newest-first quarterly report list, or []."""
    if not isinstance(payload, dict):
        return []
    reports = payload.get(key)
    return reports if isinstance(reports, list) else []


# --------------------------------------------------------------------------- #
# Block builders (pure functions over parsed inputs)
# --------------------------------------------------------------------------- #

# Tolerance mirror: keep in sync with qc._MKTCAP_TOL (2%).  We reference the
# same constant value rather than importing qc to avoid a circular dependency
# (qc imports nothing from build_snapshot, but build_snapshot is a CLI entry
# point that should not pull in the whole QC stack at import time).  If
# qc._MKTCAP_TOL ever changes, update this value too.
_MKTCAP_TOL = 0.02  # +-2 % — mirrors qc._MKTCAP_TOL exactly

# Multi-class plausibility band: computed/overview ratio must fall in this
# half-open interval for the divergence to be treated as a known multi-class
# share-count scope error (e.g. AV SharesOutstanding = one class only).
# Outside this band the divergence is implausible for a class split and is
# retained as "computed_anomaly_retained" so QC can flag it.
_MULTICLASS_LO = 0.15  # exclusive lower bound
_MULTICLASS_HI = 1.0   # exclusive upper bound


def reconcile_mktcap(mktcap_overview, mktcap_computed, last, prev_close,
                     shares_m, tol=_MKTCAP_TOL):
    """Return (mktcap, basis) using a 4-case reconciliation rule.

    Case 1 – overview absent / ≤0:
        mktcap = mktcap_computed, basis = "computed_only".

    Case 2 – both present AND computed reconciles to overview within ``tol``
        at ``last`` OR ``prev_close`` (single-class issuer):
        mktcap = mktcap_computed, basis = "reconciled_agree".
        (keeps the fresher today's-price figure)

    Case 3 – both present, diverge beyond tol, AND the ratio
        computed/overview is in the multi-class plausibility band
        (0.15 < ratio < 1.0):
        mktcap = mktcap_overview, basis = "overview_authoritative".
        (AV MarketCapitalization is issuer-level; the computed figure
        is one-class only)

    Case 4 – both present, diverge beyond tol, ratio OUTSIDE the band:
        mktcap = mktcap_computed, basis = "computed_anomaly_retained".
        (implausible for a class split; keep computed and let QC flag it)

    Parameters
    ----------
    mktcap_overview : float | None
        AV COMPANY_OVERVIEW MarketCapitalization (issuer-level).
    mktcap_computed : float | None
        last × SharesOutstanding (in absolute dollars, not millions).
    last : float | None
        Current session close/last price.
    prev_close : float | None
        Prior session close.
    shares_m : float | None
        SharesOutstanding in millions.
    tol : float
        Relative tolerance for reconciliation (default mirrors qc._MKTCAP_TOL).
    """
    # Case 1: overview absent or non-positive
    if not (isinstance(mktcap_overview, (int, float)) and mktcap_overview > 0):
        return (mktcap_computed, "computed_only")

    # mktcap_computed must also be present and positive for cases 2-4
    if not (isinstance(mktcap_computed, (int, float)) and mktcap_computed > 0):
        return (mktcap_computed, "computed_only")

    diff_last = abs(mktcap_computed - mktcap_overview) / mktcap_overview

    # Case 2: reconciles at last price within tolerance
    if diff_last <= tol:
        return (mktcap_computed, "reconciled_agree")

    # Also check prev_close reconciliation (vendor mktcap may lag one session)
    if (isinstance(prev_close, (int, float)) and prev_close > 0
            and isinstance(shares_m, (int, float)) and shares_m > 0):
        computed_prev = prev_close * shares_m * 1e6
        diff_prev = abs(computed_prev - mktcap_overview) / mktcap_overview
        if diff_prev <= tol:
            return (mktcap_computed, "reconciled_agree")

    # Diverges — check plausibility band
    ratio = mktcap_computed / mktcap_overview
    if _MULTICLASS_LO < ratio < _MULTICLASS_HI:
        # Case 3: multi-class scope error — use authoritative issuer overview
        return (mktcap_overview, "overview_authoritative")

    # Case 4: implausible ratio — keep computed, let QC flag
    return (mktcap_computed, "computed_anomaly_retained")


def build_price(quote, overview, rows, web_spot):
    """Price block: quote, 52wk range, share count, market caps, ADV."""
    gq = quote.get("Global Quote", {}) if isinstance(quote, dict) else {}
    last = num(gq.get("05. price"))
    prev_close = num(gq.get("08. previous close"))
    high = num(gq.get("03. high"))
    low = num(gq.get("04. low"))
    # QF2: capture the vendor's latest-trading-day stamp for staleness disclosure.
    # Null when absent (e.g. web-fallback global_quote lacks this field).
    latest_trading_day = gq.get("07. latest trading day") or None

    shares = num(overview.get("SharesOutstanding")) if isinstance(overview, dict) else None
    shares_m = shares / 1e6 if shares is not None else None
    mktcap_overview = num(overview.get("MarketCapitalization")) if isinstance(overview, dict) else None
    mktcap_computed = last * shares if (last is not None and shares is not None) else None

    mktcap, mktcap_basis = reconcile_mktcap(
        mktcap_overview, mktcap_computed, last, prev_close, shares_m,
    )

    wk_high = num(overview.get("52WeekHigh")) if isinstance(overview, dict) else None
    wk_low = num(overview.get("52WeekLow")) if isinstance(overview, dict) else None

    # Average dollar volume over last ~63 trading days (raw close * volume).
    dollar = [r["close"] * r["volume"] for r in rows
              if r["close"] is not None and r["volume"] is not None]
    adv = _mean(dollar[-_W3M:]) if dollar else None

    spot_block = None
    if isinstance(web_spot, dict) and num(web_spot.get("price")) is not None:
        spot_block = {"price": num(web_spot.get("price")),
                      "source_url": web_spot.get("source_url")}

    return {
        "last": last,
        "prev_close": prev_close,
        "intraday_range": [low, high],
        "wk52_high": wk_high,
        "wk52_low": wk_low,
        "shares_diluted_m": shares_m,
        "mktcap_overview": mktcap_overview,
        "mktcap_computed": mktcap_computed,
        "mktcap": mktcap,
        "mktcap_basis": mktcap_basis,
        "adv_dollar_3m": adv,
        "web_spot_check": spot_block,
        # QF2: vendor latest-trading-day stamp; moved to meta.latest_trading_day
        # by the caller (build_snapshot) -- kept here so build_price stays a
        # pure function over its inputs without separate return values.
        "_latest_trading_day": latest_trading_day,
    }


# Multi-listing sibling share classes, keyed by ticker (O15). This is a STATIC,
# EXPLICITLY-CURATED map: an entry exists ONLY when the sibling relationship is
# known and verified, never guessed. GOOG (Class C) and GOOGL (Class A) are the
# two publicly-listed Alphabet classes (Class B is unlisted). To add another
# multi-listed issuer, add its verified pair here EXPLICITLY -- an unknown ticker
# returns [] (never a fabricated sibling).
_SHARE_CLASS_SIBLINGS = {
    "GOOG": ["GOOGL"],
    "GOOGL": ["GOOG"],
}


def _parse_share_class(name):
    """Extract a share-class label from an AV overview Name, else None.

    AV's COMPANY_OVERVIEW Name encodes the listed class for multi-class issuers
    (e.g. "Alphabet Inc Class C" -> "C"). We look for a trailing "Class <X>"
    token and return the label verbatim; no class token -> None (single-class or
    unlabelled). Pure string parse, never a guess about the corporate structure.
    """
    if not isinstance(name, str):
        return None
    tokens = name.split()
    for i, tok in enumerate(tokens):
        if tok.lower() == "class" and i + 1 < len(tokens):
            label = tokens[i + 1].strip().strip(".,")
            return label or None
    return None


def build_security_master(price, overview, ticker):
    """Issuer/security-master block: formalize the security-vs-issuer split (O15).

    AV's COMPANY_OVERVIEW SharesOutstanding is a SINGLE listed share class (a
    security-level count), while its MarketCapitalization is the ISSUER-level cap
    (all classes). build_price already reconciled the authoritative cap into
    ``price.mktcap`` (G1). This block makes the split first-class and QC-checkable
    and reconciles the issuer share count from data already present -- NO guessing,
    every derived figure carries a disclosed ``shares_source``. Pure over its
    inputs; ADDITIVE (no scorer reads it).

    issuer_total_shares_m derivation (disclosed source, never guessed):
      * single-class basis (mktcap_basis in {"reconciled_agree", "computed_only"}):
        this listed class IS the whole issuer, so issuer_total = class_shares_m,
        shares_source="av_class_shares", reconciled_to_filing = (overview present).
      * multi-class basis ("overview_authoritative"): AV's class share count
        undercounts the issuer, so derive issuer_total = issuer_mktcap / last
        (the authoritative issuer cap over the class price), shares_source=
        "derived: issuer mktcap / class price", reconciled_to_filing=False. Exact
        by construction against the issuer cap; disclosed as derived.
      * degraded (no overview, or no usable last/cap to derive from): emit the
        block with nulls and shares_source="unavailable" -- never fabricate.

    issuer_diluted_shares_m == issuer_total_shares_m: the snapshot carries no
    separate issuer-diluted source, so total is the best available diluted proxy
    (disclosed by equality, not invented).
    """
    price = price if isinstance(price, dict) else {}
    ov = overview if isinstance(overview, dict) else {}

    last = price.get("last")
    class_shares_m = price.get("shares_diluted_m")
    issuer_mktcap = price.get("mktcap")
    mktcap_basis = price.get("mktcap_basis")

    share_class = _parse_share_class(ov.get("Name")) if ov else None
    other_listed = list(_SHARE_CLASS_SIBLINGS.get(ticker, []))

    def _num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    issuer_total_shares_m = None
    shares_source = "unavailable"
    reconciled_to_filing = False

    if mktcap_basis == "overview_authoritative":
        # Multi-class: AV class-share count undercounts the issuer. Derive the
        # issuer share count from the authoritative issuer cap over the class
        # price (exact vs the cap by construction). Disclosed as derived.
        if _num(issuer_mktcap) and _num(last) and last > 0:
            issuer_total_shares_m = issuer_mktcap / last / 1e6
            shares_source = "derived: issuer mktcap / class price"
            reconciled_to_filing = False
        # else: degraded -> nulls + "unavailable" (fall through)
    elif mktcap_basis in ("reconciled_agree", "computed_only"):
        # Single-class: this listed class IS the whole issuer.
        if _num(class_shares_m):
            issuer_total_shares_m = class_shares_m
            shares_source = "av_class_shares"
            # Reconciled to the filing/vendor overview iff an overview was present.
            reconciled_to_filing = bool(ov)
    # Any other basis (e.g. computed_anomaly_retained) or absent inputs -> the
    # issuer count is not derivable without guessing: leave null + "unavailable".

    issuer_diluted_shares_m = issuer_total_shares_m

    return {
        "ticker": ticker,
        "share_class": share_class,
        "class_shares_m": class_shares_m,
        "issuer_total_shares_m": issuer_total_shares_m,
        "issuer_diluted_shares_m": issuer_diluted_shares_m,
        "issuer_mktcap": issuer_mktcap,
        "mktcap_basis": mktcap_basis,
        "shares_source": shares_source,
        "reconciled_to_filing": reconciled_to_filing,
        "other_listed_classes": other_listed,
    }


def _rolling_rv30_series(adj):
    """Rolling 30-day realized-vol series over the full adjusted history.

    Each point is realized_vol over a 31-value window (30 log-returns). Simple
    loop -- fine for a few thousand rows. Empty if history too short.
    """
    out = []
    for start in range(0, len(adj) - 30):
        rv = indicators.realized_vol(adj[start:start + 31], 30)
        if rv is not None:
            out.append(rv)
    return out


def _weinstein_stage(last, ma50, ma200, ma50_slope, ma200_slope,
                     flat_band=0.005):
    """Weinstein market stage {1 basing | 2 advancing | 3 topping | 4 declining}.

    Derived from ``price.last`` vs ma50 vs ma200 and the two 20-day MA-slope
    signs (all pre-computed fields). The ``flat_band`` is a Philosophy-A default:
    a slope whose magnitude is <= ``flat_band`` (0.5% over 20 days) is treated as
    FLAT, not rising/falling -- so a barely-drifting MA does not manufacture a
    stage-2 or stage-4 read. Documented threshold, not calibrated.

    Rules (spec):
      - stage 2 (advancing): last > ma50 > ma200 AND both slopes rising (> band)
      - stage 4 (declining): last < ma50 < ma200 AND both slopes falling (< -band)
      - stage 3 (topping):   last < ma50 BUT ma200 still rising (> band)
      - stage 1 (basing):    everything else (the default)
    Returns None when any of the four MA inputs is missing (can't classify).
    """
    if last is None or ma50 is None or ma200 is None \
            or ma50_slope is None or ma200_slope is None:
        return None
    rising50 = ma50_slope > flat_band
    rising200 = ma200_slope > flat_band
    falling50 = ma50_slope < -flat_band
    falling200 = ma200_slope < -flat_band
    if last > ma50 > ma200 and rising50 and rising200:
        return 2
    if last < ma50 < ma200 and falling50 and falling200:
        return 4
    if last < ma50 and rising200:
        return 3
    return 1


def build_technicals(rows, series_source=None, next_earnings_date=None,
                     earn_q=None):
    """Technicals block from adjusted-close + volume series (oldest-first).

    ``series_source`` (optional) discloses how the series was obtained. When the
    daily series came from a stooq CSV export (close used as adjusted_close) the
    caller passes ``SERIES_SOURCE_STOOQ`` and it is echoed into the block; for the
    default AV path it is None and the key is omitted (unchanged shape).

    Wave 4A additions (all pure-OHLCV, deterministic, additive, null-safe):
      - adx14: Wilder ADX(14) trend strength (raw high/low/close).
      - stage: Weinstein 1/2/3/4 regime from price vs ma50/ma200 + slope signs.
      - ad_line_slope: 20-day slope sign+magnitude of the Chaikin A/D line.
      - upvol_ratio: up-day volume / total volume over the trailing ~50 bars.
      - vwap_52wk_high / vwap_earnings: anchored VWAPs (institutional cost basis).
        The 52wk-high anchor is the date of the max adjusted_close over the
        trailing 252 rows (derived inline). The earnings anchor is
        ``next_earnings_date`` when given, else the latest reported quarter date
        (``earn_q[0].reportedDate``); if that anchor post-dates every row (a
        future earnings date) the VWAP is honestly None.
    """
    adj = [r["adjusted_close"] for r in rows if r["adjusted_close"] is not None]
    vols = [r["volume"] for r in rows if r["volume"] is not None]

    macd = indicators.macd(adj)
    rv30 = indicators.realized_vol(adj, 30)
    rv30_series = _rolling_rv30_series(adj)
    rv30_pctile = (indicators.percentile_rank(rv30, rv30_series)
                   if rv30 is not None else None)

    vol_20 = _mean(vols[-20:]) if len(vols) >= 20 else None
    vol_90 = _mean(vols[-90:]) if len(vols) >= 90 else None
    vol_ratio = vol_20 / vol_90 if (vol_20 and vol_90) else None

    ten_yr = adj[-_TEN_YR_ROWS:]

    # A1: overnight-gap tail statistics. The gap series is open[i]/adj_close[i-1]-1
    # over the retained rows (open is parsed but was previously unused downstream).
    # All figures deterministic; the 2sigma jump threshold is a documented
    # convention (indicators.jump_count_2sigma). Null block if too few gaps.
    gaps = indicators.overnight_gap_series(rows)
    abs_gaps = sorted(abs(g) for g in gaps)
    overnight_gap = None
    if abs_gaps:
        n_gaps = len(abs_gaps)
        # p95 via nearest-rank on the sorted abs series (ceil(0.95*n) - 1, clamped).
        idx = min(n_gaps - 1, max(0, math.ceil(0.95 * n_gaps) - 1))
        overnight_gap = {
            "mean_abs": sum(abs_gaps) / n_gaps,
            "p95_abs": abs_gaps[idx],
            "max_abs": abs_gaps[-1],
            "excess_kurtosis": indicators.excess_kurtosis(gaps),
            "jump_count_2sigma": indicators.jump_count_2sigma(gaps),
            "n": n_gaps,
        }

    # -- Wave 4A: MA stack + slopes (needed both for the block and the stage) --
    ma50 = indicators.sma(adj, 50)
    ma200 = indicators.sma(adj, 200)
    ma50_slope = indicators.ma_slope(adj, 50, 20)
    ma200_slope = indicators.ma_slope(adj, 200, 20)
    last_px = adj[-1] if adj else None

    # Weinstein regime stage (guard field; pure OHLCV, null-safe).
    stage = _weinstein_stage(last_px, ma50, ma200, ma50_slope, ma200_slope)

    # ADX(14) trend strength on RAW high/low/close (same-scale range measure).
    adx14 = indicators.adx(rows, 14)

    # Chaikin A/D line 20-day slope (accumulation vs distribution).
    ad_slope = indicators.ad_line_slope(rows, 20)

    # Up-day volume share over the trailing ~50 bars.
    upvol = indicators.updown_volume(rows, 50)

    # -- Wave 4A: anchored VWAPs (institutional cost-basis levels) ------------
    # 52wk-high anchor: date of the max adjusted_close over the trailing 252 rows.
    trailing = rows[-_W12M:] if rows else []
    hi_anchor = None
    hi_val = None
    for r in trailing:
        ac = r.get("adjusted_close")
        if ac is None:
            continue
        if hi_val is None or ac > hi_val:
            hi_val = ac
            hi_anchor = r.get("date")
    vwap_52wk_high = indicators.anchored_vwap(rows, hi_anchor) if hi_anchor else None

    # Earnings anchor: next_earnings.date if given, else latest reported quarter.
    earn_anchor = next_earnings_date
    if not earn_anchor and isinstance(earn_q, list) and earn_q:
        first = earn_q[0]
        if isinstance(first, dict):
            earn_anchor = first.get("reportedDate")
    vwap_earnings = indicators.anchored_vwap(rows, earn_anchor) if earn_anchor else None

    block = {
        "ma50": ma50,
        "ma200": ma200,
        "ma50_slope_20d": ma50_slope,
        "ma200_slope_20d": ma200_slope,
        "adx14": adx14,                      # Wave 4A: Wilder ADX(14) trend strength
        "stage": stage,                      # Wave 4A: Weinstein regime 1/2/3/4
        "rsi14": indicators.rsi(adj, 14),
        "macd": macd["macd"] if macd else None,
        "macd_signal": macd["signal"] if macd else None,
        "ret_15d": indicators.pct_return(adj, 15),
        "ret_1m": indicators.pct_return(adj, _W1M),
        "ret_3m": indicators.pct_return(adj, _W3M),
        "ret_6m": indicators.pct_return(adj, _W6M),
        "ret_12m": indicators.pct_return(adj, _W12M),
        "rv20_ann": indicators.realized_vol(adj, 20),
        "rv30_ann": rv30,
        "rv30_vs_10yr_pctile": rv30_pctile,
        "dist_from_ath_pct": indicators.dist_from_high(adj),
        "vol_20d_vs_90d": vol_ratio,
        "ad_line_slope": ad_slope,           # Wave 4A: A/D-line 20d slope
        "upvol_ratio": upvol,                # Wave 4A: up-day volume share
        "vwap_52wk_high": vwap_52wk_high,     # Wave 4A: anchored VWAP @ 52wk high
        "vwap_earnings": vwap_earnings,       # Wave 4A: anchored VWAP @ earnings
        "max_dd_10yr": indicators.max_drawdown(ten_yr),
        "dd_episodes_20pct_10yr": indicators.drawdown_episodes(ten_yr, 0.20),
        "dd_episodes_30pct_10yr": indicators.drawdown_episodes(ten_yr, 0.30),
        "drawdowns_by_year": indicators.drawdowns_by_year(
            [{"date": r["date"], "adjusted_close": r["adjusted_close"]}
             for r in rows if r["adjusted_close"] is not None])[-10:],
        "ohlcv_rows": len(rows),
        "last_ohlcv_date": rows[-1]["date"] if rows else None,
        "overnight_gap": overnight_gap,   # A1: tail/gap disclosure (unscored)
    }
    if series_source:
        block["series_source"] = series_source
    return block


def build_benchmark(stock_rows, spy_rows, sector_rows=None, sector_etf=None):
    """Benchmark block: SPY returns + beta/corr of stock vs SPY.

    Track O4: when ``sector_rows`` is provided (the ticker's GICS-sector SPDR
    ETF daily series), ALSO emit ``sector_etf``, ``sector_ret_{1m,3m,6m,12m}``
    (via ``indicators.pct_return`` on the sector's adjusted-close series, the SAME
    windows SPY uses), and ``rel_sector_ret_3m`` / ``rel_sector_ret_6m`` =
    stock_ret_Nm - sector_ret_Nm (stock returns computed the same way SPY's are,
    off ``stock_rows``). When ``sector_rows`` is None, NONE of these keys are
    emitted -- the block is byte-identical to the pre-O4 benchmark (disclosed
    absence: missing sector data drops the sector benchmark, never fails).
    """
    spy_adj = [r["adjusted_close"] for r in spy_rows if r["adjusted_close"] is not None]
    stock_adj = [r["adjusted_close"] for r in stock_rows if r["adjusted_close"] is not None]
    bc = indicators.beta_corr(stock_adj, spy_adj)
    block = {
        "spy_ret_1m": indicators.pct_return(spy_adj, _W1M),
        "spy_ret_3m": indicators.pct_return(spy_adj, _W3M),
        "spy_ret_6m": indicators.pct_return(spy_adj, _W6M),
        "spy_ret_12m": indicators.pct_return(spy_adj, _W12M),
        "beta": bc["beta"] if bc else None,
        "corr": bc["corr"] if bc else None,
        "beta_n_days": bc["n_days"] if bc else None,
    }

    if sector_rows is not None:
        sector_adj = [r["adjusted_close"] for r in sector_rows
                      if r["adjusted_close"] is not None]
        sector_ret_3m = indicators.pct_return(sector_adj, _W3M)
        sector_ret_6m = indicators.pct_return(sector_adj, _W6M)
        stock_ret_3m = indicators.pct_return(stock_adj, _W3M)
        stock_ret_6m = indicators.pct_return(stock_adj, _W6M)
        block["sector_etf"] = sector_etf
        block["sector_ret_1m"] = indicators.pct_return(sector_adj, _W1M)
        block["sector_ret_3m"] = sector_ret_3m
        block["sector_ret_6m"] = sector_ret_6m
        block["sector_ret_12m"] = indicators.pct_return(sector_adj, _W12M)
        # Relative RS: stock return minus sector return (None if either leg absent).
        block["rel_sector_ret_3m"] = (
            stock_ret_3m - sector_ret_3m
            if stock_ret_3m is not None and sector_ret_3m is not None else None)
        block["rel_sector_ret_6m"] = (
            stock_ret_6m - sector_ret_6m
            if stock_ret_6m is not None and sector_ret_6m is not None else None)

    return block


def _sum4(reports, key):
    """Sum of ``key`` over the first 4 (newest) quarterly reports; None if all null."""
    vals = [num(r.get(key)) for r in reports[:4]]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _estimate_partitions(estimates, as_of_date):
    """Split estimate rows into future quarters/years, each sorted ascending.

    Returns (future_quarters, future_years) where each is a list of estimate
    dicts with a parseable date strictly after ``as_of_date``.
    """
    fq, fy = [], []
    cutoff = _parse_date(as_of_date)
    for row in estimates:
        if not isinstance(row, dict):
            continue
        d = _parse_date(row.get("date"))
        if d is None or cutoff is None or d <= cutoff:
            continue
        horizon = (row.get("horizon") or "").lower()
        if "quarter" in horizon:
            fq.append((d, row))
        elif "year" in horizon:
            fy.append((d, row))
    fq.sort(key=lambda x: x[0])
    fy.sort(key=lambda x: x[0])
    return [r for _, r in fq], [r for _, r in fy]


def build_fundamentals(income, balance, cashflow, earnings, estimates,
                       overview, as_of_date):
    """Fundamentals block: TTM sums, growth, margins, EPS, revisions, net cash."""
    inc_q = _quarterly(income)
    bal_q = _quarterly(balance)
    cf_q = _quarterly(cashflow)
    earn_q = _quarterly(earnings, "quarterlyEarnings")

    rev_ttm = _sum4(inc_q, "totalRevenue")
    gp_ttm = _sum4(inc_q, "grossProfit")
    oi_ttm = _sum4(inc_q, "operatingIncome")
    ni_ttm = _sum4(inc_q, "netIncome")

    def _margin(numer):
        if numer is None or rev_ttm in (None, 0):
            return None
        return numer / rev_ttm

    # rev growth: latest quarter vs same quarter one year ago (index 4).
    rev_growth = None
    if len(inc_q) >= 5:
        rev_now = num(inc_q[0].get("totalRevenue"))
        rev_prior = num(inc_q[4].get("totalRevenue"))
        if rev_now is not None and rev_prior not in (None, 0):
            rev_growth = rev_now / rev_prior - 1

    eps_ttm = num(overview.get("EPS")) if isinstance(overview, dict) else None
    eps_computed = None
    eps_vals = [num(r.get("reportedEPS")) for r in earn_q[:4]]
    eps_vals = [v for v in eps_vals if v is not None]
    if eps_vals:
        eps_computed = sum(eps_vals)

    # NTM EPS consensus.
    fq, fy = _estimate_partitions(estimates or [], as_of_date)
    eps_ntm = None
    eps_ntm_method = None
    if len(fq) >= 4:
        vals = [num(r.get("eps_estimate_average")) for r in fq[:4]]
        vals = [v for v in vals if v is not None]
        if len(vals) == 4:
            eps_ntm = sum(vals)
            eps_ntm_method = "sum_next_4_fiscal_quarters"
    if eps_ntm is None and fy:
        v = num(fy[0].get("eps_estimate_average"))
        if v is not None:
            eps_ntm = v
            eps_ntm_method = "nearest_future_fiscal_year"

    # Revisions + next-FY consensus from nearest FUTURE fiscal-year row.
    # QF5: trace WHY revisions_90d is null so silence is replaced by disclosure.
    revisions = None
    revisions_null_reason = None
    next_fy = None
    if not fy:
        # No future fiscal-year estimates row exists in the payload.
        revisions_null_reason = "no_future_fy_row"
    else:
        row = fy[0]
        eps_now = num(row.get("eps_estimate_average"))
        eps_90 = num(row.get("eps_estimate_average_90_days_ago"))
        pct = (eps_now / eps_90 - 1) if (eps_now is not None and eps_90) else None
        up_30d = num(row.get("eps_estimate_revision_up_trailing_30_days"))
        down_30d = num(row.get("eps_estimate_revision_down_trailing_30_days"))
        revisions = {
            "eps_now": eps_now,
            "eps_90d_ago": eps_90,
            "pct": pct,
            "up_30d": up_30d,
            "down_30d": down_30d,
        }
        # QF5: record why the pct / up_30d / down_30d fields are null within a
        # present FY row -- distinguishes "fields absent" from "row absent".
        if pct is None or up_30d is None or down_30d is None:
            absent_fields = []
            if eps_now is None:
                absent_fields.append("eps_estimate_average")
            if eps_90 is None:
                absent_fields.append("eps_estimate_average_90_days_ago")
            if up_30d is None:
                absent_fields.append("eps_estimate_revision_up_trailing_30_days")
            if down_30d is None:
                absent_fields.append("eps_estimate_revision_down_trailing_30_days")
            if absent_fields:
                revisions_null_reason = (
                    "future_fy_row_present_but_fields_absent: "
                    + ", ".join(absent_fields)
                )
        next_fy = {"rev": num(row.get("revenue_estimate_average")),
                   "eps": num(row.get("eps_estimate_average"))}

    # FCF TTM = sum4q (operatingCashflow - capitalExpenditures).
    fcf_ttm = None
    if cf_q:
        parts = []
        for r in cf_q[:4]:
            ocf = num(r.get("operatingCashflow"))
            capex = num(r.get("capitalExpenditures"))
            if ocf is not None and capex is not None:
                parts.append(ocf - capex)
        if parts:
            fcf_ttm = sum(parts)

    # Net cash is computable only from an actual balance sheet. With no balance
    # quarters at all, leave it null so the web-fundamentals gap-fill can supply
    # a cited figure (an all-zeros reconciliation from {} would be meaningless).
    net_cash = _build_net_cash(bal_q[0]) if bal_q else None
    roe = num(overview.get("ReturnOnEquityTTM")) if isinstance(overview, dict) else None

    return {
        "rev_ttm": rev_ttm,
        "rev_growth_latest_q": rev_growth,
        "gm_ttm": _margin(gp_ttm),
        "om_ttm": _margin(oi_ttm),
        "nm_ttm": _margin(ni_ttm),
        "eps_ttm": eps_ttm,
        "eps_ttm_computed": eps_computed,
        "eps_ntm_consensus": eps_ntm,
        "eps_ntm_method": eps_ntm_method,
        "revisions_90d": revisions,
        "revisions_null_reason": revisions_null_reason,  # QF5: null when revisions populated
        "next_fy_consensus": next_fy,
        "fcf_ttm": fcf_ttm,
        "net_cash_defined": net_cash,
        "roe": roe,
    }


# Fields the web_fundamentals transcription may supply, in disclosure order.
# Each maps 1:1 to a fundamentals-block key; a field is web-filled ONLY when the
# statement-derived path left it null (statement data always wins).
_WEB_FUND_FIELDS = (
    "rev_ttm", "rev_growth_latest_q", "gm_ttm", "om_ttm", "nm_ttm",
    "eps_ttm", "eps_ntm_consensus", "fcf_ttm", "net_cash_defined", "roe",
)


def apply_web_fundamentals(fundamentals, web):
    """Gap-fill ``fundamentals`` from a web_fundamentals transcription in place.

    Statement-derived data wins: a field is filled from ``web`` ONLY when its
    current value is null (the statement path could not compute it). Every filled
    field is appended to ``fundamentals['web_transcribed_fields']`` (disclosure).
    Always establishes the ``web_transcribed_fields`` array (empty if nothing was
    filled or ``web`` is absent). Returns the fundamentals dict.
    """
    filled = []
    if isinstance(web, dict):
        for field in _WEB_FUND_FIELDS:
            if fundamentals.get(field) is not None:
                continue  # statement value present -> keep it
            if field not in web or web.get(field) is None:
                continue  # web has nothing to offer here
            value = web[field]
            if field != "net_cash_defined":
                value = num(value)
                if value is None:
                    continue
            fundamentals[field] = value
            filled.append(field)
    fundamentals["web_transcribed_fields"] = filled
    return fundamentals


def _build_net_cash(bal):
    """Net-cash reconciliation from the latest quarterly balance sheet."""
    cash_st = num(bal.get("cashAndShortTermInvestments"))
    if cash_st is None:
        cc = num(bal.get("cashAndCashEquivalentsAtCarryingValue"))
        sti = num(bal.get("shortTermInvestments"))
        if cc is not None or sti is not None:
            cash_st = (cc or 0.0) + (sti or 0.0)
    lt_inv = num(bal.get("longTermInvestments")) or 0.0

    total_debt = num(bal.get("shortLongTermDebtTotal"))
    if total_debt is None:
        std = num(bal.get("shortTermDebt"))
        ltd = num(bal.get("longTermDebt"))
        if std is not None or ltd is not None:
            total_debt = (std or 0.0) + (ltd or 0.0)
        else:
            total_debt = 0.0

    if cash_st is None:
        cash_st = 0.0
    net = cash_st + lt_inv - total_debt
    return {"cash_st": cash_st, "lt_inv": lt_inv,
            "total_debt": total_debt, "net": net}


def build_valuation(price, fundamentals, overview, rows):
    """Valuation block: P/E ttm/fwd, EV/EBITDA, PEG, historical P/E medians."""
    last = price.get("last")
    eps_ttm = fundamentals.get("eps_ttm")
    eps_ntm = fundamentals.get("eps_ntm_consensus")
    fcf_ttm = fundamentals.get("fcf_ttm")
    mktcap = price.get("mktcap") or price.get("mktcap_computed")

    pe_ttm = (last / eps_ttm) if (last is not None and eps_ttm and eps_ttm > 0) else None
    pe_fwd = (last / eps_ntm) if (last is not None and eps_ntm and eps_ntm > 0) else None

    ov = overview if isinstance(overview, dict) else {}
    pe_overview = num(ov.get("PERatio"))
    ev_ebitda = num(ov.get("EVToEBITDA"))
    peg = num(ov.get("PEGRatio"))

    def _pe_median(window):
        if not (eps_ttm and eps_ttm > 0):
            return None
        adj = [r["adjusted_close"] for r in rows[-window:] if r["adjusted_close"] is not None]
        if not adj:
            return None
        pes = sorted(c / eps_ttm for c in adj)
        n = len(pes)
        mid = n // 2
        return pes[mid] if n % 2 else (pes[mid - 1] + pes[mid]) / 2

    fcf_yield = (fcf_ttm / mktcap) if (fcf_ttm is not None and mktcap) else None

    return {
        "pe_ttm": pe_ttm,
        "pe_fwd": pe_fwd,
        "pe_overview": pe_overview,
        "ev_ebitda_fwd": ev_ebitda,
        "peg": peg,
        "pe_5yr_median": _pe_median(_FIVE_YR_ROWS),
        "pe_10yr_median": _pe_median(_TEN_YR_ROWS),
        "pe_median_method": "approx_current_eps",
        "fcf_yield": fcf_yield,
    }


def _chain_date(chain_path):
    """Read the chain raw file cheaply and return the max contract 'date'.

    Chain contracts from HISTORICAL_OPTIONS carry a per-contract "date" field.
    We re-read the raw JSON (unwrapping both envelopes) WITHOUT modifying
    chain.py and take the max date present across contracts. Returns None if no
    contract carries a date (caller falls back to file mtime).
    """
    raw = load_raw(chain_path)
    contracts = None
    if isinstance(raw, list):
        contracts = raw
    elif isinstance(raw, dict):
        for key in ("data", "options"):
            if isinstance(raw.get(key), list):
                contracts = raw[key]
                break
    if not contracts:
        return None
    dates = [c.get("date") for c in contracts
             if isinstance(c, dict) and c.get("date")]
    return max(dates) if dates else None


def _nearest_expiry(expiries_list, as_of_date, target_days):
    """Expiry whose day-distance from as_of is closest to ``target_days``."""
    base = _parse_date(as_of_date)
    if base is None or not expiries_list:
        return None
    best = None
    best_diff = None
    for exp in expiries_list:
        d = _parse_date(exp)
        if d is None:
            continue
        days = (d - base).days
        diff = abs(days - target_days)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = exp
    return best


def build_options(chain_path, chain_path_manifest, price, next_earnings_date,
                  rv20_ann, as_of_date, last_ohlcv_date=None,
                  rows=None, earn_q=None):
    """Options block from the on-disk chain (never loaded into LLM context).

    Wave 4B additions (deterministic, additive, null-safe):
      - event_vol: chain.event_implied_vol on the bracketing expiries around
        ``next_earnings_date`` -- the isolated earnings-day 1-sigma + the pre/post
        IVs and expiries. Null when there is no earnings date, no bracketing
        expiry pair, or a missing ATM IV on either side.
      - rv20_ex_earnings: indicators.realized_vol_ex_earnings over the daily
        closes/dates, masking returns within +/-1 session of any own-history
        earnings print (the ``events.earnings_move_history`` quarter_end dates ==
        the quarterlyEarnings reportedDates). Null when the chain/rows are absent
        or too few unmasked returns remain. The IV-vs-realized PRIMARY GATE
        (``iv_minus_rv20``) compares iv30 against this cleaner ex-earnings RV when
        available (``rv20_for_iv_comparison`` is the RV actually used); the
        contaminated ``rv20_ann`` and a ``rv20_ex_earnings_stripped`` flag are
        emitted for disclosure. Self-limiting: equals rv20_ann when no print falls
        in the trailing-20d window.
    """
    contracts = chain.load_contracts(chain_path)
    spot = price.get("last")
    # QF3: filter to future expiries only so no expired expiry reaches
    # expected_moves or atm_iv_by_expiry. chain.expiries() stays pure.
    exps = chain.future_expiries(contracts, as_of_date)

    chain_as_of = _chain_date(chain_path)
    if chain_as_of is None:
        # An EOD chain with no per-contract date is, by definition, as of the
        # latest trading day — using file mtime here would be the *build* day
        # and would falsely trip qc.check_options_freshness.
        chain_as_of = last_ohlcv_date
    if chain_as_of is None:
        try:
            mtime = os.path.getmtime(chain_path)
            chain_as_of = datetime.fromtimestamp(mtime, tz=timezone.utc).date().isoformat()
        except OSError:
            chain_as_of = None

    exp_near = _nearest_expiry(exps, as_of_date, 0)
    exp_30 = _nearest_expiry(exps, as_of_date, 30)
    exp_60 = _nearest_expiry(exps, as_of_date, 60)
    exp_90 = _nearest_expiry(exps, as_of_date, 90)

    # Dedupe the expiry set preserving order.
    exp_set = []
    for e in (exp_near, exp_30, exp_60, exp_90):
        if e is not None and e not in exp_set:
            exp_set.append(e)

    iv30 = chain.atm_iv(contracts, spot, exp_30) if (spot and exp_30) else None

    expected_moves = []
    for e in exp_set:
        em = chain.expected_move(contracts, spot, e) if spot else None
        if em is not None:
            expected_moves.append(em)

    max_pain_by_expiry = []
    for e in exp_set:
        mp = chain.max_pain(contracts, e)
        max_pain_by_expiry.append({"expiry": e, "max_pain": mp})

    walls = chain.oi_walls(contracts, exp_30, spot) if (exp_30 and spot) else None
    skew = chain.skew_25d(contracts, spot, exp_30) if (exp_30 and spot) else None

    # Wave 4B: event-vol extraction around the next earnings print. Uses ALL
    # contracts (event_implied_vol navigates expiries itself and finds the
    # bracketing pair around next_earnings_date). Null-safe when no earnings date
    # / no bracketing pair / missing ATM IV.
    event_vol = (
        chain.event_implied_vol(contracts, spot, next_earnings_date, as_of_date)
        if (spot and next_earnings_date) else None
    )

    # Wave 4B: ex-earnings realized vol. The earnings dates are the own-history
    # reaction quarter_end dates == quarterlyEarnings reportedDates (same source
    # build_earnings_move_history reads for events.earnings_move_history).
    rv20_ex_earnings = None
    if rows:
        closes = [r.get("adjusted_close") for r in rows]
        dates = [r.get("date") for r in rows]
        earnings_dates = []
        if isinstance(earn_q, list):
            earnings_dates = [q.get("reportedDate") for q in earn_q
                              if isinstance(q, dict) and q.get("reportedDate")]
        rv20_ex_earnings = indicators.realized_vol_ex_earnings(
            closes, dates, earnings_dates, 20)

    # Wave 4B (code-review fix): the IV-vs-realized PRIMARY GATE compares iv30
    # against the CLEANER ex-earnings RV when it is available. This is
    # SELF-LIMITING: realized_vol_ex_earnings masks only days within +/-1 session
    # of a print, so with no print in the trailing-20d window it EQUALS rv20_ann
    # (zero behaviour change); it de-noises the gate ONLY when a recent print
    # actually contaminated the window (review R4: "Fixes RV20 contamination when
    # a print falls in the window"). rv20_ann is retained as a disclosed field.
    rv20_gate = rv20_ex_earnings if rv20_ex_earnings is not None else rv20_ann
    iv_minus_rv = (iv30 - rv20_gate) if (iv30 is not None and rv20_gate is not None) else None
    rv20_ex_earnings_stripped = (
        rv20_ex_earnings is not None and rv20_ann is not None
        and abs(rv20_ex_earnings - rv20_ann) > 1e-9)

    return {
        "chain_file_path": chain_path_manifest,
        "chain_as_of": chain_as_of,
        # QF3: filter to future contracts so atm_iv_by_expiry never sees an
        # expired expiry (atm_iv_by_expiry internally calls chain.expiries).
        "atm_iv_by_expiry": (
            chain.atm_iv_by_expiry(
                [c for c in contracts if c.get("expiration", "") >= (as_of_date or "")],
                spot,
            ) if spot else []
        ),
        "expected_moves": expected_moves,
        "max_pain_by_expiry": max_pain_by_expiry,
        "oi_walls": walls,
        "skew_25d_30d": skew,
        "rv20_for_iv_comparison": rv20_gate,       # the RV the gate actually used
        "rv20_ann": rv20_ann,                      # contaminated RV, disclosed
        "iv_minus_rv20": iv_minus_rv,
        "event_vol": event_vol,                    # Wave 4B: isolated earnings-day vol
        "rv20_ex_earnings": rv20_ex_earnings,      # Wave 4B: print-days masked RV
        "rv20_ex_earnings_stripped": rv20_ex_earnings_stripped,  # gate de-noised?
    }, contracts, iv30


def _implied_move_next_earnings(contracts, spot, next_earnings_date, as_of_date):
    """one_sigma_pct at the first expiry >= next_earnings date; None if no date."""
    if not next_earnings_date or spot is None:
        return None
    ne = _parse_date(next_earnings_date)
    if ne is None:
        return None
    for exp in chain.expiries(contracts):
        d = _parse_date(exp)
        if d is not None and d >= ne:
            em = chain.expected_move(contracts, spot, exp)
            return em["one_sigma_pct"] if em else None
    return None


def build_sentiment(overview, price, pc_realtime, short_interest, news,
                    insider, contracts, iv30, iv_history, next_earnings_date,
                    as_of_date, ticker=None, options=None):
    """Sentiment block: ratings, PT, P/C, IV, insider flow. Text slots stay null.

    Wave 3A additions (all deterministic, additive, null-safe):
      - news_heat: EWMA of ticker_sentiment_score x relevance_score over the
        raw feed with half-life 3 days, plus an article-volume z-spike.
      - dtc: days-to-cover from SI% + shares + ADV.
      - skew_25d_30d: promoted from the options block for scorer locality.
      - insider_classification: Cohen/Malloy/Pomorski routine-vs-opportunistic
        (active only with >= 24 months of per-insider history; else graceful).
    """
    ov = overview if isinstance(overview, dict) else {}

    def _rating(key):
        v = num(ov.get(key))
        return int(v) if v is not None else 0

    sb = _rating("AnalystRatingStrongBuy")
    b = _rating("AnalystRatingBuy")
    h = _rating("AnalystRatingHold")
    s = _rating("AnalystRatingSell")
    ss = _rating("AnalystRatingStrongSell")
    ratings = {"strong_buy": sb, "buy": b, "hold": h, "sell": s,
               "strong_sell": ss, "n": sb + b + h + s + ss}

    consensus_pt = num(ov.get("AnalystTargetPrice"))
    last = price.get("last")
    pt_vs = (consensus_pt / last - 1) if (consensus_pt is not None and last) else None

    # Full-chain P/C from the on-disk chain: OI-based for positioning,
    # volume-based for like-for-like comparison with vendor realtime P/C.
    pc_full = chain.put_call_ratio(contracts) if contracts else None
    pc_full_volume = chain.put_call_ratio_volume(contracts) if contracts else None

    # Realtime P/C + by-expiry from the pc file.
    pc_rt = None
    pc_by_expiry = []
    if isinstance(pc_realtime, dict):
        pc_rt = num(pc_realtime.get("put_call_ratio_full_chain"))
        for row in (pc_realtime.get("put_call_ratio_by_expiration") or [])[:6]:
            if isinstance(row, dict):
                pc_by_expiry.append({"date": row.get("date"),
                                     "value": num(row.get("value"))})

    # IV percentile within the 1yr history samples.
    iv_pctile = None
    if isinstance(iv_history, dict) and iv30 is not None:
        samples = [num(x.get("atm_iv")) for x in (iv_history.get("samples") or [])
                   if isinstance(x, dict)]
        samples = [x for x in samples if x is not None]
        iv_pctile = indicators.percentile_rank(iv30, samples)

    implied_move = _implied_move_next_earnings(contracts, last, next_earnings_date,
                                               as_of_date) if contracts else None

    insider_net, insider_method = _insider_net_90d(insider, as_of_date)
    insider_classification = build_insider_classification(insider, as_of_date)

    si = short_interest if isinstance(short_interest, dict) else {}
    si_pct = num(si.get("short_interest_pct")) if si else None

    # Days-to-cover (DTC): trailing-90d short interest as a share count divided
    # by average daily SHARE volume (ADV$ / last). CAVEAT: shares_diluted_m is a
    # DILUTED share count, not the shares-outstanding float SI% is typically
    # struck against, so this DTC is a deterministic approximation, not the
    # exchange-published days-to-cover. Null when any input is null/zero.
    dtc = None
    adv = price.get("adv_dollar_3m")
    shares_m = price.get("shares_diluted_m")
    if (si_pct and adv and last and shares_m
            and si_pct > 0 and adv > 0 and last > 0 and shares_m > 0):
        si_shares = (si_pct / 100.0) * shares_m * 1e6
        avg_daily_shares = adv / last
        if avg_daily_shares > 0:
            dtc = si_shares / avg_daily_shares

    # Promote the already-computed 25d/30d skew into the sentiment block for
    # scorer locality (no recomputation -- read straight off the options block).
    skew_25d_30d = None
    if isinstance(options, dict):
        skew_25d_30d = options.get("skew_25d_30d")

    news_heat = _news_heat(news, as_of_date, ticker)

    return {
        "ratings": ratings,
        "consensus_pt": consensus_pt,
        "pt_vs_price_pct": pt_vs,
        "short_interest_pct": si_pct,
        "si_trend": si.get("si_trend") if si else None,
        "si_as_of": si.get("as_of") if si else None,
        "dtc": dtc,
        "put_call_ratio_full_chain": pc_full,
        "put_call_ratio_full_chain_volume": pc_full_volume,
        "put_call_ratio_realtime": pc_rt,
        "put_call_by_expiry": pc_by_expiry,
        "skew_25d_30d": skew_25d_30d,
        "iv30": iv30,
        "iv_pctile_1yr": iv_pctile,
        "implied_move_next_earnings_pct": implied_move,
        "insider_net_90d_usd": insider_net,
        "insider_method": insider_method,
        "insider_classification": insider_classification,
        "news_heat": news_heat,
        "news_sentiment_summary": None,   # LLM slot
        "inst_flow_notes": None,          # LLM slot
    }


def _insider_net_90d(insider, as_of_date):
    """Net insider dollar flow over the trailing 90 days (priced rows only).

    A = acquisition (+), D = disposal (-). Rows with blank/zero share_price
    (RSU grants/vests) are EXCLUDED from the dollar math. Window is
    [as_of_date - 90d, as_of_date].
    """
    method = ("sum of A(+)/D(-) shares*price for priced rows "
              "(share_price>0) with transaction_date within 90 days of as_of")
    if not isinstance(insider, dict):
        return None, method
    rows = insider.get("data")
    if not isinstance(rows, list):
        return None, method
    end = _parse_date(as_of_date)
    if end is None:
        return None, method
    start = end - timedelta(days=90)

    total = 0.0
    counted = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = _parse_date(r.get("transaction_date"))
        if d is None or d < start or d > end:
            continue
        price = num(r.get("share_price"))
        if price is None or price <= 0:
            continue  # RSU grant/vest -- excluded
        shares = num(r.get("shares"))
        if shares is None:
            continue
        sign = 1.0 if (r.get("acquisition_or_disposal") or "").upper() == "A" else -1.0
        total += sign * shares * price
        counted += 1
    return (total if counted else 0.0), method


_NEWS_HALF_LIFE_DAYS = 3   # cited default: RavenPack/MSCI 2-5d news decay.


def _parse_news_time(text):
    """Parse an Alpha Vantage time_published stamp (YYYYMMDDTHHMMSS) to a date.

    Only the leading YYYYMMDD is needed. Returns a date or None.
    """
    if not isinstance(text, str) or len(text) < 8:
        return None
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except (ValueError, TypeError):
        return None


def _news_heat(news, as_of_date, ticker):
    """Sentiment-dynamics heat from the raw NEWS_SENTIMENT feed.

    For each article in ``news["feed"]`` that mentions ``ticker`` (via its
    ticker_sentiment array), take this ticker's ticker_sentiment_score weighted
    by relevance_score AND a half-life-3-day time decay on the article's age
    (as_of_date - time_published). Returns:

        {ewma, volume_z, half_life_days, n_articles}

    where ``ewma`` is the relevance-and-decay-weighted mean of the per-ticker
    sentiment scores, ``volume_z`` is the z-score of the trailing ~3-day article
    count vs the per-day counts across the feed (None when < 5 distinct days of
    history), ``n_articles`` counts articles mentioning the ticker. Returns None
    (a null block) when news is absent/empty or no article mentions the ticker.
    """
    if not isinstance(news, dict):
        return None
    feed = news.get("feed")
    if not isinstance(feed, list) or not feed:
        return None
    if not ticker:
        return None
    tkr = ticker.upper()
    end = _parse_date(as_of_date)

    pairs = []             # (score, weight) for the EWMA
    per_day = {}           # article date -> count (ticker-mentioning only)
    n_articles = 0
    for art in feed:
        if not isinstance(art, dict):
            continue
        ts = art.get("ticker_sentiment")
        if not isinstance(ts, list):
            continue
        match = None
        for row in ts:
            if isinstance(row, dict) and (row.get("ticker") or "").upper() == tkr:
                match = row
                break
        if match is None:
            continue
        n_articles += 1
        art_date = _parse_news_time(art.get("time_published"))
        if art_date is not None:
            per_day[art_date] = per_day.get(art_date, 0) + 1

        score = num(match.get("ticker_sentiment_score"))
        relevance = num(match.get("relevance_score"))
        if score is None:
            continue
        if relevance is None or relevance < 0:
            relevance = 0.0
        # Time-decay weight (half-life 3d); undated articles get age 0 (no decay).
        if end is not None and art_date is not None:
            age_days = (end - art_date).days
            if age_days < 0:
                age_days = 0
        else:
            age_days = 0
        decay = indicators.halflife_weight(age_days, _NEWS_HALF_LIFE_DAYS)
        if decay is None:
            decay = 1.0
        weight = relevance * decay
        pairs.append((score, weight))

    if n_articles == 0:
        return None

    ewma = indicators.ewma_halflife(pairs)

    # Volume z-spike: the trailing ~3-day article count vs the per-day counts
    # across the feed. Guard < 5 distinct days of history -> None.
    volume_z = None
    if end is not None and len(per_day) >= 5:
        recent_start = end - timedelta(days=3)
        recent_count = sum(c for d, c in per_day.items() if d >= recent_start)
        counts_hist = list(per_day.values())
        volume_z = indicators.zscore(float(recent_count), [float(c) for c in counts_hist])

    return {
        "ewma": ewma,
        "volume_z": volume_z,
        "half_life_days": _NEWS_HALF_LIFE_DAYS,
        "n_articles": n_articles,
    }


def build_insider_classification(insider, as_of_date):
    """Cohen/Malloy/Pomorski routine-vs-opportunistic insider classification.

    Retains the FULL per-insider row list (unlike _insider_net_90d, which only
    keeps a 90d scalar). Groups priced rows by ``executive`` and:

      - history_months: calendar-month span from the earliest to the latest
        priced transaction across ALL insiders.
      - ROUTINE trade (per Cohen/Malloy/Pomorski): the insider transacted in the
        SAME calendar month in >= 2 of the 3 prior years (requires >= 24 months
        of history to be meaningful); OPPORTUNISTIC otherwise.
      - opportunistic_net_usd / routine_net_usd: signed sum(shares*price)
        (D negative) over each class, restricted to the trailing 90 days.
      - opportunistic_cluster: >= 2 DISTINCT opportunistic insiders trading the
        SAME side (all A or all D) within any 30-day window.

    GRACEFUL DEGRADE: when history_months < 24 the classifier cannot separate
    routine from opportunistic, so ``classifier_active`` is False and the
    opportunistic/routine splits + cluster flag are null (the scorer falls back
    to insider_net_90d). Returns None (null block) when there are no priced rows.
    """
    if not isinstance(insider, dict):
        return None
    rows_raw = insider.get("data")
    if not isinstance(rows_raw, list):
        return None
    end = _parse_date(as_of_date)

    # Retain the full priced-row list with parsed fields.
    rows = []
    for r in rows_raw:
        if not isinstance(r, dict):
            continue
        d = _parse_date(r.get("transaction_date"))
        price = num(r.get("share_price"))
        shares = num(r.get("shares"))
        if d is None or price is None or price <= 0 or shares is None:
            continue  # RSU grant/vest / unparseable -- excluded
        side = "A" if (r.get("acquisition_or_disposal") or "").upper() == "A" else "D"
        rows.append({
            "executive": (r.get("executive") or "").strip(),
            "date": d,
            "shares": shares,
            "price": price,
            "side": side,
        })

    if not rows:
        return None

    n_insiders = len({r["executive"] for r in rows})

    earliest = min(r["date"] for r in rows)
    latest = max(r["date"] for r in rows)
    history_months = (latest.year - earliest.year) * 12 + (latest.month - earliest.month)

    # Group each insider's transaction (year, month) history for the CMP test.
    by_insider = {}
    for r in rows:
        by_insider.setdefault(r["executive"], []).append((r["date"].year, r["date"].month))

    def _is_routine(insider_name, txn_date):
        """Same calendar month in >= 2 of the 3 prior years -> routine."""
        hist = by_insider.get(insider_name, [])
        prior_years = {txn_date.year - 1, txn_date.year - 2, txn_date.year - 3}
        hits = len({y for (y, m) in hist if m == txn_date.month and y in prior_years})
        return hits >= 2

    if history_months < 24:
        # Graceful: insufficient history to classify. Splits/cluster null.
        return {
            "classifier_active": False,
            "opportunistic_cluster": None,
            "opportunistic_net_usd": None,
            "routine_net_usd": None,
            "history_months": history_months,
            "n_insiders": n_insiders,
        }

    # Active classifier: split trailing-90d flow into routine vs opportunistic.
    start_90 = end - timedelta(days=90) if end is not None else None
    opp_net = 0.0
    rou_net = 0.0
    opp_recent = []   # opportunistic trades in the 90d window, for clustering
    for r in rows:
        if start_90 is not None and (r["date"] < start_90 or r["date"] > end):
            continue
        signed = (1.0 if r["side"] == "A" else -1.0) * r["shares"] * r["price"]
        if _is_routine(r["executive"], r["date"]):
            rou_net += signed
        else:
            opp_net += signed
            opp_recent.append(r)

    # Cluster: >= 2 distinct opportunistic insiders SAME side within any 30d window.
    opportunistic_cluster = False
    opp_sorted = sorted(opp_recent, key=lambda x: x["date"])
    for i, anchor in enumerate(opp_sorted):
        window_end = anchor["date"] + timedelta(days=30)
        names = set()
        for other in opp_sorted[i:]:
            if other["date"] > window_end:
                break
            if other["side"] == anchor["side"]:
                names.add(other["executive"])
        if len(names) >= 2:
            opportunistic_cluster = True
            break

    return {
        "classifier_active": True,
        "opportunistic_cluster": opportunistic_cluster,
        "opportunistic_net_usd": opp_net,
        "routine_net_usd": rou_net,
        "history_months": history_months,
        "n_insiders": n_insiders,
    }


def _parse_earnings_calendar(payload):
    """Parse EARNINGS_CALENDAR into next_earnings {date, time, consensus_eps}.

    Two shapes: (a) CSV-in-JSON {"result": "<csv text>"} -- possibly header-only;
    (b) web-fallback dict {"date","time","consensus_eps","consensus_rev",...}.
    Returns None if no row is available.
    """
    if not isinstance(payload, dict):
        return None
    # (b) web-fallback: dict carrying a "date" key directly.
    if "date" in payload and "result" not in payload:
        return {"date": payload.get("date"),
                "time": payload.get("time"),
                "consensus_eps": num(payload.get("consensus_eps"))}
    result = payload.get("result")
    if not isinstance(result, str):
        return None
    reader = csv.DictReader(io.StringIO(result))
    for row in reader:
        report_date = row.get("reportDate")
        if not report_date:
            continue
        return {"date": report_date,
                "time": row.get("timeOfTheDay"),
                "consensus_eps": num(row.get("estimate"))}
    return None


def _days_between(as_of_date, target_date):
    """Calendar days from ``as_of_date`` to ``target_date`` (both YYYY-MM-DD).

    A1: this is the exact subtraction that already lives at
    score_sentiment.py:642-659 (_days_to_earnings); replicated inline here (no
    cross-module import) so the snapshot can compute events.days_to_event.
    Returns None if either date is absent/unparseable.
    """
    base = _parse_date(as_of_date)
    tgt = _parse_date(target_date)
    if base is None or tgt is None:
        return None
    return (tgt - base).days


def build_earnings_move_history(earn_q, rows):
    """Up-to-8 {"quarter_end", "move_pct"} from the ticker's own reported quarters.

    A1 reaction-window convention (documented -- a MEASUREMENT, not a
    calibration): for a reportedDate D,
        move_pct = close[first trading day >= D+1]
                   / close[last trading day <= D-1] - 1
    which spans the report and is robust to BMO/AMC timing ambiguity. A quarter
    is SKIPPED (not recorded) when its reportedDate is absent/unparseable or when
    OHLCV is missing on either side of the report window.

    ``earn_q`` is the newest-first quarterlyEarnings list; ``rows`` are the
    oldest-first parsed daily rows (date + close). Output is newest-first (the
    same order as ``earn_q``), capped at 8 quarters.
    """
    if not isinstance(earn_q, list) or not rows:
        return []
    # Ascending list of (date, close) for trading days that carry a close.
    dated = sorted(
        ((r["date"], r["close"]) for r in rows
         if r.get("date") and r.get("close") is not None),
        key=lambda x: x[0],
    )
    if not dated:
        return []
    dates = [d for d, _ in dated]
    close_by_date = {d: c for d, c in dated}

    import bisect

    out = []
    for q in earn_q[:8]:
        if not isinstance(q, dict):
            continue
        report_date = q.get("reportedDate")
        d = _parse_date(report_date)
        if d is None:
            continue
        before_key = (d - timedelta(days=1)).isoformat()
        after_key = (d + timedelta(days=1)).isoformat()
        # last trading day <= D-1: rightmost date <= before_key.
        i_before = bisect.bisect_right(dates, before_key) - 1
        # first trading day >= D+1: leftmost date >= after_key.
        i_after = bisect.bisect_left(dates, after_key)
        if i_before < 0 or i_after >= len(dates):
            continue  # missing OHLCV around the report window -> skip
        pre_close = close_by_date[dates[i_before]]
        post_close = close_by_date[dates[i_after]]
        if pre_close in (None, 0):
            continue
        out.append({
            "quarter_end": report_date,
            "move_pct": post_close / pre_close - 1,
        })
    return out


def build_events(earnings_calendar, overview, as_of_date, earn_q=None,
                 rows=None, implied_move=None):
    """Events block: next earnings + dividends + event-aware disclosure fields.

    A1 additions (all deterministic, additive, null-safe):
      - days_to_event: integer days from as_of_date to next_earnings.date.
      - implied_move: the sentiment.implied_move_next_earnings_pct value, copied
        here for locality (passed in by the caller; no re-computation).
      - earnings_move_history: up to 8 own-history reaction moves.
      - implied_move_vs_own_history_pctile: percentile rank of implied_move
        within the ABS move-history values.
    Catalysts remain an LLM slot.
    """
    ne = _parse_earnings_calendar(earnings_calendar)
    ov = overview if isinstance(overview, dict) else {}
    dividends = {
        "per_share": num(ov.get("DividendPerShare")),
        "ex_date": ov.get("ExDividendDate") if ov.get("ExDividendDate") not in (None, "None", "-") else None,
        "pay_date": ov.get("DividendDate") if ov.get("DividendDate") not in (None, "None", "-") else None,
    }

    ne_date = ne.get("date") if isinstance(ne, dict) else None
    days_to_event = _days_between(as_of_date, ne_date)

    move_history = build_earnings_move_history(earn_q or [], rows or [])

    # Percentile rank of the implied move within this name's OWN reaction
    # history (abs values): 100 * count(abs_move <= implied_move) / n. Null only
    # when the implied move is absent or there is no usable history. Computed
    # inline (not via indicators.percentile_rank, whose >=10-sample guard would
    # always null a max-8-quarter history).
    implied_vs_own = None
    if implied_move is not None and move_history:
        abs_moves = [abs(m["move_pct"]) for m in move_history
                     if m.get("move_pct") is not None]
        if abs_moves:
            at_or_below = sum(1 for a in abs_moves if a <= implied_move)
            implied_vs_own = 100 * at_or_below / len(abs_moves)

    return {
        "next_earnings": ne,
        "days_to_event": days_to_event,
        "implied_move": implied_move,
        "earnings_move_history": move_history,
        "implied_move_vs_own_history_pctile": implied_vs_own,
        "dividends": dividends,
        "catalysts": [],   # LLM slot
    }


def build_macro(treasury):
    """Macro block: latest 10-year treasury yield (data is newest-first)."""
    if not isinstance(treasury, dict):
        return {"treasury_10y": None}
    data = treasury.get("data")
    if not isinstance(data, list) or not data:
        return {"treasury_10y": None}
    latest = data[0]
    if not isinstance(latest, dict):
        return {"treasury_10y": None}
    value = num(latest.get("value"))
    if value is None:
        return {"treasury_10y": None}
    return {"treasury_10y": {"value": value, "date": latest.get("date")}}


def build_sources(manifest_files, present_keys):
    """Provenance source list for the files actually present."""
    sources = []
    for key in present_keys:
        entry = manifest_files.get(key, {})
        sources.append({
            "field_group": key,
            "endpoint_or_url": entry.get("endpoint_or_url"),
            "retrieved_utc": entry.get("retrieved_utc"),
            "covers": COVERS.get(key, []),
        })
    return sources


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _resolve(bundle, rel):
    """Resolve a manifest path: absolute passthrough, else bundle-relative."""
    if os.path.isabs(rel):
        return rel
    return os.path.join(bundle, rel)


def build_snapshot(bundle, ticker):
    """Build the full snapshot dict from a bundle directory. Raises BuildError."""
    manifest_path = os.path.join(bundle, "manifest.json")
    try:
        with open(manifest_path) as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        raise BuildError(f"cannot read manifest.json in {bundle}: {exc}")

    files = manifest.get("files", {})
    as_of_utc = manifest.get("as_of_utc")
    as_of_date = _as_of_date(as_of_utc)

    # Required-file presence check.
    missing_required = [k for k in REQUIRED
                        if k not in files or not os.path.exists(
                            _resolve(bundle, files[k]["path"]))]
    if missing_required:
        raise BuildError("missing REQUIRED file(s): " + ", ".join(missing_required))

    # Load a raw file by manifest key (None if absent).
    def load_key(key):
        entry = files.get(key)
        if not entry:
            return None
        path = _resolve(bundle, entry["path"])
        if not os.path.exists(path):
            return None
        return load_raw(path)

    # Daily/spy may be AV JSON or bare stooq CSV; load with the CSV-aware loader.
    def load_daily_key(key):
        entry = files.get(key)
        if not entry:
            return None
        path = _resolve(bundle, entry["path"])
        if not os.path.exists(path):
            return None
        return load_daily_raw(path)

    quote = load_key("global_quote")
    overview = load_key("overview")
    daily = load_daily_key("daily_adjusted")
    spy = load_daily_key("spy_daily_adjusted")
    # Track O4 OPTIONAL: the GICS-sector SPDR ETF daily series (absent -> None ->
    # the sector benchmark drops out; never a REQUIRED-file failure).
    sector_daily = load_daily_key("sector_daily_adjusted")

    for name, obj in (("global_quote", quote), ("overview", overview),
                      ("daily_adjusted", daily), ("spy_daily_adjusted", spy)):
        if obj is None:
            raise BuildError(f"REQUIRED file {name} present but unparseable")

    # Disclose the daily series provenance: a stooq CSV export uses close as
    # adjusted_close (already split-adjusted); the AV path carries no label.
    series_source = SERIES_SOURCE_STOOQ if is_stooq_csv_daily(daily) else None

    try:
        rows = parse_daily_rows(daily)
        spy_rows = parse_daily_rows(spy)
    except BuildError as exc:
        raise BuildError(f"daily series parse failure: {exc}")
    if not rows:
        raise BuildError("daily_adjusted parsed to zero rows")

    # Track O4 OPTIONAL sector series: resolve the ETF from the overview Sector and
    # parse the sector daily file if present. Absent/unresolved -> None (the sector
    # benchmark drops out; a parse hiccup on this OPTIONAL series must not fail the
    # whole build, so it degrades to None rather than raising).
    sector_etf = resolve_sector_etf(
        overview.get("Sector") if isinstance(overview, dict) else None)
    sector_rows = None
    if sector_daily is not None and sector_etf is not None:
        try:
            parsed_sector = parse_daily_rows(sector_daily)
        except BuildError:
            parsed_sector = None
        if parsed_sector:
            sector_rows = parsed_sector

    income = load_key("income_statement")
    balance = load_key("balance_sheet")
    cashflow = load_key("cash_flow")
    earnings = load_key("earnings")
    estimates_raw = load_key("earnings_estimates")
    estimates = estimates_raw.get("estimates") if isinstance(estimates_raw, dict) else None
    news = load_key("news_sentiment")
    insider = load_key("insider_transactions")
    pc_realtime = load_key("pc_ratio_realtime")
    earnings_calendar = load_key("earnings_calendar")
    treasury = load_key("treasury_yield")
    web_spot = load_key("web_spot_check")
    short_interest = load_key("short_interest")
    web_fundamentals = load_key("web_fundamentals")

    # iv_history is a top-level manifest path, not under files{}.
    iv_history = None
    iv_hist_rel = manifest.get("iv_history_path")
    if iv_hist_rel:
        iv_path = _resolve(bundle, iv_hist_rel)
        if os.path.exists(iv_path):
            iv_history = load_raw(iv_path)

    # Parse next-earnings + reported quarters up front so options, sentiment and
    # the technicals VWAP anchors can key off them; the full events block (which
    # surfaces the sentiment-computed implied move) is assembled AFTER sentiment
    # below to reuse that value without re-calling the chain (A1).
    next_earnings = _parse_earnings_calendar(earnings_calendar)
    next_earnings_date = next_earnings.get("date") if isinstance(next_earnings, dict) else None
    # Own-history earnings reactions read reportedDate from quarterlyEarnings;
    # Wave 4A also uses the latest reportedDate as the vwap_earnings fallback anchor.
    earn_q = _quarterly(earnings, "quarterlyEarnings")

    # -- assemble blocks ---------------------------------------------------
    price = build_price(quote, overview, rows, web_spot)
    # QF2: extract the vendor latest-trading-day stamp from the price block
    # (build_price stores it under a private key) and move it to meta.
    latest_trading_day = price.pop("_latest_trading_day", None)
    # O15: issuer/security-master block -- formalizes the security-vs-issuer split
    # already implicit in price (AV SharesOutstanding = one class; mktcap = issuer).
    # Pure over the reconciled price block + overview; ADDITIVE (no scorer reads it).
    security_master = build_security_master(price, overview, ticker)
    technicals = build_technicals(rows, series_source=series_source,
                                  next_earnings_date=next_earnings_date,
                                  earn_q=earn_q)
    benchmark = build_benchmark(rows, spy_rows, sector_rows=sector_rows,
                                sector_etf=sector_etf if sector_rows else None)
    fundamentals = build_fundamentals(income, balance, cashflow, earnings,
                                      estimates, overview, as_of_date)
    # Gap-fill from cited web sources ONLY where the statement path found nothing;
    # this runs before valuation so a web-supplied eps_ntm/fcf feeds the multiples.
    apply_web_fundamentals(fundamentals, web_fundamentals)
    valuation = build_valuation(price, fundamentals, overview, rows)

    # Options depend on the chain file being present.
    options = None
    contracts = None
    iv30 = None
    chain_entry = files.get("options_chain")
    if chain_entry:
        chain_path = _resolve(bundle, chain_entry["path"])
        if os.path.exists(chain_path):
            try:
                options, contracts, iv30 = build_options(
                    chain_path, chain_entry["path"], price, next_earnings_date,
                    technicals["rv20_ann"], as_of_date,
                    last_ohlcv_date=technicals.get("last_ohlcv_date"),
                    rows=rows, earn_q=earn_q)
            except ValueError:
                options, contracts, iv30 = None, None, None

    sentiment = build_sentiment(overview, price, pc_realtime, short_interest,
                                news, insider, contracts, iv30, iv_history,
                                next_earnings_date, as_of_date,
                                ticker=ticker, options=options)

    # A1: assemble events AFTER sentiment so events.implied_move reuses the
    # already-computed sentiment.implied_move_next_earnings_pct (no double chain
    # call). All event fields are deterministic + null-safe.
    events = build_events(
        earnings_calendar, overview, as_of_date,
        earn_q=earn_q, rows=rows,
        implied_move=sentiment.get("implied_move_next_earnings_pct"),
    )
    macro = build_macro(treasury)

    # -- provenance + missing disclosure -----------------------------------
    present_keys = [k for k in COVERS
                    if k in files and os.path.exists(_resolve(bundle, files[k]["path"]))]
    # options_chain counts as present only if it actually loaded.
    if options is None and "options_chain" in present_keys:
        present_keys.remove("options_chain")

    # web_fundamentals is a fallback-only source (absent in the normal AV path);
    # its presence/use is disclosed via fundamentals.web_transcribed_fields, so we
    # do NOT report its absence as a "missing" expected source (that would be noise
    # on every standard-mode build). sector_daily_adjusted (Track O4) is the same:
    # an OPTIONAL source whose presence/absence is disclosed IN-BAND via the
    # benchmark block's sector_etf / sector_ret_* keys (present only when a sector
    # series was fetched AND the sector resolved). It is absent by design on any
    # build without sector data, so listing it in meta.missing would be noise.
    _MISSING_DISCLOSURE_EXCLUDED = ("web_fundamentals", "sector_daily_adjusted")
    optional_keys = [k for k in COVERS
                     if k not in REQUIRED and k not in _MISSING_DISCLOSURE_EXCLUDED]
    missing = [k for k in optional_keys if k not in present_keys]

    snapshot = {
        "meta": {
            "ticker": ticker,
            "as_of_utc": as_of_utc,
            "schema_version": SCHEMA_VERSION,
            "latest_trading_day": latest_trading_day,   # QF2: vendor stamp, null when absent
            "missing": missing,
            "data_mode": manifest.get("data_mode", "alpha_vantage"),
            "data_source": manifest.get("data_source", "alphavantage"),
            "api_tier_notes": manifest.get("api_tier_notes", []),
            "sources": build_sources(files, present_keys),
            "qc": {"passed": None, "checks": [], "waivers": []},
        },
        "price": price,
        "security_master": security_master,
        "technicals": technicals,
        "benchmark": benchmark,
        "fundamentals": fundamentals,
        "valuation": valuation,
        "sentiment": sentiment,
        "options": options,
        "events": events,
        "macro": macro,
    }
    return snapshot, as_of_date


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a QC-ready snapshot.json from a raw Alpha Vantage bundle.")
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--ticker", required=True, help="ticker symbol")
    parser.add_argument("--out", default=None, help="output path (default derived)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 2

    try:
        snapshot, as_of_date = build_snapshot(args.bundle, args.ticker.upper())
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    out = args.out
    if out is None:
        stamp = as_of_date or "unknown"
        out = os.path.join(args.bundle,
                           f"snapshot_{args.ticker.upper()}_{stamp}.json")
    with open(out, "w") as fh:
        json.dump(snapshot, fh, indent=2)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
