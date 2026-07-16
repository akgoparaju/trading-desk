"""Snapshot quality-control gate for the trade-decision plugin.

WHY THIS MODULE EXISTS: This is the central rigor mechanism of the whole system.
Before any snapshot is allowed to drive a trade decision, it must pass a BLOCKING
quality gate: internal-arithmetic consistency (does the market cap the LLM will
cite actually equal price * shares?), sane ranges, cross-source agreement, data
freshness, and full provenance. Every field the LLM will reason over is either
accounted for by a named source or explicitly disclosed as missing.

Each check is a pure function over the snapshot dict returning
    {"check": <name>, "passed": True|False|None, "detail": <str>}
where passed=None means SKIPPED (required inputs absent). A skip never fails the
gate but is always disclosed in the attestation. All checks are DEFENSIVE:
missing/null inputs skip with an explanatory detail, never raise KeyError.

stdlib-only. Time math uses datetime; no external clock is read (as_of_utc is
the reference instant baked into the snapshot).
"""

from datetime import datetime, timezone

# Relative tolerances / absolute windows for arithmetic checks.
_MKTCAP_TOL = 0.02          # +-2% price*shares vs overview
_SPOTCHECK_TOL = 0.015      # +-1.5% last vs web spot check
_PE_TTM_TOL = 0.03          # +-3% pe_ttm vs last/eps_ttm
_PE_FWD_TOL = 0.05          # +-5% pe_fwd vs last/eps_ntm_consensus
_NET_CASH_TOL = 1e6         # +-$1M reconciliation
_PC_TOL = 0.15              # |pc_full_chain - pc_realtime| max spread

# Top-level snapshot blocks whose presence must be provenance-accounted-for.
# "macro" is intentionally excluded: it is context (risk-free rate), never a
# scored input, and its single source (treasury_yield) is still staleness-checked.
_PROVENANCE_BLOCKS = [
    "price", "technicals", "benchmark", "fundamentals",
    "valuation", "sentiment", "options", "events",
]

# Staleness windows (days) per source field_group. Unknown group -> 7 days.
_STALENESS_WINDOWS = {
    "global_quote": 1,
    "web_spot_check": 1,
    "daily_adjusted": 4,
    "spy_daily_adjusted": 4,
    "income_statement": 120,
    "balance_sheet": 120,
    "cash_flow": 120,
    "earnings": 120,
    "earnings_estimates": 120,
    "overview": 7,
    "news_sentiment": 7,
    "insider_transactions": 90,
    "options_chain": 4,
    "pc_ratio_realtime": 4,
    "earnings_calendar": 7,
    "treasury_yield": 7,
    "short_interest": 14,
}
_DEFAULT_STALENESS_WINDOW = 7


def _result(name, passed, detail):
    """Build a check result dict."""
    return {"check": name, "passed": passed, "detail": detail}


def _get(block, key):
    """Safely read block[key]; None if block is not a dict or key absent."""
    if not isinstance(block, dict):
        return None
    return block.get(key)


def _is_num(value):
    """True if ``value`` is a real (non-bool) number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_iso(ts):
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or None.

    Accepts a trailing 'Z' (mapped to +00:00) and date-only strings. Naive
    results are assumed UTC.
    """
    if not isinstance(ts, str):
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_mktcap(s):
    """shares x price within +-2% of price.mktcap_overview, staleness-aware.

    COMPANY_OVERVIEW's MarketCapitalization is computed by the vendor from the
    PRIOR session's close, so on a big move day shares x last legitimately
    diverges from it (validation finding, AAPL +4% 2026-07-16). The check's
    real target is share-count / unit errors, so it passes if EITHER
    shares x last OR shares x prev_close reconciles; matching on prev_close
    is disclosed as vendor staleness in the detail.
    """
    price = _get(s, "price")
    last = _get(price, "last")
    prev = _get(price, "prev_close")
    shares_m = _get(price, "shares_diluted_m")
    overview = _get(price, "mktcap_overview")
    if not (_is_num(last) and _is_num(shares_m) and _is_num(overview)):
        return _result("check_mktcap", None,
                       "SKIP: last, shares_diluted_m, or mktcap_overview absent/non-numeric")
    if overview == 0:
        return _result("check_mktcap", None, "SKIP: mktcap_overview is zero")
    computed = last * shares_m * 1e6
    diff = abs(computed - overview) / abs(overview)
    if diff <= _MKTCAP_TOL:
        return _result("check_mktcap", True,
                       f"computed {computed:.4g} vs overview {overview:.4g}: "
                       f"{diff:.2%} diff (tol {_MKTCAP_TOL:.0%})")
    if _is_num(prev) and prev > 0:
        computed_prev = prev * shares_m * 1e6
        diff_prev = abs(computed_prev - overview) / abs(overview)
        if diff_prev <= _MKTCAP_TOL:
            return _result("check_mktcap", True,
                           f"overview cap matches shares x prev_close ({diff_prev:.2%} diff) "
                           f"but not shares x last ({diff:.2%}) — vendor mktcap is prior-session "
                           f"stale; share count reconciles (tol {_MKTCAP_TOL:.0%})")
    return _result("check_mktcap", False,
                   f"computed {computed:.4g} vs overview {overview:.4g}: "
                   f"{diff:.2%} diff (tol {_MKTCAP_TOL:.0%}; prev_close reconciliation also failed)")


