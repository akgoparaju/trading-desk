"""Report QC gate (§12, BLOCKING) for the trading-desk plugin.

WHY THIS MODULE EXISTS: render_report.py writes the report SKELETON from the
bundle (every number script-minted). After the LLM fills the prose slots, this
gate verifies the FINAL document numerically against the bundle so a report can
NEVER ship with a number that is not in the bundle. This is the enforcement half
of the slot architecture: render_report prevents number leakage by construction;
report_qc catches any number the LLM smuggled into a prose slot.

CLI: python3 scripts/report_qc.py --bundle <dir> --report <md path>
     [--waive "check:reason"]...  -> prints a check table + verdict, exits 0/1.
The same gate runs number_provenance over the docket's `--pdf-slots` prose and over
a company-context `--context` module (the latter adds structural checks over its
findings registry / live_tape / mode). Exactly one of {--report, --pdf-slots,
--context} is required.

CHECKS (waiver mechanics mirror qc_gate.py):
 1. number_provenance   -- every numeric token in the report must trace to a
                           snapshot/module numeric leaf (global) or to a number
                           inside one of the WHITELISTED bundle strings the
                           renderer echoes (with rounding + %-form tolerances).
                           Date- and version-shaped tokens are matched EXACTLY
                           against the bundle's own dates/versions (a fake date or
                           bogus version orphans); only the three exact page
                           headers are chrome. Orphans FAIL (list capped at 20).
 2. composite_arithmetic-- Σ(weight × score) == composite score ±0.01; each
                           contribution consistent.
 3. ev_consistency      -- scenario probs sum 1 ±1e-6; ev_at_current recomputed
                           from scenarios & last ±0.001.
 4. invalidation_both_legs -- report text contains both the technical level and the
                           fundamental metric text from module_tradeplan.
 5. sizing_within_cap   -- recommended_pct <= cap_pct.
 6. strikes_in_chain    -- every recommended+hedge strike exists in the chain
                           file (SKIP + disclose if no structures).
 7. pop_method_labeled  -- every recommended structure has a pop_method; the
                           report's strategy table mentions "PoP" + a method.
 8. expression_consistency -- expression.recommended_for_profile appears; if
                           executable is false, the executability note appears.
 9. footer_integrity    -- as_of present; every module rubric_version present;
                           disclaimer present.
10. word_cap            -- total words across the Page 1-3 sections <= 2100.
11. no_empty_slots      -- no `<!-- SLOT:` markers remain.

DELTA reports (auto-detected by filename or --delta) run checks {1, 9, 11} only.

stdlib-only.
"""

import argparse
import json
import os
import re
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import render_report, chain as chain_mod, decision_contract

_WORD_CAP = 2100
_ORPHAN_CAP = 20

# A numeric token: optional leading $, digits with optional thousands separators
# and a decimal part, optional trailing %. We deliberately do NOT capture bare
# integers embedded in ISO dates (handled by pre-stripping date substrings).
_NUM_RE = re.compile(r"\$?-?\d[\d,]*\.?\d*%?")
# ISO date substrings (YYYY-MM-DD) are stripped before number extraction so a date
# never contributes three orphan integers.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Full ISO-8601 timestamp (date + time), e.g. 2026-07-17T18:38:07Z.
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z?")
# A semver-shaped version token (rubric/expression/schema/plugin versions), with or
# without a leading 'v'. Matched EXACTLY against the bundle's own versions in
# number_provenance -- an out-of-bundle version (e.g. v9.99.99) orphans.
_VERSION_TOKEN_RE = re.compile(r"v?\d+\.\d+\.\d+")
# Fixed report labels that embed a literal number (report chrome, not data): the
# "52-Week"/"52wk" column label. Stripped before number extraction so its digits
# never register as orphans. NOTE: page headers are handled separately by
# _strip_allowed_page_headers -- ONLY the three exact headers render_report emits
# are chrome; any other "## Page N" line's digits are treated as ordinary numbers.
_LABEL_RE = re.compile(r"52[\s-]?wk|52[\s-]?week", re.IGNORECASE)
# The three EXACT page headers render_report emits. Their trailing digit (1/2/3) is
# report chrome; digits in any OTHER "## Page ..." line are ordinary numeric tokens.
_ALLOWED_PAGE_HEADERS = (
    "## Page 1 — Decision",
    "## Page 2 — Evidence",
    "## Page 3 — Context & Protocol",
)


def _result(name, passed, detail):
    return {"check": name, "passed": passed, "detail": detail}


# --------------------------------------------------------------------------- #
# Numeric-token extraction + allowed-set construction.
# --------------------------------------------------------------------------- #

def _canonical(tok):
    """Normalize a numeric token to a float, or None if not parseable.

    Strips $, %, commas, and a leading sign. A trailing % is treated as the raw
    number (95% -> 95.0), NOT divided -- percent-vs-fraction matching happens in
    build_allowed_set.
    """
    t = tok.strip().lstrip("$")
    is_pct = t.endswith("%")
    if is_pct:
        t = t[:-1]
    t = t.replace(",", "")
    if t in ("", "-", "."):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def extract_numbers(text):
    """Every numeric token in ``text`` as a list of raw string tokens.

    Dates (YYYY-MM-DD), semver-shaped version strings (vX.Y.Z), and the "52-Week"
    label are stripped first so they never register as orphan numbers. Returns the
    raw matched strings (e.g. '$95.00', '8.5%', '0.175') so the caller can report
    the orphan exactly as it appears.

    NOTE: this is the GENERIC extractor used to mine numbers from bundle strings
    (allowed-set construction). The report side of number_provenance does its own
    date/version/page-header handling (exact-match, not blind scrub) so that an
    out-of-bundle date or version in prose ORPHANS rather than being silently
    scrubbed away; see check_number_provenance.
    """
    scrubbed = _TS_RE.sub(" ", text)   # full timestamps before bare dates
    scrubbed = _DATE_RE.sub(" ", scrubbed)
    scrubbed = _VERSION_TOKEN_RE.sub(" ", scrubbed)
    scrubbed = _LABEL_RE.sub(" ", scrubbed)
    out = []
    for m in _NUM_RE.finditer(scrubbed):
        tok = m.group(0)
        if _canonical(tok) is None:
            continue
        out.append(tok)
    return out


def _iter_numeric_leaves(obj):
    """Yield every NUMERIC (non-bool) leaf across a nested dict/list.

    String leaves are NOT scanned here. Scanning every string leaf for embedded
    numbers was too permissive: prose could cite a number that only ever appears
    inside an arithmetic string or method label and never in a scripted table, and
    a random integer passed ~27% of the time. Numeric-leaf scanning stays global;
    string-leaf numbers are admitted only from a WHITELIST of paths the renderer
    actually echoes (see _iter_whitelisted_string_numbers).
    """
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield float(obj)
        return
    if isinstance(obj, str):
        return
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_numeric_leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_numeric_leaves(v)


def _strings_at(obj):
    """Yield every string leaf under ``obj`` (recursing dicts/lists)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings_at(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings_at(v)


def _dig(obj, *path):
    """Follow a key path through nested dicts; None if any hop is absent."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _iter_whitelisted_string_numbers(docs):
    """Yield numbers found inside the WHITELISTED bundle strings only.

    Each path below is a string (or nest of strings) that render_report echoes
    into the report body OR that is the designated evidence-brief / prose source
    the LLM cites from -- so a number living only inside one of these strings is
    legitimately in-bundle. Every other string leaf is NOT admitted, closing the
    fabrication channel where prose cited a number that appears only inside some
    unrelated arithmetic string. Paths verified against render_report.py; absent
    paths simply contribute nothing.
    """
    snapshot = docs.get("snapshot") or {}
    meta = snapshot.get("meta") or {}

    sources = []

    # snapshot meta.qc -- the attestation is printed verbatim in the footer
    # ("QC attestation: ..."); check details back the LLM's integrity prose.
    sources.append(meta.get("qc"))
    # snapshot meta.api_tier_notes -- printed verbatim in the footer.
    sources.append(meta.get("api_tier_notes"))
    # snapshot sentiment.insider_method -- the labeled method the smart-money prose cites.
    sources.append(_dig(snapshot, "sentiment", "insider_method"))

    tp = docs.get("module_tradeplan") or {}
    sp = tp.get("stock_plan") or {}
    sources.append(_dig(sp, "sizing", "arithmetic"))
    sources.append(_dig(sp, "hedge", "trigger"))
    sources.append(_dig(tp, "expression", "executability_note"))
    # invalidation fundamental_leg strings (metric/threshold/justification text).
    sources.append(_dig(sp, "invalidation", "fundamental_leg"))
    # O19: risk_units arithmetic disclosure string (numbers LLM prose may cite).
    sources.append(_dig(sp, "risk_units", "arithmetic"))

    opts = docs.get("module_options") or {}
    for st in (opts.get("recommended_structures") or []):
        if isinstance(st, dict):
            sources.append(st.get("arithmetic"))
            sources.append(st.get("pop_method"))
    for dec in (opts.get("declined") or []):
        if isinstance(dec, dict):
            sources.append(dec.get("reason"))
    sources.append(opts.get("warnings_global"))
    sources.append(opts.get("liquidity_verdict"))
    sources.append(_dig(opts, "vol_dashboard", "disclosure"))

    comp = docs.get("module_composite") or {}
    sources.append(comp.get("renormalization_note"))
    # thesis subscore arithmetic strings (the conviction rationale the thesis prose cites).
    sources.append(_dig(comp, "thesis_conviction", "subscores"))

    # Each evidence module's renormalization_note plus its per-subscore
    # ``arithmetic`` strings. The subscore arithmetic is the scoring rationale the
    # evidence-brief prose cites verbatim (e.g. technical "ext 19.9%", fundamental
    # "fcf_margin ... = 0.2861" -> "28.6%", "pe_fwd/pe_5yr_median = 1.6684" ->
    # "1.67x", sentiment "buy_pct 59.6%"); the real V1 report forced this path.
    for key in ("module_technical", "module_risk", "module_sentiment",
                "module_fundamental"):
        m = docs.get(key)
        if isinstance(m, dict):
            sources.append(m.get("renormalization_note"))
            for sub in (m.get("subscores") or []):
                if isinstance(sub, dict):
                    sources.append(sub.get("arithmetic"))

    for src in sources:
        if src is None:
            continue
        for s in _strings_at(src):
            for tok in extract_numbers(s):
                val = _canonical(tok)
                if val is not None:
                    yield val


