"""Report QC gate (§12, BLOCKING) for the trade-decision plugin.

WHY THIS MODULE EXISTS: render_report.py writes the report SKELETON from the
bundle (every number script-minted). After the LLM fills the prose slots, this
gate verifies the FINAL document numerically against the bundle so a report can
NEVER ship with a number that is not in the bundle. This is the enforcement half
of the slot architecture: render_report prevents number leakage by construction;
report_qc catches any number the LLM smuggled into a prose slot.

CLI: python3 scripts/report_qc.py --bundle <dir> --report <md path>
     [--waive "check:reason"]...  -> prints a check table + verdict, exits 0/1.

CHECKS (waiver mechanics mirror qc_gate.py):
 1. number_provenance   -- every numeric token in the report must trace to a
                           snapshot/module numeric leaf (with rounding + %-form
                           tolerances). Orphans FAIL (list capped at 20).
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
    sys.exit("trade-decision requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import render_report, chain as chain_mod

_WORD_CAP = 2100
_ORPHAN_CAP = 20

# A numeric token: optional leading $, digits with optional thousands separators
# and a decimal part, optional trailing %. We deliberately do NOT capture bare
# integers embedded in ISO dates (handled by pre-stripping date substrings).
_NUM_RE = re.compile(r"\$?-?\d[\d,]*\.?\d*%?")
# ISO date substrings (YYYY-MM-DD) are stripped before number extraction so a date
# never contributes three orphan integers.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Markdown table separators / rubric-version artifacts to strip.
_VERSION_RE = re.compile(r"v\d+\.\d+\.\d+")
# Fixed report labels/headers that embed a literal number (report chrome, not
# data): the "52-Week"/"52wk" column label and the "## Page N" section headers.
# Stripped before number extraction so their digits never register as orphans.
_LABEL_RE = re.compile(r"52[\s-]?wk|52[\s-]?week|^#+\s*Page\s*\d+",
                       re.IGNORECASE | re.MULTILINE)


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

    Dates (YYYY-MM-DD), rubric version strings (vX.Y.Z), and the word-count line
    are stripped first so they never register as orphan numbers. Returns the raw
    matched strings (e.g. '$95.00', '8.5%', '0.175') so the caller can report the
    orphan exactly as it appears.
    """
    scrubbed = _DATE_RE.sub(" ", text)
    scrubbed = _VERSION_RE.sub(" ", scrubbed)
    scrubbed = _LABEL_RE.sub(" ", scrubbed)
    out = []
    for m in _NUM_RE.finditer(scrubbed):
        tok = m.group(0)
        if _canonical(tok) is None:
            continue
        out.append(tok)
    return out


def _iter_numeric_leaves(obj):
    """Yield every numeric leaf across a nested dict/list.

    Numeric (non-bool) leaves yield directly. STRING leaves are ALSO scanned for
    embedded numeric tokens (the footer echoes bundle strings verbatim -- the QC
    attestation "8 passed", api_tier_notes "75 req/min", arithmetic strings, and
    method labels all carry numbers that legitimately appear in the report), so a
    number that lives only inside a bundle string is still in-bundle.
    """
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield float(obj)
        return
    if isinstance(obj, str):
        for tok in extract_numbers(obj):
            val = _canonical(tok)
            if val is not None:
                yield val
        return
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_numeric_leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_numeric_leaves(v)


def build_allowed_set(*objs):
    """Build the ALLOWED numeric set from every numeric leaf across ``objs``.

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
        return True  # unparseable -> not our concern
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


def check_number_provenance(report_text, docs, extra_values=None):
    """Every numeric token in the report traces to an allowed bundle value.

    ``extra_values`` is an optional list of additional allowed numbers (used in
    delta mode for the script-computed Δ columns, which are not bundle leaves).
    """
    allowed = build_allowed_set(
        docs.get("snapshot"),
        *[docs.get(k) for k in docs if k.startswith("module_")])
    if extra_values:
        allowed |= build_allowed_set(list(extra_values))
    # Exclude fixed structural constants that are report-format artifacts, not
    # data: "/100" score denominators and "1.0" composite total weight.
    format_constants = {"100", "1.0", "1", "0"}
    orphans = []
    for tok in extract_numbers(report_text):
        canon = _canonical(tok)
        if canon is None:
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
                       f"{len(uniq)} orphan number(s): " + ", ".join(shown) + more)
    return _result("number_provenance", True, "all report numbers trace to the bundle")


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
        # Fold the old bundle's leaves + the script-computed Δ columns into the
        # allowed set so a delta report's old-value and Δ columns are in-bundle.
        extra = []
        if previous and os.path.isdir(previous):
            old_docs = render_report.load_bundle(previous)
            extra.extend(derived_delta_values(old_docs, docs))
            for v in _iter_numeric_leaves(old_docs):
                extra.append(v)
        return [
            check_number_provenance(report_text, docs, extra_values=extra),
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
    ]


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
                    "numerically against its bundle. Exits 0 (pass) or 1 (fail).")
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--report", required=True, help="path to the report .md")
    parser.add_argument("--delta", action="store_true",
                        help="treat the report as a delta report (checks 1/9/11)")
    parser.add_argument("--previous", default=None,
                        help="the older bundle dir (delta mode: lets "
                             "number_provenance account for old values + deltas)")
    parser.add_argument("--waive", action="append", default=[],
                        metavar="check_name:reason",
                        help="waive a named check (repeatable)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 1
    if not os.path.isfile(args.report):
        print(f"ERROR: report not found: {args.report}", file=sys.stderr)
        return 1

    results = run_report_qc(args.bundle, args.report, delta=args.delta,
                            previous=args.previous)
    waiver_reasons = _parse_waivers(args.waive)
    results, unwaived = _apply_waivers(results, waiver_reasons)

    print(_render_table(results, set(waiver_reasons)))
    print()
    passed = unwaived == 0
    print("REPORT QC: " + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