def check_ma_ordering(s):
    """Moving-average ordering vs technicals.trend_claim (skip if claim absent).

    uptrend   requires last > ma50 > ma200
    downtrend requires last < ma50 < ma200
    sideways / any other claim -> skip (no orderable assertion).

    PHASE-LATENT BY DESIGN: the snapshot builder never emits trend_claim (a
    mechanical claim would make this check circular). The claim is stamped by
    the technical-analysis skill (Phase 2); until then this check reports SKIP
    at the snapshot gate, and goes live when a downstream skill asserts trend.
    """
    tech = _get(s, "technicals")
    claim = _get(tech, "trend_claim")
    if claim is None:
        return _result("check_ma_ordering", None, "SKIP: no trend_claim to verify")
    last = _get(_get(s, "price"), "last")
    ma50 = _get(tech, "ma50")
    ma200 = _get(tech, "ma200")
    if not (_is_num(last) and _is_num(ma50) and _is_num(ma200)):
        return _result("check_ma_ordering", None,
                       "SKIP: last, ma50, or ma200 absent/non-numeric")
    if claim == "uptrend":
        passed = last > ma50 > ma200
        return _result("check_ma_ordering", passed,
                       f"uptrend needs last>ma50>ma200: {last} / {ma50} / {ma200}")
    if claim == "downtrend":
        passed = last < ma50 < ma200
        return _result("check_ma_ordering", passed,
                       f"downtrend needs last<ma50<ma200: {last} / {ma50} / {ma200}")
    return _result("check_ma_ordering", None,
                   f"SKIP: trend_claim {claim!r} implies no MA ordering")


def check_ranges(s):
    """Sanity ranges on indicators/valuation.

    0 <= rsi14 <= 100; rv20_ann > 0; rv30_ann > 0; sentiment.iv30 > 0 if present;
    valuation.pe_ttm / pe_fwd > 0 if present (non-null). Missing fields skip that
    sub-rule; if every sub-rule is absent the check itself skips.
    """
    tech = _get(s, "technicals")
    sent = _get(s, "sentiment")
    val = _get(s, "valuation")

    problems = []
    checked = 0

    rsi = _get(tech, "rsi14")
    if _is_num(rsi):
        checked += 1
        if not (0 <= rsi <= 100):
            problems.append(f"rsi14={rsi} out of [0,100]")

    for key, block in (("rv20_ann", tech), ("rv30_ann", tech),
                       ("iv30", sent), ("pe_ttm", val), ("pe_fwd", val)):
        v = _get(block, key)
        if _is_num(v):
            checked += 1
            if v <= 0:
                problems.append(f"{key}={v} not > 0")

    if checked == 0:
        return _result("check_ranges", None, "SKIP: no range-checkable fields present")
    if problems:
        return _result("check_ranges", False, "; ".join(problems))
    return _result("check_ranges", True, f"all {checked} range checks passed")