def build_allowed_set(*objs):
    """Build the ALLOWED numeric set from every NUMERIC leaf across ``objs``.

    String leaves are NOT scanned here (see _iter_numeric_leaves); whitelisted
    string numbers are folded in separately by check_number_provenance.

    Each allowed value ``v`` contributes several tolerance-expanded forms:
      - v itself, and v rounded to 0/1/2 dp (abs);
      - abs(v) (sign-insensitive);
      - the %-rendering v*100 and its 0/1/2-dp roundings (a fraction 0.085 renders
        as 8.5% in the report);
      - the fraction v/100 and its roundings (a percent 8.5 stored as 8.5 might be
        cited as 0.085);
    All entries are stored rounded to 2 dp as the match key; is_allowed applies a
    ±0.01 absolute slack on top.
    """
    allowed = set()

    def add(x):
        if x is None:
            return
        allowed.add(round(x, 2))

    for obj in objs:
        for v in _iter_numeric_leaves(obj):
            for base in (v, abs(v), v * 100.0, v / 100.0, abs(v) * 100.0,
                         abs(v) / 100.0):
                add(base)
                add(round(base, 0))
                add(round(base, 1))
                add(round(base, 2))
    return allowed


def is_allowed(token, allowed):
    """True if the numeric ``token`` matches any allowed value within ±0.01.

    Matching is done against the token's absolute value at 2-dp resolution, so a
    minus sign in prose (a percent shown negative) does not create an orphan.
    """
    val = _canonical(token)
    if val is None:
        return False  # unparseable -> not provably in-bundle (belt-and-braces;
        #               extract_numbers already prefilters parseable tokens)
    key = round(abs(val), 2)
    # direct 2-dp membership.
    if key in allowed:
        return True
    # ±0.01 slack: check neighbors.
    for delta in (-0.02, -0.01, 0.01, 0.02):
        if round(key + delta, 2) in allowed:
            return True
    return False


# --------------------------------------------------------------------------- #
# Report section splitting.
# --------------------------------------------------------------------------- #

def _page_sections(report_text):
    """Split the report into the Page-1/2/3 section bodies (list of strings).

    Splits on the ``## Page`` headers. Returns the section bodies (excluding any
    preamble before the first Page header).
    """
    parts = re.split(r"^## Page ", report_text, flags=re.MULTILINE)
    # parts[0] is the preamble (title); the rest are the three pages.
    return parts[1:]


# --------------------------------------------------------------------------- #
# Exact-match allowance sets for dates and versions (fabrication-channel close).
# --------------------------------------------------------------------------- #

def build_allowed_dates(*docs_list):
    """Every date-shaped (YYYY-MM-DD) string anywhere in the given bundle docs.

    Collected from BOTH keys and values across the whole nested structure (as_of,
    expiries, fiscal/transaction/sample dates, catalyst dates, retrieved_utc
    timestamps...) plus the report's own date derived from each snapshot's as_of.
    A date-shaped token in the report that is not in this set is an ORPHAN (a
    fabricated date can no longer hide by being shape-scrubbed away).
    """
    dates = set()
    for docs in docs_list:
        if docs is None:
            continue
        blob = json.dumps(docs)
        dates.update(_DATE_RE.findall(blob))
        snap = docs.get("snapshot") if isinstance(docs, dict) else None
        as_of = _dig(snap or {}, "meta", "as_of_utc")
        if isinstance(as_of, str) and len(as_of) >= 10:
            m = _DATE_RE.match(as_of)
            if m:
                dates.add(m.group(0))
    return dates


def build_allowed_versions(*docs_list):
    """Every version string the bundle carries, in both raw and 'v'-prefixed form.

    Sources: each module's rubric_version, the expression rule_version, the
    snapshot schema_version, and the plugin version. render_report renders these
    as 'v1.0.0' (rubric), 'expression-v1.0.0' (rule), 'snapshot schema 0.2.1'
    (raw), and the plugin '0.3.0' (raw), so both the raw 'X.Y.Z' and the 'vX.Y.Z'
    rendering are admitted. A version-shaped token not in this set is an ORPHAN.
    """
    versions = set()

    def add(ver):
        if not isinstance(ver, str):
            return
        for m in _VERSION_TOKEN_RE.findall(ver):
            core = m[1:] if m.startswith("v") else m
            versions.add(core)
            versions.add("v" + core)

    for docs in docs_list:
        if docs is None:
            continue
        for key, m in docs.items():
            if key.startswith("module_") and isinstance(m, dict):
                add(m.get("rubric_version"))
        tp = docs.get("module_tradeplan") or {}
        add(_dig(tp, "expression", "rule_version"))
        snap = docs.get("snapshot") or {}
        add(_dig(snap, "meta", "schema_version"))
    add(render_report._plugin_version())
    return versions


def build_allowed_stamps(*docs_list):
    """Governance stamps prose may cite VERBATIM: the fundamental module's
    ``sector_scale`` ("<name>@<version>") and the composite ``weight_set``'s
    "<set>@<version>" (with or without its "CUSTOM " prefix).

    Exact-match only. Scale/weight-set versions are not X.Y.Z-shaped (e.g.
    "2026.1"), so without this their digits orphan in the numeric scan — yet
    citing the active scale is exactly what disciplined prose should do. The
    full ``name@version`` IS the identity, so only the full stamp is admitted;
    a bare version tail ("2026.1" alone) still orphans, and a stamp that is
    not in the bundle keeps its digits and orphans in the numeric scan.
    """
    stamps = set()
    for docs in docs_list:
        if docs is None:
            continue
        fund = docs.get("module_fundamental") or {}
        ss = fund.get("sector_scale")
        if isinstance(ss, str) and "@" in ss:
            stamps.add(ss)
        comp = docs.get("module_composite") or {}
        ws = comp.get("weight_set")
        if isinstance(ws, str) and "@" in ws:
            stamps.add(ws)                       # "CUSTOM deep-value@1.0"
            stamps.add(ws.split(None, 1)[-1])    # "deep-value@1.0"
    return stamps


# --------------------------------------------------------------------------- #
# Checks.
# --------------------------------------------------------------------------- #

def derived_delta_values(old_docs, new_docs):
    """The script-computed differences a delta report prints (new - old).

    A delta report's Δ columns are NOT bundle leaves (they are differences), so
    number_provenance must be told about them explicitly or it would flag every
    Δ as an orphan. This mirrors render_report's delta arithmetic: composite
    dimension-score deltas, the composite-score delta, and EV-metric deltas.
    """
    vals = []
    oc = old_docs.get("module_composite") or {}
    nc = new_docs.get("module_composite") or {}

    old_dims = {d.get("name"): d.get("score") for d in (oc.get("dimensions") or [])}
    new_dims = {d.get("name"): d.get("score") for d in (nc.get("dimensions") or [])}
    for name in set(old_dims) | set(new_dims):
        o, n = old_dims.get(name), new_dims.get(name)
        if o is not None and n is not None:
            vals.append(n - o)

    os_, ns = oc.get("score"), nc.get("score")
    if os_ is not None and ns is not None:
        vals.append(ns - os_)
    return vals


def _contract_rendered_numbers(docs):
    """Yield the numeric decision-contract fields the report renders that are NOT
    already bundle leaves — the O10b EV-uncertainty band (v1.1.0).

    render_report.build_capital_status renders ``ev_band`` ([low, high]) around
    ``ev_at_current`` off the contract build_contract mints deterministically from
    the bundle. The band endpoints and halfwidth are derived (ev_at_current ±
    k·spread), so they have no direct leaf; the QC re-builds the same contract and
    admits exactly those derived values. If the contract cannot be built (no
    composite) this yields nothing (the band is not rendered either).
    """
    if not isinstance(docs.get("module_composite"), dict):
        return
    try:
        contract = decision_contract.build_contract(docs)
    except Exception:  # pragma: no cover - contract build is pure; defensive only
        return
    band = contract.get("ev_band")
    if isinstance(band, (list, tuple)):
        for v in band:
            if isinstance(v, (int, float)):
                yield v
    for key in ("ev_uncertainty_halfwidth", "ev_at_current"):
        v = contract.get(key)
        if isinstance(v, (int, float)):
            yield v


