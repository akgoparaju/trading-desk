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

SCHEMA_VERSION = "0.2.1"

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
}

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

def build_price(quote, overview, rows, web_spot):
    """Price block: quote, 52wk range, share count, market caps, ADV."""
    gq = quote.get("Global Quote", {}) if isinstance(quote, dict) else {}
    last = num(gq.get("05. price"))
    prev_close = num(gq.get("08. previous close"))
    high = num(gq.get("03. high"))
    low = num(gq.get("04. low"))

    shares = num(overview.get("SharesOutstanding")) if isinstance(overview, dict) else None
    shares_m = shares / 1e6 if shares is not None else None
    mktcap_overview = num(overview.get("MarketCapitalization")) if isinstance(overview, dict) else None
    mktcap_computed = last * shares if (last is not None and shares is not None) else None

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
        "adv_dollar_3m": adv,
        "web_spot_check": spot_block,
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


def build_technicals(rows, series_source=None):
    """Technicals block from adjusted-close + volume series (oldest-first).

    ``series_source`` (optional) discloses how the series was obtained. When the
    daily series came from a stooq CSV export (close used as adjusted_close) the
    caller passes ``SERIES_SOURCE_STOOQ`` and it is echoed into the block; for the
    default AV path it is None and the key is omitted (unchanged shape).
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

    block = {
        "ma50": indicators.sma(adj, 50),
        "ma200": indicators.sma(adj, 200),
        "ma50_slope_20d": indicators.ma_slope(adj, 50, 20),
        "ma200_slope_20d": indicators.ma_slope(adj, 200, 20),
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
        "max_dd_10yr": indicators.max_drawdown(ten_yr),
        "dd_episodes_20pct_10yr": indicators.drawdown_episodes(ten_yr, 0.20),
        "dd_episodes_30pct_10yr": indicators.drawdown_episodes(ten_yr, 0.30),
        "drawdowns_by_year": indicators.drawdowns_by_year(
            [{"date": r["date"], "adjusted_close": r["adjusted_close"]}
             for r in rows if r["adjusted_close"] is not None])[-10:],
        "ohlcv_rows": len(rows),
        "last_ohlcv_date": rows[-1]["date"] if rows else None,
    }
    if series_source:
        block["series_source"] = series_source
    return block


def build_benchmark(stock_rows, spy_rows):
    """Benchmark block: SPY returns + beta/corr of stock vs SPY."""
    spy_adj = [r["adjusted_close"] for r in spy_rows if r["adjusted_close"] is not None]
    stock_adj = [r["adjusted_close"] for r in stock_rows if r["adjusted_close"] is not None]
    bc = indicators.beta_corr(stock_adj, spy_adj)
    return {
        "spy_ret_1m": indicators.pct_return(spy_adj, _W1M),
        "spy_ret_3m": indicators.pct_return(spy_adj, _W3M),
        "spy_ret_6m": indicators.pct_return(spy_adj, _W6M),
        "spy_ret_12m": indicators.pct_return(spy_adj, _W12M),
        "beta": bc["beta"] if bc else None,
        "corr": bc["corr"] if bc else None,
        "beta_n_days": bc["n_days"] if bc else None,
    }


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
    revisions = None
    next_fy = None
    if fy:
        row = fy[0]
        eps_now = num(row.get("eps_estimate_average"))
        eps_90 = num(row.get("eps_estimate_average_90_days_ago"))
        pct = (eps_now / eps_90 - 1) if (eps_now is not None and eps_90) else None
        revisions = {
            "eps_now": eps_now,
            "eps_90d_ago": eps_90,
            "pct": pct,
            "up_30d": num(row.get("eps_estimate_revision_up_trailing_30_days")),
            "down_30d": num(row.get("eps_estimate_revision_down_trailing_30_days")),
        }
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
    mktcap = price.get("mktcap_computed")

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
                  rv20_ann, as_of_date, last_ohlcv_date=None):
    """Options block from the on-disk chain (never loaded into LLM context)."""
    contracts = chain.load_contracts(chain_path)
    spot = price.get("last")
    exps = chain.expiries(contracts)

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

    iv_minus_rv = (iv30 - rv20_ann) if (iv30 is not None and rv20_ann is not None) else None

    return {
        "chain_file_path": chain_path_manifest,
        "chain_as_of": chain_as_of,
        "atm_iv_by_expiry": chain.atm_iv_by_expiry(contracts, spot) if spot else [],
        "expected_moves": expected_moves,
        "max_pain_by_expiry": max_pain_by_expiry,
        "oi_walls": walls,
        "skew_25d_30d": skew,
        "rv20_for_iv_comparison": rv20_ann,
        "iv_minus_rv20": iv_minus_rv,
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
                    as_of_date):
    """Sentiment block: ratings, PT, P/C, IV, insider flow. Text slots stay null."""
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

    si = short_interest if isinstance(short_interest, dict) else {}

    return {
        "ratings": ratings,
        "consensus_pt": consensus_pt,
        "pt_vs_price_pct": pt_vs,
        "short_interest_pct": num(si.get("short_interest_pct")) if si else None,
        "si_trend": si.get("si_trend") if si else None,
        "si_as_of": si.get("as_of") if si else None,
        "put_call_ratio_full_chain": pc_full,
        "put_call_ratio_full_chain_volume": pc_full_volume,
        "put_call_ratio_realtime": pc_rt,
        "put_call_by_expiry": pc_by_expiry,
        "iv30": iv30,
        "iv_pctile_1yr": iv_pctile,
        "implied_move_next_earnings_pct": implied_move,
        "insider_net_90d_usd": insider_net,
        "insider_method": insider_method,
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


def build_events(earnings_calendar, overview):
    """Events block: next earnings + dividends. Catalysts are an LLM slot."""
    ne = _parse_earnings_calendar(earnings_calendar)
    ov = overview if isinstance(overview, dict) else {}
    dividends = {
        "per_share": num(ov.get("DividendPerShare")),
        "ex_date": ov.get("ExDividendDate") if ov.get("ExDividendDate") not in (None, "None", "-") else None,
        "pay_date": ov.get("DividendDate") if ov.get("DividendDate") not in (None, "None", "-") else None,
    }
    return {
        "next_earnings": ne,
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

    # -- assemble blocks ---------------------------------------------------
    price = build_price(quote, overview, rows, web_spot)
    technicals = build_technicals(rows, series_source=series_source)
    benchmark = build_benchmark(rows, spy_rows)
    fundamentals = build_fundamentals(income, balance, cashflow, earnings,
                                      estimates, overview, as_of_date)
    # Gap-fill from cited web sources ONLY where the statement path found nothing;
    # this runs before valuation so a web-supplied eps_ntm/fcf feeds the multiples.
    apply_web_fundamentals(fundamentals, web_fundamentals)
    valuation = build_valuation(price, fundamentals, overview, rows)
    events = build_events(earnings_calendar, overview)
    next_earnings_date = events["next_earnings"]["date"] if events["next_earnings"] else None

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
                    last_ohlcv_date=technicals.get("last_ohlcv_date"))
            except ValueError:
                options, contracts, iv30 = None, None, None

    sentiment = build_sentiment(overview, price, pc_realtime, short_interest,
                                news, insider, contracts, iv30, iv_history,
                                next_earnings_date, as_of_date)
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
    # on every standard-mode build).
    optional_keys = [k for k in COVERS
                     if k not in REQUIRED and k != "web_fundamentals"]
    missing = [k for k in optional_keys if k not in present_keys]

    snapshot = {
        "meta": {
            "ticker": ticker,
            "as_of_utc": as_of_utc,
            "schema_version": SCHEMA_VERSION,
            "missing": missing,
            "data_mode": manifest.get("data_mode", "alpha_vantage"),
            "data_source": manifest.get("data_source", "alphavantage"),
            "api_tier_notes": manifest.get("api_tier_notes", []),
            "sources": build_sources(files, present_keys),
            "qc": {"passed": None, "checks": [], "waivers": []},
        },
        "price": price,
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