def check_price_spotcheck(s):
    """|price.last - price.web_spot_check.price| / last <= 1.5% (skip if absent)."""
    price = _get(s, "price")
    spot = _get(price, "web_spot_check")
    if spot is None:
        return _result("check_price_spotcheck", None, "SKIP: no web_spot_check")
    last = _get(price, "last")
    web = _get(spot, "price")
    if not (_is_num(last) and _is_num(web)):
        return _result("check_price_spotcheck", None,
                       "SKIP: last or web_spot_check.price absent/non-numeric")
    if last == 0:
        return _result("check_price_spotcheck", None, "SKIP: last is zero")
    diff = abs(last - web) / abs(last)
    passed = diff <= _SPOTCHECK_TOL
    return _result("check_price_spotcheck", passed,
                   f"last {last} vs spot {web}: {diff:.2%} diff (tol {_SPOTCHECK_TOL:.1%})")


def check_pe_arithmetic(s):
    """P/E cross-checks against price/earnings arithmetic.

    pe_ttm vs last/eps_ttm (+-3%); pe_fwd vs last/eps_ntm_consensus (+-5%).
    A leg is SKIPPED where its eps <= 0 (P/E not meaningful) or any input is
    null; if BOTH legs skip, the check skips overall.
    """
    price = _get(s, "price")
    val = _get(s, "valuation")
    fund = _get(s, "fundamentals")
    last = _get(price, "last")

    problems = []
    skips = []
    checked = 0

    legs = (
        ("pe_ttm", _get(val, "pe_ttm"), _get(fund, "eps_ttm"), _PE_TTM_TOL),
        ("pe_fwd", _get(val, "pe_fwd"), _get(fund, "eps_ntm_consensus"), _PE_FWD_TOL),
    )
    for name, reported_pe, eps, tol in legs:
        if not _is_num(reported_pe) or not _is_num(eps) or not _is_num(last):
            skips.append(f"{name}: input null")
            continue
        if eps <= 0:
            skips.append(f"{name}: negative EPS, P/E n/m")
            continue
        checked += 1
        implied = last / eps
        diff = abs(reported_pe - implied) / abs(implied) if implied else float("inf")
        if diff > tol:
            problems.append(f"{name}={reported_pe} vs last/eps {implied:.4g}: "
                            f"{diff:.2%} diff (tol {tol:.0%})")

    if checked == 0:
        return _result("check_pe_arithmetic", None,
                       "SKIP: " + ("; ".join(skips) or "no legs evaluable"))
    if problems:
        return _result("check_pe_arithmetic", False, "; ".join(problems + skips))
    detail = f"{checked} leg(s) within tolerance"
    if skips:
        detail += "; " + "; ".join(skips)
    return _result("check_pe_arithmetic", True, detail)


def check_net_cash(s):
    """net_cash_defined: cash_st + lt_inv - total_debt == net within +-$1M."""
    ncd = _get(_get(s, "fundamentals"), "net_cash_defined")
    cash_st = _get(ncd, "cash_st")
    lt_inv = _get(ncd, "lt_inv")
    total_debt = _get(ncd, "total_debt")
    net = _get(ncd, "net")
    if not all(_is_num(v) for v in (cash_st, lt_inv, total_debt, net)):
        return _result("check_net_cash", None,
                       "SKIP: net_cash_defined component absent/non-numeric")
    computed = cash_st + lt_inv - total_debt
    delta = abs(computed - net)
    passed = delta <= _NET_CASH_TOL
    return _result("check_net_cash", passed,
                   f"cash_st+lt_inv-total_debt = {computed:.4g} vs net {net:.4g}: "
                   f"delta {delta:.4g} (tol {_NET_CASH_TOL:.0g})")