def check_number_provenance(report_text, docs, extra_values=None,
                            previous_docs=None):
    """Every numeric / date / version token in the report traces to the bundle.

    Three orthogonal allowance sets are built from the bundle (and, in delta mode,
    the previous bundle):

      * NUMBERS -- every numeric leaf (global) plus numbers inside the WHITELISTED
        bundle strings the renderer echoes / the LLM cites. A prose number that
        appears only inside some other (non-whitelisted) string now ORPHANS.
      * DATES -- every date-shaped string in the bundle plus the report's own
        date. A date-shaped token not in the set ORPHANS (fake dates can no longer
        pass by being shape-scrubbed).
      * VERSIONS -- every rubric/expression/schema/plugin version (raw + 'v'-form).
        A version-shaped token not in the set ORPHANS (e.g. v9.99.99).
      * STAMPS -- governance stamps (sector_scale "name@version", weight_set
        "CUSTOM set@version") scrubbed as exact strings BEFORE the token scans,
        so prose may cite the active scale/weight set verbatim; a stamp not in
        the bundle keeps its digits and orphans numerically.

    Page headers: only the three EXACT headers render_report emits are chrome;
    their digits are stripped before scanning. Any other "## Page N" line keeps
    its digits, so "## Page 777" surfaces 777 as an ordinary numeric orphan.

    ``extra_values`` is an optional list of additional allowed numbers (delta mode:
    the script-computed Δ columns, which are not bundle leaves).
    """
    docs_for_dv = [docs] + ([previous_docs] if previous_docs else [])

    allowed = build_allowed_set(
        docs.get("snapshot"),
        *[docs.get(k) for k in docs if k.startswith("module_")])
    allowed |= build_allowed_set(list(_iter_whitelisted_string_numbers(docs)))
    # The page-1 capital-status block renders numbers straight off the decision
    # contract. Most (grade/score/hurdle_clearing_price) are echoes of bundle
    # leaves, but the O10b EV-uncertainty band (ev_band endpoints + halfwidth,
    # v1.1.0) is a DERIVED number with no direct leaf. Fold the contract's
    # render-surfaced numeric fields into the allowed set so the band traces to
    # the (deterministic, bundle-derived) contract rather than orphaning.
    allowed |= build_allowed_set(list(_contract_rendered_numbers(docs)))
    if previous_docs:
        allowed |= build_allowed_set(
            previous_docs.get("snapshot"),
            *[previous_docs.get(k) for k in previous_docs
              if k.startswith("module_")])
        allowed |= build_allowed_set(
            list(_iter_whitelisted_string_numbers(previous_docs)))
    if extra_values:
        allowed |= build_allowed_set(list(extra_values))

    allowed_dates = build_allowed_dates(*docs_for_dv)
    allowed_versions = build_allowed_versions(*docs_for_dv)
    allowed_stamps = build_allowed_stamps(*docs_for_dv)

    orphans = []

    # 1) Page headers: strip ONLY the three exact chrome headers, leaving any
    #    other "## Page ..." line (and its digits) in place for the numeric scan.
    scanned = report_text
    for header in _ALLOWED_PAGE_HEADERS:
        scanned = scanned.replace(header, " ")

    # 1b) Governance stamps: scrub exact bundle-carried stamps (longest first so
    #     "CUSTOM x@1.0" goes before its "x@1.0" suffix). A fabricated stamp is
    #     NOT scrubbed — its digits fall through and orphan numerically.
    for stamp in sorted(allowed_stamps, key=len, reverse=True):
        scanned = scanned.replace(stamp, " ")

    # 2) Version tokens: exact-match, then remove so their digit fragments do not
    #    re-enter the numeric scan.
    for m in _VERSION_TOKEN_RE.findall(scanned):
        core = m[1:] if m.startswith("v") else m
        if core not in allowed_versions and m not in allowed_versions:
            orphans.append(m)
    scanned = _VERSION_TOKEN_RE.sub(" ", scanned)

    # 3a) FULL ISO timestamps first (date scrub alone leaves the time-of-day
    #     digits to orphan as numbers — live-refresh finding: reused sources'
    #     retrieved_utc minutes tripped provenance). The timestamp is verified
    #     by its DATE component (times come from bundle retrieved_utc strings;
    #     a fabricated timestamp is still caught by its date).
    for m in _TS_RE.findall(scanned):
        if m[:10] not in allowed_dates:
            orphans.append(m)
    scanned = _TS_RE.sub(" ", scanned)

    # 3b) Date tokens: exact-match, then remove so their integers do not re-enter.
    for m in _DATE_RE.findall(scanned):
        if m not in allowed_dates:
            orphans.append(m)
    scanned = _DATE_RE.sub(" ", scanned)

    # 4) Numeric tokens on what remains (52-Week label scrubbed inside
    #    extract_numbers). Exclude fixed structural constants that are report
    #    format artifacts, not data: "/100" denominators, "1.0" total weight.
    format_constants = {"100", "1.0", "1", "0"}
    scanned = _LABEL_RE.sub(" ", scanned)
    for m in _NUM_RE.finditer(scanned):
        tok = m.group(0)
        if _canonical(tok) is None:
            continue
        raw = tok.strip().lstrip("$").rstrip("%").replace(",", "").lstrip("-")
        if raw in format_constants:
            continue
        if not is_allowed(tok, allowed):
            orphans.append(tok)

    # de-dupe preserving order.
    seen = set()
    uniq = [o for o in orphans if not (o in seen or seen.add(o))]
    if uniq:
        shown = uniq[:_ORPHAN_CAP]
        more = f" (+{len(uniq) - len(shown)} more)" if len(uniq) > len(shown) else ""
        return _result("number_provenance", False,
                       f"{len(uniq)} orphan token(s): " + ", ".join(shown) + more)
    return _result("number_provenance", True,
                   "all report numbers, dates, and versions trace to the bundle")


def check_composite_arithmetic(docs):
    comp = docs.get("module_composite")
    if not isinstance(comp, dict):
        return _result("composite_arithmetic", None, "SKIP: no module_composite")
    dims = comp.get("dimensions") or []
    if not dims:
        return _result("composite_arithmetic", None, "SKIP: no dimensions")
    total = 0.0
    for d in dims:
        w = d.get("weight_renormalized")
        if w is None:
            w = d.get("weight")
        s = d.get("score")
        contrib = d.get("contribution")
        if w is None or s is None:
            continue
        expected_contrib = w * s
        if contrib is not None and abs(expected_contrib - contrib) > 0.01:
            return _result("composite_arithmetic", False,
                           f"dimension {d.get('name')}: weight*score "
                           f"{expected_contrib:.4g} != contribution {contrib}")
        total += (contrib if contrib is not None else expected_contrib)
    score = comp.get("score")
    if score is None:
        return _result("composite_arithmetic", None, "SKIP: no composite score")
    if abs(total - score) > 0.01:
        return _result("composite_arithmetic", False,
                       f"Σ contributions {total:.4g} != composite score {score} "
                       f"(tol 0.01)")
    return _result("composite_arithmetic", True,
                   f"Σ contributions {total:.4g} == composite score {score}")


def check_ev_consistency(docs):
    comp = docs.get("module_composite")
    if not isinstance(comp, dict):
        return _result("ev_consistency", None, "SKIP: no module_composite")
    ev = comp.get("ev") or {}
    scenarios = ev.get("scenarios") or []
    if not scenarios:
        return _result("ev_consistency", None, "SKIP: no scenarios")
    prob_sum = sum(sc.get("prob", 0) for sc in scenarios)
    if abs(prob_sum - 1.0) > 1e-6:
        return _result("ev_consistency", False,
                       f"scenario probs sum {prob_sum} != 1 (tol 1e-6)")
    last = (docs.get("snapshot", {}) or {}).get("price", {}).get("last")
    reported = ev.get("ev_at_current")
    if last is None or reported is None:
        return _result("ev_consistency", True,
                       f"probs sum {prob_sum}; ev_at_current not recomputable "
                       "(last or ev_at_current absent)")
    recomputed = sum(sc["prob"] * (sc["price_target"] / last - 1) for sc in scenarios)
    if abs(recomputed - reported) > 0.001:
        return _result("ev_consistency", False,
                       f"ev_at_current recomputed {recomputed:.4g} != reported "
                       f"{reported} (tol 0.001)")
    return _result("ev_consistency", True,
                   f"probs sum {prob_sum}; ev_at_current {reported} reproduced")


def check_invalidation_both_legs(report_text, docs):
    tp = docs.get("module_tradeplan")
    if not isinstance(tp, dict):
        return _result("invalidation_both_legs", None, "SKIP: no module_tradeplan")
    inv = (tp.get("stock_plan", {}) or {}).get("invalidation", {}) or {}
    tech = inv.get("technical_leg") or {}
    fund = inv.get("fundamental_leg") or {}
    tech_level = tech.get("level")
    fund_metric = fund.get("metric")

    problems = []
    if tech_level is not None:
        # the technical level must appear as a number in the report.
        level_str = render_report._fmt(tech_level)
        if level_str not in report_text:
            problems.append(f"technical invalidation level {level_str} absent")
    if fund_metric:
        if fund_metric not in report_text:
            problems.append(f"fundamental invalidation metric text "
                            f"'{fund_metric}' absent")
    if problems:
        return _result("invalidation_both_legs", False, "; ".join(problems))
    return _result("invalidation_both_legs", True,
                   "both invalidation legs present in the report")


def check_sizing_within_cap(docs):
    tp = docs.get("module_tradeplan")
    if not isinstance(tp, dict):
        return _result("sizing_within_cap", None, "SKIP: no module_tradeplan")
    sizing = (tp.get("stock_plan", {}) or {}).get("sizing", {}) or {}
    rec = sizing.get("recommended_pct")
    cap = sizing.get("cap_pct")
    if rec is None or cap is None:
        return _result("sizing_within_cap", None,
                       "SKIP: recommended_pct or cap_pct absent")
    if rec > cap + 1e-9:
        return _result("sizing_within_cap", False,
                       f"recommended_pct {rec} > cap_pct {cap}")
    return _result("sizing_within_cap", True,
                   f"recommended_pct {rec} <= cap_pct {cap}")


def check_strikes_in_chain(docs, bundle):
    options = docs.get("module_options")
    if not isinstance(options, dict):
        return _result("strikes_in_chain", None, "SKIP: no module_options")
    strikes_needed = set()
    for st in options.get("recommended_structures", []) or []:
        for s in st.get("strikes", []) or []:
            strikes_needed.add(round(float(s), 4))
        for lg in st.get("legs", []) or []:
            if lg.get("strike") is not None:
                strikes_needed.add(round(float(lg["strike"]), 4))
    hedge = options.get("hedge_structure")
    if isinstance(hedge, dict):
        for lg in hedge.get("legs", []) or []:
            if lg.get("strike") is not None:
                strikes_needed.add(round(float(lg["strike"]), 4))

    if not strikes_needed:
        return _result("strikes_in_chain", None,
                       "SKIP: no recommended/hedge structures with strikes")

    snapshot = docs.get("snapshot") or {}
    chain_file = (snapshot.get("options") or {}).get("chain_file_path")
    if not chain_file:
        return _result("strikes_in_chain", None,
                       "SKIP: snapshot has no options.chain_file_path")
    chain_path = chain_file if os.path.isabs(chain_file) \
        else os.path.join(bundle, chain_file)
    try:
        contracts = chain_mod.load_contracts(chain_path)
    except (OSError, ValueError) as exc:
        return _result("strikes_in_chain", False,
                       f"cannot load chain {chain_path}: {exc}")
    listed = {round(float(c["strike"]), 4) for c in contracts if "strike" in c}
    missing = sorted(strikes_needed - listed)
    if missing:
        return _result("strikes_in_chain", False,
                       "strikes not in chain: "
                       + ", ".join(render_report._fmt(s) for s in missing))
    return _result("strikes_in_chain", True,
                   f"all {len(strikes_needed)} strike(s) exist in the chain")


def check_pop_method_labeled(report_text, docs):
    options = docs.get("module_options")
    if not isinstance(options, dict):
        return _result("pop_method_labeled", None, "SKIP: no module_options")
    rec = options.get("recommended_structures", []) or []
    if not rec:
        return _result("pop_method_labeled", None,
                       "SKIP: no recommended structures")
    for st in rec:
        if not st.get("pop_method"):
            return _result("pop_method_labeled", False,
                           f"structure {st.get('name')} has no pop_method")
    if "PoP" not in report_text:
        return _result("pop_method_labeled", False,
                       "report strategy table lacks a 'PoP' label")
    if "delta" not in report_text.lower():
        return _result("pop_method_labeled", False,
                       "report lacks a PoP-method mention (delta)")
    return _result("pop_method_labeled", True,
                   "every recommended structure has a labeled pop_method")


def check_expression_consistency(report_text, docs):
    tp = docs.get("module_tradeplan")
    if not isinstance(tp, dict):
        return _result("expression_consistency", None, "SKIP: no module_tradeplan")
    expr = tp.get("expression", {}) or {}
    rec = expr.get("recommended_for_profile")
    problems = []
    if rec and rec not in report_text:
        problems.append("recommended_for_profile text absent from report")
    if expr.get("executable") is False:
        note = expr.get("executability_note")
        if note and note not in report_text:
            problems.append("executability_note absent while executable=false")
    if problems:
        return _result("expression_consistency", False, "; ".join(problems))
    return _result("expression_consistency", True, "expression consistent")


def check_footer_integrity(report_text, docs):
    problems = []
    snapshot = docs.get("snapshot") or {}
    as_of = (snapshot.get("meta", {}) or {}).get("as_of_utc")
    if not as_of or as_of not in report_text:
        problems.append("as_of timestamp absent from footer")
    for key in ("module_technical", "module_risk", "module_sentiment",
                "module_fundamental", "module_composite", "module_tradeplan",
                "module_options"):
        m = docs.get(key)
        if isinstance(m, dict) and m.get("rubric_version"):
            ver = f"v{m['rubric_version']}"
            if ver not in report_text:
                problems.append(f"{key} rubric_version {ver} absent")
    if "not financial advice" not in report_text.lower():
        problems.append("disclaimer absent")
    if problems:
        return _result("footer_integrity", False, "; ".join(problems))
    return _result("footer_integrity", True,
                   "as_of, rubric versions, and disclaimer all present")


def check_word_cap(report_text):
    sections = _page_sections(report_text)
    if not sections:
        return _result("word_cap", None, "SKIP: no Page sections found")
    words = sum(len(s.split()) for s in sections)
    if words > _WORD_CAP:
        return _result("word_cap", False,
                       f"{words} words across pages 1-3 > cap {_WORD_CAP}")
    return _result("word_cap", True, f"{words} words <= cap {_WORD_CAP}")


def check_no_empty_slots(report_text):
    slots = re.findall(r"<!-- SLOT:([a-z_]+) -->", report_text)
    if slots:
        shown = slots[:_ORPHAN_CAP]
        return _result("no_empty_slots", False,
                       f"{len(slots)} unfilled slot(s): " + ", ".join(shown))
    return _result("no_empty_slots", True, "no unfilled slots remain")


# --------------------------------------------------------------------------- #
# G4a: semantic assertions.
#
# number_provenance catches a number the LLM invented; these catch a number that
# is IN the bundle but is described with a claim that contradicts the bundle. Each
# returns _result(name, passed|None, detail): FAIL (passed=False) only on a genuine
# contradiction, SKIP (None) when the inputs the assertion needs are absent. They
# never mint numbers -- they read the same module leaves the deterministic layer
# scored and check that the prose agrees with them.
# --------------------------------------------------------------------------- #

# "first positive-EV" / "first positive EV" (tolerant of a hyphen OR a space
# between "positive" and "EV"). Case-insensitive.
_FIRST_POSITIVE_EV_RE = re.compile(r"first\s+positive[\s-]+ev", re.IGNORECASE)
# A PAST-TENSE / present ASSERTION that a level was retaken: "reclaimed <N>",
# "reclaims <N>", "reclaimed the swing-low at <N>", "reclaimed back above <N>".
# Only the verb/participle forms "reclaimed"/"reclaims" match (the noun "a reclaim"
# does not), and the intervening window is captured so the check can REJECT a
# forward-looking directional -- "reclaims TOWARD 367", "reclaim TO 367" -- which
# asserts nothing about current price and is legitimate prose. Number group is a
# plain price (optional $ and thousands sep).
_RECLAIM_LEVEL_RE = re.compile(
    r"reclaim(?:ed|s)\b([^.\n]{0,40}?)\$?(\d[\d,]*\.?\d*)", re.IGNORECASE)
# Directional connectors that turn a "reclaim(ed) ... N" into a FORWARD-LOOKING
# statement ("reclaims toward 367") rather than an assertion the level was retaken.
# If one of these appears in the intervening window before the level, the level is
# a target, not a claim -> not checked.
_RECLAIM_FORWARD_RE = re.compile(r"\b(toward|towards|to|of)\b", re.IGNORECASE)
# "12-month" / "12 month" horizon label (case-insensitive, hyphen or space).
_TWELVE_MONTH_RE = re.compile(r"12[\s-]month", re.IGNORECASE)


def check_first_positive_ev_label(text_blob, docs):
    """FAIL if prose calls a level the "first positive-EV" entry while the CURRENT
    price already has positive EV.

    Rationale (GOOG live defect): the engine's field is ev_*breakeven*_entry (the
    HURDLE-clearing price), not the first price at which EV turns positive. When
    ev_at_current > 0, EV is already positive at spot, so no higher-priced "first
    positive-EV entry" can exist -- the phrase mislabels the hurdle-breakeven as
    the first-positive-EV level. SKIP when the phrase is absent or ev_at_current is
    unavailable.
    """
    name = "first_positive_ev_label"
    if not _FIRST_POSITIVE_EV_RE.search(text_blob or ""):
        return _result(name, None, "SKIP: no 'first positive-EV' phrase in text")
    ev_at_current = _dig(docs.get("module_composite") or {}, "ev", "ev_at_current")
    if ev_at_current is None:
        return _result(name, None,
                       "SKIP: 'first positive-EV' present but ev_at_current absent")
    if ev_at_current > 0:
        breakeven = _dig(docs.get("module_composite") or {}, "ev", "ev_breakeven_entry")
        return _result(name, False,
                       f"prose claims a 'first positive-EV' entry but "
                       f"ev_at_current={ev_at_current} > 0 (EV is already positive "
                       f"at spot); the labeled level is the hurdle-breakeven entry "
                       f"(ev_breakeven_entry={breakeven}), not the first positive-EV "
                       f"level.")
    return _result(name, True,
                   f"'first positive-EV' phrase present and ev_at_current="
                   f"{ev_at_current} <= 0 (consistent)")


def check_reclaimed_level(text, docs):
    """FAIL if prose ASSERTS the price "reclaimed N" while last < N.

    "Reclaimed X" asserts price is back above X; if the snapshot's last is below X
    the claim is false. A FORWARD-LOOKING directional -- "reclaims toward 367", "a
    reclaim to 367 reasserts" -- is NOT an assertion about current price (it names a
    target) and is skipped via the directional-connector guard. SKIP overall when
    there is no asserting reclaim phrase with a number, or price.last is absent.
    """
    name = "reclaimed_level"
    matches = _RECLAIM_LEVEL_RE.findall(text or "")
    # Keep only assertion matches (no directional connector before the level).
    assertions = [(window, raw) for window, raw in matches
                  if not _RECLAIM_FORWARD_RE.search(window)]
    if not assertions:
        return _result(name, None,
                       "SKIP: no asserting 'reclaimed <level>' phrase in text")
    last = _dig(docs.get("snapshot") or {}, "price", "last")
    if not isinstance(last, (int, float)):
        return _result(name, None,
                       "SKIP: 'reclaimed' phrase present but price.last absent")
    for _window, raw in assertions:
        level = _canonical(raw)
        if level is None:
            continue
        if last < level:
            return _result(name, False,
                           f"prose claims price 'reclaimed' {render_report._fmt(level)} "
                           f"but last {render_report._fmt(last)} < "
                           f"{render_report._fmt(level)} (not reclaimed).")
    return _result(name, True,
                   f"all reclaim levels <= last {render_report._fmt(last)}")