def check_options_freshness(s):
    """Options-block internal freshness/agreement (skip if options block absent).

    options.chain_as_of must equal technicals.last_ohlcv_date. The P/C
    agreement leg compares LIKE WITH LIKE: the chain's VOLUME-based P/C vs the
    vendor realtime P/C (also volume-based), within +-0.15. The OI-based chain
    P/C is a positioning metric and legitimately diverges from volume P/C on
    big-move days (validation finding, MU 2026-07-16: 1.29 OI vs 0.93 volume)
    — it is never compared against realtime. Skips entirely if the options
    block is missing or null.
    """
    options = _get(s, "options")
    if options is None:
        return _result("check_options_freshness", None, "SKIP: no options block")

    problems = []
    chain_as_of = _get(options, "chain_as_of")
    last_ohlcv = _get(_get(s, "technicals"), "last_ohlcv_date")
    if chain_as_of is None or last_ohlcv is None:
        problems.append("SKIP-leg: chain_as_of or last_ohlcv_date absent")
    elif chain_as_of != last_ohlcv:
        problems.append(f"chain_as_of {chain_as_of} != last_ohlcv_date {last_ohlcv}")

    sent = _get(s, "sentiment")
    pc_vol = _get(sent, "put_call_ratio_full_chain_volume")
    pc_rt = _get(sent, "put_call_ratio_realtime")
    if _is_num(pc_vol) and _is_num(pc_rt):
        spread = abs(pc_vol - pc_rt)
        if spread > _PC_TOL:
            problems.append(f"pc_chain_volume {pc_vol} vs pc_realtime {pc_rt}: "
                            f"spread {spread:.3f} (tol {_PC_TOL})")
    elif _is_num(pc_rt):
        problems.append("SKIP-leg: volume-based chain P/C absent; OI-vs-volume "
                        "comparison suppressed (methodology mismatch)")

    date_leg_verified = (chain_as_of is not None and last_ohlcv is not None
                         and chain_as_of == last_ohlcv)
    pc_leg_verified = _is_num(pc_vol) and _is_num(pc_rt)

    hard_fails = [p for p in problems if not p.startswith("SKIP-leg")]
    if hard_fails:
        return _result("check_options_freshness", False, "; ".join(problems))
    if not (date_leg_verified or pc_leg_verified):  # nothing verifiable
        return _result("check_options_freshness", None, "; ".join(problems))
    detail_bits = []
    if date_leg_verified:
        detail_bits.append(f"chain_as_of == last_ohlcv_date ({chain_as_of})")
    if pc_leg_verified:
        detail_bits.append("pc spread ok (volume-based)")
    detail_bits.extend(p for p in problems if p.startswith("SKIP-leg"))
    return _result("check_options_freshness", True, "; ".join(detail_bits))


def check_provenance(s):
    """Every present block is sourced or declared missing; sources well-formed.

    For each block in [price, technicals, benchmark, fundamentals, valuation,
    sentiment, options, events]: if present (non-null) it must appear in some
    meta.sources[].covers OR be listed in meta.missing. Every source entry must
    carry endpoint_or_url and retrieved_utc.
    """
    meta = _get(s, "meta")
    if not isinstance(meta, dict):
        return _result("check_provenance", None, "SKIP: no meta block")

    sources = meta.get("sources")
    if not isinstance(sources, list):
        sources = []
    missing = meta.get("missing")
    if not isinstance(missing, list):
        missing = []

    covered = set()
    malformed = []
    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            malformed.append(f"source[{i}] not a dict")
            continue
        if not src.get("endpoint_or_url"):
            malformed.append(f"source[{i}] missing endpoint_or_url")
        if not src.get("retrieved_utc"):
            malformed.append(f"source[{i}] missing retrieved_utc")
        for block in src.get("covers", []) or []:
            covered.add(block)

    uncovered = []
    for block in _PROVENANCE_BLOCKS:
        value = s.get(block)
        if value is None:  # block absent/null needs no provenance
            continue
        if block in covered or block in missing:
            continue
        uncovered.append(block)

    problems = []
    if uncovered:
        problems.append("uncovered blocks: " + ", ".join(uncovered))
    if malformed:
        problems.append("; ".join(malformed))
    if problems:
        return _result("check_provenance", False, "; ".join(problems))
    return _result("check_provenance", True,
                   f"{len(_PROVENANCE_BLOCKS)} blocks accounted for; "
                   f"{len(sources)} sources well-formed")