def check_version_labels(text, docs):
    """FAIL if a rubric version token 'vX.Y.Z' in the report is not one any module
    actually claims.

    build_allowed_versions collects every version the bundle carries (each module's
    rubric_version, the expression rule_version, the snapshot schema_version, the
    plugin version) in both raw and 'v'-prefixed form. A separate legitimate
    version -- e.g. 'confidence-v1.0.0' (the confidence-scorer artifact version, NOT
    a rubric label) -- is admitted iff its X.Y.Z appears among those bundle versions
    (it does, as the modules' confidence.version). A version-shaped token whose
    core X.Y.Z is in NO bundle module orphans. SKIP when no version token appears.
    """
    name = "version_labels"
    tokens = _VERSION_TOKEN_RE.findall(text or "")
    if not tokens:
        return _result(name, None, "SKIP: no version-shaped tokens in text")
    allowed = build_allowed_versions(docs)
    # Also admit each module's confidence.version (confidence-vX.Y.Z is a separate,
    # legitimate artifact version -- see the confidence subsystem). This is additive
    # to build_allowed_versions and never removes a rubric requirement.
    for key, m in (docs or {}).items():
        if key.startswith("module_") and isinstance(m, dict):
            cv = _dig(m, "confidence", "version")
            if isinstance(cv, str):
                core = cv[1:] if cv.startswith("v") else cv
                allowed.add(core)
                allowed.add("v" + core)
    orphans = []
    for tok in tokens:
        core = tok[1:] if tok.startswith("v") else tok
        if core not in allowed and tok not in allowed:
            orphans.append(tok)
    if orphans:
        # Dedupe preserving order.
        seen = []
        for o in orphans:
            if o not in seen:
                seen.append(o)
        return _result(name, False,
                       "version label(s) no module claims: "
                       + ", ".join(seen[:_ORPHAN_CAP]))
    return _result(name, True,
                   f"all {len(tokens)} version token(s) match an owning module")


def check_horizon_consistency(text, docs):
    """FAIL if the report carries a "12-month" horizon label while the composite's
    hurdle horizon is not 1.0 years (label contradicts the hurdle horizon).

    The hurdle_total is defined over horizon_years_convention; a "12-month" label on
    a 1.5y-hurdle bundle is the chart-title / prose horizon slip the review flagged
    (no computational impact, but a stated contradiction). SKIP when there is no
    12-month label or the convention is absent.
    """
    name = "horizon_consistency"
    if not _TWELVE_MONTH_RE.search(text or ""):
        return _result(name, None, "SKIP: no '12-month' horizon label in text")
    horizon_years = _dig(docs.get("module_composite") or {}, "ev",
                         "horizon_years_convention")
    if horizon_years is None:
        return _result(name, None,
                       "SKIP: '12-month' label present but "
                       "horizon_years_convention absent")
    if abs(horizon_years - 1.0) > 1e-9:
        return _result(name, False,
                       f"report carries a '12-month' horizon label but the hurdle "
                       f"horizon is {horizon_years}y "
                       f"(horizon_years_convention={horizon_years}); the label "
                       f"contradicts the hurdle horizon.")
    return _result(name, True,
                   "'12-month' label consistent with horizon_years_convention 1.0")


# A bare buy directive in the GOVERNING capital call: whole-word buy or accumulate
# (case-insensitive). "Accumulate-on-weakness" matches on the "accumulate" stem.
_BUY_DIRECTIVE_RE = re.compile(r"\b(?:buy|accumulate)\w*", re.IGNORECASE)
# A line labeled as a demoted evidence read (the composite action shown as
# disclosure, NOT the governing capital call). Case-insensitive; matches the
# renderer's "_evidence read:_ ..." annotation and any "evidence read" label.
_EVIDENCE_READ_RE = re.compile(r"evidence\s+read", re.IGNORECASE)


def _the_call_governing_lines(report_text):
    """The page-1 GOVERNING call lines: everything under '### The Call' up to the
    next '###' section header, EXCLUDING lines labeled 'evidence read'.

    The capital-status block (bullets) and the governing headline are the binding
    call; a line demoted to '_evidence read:_' is disclosure and is intentionally
    dropped so a composite 'Accumulate' shown ONLY there does not read as a bare
    buy directive. Returns the joined governing text (may be '' if the section is
    absent).
    """
    sections = _page_sections(report_text)
    if not sections:
        return ""
    # page1 body is the first section (its header line "1 — Decision" was consumed
    # by the split on "## Page ").
    page1 = sections[0]
    # Locate the '### The Call' subsection within page 1.
    m = re.search(r"^###\s+The Call\s*$", page1, flags=re.MULTILINE)
    if not m:
        return ""
    body = page1[m.end():]
    # Cut at the next '### ' header (e.g. '### Composite').
    nxt = re.search(r"^###\s", body, flags=re.MULTILINE)
    if nxt:
        body = body[:nxt.start()]
    governing = [ln for ln in body.splitlines()
                 if not _EVIDENCE_READ_RE.search(ln)]
    return "\n".join(governing)


def check_capital_action_governed(report_text, docs):
    """FAIL if the contract is capital-INELIGIBLE yet the page-1 GOVERNING capital
    call still carries a bare buy directive (BUY|ACCUMULATE) outside a labeled
    'evidence read' line.

    Enforces the review's assertion ``BUY|ACCUMULATE ⇒ capital_eligible``: when
    ``capital_eligible`` is False the governing call must be the capital status
    (WAIT / HOLD_NO_ADD), and the composite action ("Hold/Accumulate-on-weakness")
    may appear ONLY inside a clearly-labeled 'evidence read' annotation. A buy word
    on the governing headline (or in the capital-status bullets) is a govern
    violation.

    SKIP when: the contract cannot be built (no composite), ``capital_eligible`` is
    not False (True or unknown), or the '### The Call' section is absent.
    """
    name = "capital_action_governed"
    composite = docs.get("module_composite") if isinstance(docs, dict) else None
    if not isinstance(composite, dict):
        return _result(name, None,
                       "SKIP: module_composite absent — no contract to govern")
    contract = decision_contract.build_contract(docs)
    if contract.get("capital_eligible") is not False:
        return _result(name, None,
                       "SKIP: capital_eligible is not False "
                       f"({contract.get('capital_eligible')}) — nothing to govern")
    governing = _the_call_governing_lines(report_text)
    if not governing:
        return _result(name, None,
                       "SKIP: '### The Call' section not found in report")
    m = _BUY_DIRECTIVE_RE.search(governing)
    if m:
        blockers = contract.get("capital_blockers") or []
        return _result(name, False,
                       f"capital is INELIGIBLE (blockers: "
                       f"{', '.join(blockers) or 'none'}) but the GOVERNING call "
                       f"carries a bare buy directive '{m.group(0)}' outside an "
                       f"'evidence read' annotation; the composite action must be "
                       f"demoted to a labeled evidence read when capital is blocked.")
    return _result(name, True,
                   "capital INELIGIBLE and the governing call carries no bare "
                   "buy directive (composite action demoted / absent)")


# Regex to locate a Size row in a Markdown pipe-table (the render emits exactly
# "| Size | <value> |").  Used by check_size_governed to find the row to inspect.
_SIZE_ROW_RE = re.compile(r"\|\s*Size\s*\|([^|\n]+)\|", re.IGNORECASE)


def check_size_governed(report_text, docs):
    """FAIL if the contract is capital-INELIGIBLE yet the trade-plan Size row
    does not carry the 'no new risk now' governed framing.

    Enforces the G5b prescription: when ``capital_eligible`` is False the Size
    row MUST lead with "no new risk now" so it cannot be read as "deploy N% now"
    while capital is blocked.  The sizing numbers themselves are unaffected —
    they are the conditional entry-ladder sizes.

    SKIP when: module_composite absent (cannot build contract), ``capital_eligible``
    is not False (True or unknown — nothing to govern), or no Size row is present
    in the report (delta reports / older bundles).
    """
    name = "size_governed"
    composite = docs.get("module_composite") if isinstance(docs, dict) else None
    if not isinstance(composite, dict):
        return _result(name, None,
                       "SKIP: module_composite absent — no contract to build")
    contract = decision_contract.build_contract(docs)
    if contract.get("capital_eligible") is not False:
        return _result(name, None,
                       "SKIP: capital_eligible is not False "
                       f"({contract.get('capital_eligible')}) — nothing to govern")
    m = _SIZE_ROW_RE.search(report_text)
    if not m:
        return _result(name, None,
                       "SKIP: no Size row found in report — delta or older bundle")
    size_cell = m.group(1).strip()
    if "no new risk now" not in size_cell:
        blockers = contract.get("capital_blockers") or []
        return _result(name, False,
                       f"capital is INELIGIBLE (blockers: "
                       f"{', '.join(blockers) or 'none'}) but the Size row "
                       f"presents deployable risk framing without 'no new risk now': "
                       f"'{size_cell}'")
    return _result(name, True,
                   "capital INELIGIBLE and the Size row carries 'no new risk now' "
                   "governed framing")


# C\d+ token regex (finding citation): same pattern as _FINDING_REF_RE above
# but compiled once here for use in check_judgment_flag_citations.
_CID_RE = re.compile(r"C\d+")


def check_judgment_flag_citations(bundle):
    """C-ID referential-integrity gate on all non-default judgment flags (B29).

    Collects every non-default judgment-flag justification string across the
    four scored module JSONs, then verifies two things for each:

      * GROUNDING: the justification contains >=1 C<n> token (it cites a
        context finding). Zero tokens -> FAIL ("ungrounded judgment flag").
      * REFERENTIAL INTEGRITY: every C<n> token in the justification matches
        a finding id in module_context.findings[]. An orphan C-ID (one that
        does NOT appear in the registry) -> FAIL ("orphan citation").

    When module_context.json is ABSENT from the bundle (the web_compressed
    floor where no registry exists to cite) → passes automatically: there is
    no registry to check against, and the compressed-floor disclosure covers
    the omission.

    A module JSON that is absent from the bundle is SKIPPED (not a failure;
    only present modules are checked).

    Returns the standard {check, passed, detail} shape. Wired into the BLOCKING
    check list in run_report_qc so a failure makes the gate exit 1.
    """
    check_name = "judgment_flag_citations"

    # ---- load module_context.json; auto-pass if absent ----
    context_path = os.path.join(bundle, "module_context.json")
    if not os.path.isfile(context_path):
        return _result(check_name, True,
                       "no module_context.json — compressed floor, auto-pass")

    try:
        with open(context_path) as fh:
            ctx = json.load(fh)
    except (OSError, ValueError) as exc:
        return _result(check_name, False,
                       f"cannot parse module_context.json: {exc}")

    # Build the valid finding-ID set from module_context.findings[].id
    valid_ids = set()
    for f in (ctx.get("findings") or []):
        if isinstance(f, dict) and isinstance(f.get("id"), str):
            valid_ids.add(f["id"])

    # ---- collect non-default judgment-flag justifications per module ----
    # Each entry: (module_label, flag_name, justification_string)
    to_check = []

    # Helper: load a module JSON from the bundle; return None if absent.
    def _load_module(filename):
        path = os.path.join(bundle, filename)
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    # --- technical ---
    tech = _load_module("module_technical.json")
    if tech is not None:
        flags = tech.get("flags") or {}
        if flags.get("divergence", "none") != "none":
            just = flags.get("divergence_justification")
            if just:
                to_check.append(("technical", "divergence_justification", just))

    # --- sentiment ---
    sent = _load_module("module_sentiment.json")
    if sent is not None:
        flags = sent.get("flags") or {}
        if flags.get("rating_actions", "neutral") != "neutral":
            just = flags.get("rating_actions_justification")
            if just:
                to_check.append(("sentiment", "rating_actions_justification", just))
        if flags.get("inst_flow", "unknown") != "unknown":
            just = flags.get("inst_flow_justification")
            if just:
                to_check.append(("sentiment", "inst_flow_justification", just))
        if flags.get("insider_baseline", "normal") != "normal":
            just = flags.get("insider_baseline_justification")
            if just:
                to_check.append(("sentiment", "insider_baseline_justification", just))

    # --- risk ---
    risk = _load_module("module_risk.json")
    if risk is not None:
        flags = risk.get("flags") or {}
        if flags.get("top_risk") is not None and flags.get("stress_pct") is not None:
            just = flags.get("top_risk")  # top_risk IS the justification string
            if just:
                to_check.append(("risk", "top_risk", just))

    # --- composite ---
    comp = _load_module("module_composite.json")
    if comp is not None:
        flags = comp.get("flags") or {}
        if flags.get("variant", "none") != "none":
            just = flags.get("variant_justification")
            if just:
                to_check.append(("composite", "variant_justification", just))
        if flags.get("catalyst_clarity", "vague") != "vague":
            just = flags.get("catalyst_clarity_justification")
            if just:
                to_check.append(("composite", "catalyst_clarity_justification", just))
        # NOTE: composite `invalidation` is DELIBERATELY EXEMPT from the C-ID
        # citation gate (matching score_composite, which does not require a C-ID
        # in --invalidation-justification): the invalidation legs cite trade-plan
        # LEVELS + fundamental metrics, not context findings. Enforcing a C-ID
        # here would fail an otherwise-valid composite. (Code-review fix, 4C.)

    # ---- verify grounding + referential integrity ----
    for module_label, flag_name, justification in to_check:
        cids = _CID_RE.findall(justification)

        # Grounding: must cite at least one C-ID
        if not cids:
            return _result(check_name, False,
                           f"ungrounded judgment flag {module_label}.{flag_name}"
                           " — cite a context finding (C<n>)")

        # Referential integrity: every cited C-ID must exist in the registry
        for cid in cids:
            if cid not in valid_ids:
                return _result(check_name, False,
                               f"orphan citation {cid} in {module_label}.{flag_name}"
                               " — not in module_context.findings")

    n = len(to_check)
    return _result(check_name, True,
                   f"{n} non-default judgment flag(s) checked — all grounded and"
                   " citations resolve" if n else
                   "no non-default judgment flags — check trivially passes")


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #

def _is_delta_report(report_path, delta_flag):
    if delta_flag:
        return True
    return "Delta_Report" in os.path.basename(report_path)


def run_report_qc(bundle, report_path, delta=False, previous=None):
    """Run the applicable checks and return a list of result dicts.

    Full reports run all 11 checks; delta reports run {1, 9, 11} only. For a delta
    report, ``previous`` (the old bundle dir) lets number_provenance account for
    the old bundle's values AND the script-computed Δ columns; without it the Δ
    columns would read as orphans.
    """
    with open(report_path) as fh:
        report_text = fh.read()
    docs = render_report.load_bundle(bundle)

    is_delta = _is_delta_report(report_path, delta)

    if is_delta:
        # Fold the old bundle (numeric + whitelisted strings + dates + versions)
        # and the script-computed Δ columns into the allowed sets so a delta
        # report's old-value and Δ columns are in-bundle. ``previous_docs`` gives
        # number_provenance the old bundle's dates/versions too (the Comparison
        # header prints the old as_of).
        extra = []
        old_docs = None
        if previous and os.path.isdir(previous):
            old_docs = render_report.load_bundle(previous)
            extra.extend(derived_delta_values(old_docs, docs))
        return [
            check_number_provenance(report_text, docs, extra_values=extra,
                                    previous_docs=old_docs),
            check_footer_integrity(report_text, docs),
            check_no_empty_slots(report_text),
        ]

    return [
        check_number_provenance(report_text, docs),
        check_composite_arithmetic(docs),
        check_ev_consistency(docs),
        check_invalidation_both_legs(report_text, docs),
        check_sizing_within_cap(docs),
        check_strikes_in_chain(docs, bundle),
        check_pop_method_labeled(report_text, docs),
        check_expression_consistency(report_text, docs),
        check_footer_integrity(report_text, docs),
        check_word_cap(report_text),
        check_no_empty_slots(report_text),
        check_judgment_flag_citations(bundle),
        # G4a semantic assertions over the final report prose.
        check_first_positive_ev_label(report_text, docs),
        check_reclaimed_level(report_text, docs),
        check_version_labels(report_text, docs),
        check_horizon_consistency(report_text, docs),
        # G4b/G5: the capital call must be GOVERNED by the decision contract --
        # an ineligible bundle may not carry a bare buy directive on the governing
        # call (BUY|ACCUMULATE ⇒ capital_eligible).
        check_capital_action_governed(report_text, docs),
        # G5b: the trade-plan Size row must not present deployable risk framing
        # while capital is INELIGIBLE (size_governed ⇒ capital_eligible).
        check_size_governed(report_text, docs),
    ]


# --------------------------------------------------------------------------- #
# pdf_slots.json provenance gate (§7): number_provenance over the LLM-authored
# docket prose slots, using the SAME allowed-set machinery as the report gate.
# On PASS the caller stamps {"qc_passed": true, "checked_utc"} INTO the slots file
# and render_pdf.py refuses to render exec/detail unless that stamp is present.
# --------------------------------------------------------------------------- #

# The slot keys the docket carries (contract-pinned shape). Their string values
# are the LLM-authored prose the render embeds; every number in them must trace to
# the bundle exactly like a report prose slot. ``qc_passed``/``checked_utc`` are
# the stamp keys and are NOT prose (skipped when collecting slot strings).
#
# The pdf_slots shape (C4 extension): thesis_bullets / desk_read / positioning /
# delta_interpretation, PLUS ``evidence_notes`` — a dict of ~200-word per-dimension
# prose notes {"technical","fundamental","sentiment","risk","options"} that
# render_pdf embeds as the BODY of each EVIDENCE section (the arithmetic string is
# demoted to a small "SCORING TRAIL" exhibit). Those notes are LLM-authored prose
# that MUST pass number_provenance exactly like every other slot: collect_slot_strings
# recurses the whole structure, so each evidence_notes value is scanned and a
# fabricated number in a note orphans. Older bundles without evidence_notes are
# unaffected (the key is simply absent; render_pdf falls back to the brief /
# arithmetic).
_SLOT_STAMP_KEYS = ("qc_passed", "checked_utc")
# The evidence_notes sub-keys (contract-pinned, informational). Their VALUES are
# prose and are scanned; this tuple documents the expected dimensions.
_EVIDENCE_NOTE_DIMS = ("technical", "fundamental", "sentiment", "risk", "options")


def collect_slot_strings(slots):
    """Every prose string across the pdf_slots structure (recursing dict/list).

    Skips the stamp keys (qc_passed/checked_utc) so a re-run over an already-
    stamped file never scans the stamp itself. The recursion is exhaustive over
    dicts and lists, so the C4 ``evidence_notes`` map (per-dimension ~200-word
    notes) is scanned identically to the other slots — a fabricated number in an
    evidence note orphans exactly like one in a thesis bullet. Returns a flat list
    of strings.
    """
    out = []

    def walk(obj, skip_stamp=False):
        if isinstance(obj, str):
            out.append(obj)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if skip_stamp and k in _SLOT_STAMP_KEYS:
                    continue
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(slots, skip_stamp=True)
    return out