def check_staleness(s):
    """Every source is within its field_group freshness window vs as_of_utc.

    age_days = (as_of_utc - retrieved_utc) in days. Window per field_group per
    the table (unknown group -> 7 days). Any source older than its window fails,
    listing the offenders.
    """
    meta = _get(s, "meta")
    if not isinstance(meta, dict):
        return _result("check_staleness", None, "SKIP: no meta block")
    as_of = _parse_iso(meta.get("as_of_utc"))
    if as_of is None:
        return _result("check_staleness", None, "SKIP: as_of_utc absent/unparseable")
    sources = meta.get("sources")
    if not isinstance(sources, list) or not sources:
        return _result("check_staleness", None, "SKIP: no sources to age")

    offenders = []
    unparseable = []
    checked = 0
    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            continue
        group = src.get("field_group")
        retrieved = _parse_iso(src.get("retrieved_utc"))
        if retrieved is None:
            unparseable.append(f"source[{i}] retrieved_utc unparseable")
            continue
        checked += 1
        window = _STALENESS_WINDOWS.get(group, _DEFAULT_STALENESS_WINDOW)
        age_days = (as_of - retrieved).total_seconds() / 86400.0
        if age_days > window:
            offenders.append(f"{group}: {age_days:.1f}d old (window {window}d)")

    if checked == 0:
        return _result("check_staleness", None,
                       "SKIP: no source had a parseable retrieved_utc")
    if offenders:
        detail = "stale: " + "; ".join(offenders)
        if unparseable:
            detail += " | " + "; ".join(unparseable)
        return _result("check_staleness", False, detail)
    detail = f"all {checked} sources within window"
    if unparseable:
        detail += " (" + "; ".join(unparseable) + ")"
    return _result("check_staleness", True, detail)


ALL_CHECKS = [
    check_mktcap,
    check_ma_ordering,
    check_ranges,
    check_price_spotcheck,
    check_pe_arithmetic,
    check_net_cash,
    check_options_freshness,
    check_provenance,
    check_staleness,
]


def _build_attestation(snapshot, results, waived_names):
    """One-paragraph human-readable summary of the QC run."""
    meta = _get(snapshot, "meta") or {}
    ticker = meta.get("ticker", "UNKNOWN")
    as_of_raw = meta.get("as_of_utc", "unknown date")
    as_of_date = as_of_raw[:10] if isinstance(as_of_raw, str) else "unknown date"

    passed = [r for r in results if r["passed"] is True]
    skipped = [r for r in results if r["passed"] is None]
    waived = [r for r in results if r["check"] in waived_names]
    # A failed-but-waived check is reported as waived, not failed.
    failed = [r for r in results if r["passed"] is False and r["check"] not in waived_names]

    parts = [
        f"QC attestation for {ticker} as of {as_of_date}: "
        f"{len(passed)} passed / {len(failed)} failed / "
        f"{len(waived)} waived / {len(skipped)} skipped."
    ]
    if waived:
        parts.append("Waived: " + "; ".join(r["check"] for r in waived) + ".")
    if skipped:
        parts.append("Skipped: " + "; ".join(r["check"] for r in skipped) + ".")
    staleness = next((r for r in results if r["check"] == "check_staleness"), None)
    if staleness is not None and staleness["passed"] is not True:
        parts.append("Staleness disclosure: " + staleness["detail"] + ".")
    if failed:
        parts.append("Failed: " + "; ".join(r["check"] for r in failed) + ".")
    return " ".join(parts)


def run_qc(snapshot: dict) -> dict:
    """Run all checks, apply waivers, and produce a blocking gate verdict.

    Waivers live at snapshot.meta.qc.waivers as [{"check", "reason"}]. A FAILED
    check whose name is waived counts as waived (not a gate failure) and has its
    detail prefixed "WAIVED: <reason>: ". The gate passes iff there are no
    UNWAIVED failures. Returns {"passed", "checks", "attestation"}.
    """
    meta = _get(snapshot, "meta") or {}
    qc_meta = meta.get("qc") or {}
    raw_waivers = qc_meta.get("waivers") or []
    waiver_reasons = {}
    for w in raw_waivers:
        if isinstance(w, dict) and w.get("check"):
            waiver_reasons[w["check"]] = w.get("reason", "")

    results = []
    unwaived_failures = 0
    for check in ALL_CHECKS:
        try:
            res = check(snapshot)
        except Exception as exc:  # defensive: a check must never crash the gate
            res = _result(check.__name__, False, f"check raised {type(exc).__name__}: {exc}")

        if res["passed"] is False and res["check"] in waiver_reasons:
            reason = waiver_reasons[res["check"]]
            res = _result(res["check"], False,
                          f"WAIVED: {reason}: {res['detail']}")
        elif res["passed"] is False:
            unwaived_failures += 1
        results.append(res)

    waived_names = set(waiver_reasons)
    attestation = _build_attestation(snapshot, results, waived_names)
    return {
        "passed": unwaived_failures == 0,
        "checks": results,
        "attestation": attestation,
    }