def run_pdf_slots_qc(bundle, slots, previous=None):
    """Run number_provenance over the concatenated slot prose. Returns a result
    list (one number_provenance result) so the CLI table/waiver code is reused.

    ``previous`` (an older bundle dir) folds the old bundle's values AND the
    script-computed Δ columns into the allowed set so a delta_interpretation slot
    may legitimately cite a Δ (mirrors the delta-report handling).
    """
    docs = render_report.load_bundle(bundle)
    slot_text = "\n".join(collect_slot_strings(slots))

    extra = []
    old_docs = None
    if previous and os.path.isdir(previous):
        old_docs = render_report.load_bundle(previous)
        extra.extend(derived_delta_values(old_docs, docs))

    return [
        check_number_provenance(slot_text, docs, extra_values=extra,
                                previous_docs=old_docs),
        # G4a: the "first positive-EV" slip lives in the LLM-authored docket prose
        # (pdf_slots), so the semantic assertion runs here too -- this is where the
        # GOOG live defect ("the first positive-EV entry is 334.69") is caught.
        check_first_positive_ev_label(slot_text, docs),
    ]


def _stamp_slots(slots_path, slots):
    """Write {"qc_passed": true, "checked_utc": <UTC ISO Z>} INTO the slots file.

    Preserves the original slot content; only adds/overwrites the two stamp keys.
    The timestamp is generated fresh (this is a provenance attestation, not a
    reproducibility-critical value), formatted as a Z-suffixed ISO-8601 UTC.
    """
    from datetime import datetime, timezone
    stamped = dict(slots)
    stamped["qc_passed"] = True
    stamped["checked_utc"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    with open(slots_path, "w") as fh:
        json.dump(stamped, fh, indent=2)


# --------------------------------------------------------------------------- #
# module_context.json provenance + structure gate (company-context, coverage-first).
#
# The company-context module (skill: company-context, v1.0.0) is the coverage-
# distilled / web-compressed business+competitive+cases+risks brief that feeds
# score_fundamental's --moat justification and grounds composite's conviction. It
# is UNSCORED as a dimension — findings are its citation registry. This gate runs
# BOTH:
#   (1) number_provenance over EVERY prose string field (reusing the SAME allowed-
#       set machinery as the report / pdf-slots gates) — a number in any narrative
#       that is not a bundle leaf ORPHANS, exactly like a report prose slot;
#   (2) structural checks over the findings registry, live_tape, and mode.
# On PASS the caller stamps {"qc_passed": true, "checked_utc"} INTO module.qc so a
# downstream consumer can tell the context passed its gate.
# --------------------------------------------------------------------------- #

# The two legal SOURCING modes the contract pins.
_CONTEXT_MODES = ("coverage_distilled", "web_compressed")
# Non-prose / structural top-level keys: their string VALUES are identifiers,
# dates, mode labels, findings IDs+sources, or the stamp — NOT narrative prose, so
# they are excluded from number_provenance (the structural checks cover them). The
# findings' ``source`` strings are citation anchors (artifact sections / URLs), not
# claims, so numbers inside a section name or URL must not orphan the whole gate.
_CONTEXT_META_KEYS = ("skill", "version", "ticker", "as_of", "mode", "qc")
# A finding ID is C followed by digits (C1..Cn); prose references them as "(C3)".
_FINDING_ID_RE = re.compile(r"^C\d+$")
_FINDING_REF_RE = re.compile(r"C\d+")
# An inline finding reference token, optionally parenthesized: "(C3)" / "C3". These
# are CITATION CHROME (they point into findings[]), not numeric claims -- scrubbed
# from the prose before number_provenance so the reference digit never orphans.
_CONTEXT_REF_SCRUB_RE = re.compile(r"\(?\bC\d+\)?")
# Financial shorthand: a numeric run carrying ONLY a TRAILING unit suffix
# (42B, 9999M, 30x, 45pct, 200bps, 3nm). The suffix is a magnitude/unit label, NOT
# an identifier -- the numeric part IS a data figure and MUST trace to the bundle,
# exactly as the report gate treats "$42B" (its _NUM_RE already yields 42 and checks
# it). We strip ONLY the suffix here, leaving the numeric part for the number scan.
# Anchored: digits (optional thousands/decimal) + one suffix + word boundary, with
# NO leading letter (that would be a product name -- handled below). The suffix
# alternation is ordered longest-first so "bps"/"pct" win over "b"/... boundaries.
_CONTEXT_UNIT_SUFFIX_RE = re.compile(
    r"\b(\d[\d,]*\.?\d*)(?:bps|pct|nm|mm|[BMKTxX])\b")
# A product/model name: an alphanumeric run still carrying a letter AND a digit
# AFTER unit-suffix stripping (A100, H200, GB300, HBM3E, RTX4090). The letter is
# part of an identifier, not a data figure, so the whole token is scrubbed before
# the numeric scan. This runs AFTER _CONTEXT_UNIT_SUFFIX_RE, so a pure unit-suffixed
# numeric (42B) has already had its suffix stripped to a letter-free "42" and no
# longer matches here -- its number flows to provenance. Real figures ("$95.00" /
# "8.5%" / "130") have no letter and are untouched.
_CONTEXT_PRODUCT_NAME_RE = re.compile(
    r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]+\b")


# Per-item keys that are CITATION ANCHORS, not narrative prose (parallel to a
# finding's ``source``): a numeric section name or URL fragment inside one of these
# must not orphan the number scan. risks[].anchor names the coverage artifact
# section / URL grounding the risk, exactly like findings[].source.
_CONTEXT_ANCHOR_KEYS = ("anchor",)


def collect_context_strings(module):
    """Every PROSE string in module_context to number-check (recursing dict/list).

    Scans the narrative fields — business / competitive / live_tape / cases /
    risks — where the LLM argues the situation; a number in any of these must
    trace to the bundle. EXCLUDED:
      * the structural/meta top-level keys (skill/version/ticker/as_of/mode/qc)
        and the whole ``findings`` list — a finding's ``claim`` is the citation
        registry entry and its ``source`` is an artifact-section or URL anchor
        (numbers in a section name or URL are not data claims);
      * per-item CITATION ANCHOR keys (risks[].anchor) — the coverage-artifact
        section / URL grounding a risk, parallel to a finding's ``source``.
    Prose that needs a number cites the finding ID "(C3)", and the number itself
    must appear in a scanned narrative field, where this gate checks it.
    """
    out = []

    def walk(obj):
        if isinstance(obj, str):
            out.append(obj)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if k in _CONTEXT_ANCHOR_KEYS:
                    continue
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    if isinstance(module, dict):
        for k, v in module.items():
            if k in _CONTEXT_META_KEYS or k == "findings":
                continue
            walk(v)
    return out


def _context_prose_for_refs(module):
    """The concatenated cases + competitive prose (where a finding ref must appear).

    A finding is only load-bearing when the argued narrative cites it; the
    structural check requires at least one C\\d+ reference somewhere in the cases
    or competitive prose. Returns that prose as one string.
    """
    parts = []
    for k in ("cases", "competitive"):
        for s in collect_context_strings({k: module.get(k)}):
            parts.append(s)
    return "\n".join(parts)


def check_context_structure(module, as_of=None):
    """Structural checks over the findings registry, live_tape, and mode.

    * findings: IDs match C\\d+, are unique, and are sequential C1..Cn; every
      finding carries a non-empty claim AND a non-empty source.
    * at least one C\\d+ reference appears in the cases or competitive prose (a
      finding registry no prose cites is dead weight — the whole point is inline
      grounding).
    * live_tape: every entry's date parses as YYYY-MM-DD and is <= as_of.
    * mode is one of the two legal values.
    """
    problems = []

    # --- mode ---
    mode = module.get("mode")
    if mode not in _CONTEXT_MODES:
        problems.append(
            f"mode {mode!r} not one of {_CONTEXT_MODES}")

    # --- findings registry ---
    findings = module.get("findings")
    if not isinstance(findings, list) or not findings:
        problems.append("findings[] is empty or not a list")
        findings = []
    ids = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            problems.append(f"findings[{i}] is not an object")
            continue
        fid = f.get("id")
        if not isinstance(fid, str) or not _FINDING_ID_RE.match(fid or ""):
            problems.append(f"findings[{i}] id {fid!r} is not C<n>")
        else:
            ids.append(fid)
        if not (isinstance(f.get("claim"), str) and f["claim"].strip()):
            problems.append(f"finding {fid!r} has an empty/missing claim")
        if not (isinstance(f.get("source"), str) and f["source"].strip()):
            problems.append(f"finding {fid!r} has an empty/missing source")
    # unique IDs
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        problems.append("duplicate finding id(s): " + ", ".join(dupes))
    # sequential C1..Cn (order-insensitive: the SET must equal {C1..Cn}).
    if ids and not dupes:
        nums = sorted(int(x[1:]) for x in ids)
        expected = list(range(1, len(nums) + 1))
        if nums != expected:
            problems.append(
                "finding ids not sequential C1..C%d (got %s)"
                % (len(nums), ", ".join("C%d" % n for n in nums)))

    # --- at least one finding referenced from cases / competitive prose ---
    ref_prose = _context_prose_for_refs(module)
    if not _FINDING_REF_RE.search(ref_prose):
        problems.append("no finding reference (C<n>) appears in cases or "
                        "competitive prose")

    # --- live_tape dates parse and are <= as_of ---
    from datetime import date
    as_of_d = None
    if isinstance(as_of, str):
        m = _DATE_RE.match(as_of)
        if m:
            try:
                as_of_d = date.fromisoformat(m.group(0))
            except ValueError:
                as_of_d = None
    live = module.get("live_tape")
    if not isinstance(live, list):
        problems.append("live_tape is not a list")
        live = []
    for i, ev in enumerate(live):
        if not isinstance(ev, dict):
            problems.append(f"live_tape[{i}] is not an object")
            continue
        d = ev.get("date")
        parsed = None
        if isinstance(d, str) and _DATE_RE.fullmatch(d):
            try:
                parsed = date.fromisoformat(d)
            except ValueError:
                parsed = None
        if parsed is None:
            problems.append(f"live_tape[{i}] date {d!r} does not parse (YYYY-MM-DD)")
        elif as_of_d is not None and parsed > as_of_d:
            problems.append(
                f"live_tape[{i}] date {d} is after as_of {as_of_d.isoformat()}")

    if problems:
        return _result("context_structure", False, "; ".join(problems))
    return _result("context_structure", True,
                   f"{len(ids)} finding(s) C1..C{len(ids)}, live_tape dated "
                   f"<= as_of, mode={mode}")


def _scrub_context_prose(text, module):
    """Remove context-specific CHROME before number_provenance scans the prose.

    Non-data token classes are stripped so they never orphan; financial shorthand
    is UNwrapped so its number DOES trace:
      * inline finding references ("(C3)" / "C3") -- citation markers into
        findings[], not figures;
      * financial shorthand with a TRAILING unit suffix (42B, 9999M, 30x, 45pct,
        200bps, 3nm) -- the suffix is a magnitude/unit label, but the NUMBER is a
        real data figure that MUST trace to the bundle. Only the suffix is stripped,
        exactly mirroring the report gate's handling of "$42B" (its _NUM_RE already
        yields 42 and checks it); the surviving number flows into the numeric scan;
      * product/model names with a letter glued to a digit (HBM3E, A100, GB300) --
        AFTER the suffix strip above, a token still carrying BOTH a letter and a
        digit is an identifier, not a data claim (real figures like $95.00 / 8.5% /
        130 keep a letter-free numeric run and are untouched);
      * the module's OWN live_tape dates -- these are LLM-authored event dates,
        validated separately by context_structure (parse + <= as_of), so they are
        not checked against the bundle's date set (a live-tape event legitimately
        post-dates the snapshot's fetch dates within the as_of ceiling).
    """
    scrubbed = _CONTEXT_REF_SCRUB_RE.sub(" ", text)
    # Unwrap unit-suffixed figures FIRST (keep the number), then scrub the residual
    # product-name identifiers. Order matters: after "42B" -> "42", the product-name
    # scrub sees a letter-free "42" and leaves it for provenance.
    scrubbed = _CONTEXT_UNIT_SUFFIX_RE.sub(r"\1 ", scrubbed)
    scrubbed = _CONTEXT_PRODUCT_NAME_RE.sub(" ", scrubbed)
    live = module.get("live_tape")
    if isinstance(live, list):
        for ev in live:
            if isinstance(ev, dict):
                d = ev.get("date")
                if isinstance(d, str) and _DATE_RE.fullmatch(d):
                    scrubbed = scrubbed.replace(d, " ")
    return scrubbed


def run_context_qc(bundle, module, previous=None):
    """Run BOTH context checks and return a result list (reuses the CLI table).

    number_provenance runs over every prose narrative string (the SAME allowed-set
    machinery as the report / pdf-slots gates), after context CHROME is scrubbed
    (finding refs, product-name digits, the module's own live_tape dates -- see
    _scrub_context_prose); context_structure runs the findings / live_tape / mode
    checks. ``previous`` folds an older bundle's values into the allowed set (a
    carried-forward refresh may cite a prior figure).
    """
    docs = render_report.load_bundle(bundle)
    prose_text = _scrub_context_prose(
        "\n".join(collect_context_strings(module)), module)

    extra = []
    old_docs = None
    if previous and os.path.isdir(previous):
        old_docs = render_report.load_bundle(previous)
        extra.extend(derived_delta_values(old_docs, docs))

    return [
        check_number_provenance(prose_text, docs, extra_values=extra,
                                previous_docs=old_docs),
        check_context_structure(module, as_of=module.get("as_of")),
    ]


def _stamp_context(context_path, module):
    """Write {"qc_passed": true, "checked_utc": <UTC ISO Z>} INTO module.qc.

    Preserves the original module content; only sets the ``qc`` key (contract:
    ``qc`` is null pre-gate, an attestation object post-gate). Mirrors _stamp_slots.
    """
    from datetime import datetime, timezone
    stamped = dict(module)
    stamped["qc"] = {
        "qc_passed": True,
        "checked_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(context_path, "w") as fh:
        json.dump(stamped, fh, indent=2)


# --------------------------------------------------------------------------- #
# Waivers + CLI (mirrors qc_gate.py).
# --------------------------------------------------------------------------- #

def _parse_waivers(raw_waivers):
    """Parse repeated 'name:reason' strings into {name: reason} dict."""
    out = {}
    for w in raw_waivers or []:
        if ":" in w:
            name, reason = w.split(":", 1)
            name, reason = name.strip(), reason.strip()
        else:
            name, reason = w.strip(), ""
        if name:
            out[name] = reason
    return out


def _apply_waivers(results, waiver_reasons):
    """Return (results, unwaived_failures). A FAILED check whose name is waived is
    marked WAIVED (detail prefixed) and does not count as an unwaived failure."""
    unwaived = 0
    for res in results:
        if res["passed"] is False and res["check"] in waiver_reasons:
            reason = waiver_reasons[res["check"]]
            res["detail"] = f"WAIVED: {reason}: {res['detail']}"
        elif res["passed"] is False:
            unwaived += 1
    return results, unwaived


def _status(res, waiver_names):
    if res["check"] in waiver_names and res["passed"] is False:
        return "WAIVED"
    if res["passed"] is True:
        return "PASS"
    if res["passed"] is False:
        return "FAIL"
    return "SKIP"


def _render_table(results, waiver_names):
    name_w = max([len(r["check"]) for r in results] + [len("check")])
    header = f"{'check'.ljust(name_w)}  STATUS  detail"
    lines = [header, "-" * len(header)]
    for r in results:
        status = _status(r, waiver_names)
        lines.append(f"{r['check'].ljust(name_w)}  {status.ljust(6)}  {r['detail']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report QC gate (§12, blocking): verify a rendered report "
                    "(or the docket's pdf_slots.json prose) numerically against "
                    "its bundle. Exits 0 (pass) or 1 (fail).")
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--report", default=None, help="path to the report .md")
    parser.add_argument("--pdf-slots", dest="pdf_slots", default=None,
                        help="path to the docket pdf_slots.json (runs "
                             "number_provenance over its prose slots; stamps "
                             "qc_passed=true INTO the file on pass)")
    parser.add_argument("--context", dest="context", default=None,
                        help="path to a company-context module_context.json (runs "
                             "number_provenance over its prose + structural checks "
                             "over its findings/live_tape/mode; stamps qc.qc_passed="
                             "true INTO the file on pass)")
    parser.add_argument("--delta", action="store_true",
                        help="treat the report as a delta report (checks 1/9/11)")
    parser.add_argument("--previous", default=None,
                        help="the older bundle dir (delta / pdf-slots mode: lets "
                             "number_provenance account for old values + deltas)")
    parser.add_argument("--waive", action="append", default=[],
                        metavar="check_name:reason",
                        help="waive a named check (repeatable)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 1
    if sum(bool(x) for x in (args.report, args.pdf_slots, args.context)) != 1:
        print("ERROR: pass exactly one of --report, --pdf-slots, or --context",
              file=sys.stderr)
        return 1

    waiver_reasons = _parse_waivers(args.waive)

    # --------------------------- pdf_slots mode --------------------------- #
    if args.pdf_slots:
        if not os.path.isfile(args.pdf_slots):
            print(f"ERROR: pdf_slots not found: {args.pdf_slots}", file=sys.stderr)
            return 1
        try:
            with open(args.pdf_slots) as fh:
                slots = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"ERROR: cannot parse pdf_slots {args.pdf_slots}: {exc}",
                  file=sys.stderr)
            return 1

        results = run_pdf_slots_qc(args.bundle, slots, previous=args.previous)
        results, unwaived = _apply_waivers(results, waiver_reasons)
        print(_render_table(results, set(waiver_reasons)))
        print()
        passed = unwaived == 0
        if passed:
            _stamp_slots(args.pdf_slots, slots)
            print("PDF SLOTS QC: PASS (qc_passed stamp written)")
        else:
            print("PDF SLOTS QC: FAIL")
        return 0 if passed else 1

    # --------------------------- context mode --------------------------- #
    if args.context:
        if not os.path.isfile(args.context):
            print(f"ERROR: context not found: {args.context}", file=sys.stderr)
            return 1
        try:
            with open(args.context) as fh:
                module = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"ERROR: cannot parse context {args.context}: {exc}",
                  file=sys.stderr)
            return 1

        results = run_context_qc(args.bundle, module, previous=args.previous)
        results, unwaived = _apply_waivers(results, waiver_reasons)
        print(_render_table(results, set(waiver_reasons)))
        print()
        passed = unwaived == 0
        if passed:
            _stamp_context(args.context, module)
            print("CONTEXT QC: PASS (qc.qc_passed stamp written)")
        else:
            print("CONTEXT QC: FAIL")
        return 0 if passed else 1

    # --------------------------- report mode --------------------------- #
    if not os.path.isfile(args.report):
        print(f"ERROR: report not found: {args.report}", file=sys.stderr)
        return 1

    results = run_report_qc(args.bundle, args.report, delta=args.delta,
                            previous=args.previous)
    results, unwaived = _apply_waivers(results, waiver_reasons)

    print(_render_table(results, set(waiver_reasons)))
    print()
    passed = unwaived == 0
    print("REPORT QC: " + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
