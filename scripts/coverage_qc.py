"""Coverage QC gate (B1/B2, BLOCKING) for the trading-desk plugin.

WHY THIS MODULE EXISTS: the user demanded FULL FSI initiation depth three times.
The shipped "proportionate depth" override in full-trade-analysis Phase 0.5 was
implementation drift the user never chose. Project law: "an instruction without a
required artifact is a suggestion." This gate is that required artifact — it makes
FSI-initiation depth (a) the DEFAULT, (b) QC-checkable by script, and (c) provenance-
recorded via coverage_manifest.json. Shallow coverage survives ONLY as an explicit
per-run user request, recorded in the manifest as depth_mode "shallow (user-requested)"
and re-checked in --mode shallow; an implementer never chooses shallow.

CLI: python3 scripts/coverage_qc.py --coverage <dir> [--mode full|shallow]
     [--waive "check:reason"]...  -> prints a PASS/FAIL/WAIVED check table + verdict,
     exits 0 (all pass/waived) or 1 (any unwaived failure). Waiver mechanics + table
     style mirror report_qc.py exactly.

The <dir> is an FSI-initiation `coverage/` directory holding the markdown-first
artifacts full-trade-analysis Phase 0.5 lands there (research.md / model.md /
valuation.md), the transcribed valuation_anchors.json, and the coverage_manifest.json
provenance record.

CHECKS (each a house-style result dict {check, passed, detail}):
 1. artifacts_present   -- research.md, model.md, valuation.md,
                           valuation_anchors.json, coverage_manifest.json all exist.
 2. manifest_shape      -- coverage_manifest.json parses and carries depth_mode,
                           skills_invoked, data_endpoints, artifacts, generated_utc;
                           the --mode flag and manifest depth_mode must AGREE.
 3. fsi_invoked         -- skills_invoked names the equity-research initiating-coverage
                           skill (the REAL FSI initiation skill).
 4. subskills_invoked   -- (FULL only; auto-pass shallow) skills_invoked names >=2
                           distinct financial-analysis:* sub-skills (the REAL names:
                           3-statement-model / dcf-model / comps-analysis).
 5. research_depth      -- research.md carries ALL nine REAL FSI Task-1 sections
                           (initiating-coverage references/task1-company-research.md
                           §Step 7), each >=150 words, total >=2500 (full) / >=800
                           (shallow). Word count is a plain whitespace split.
 6. model_depth         -- model.md shows a 3-statement structure (income / balance /
                           cash-flow sections) and >=3 FORWARD fiscal years (>=1
                           shallow); the statements are required in both modes.
 7. valuation_depth     -- valuation.md has a DCF section naming wacc/discount rate
                           AND terminal growth, a comps table with >=4 comparable
                           rows (>=2 shallow), and bull/base/bull (or low/mid/high)
                           scenario values.
 8. anchors_coherent    -- valuation_anchors.json validates (same required keys +
                           positivity as score_fundamental.validate_anchors, duplicated
                           locally per house convention) AND dcf_base/bear/bull each
                           appear (as numbers, +/-0.5% or exact string) in
                           valuation.md — anchors are transcriptions, not inventions.

THRESHOLDS ARE FLOORS, not targets. FSI's own templates set the real target (Task 1 =
6-8K words / 9 sections; Task 3 comps = 5-10 peers). The floor exists to make SILENT
SHRINKAGE fail loudly — a coverage run that quietly degraded below a defensible
minimum cannot pass.

stdlib-only.
"""

import argparse
import json
import os
import re
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])


# --------------------------------------------------------------------------- #
# Named floor constants. FSI's initiating-coverage templates set the TARGET; these
# are FLOORS below the target — a run under a floor has silently shrunk and fails.
#   research: Task-1 target is 6,000-8,000 words across 9 sections. The full floor
#     (2,500 total, 150/section) is well under target, so it only catches a doc that
#     collapsed to a stub. Shallow total floor (800) is the explicit user-requested
#     quick pass.
#   model: Task-2 target is 5 forward years across the 3 statements. Full floor is 3
#     forward years; shallow accepts 1 (still with all three statements present).
#   valuation: Task-3 comps target is 5-10 peers. Full floor is 4 rows; shallow 2.
# --------------------------------------------------------------------------- #
_RESEARCH_TOTAL_FULL = 2500
_RESEARCH_TOTAL_SHALLOW = 800
_RESEARCH_SECTION_MIN = 150          # per-section floor (mode-independent)
_MODEL_FWD_YEARS_FULL = 3
_MODEL_FWD_YEARS_SHALLOW = 1
_COMPS_ROWS_FULL = 4
_COMPS_ROWS_SHALLOW = 2

# The two legal manifest depth_mode values. "full" is the DEFAULT; "shallow
# (user-requested)" is the ONLY sanctioned shallow value — the parenthetical is
# load-bearing (an implementer cannot record a bare "shallow").
_DEPTH_FULL = "full"
_DEPTH_SHALLOW = "shallow (user-requested)"
_LEGAL_DEPTH_MODES = (_DEPTH_FULL, _DEPTH_SHALLOW)

# The nine REAL FSI Task-1 research sections, from the cached initiating-coverage
# references/task1-company-research.md §Step 7 "Synthesis and Writing" structure.
# Each entry is (canonical_label, header_regex). The regex is case-insensitive and
# flexible about the "## " heading level and separators (— / - / & / and), so a
# section renamed slightly still matches while an ABSENT section fails.
_RESEARCH_SECTION_SPECS = [
    ("Company Overview", r"company\s+overview"),
    ("Company History", r"company\s+history"),
    ("Management Team", r"management\s+team"),
    ("Products & Services", r"products?\s*(?:&|and|/)\s*services?"),
    ("Customers & Go-to-Market", r"customers?\s*(?:&|and|/)\s*go[\s-]*to[\s-]*market"),
    ("Industry Overview", r"industry\s+overview"),
    ("Competitive Landscape", r"competitive\s+landscape"),
    ("Market Opportunity", r"market\s+opportunity"),
    ("Risk Assessment", r"risk\s+assessment"),
]

# The three financial statements a 3-statement model must show (real Task-2 tabs 2-4:
# Income Statement / Cash Flow Statement / Balance Sheet). Each is (label, regex).
_STATEMENT_SPECS = [
    ("income statement", r"income\s+statement"),
    ("balance sheet", r"balance\s+sheet"),
    ("cash flow", r"cash[\s-]*flow"),
]

# The REAL equity-research initiation skill name (cache: equity-research/skills/
# initiating-coverage). Matched as a substring so "equity-research:initiating-coverage"
# or a bare "initiating-coverage" both satisfy fsi_invoked.
_FSI_INITIATION_SKILL = "initiating-coverage"
# The financial-analysis sub-skill prefix (cache: financial-analysis/skills/*). A
# sub-skill entry is any skills_invoked whose skill string carries this prefix. The
# REAL sub-skill names the initiation workflow prescribes are 3-statement-model /
# dcf-model / comps-analysis (Task 2 model, Task 3 DCF, Task 3 comps).
_SUBSKILL_PREFIX = "financial-analysis:"

# Required manifest keys (B2 provenance shape).
_MANIFEST_KEYS = ("depth_mode", "skills_invoked", "data_endpoints", "artifacts",
                  "generated_utc")

_REQUIRED_ARTIFACTS = ("research.md", "model.md", "valuation.md",
                       "valuation_anchors.json", "coverage_manifest.json")

# Anchor keys duplicated locally per house convention (report_qc duplicates
# validate_anchors' semantics rather than importing across script boundaries when a
# gate must stand alone). Kept in lockstep with score_fundamental._ANCHOR_REQUIRED.
_ANCHOR_REQUIRED = ("dcf_base", "dcf_bear", "dcf_bull", "comps_low", "comps_high")
# Anchors whose value must ALSO be transcribed into valuation.md (the DCF scenarios).
_ANCHOR_TRANSCRIBED = ("dcf_base", "dcf_bear", "dcf_bull")
# Transcription tolerance: an anchor is "in valuation.md" if a numeric token within
# +/-0.5% of it appears, OR its exact string does. 0.5% absorbs rounding (120.0 vs
# 120.00) without admitting a different figure (111 vs 120).
_TRANSCRIPTION_TOL = 0.005

# A numeric token (same shape family as report_qc._NUM_RE): optional $, digits with
# thousands/decimals, optional trailing %.
_NUM_RE = re.compile(r"\$?-?\d[\d,]*\.?\d*%?")
# A fiscal-year token: optional FY prefix, a 20\d\d year, optional trailing E.
# ONLY the trailing "E" marks an unambiguous estimate (projected column, e.g.
# 2026E / FY2026E). A bare or FY-prefixed year with NO trailing E (2025 / FY2025)
# is a candidate that counts as forward ONLY when it EXCEEDS the latest historical
# year (the "FY" prefix is common on the latest HISTORICAL year in a model, so it
# cannot by itself mean "forward"). See _forward_year_count.
_YEAR_TOKEN_RE = re.compile(r"(?:FY)?(20\d\d)(E?)")
# A markdown table data row whose FIRST cell is ticker-like (1-6 uppercase letters,
# optionally dotted, e.g. BRK.B). Header/separator rows are excluded.
_COMPS_ROW_RE = re.compile(r"^\|\s*([A-Z]{1,6}(?:\.[A-Z])?)\s*\|")


def _result(name, passed, detail):
    return {"check": name, "passed": passed, "detail": detail}


def _canonical_num(tok):
    """Numeric token -> float, or None. Strips $, %, commas, sign."""
    t = tok.strip().lstrip("$")
    if t.endswith("%"):
        t = t[:-1]
    t = t.replace(",", "").lstrip("-")
    if t in ("", "-", "."):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Local validate_anchors (duplicated per house convention; kept in lockstep with
# score_fundamental.validate_anchors).
# --------------------------------------------------------------------------- #

def validate_anchors(anchors):
    """Return a list of named issues for a valuation_anchors dict ([] = valid).

    Requires dcf_base/dcf_bear/dcf_bull/comps_low/comps_high present + positive;
    current_pb (optional) must be positive when present. Mirrors
    score_fundamental.validate_anchors so a fat-fingered anchors file fails the
    coverage gate the same way it would fail scoring.
    """
    issues = []
    if not isinstance(anchors, dict):
        return ["anchors is not a JSON object"]
    for key in _ANCHOR_REQUIRED:
        v = anchors.get(key)
        if v is None:
            issues.append(f"missing required anchor: {key}")
        elif not isinstance(v, (int, float)) or isinstance(v, bool):
            issues.append(f"anchor {key} must be numeric")
        elif v <= 0:
            issues.append(f"anchor {key} must be positive (got {v})")
    if "current_pb" in anchors and anchors["current_pb"] is not None:
        cpb = anchors["current_pb"]
        if not isinstance(cpb, (int, float)) or isinstance(cpb, bool) or cpb <= 0:
            issues.append("anchor current_pb must be positive when present")
    return issues


# --------------------------------------------------------------------------- #
# File helpers.
# --------------------------------------------------------------------------- #

def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _split_research_sections(text):
    """Split research.md into {canonical_label: body_text} for the sections that
    ARE present. A section body runs from its matched header to the next ## header
    (or EOF). Matching uses the flexible per-section regex against ## header lines.
    """
    # Collect (line_index, canonical_label) for each header line that matches a spec.
    lines = text.splitlines()
    header_hits = []  # (line_idx, label)
    header_line_re = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")
    for i, line in enumerate(lines):
        m = header_line_re.match(line)
        if not m:
            continue
        title = m.group(1)
        for label, rx in _RESEARCH_SECTION_SPECS:
            if re.search(rx, title, re.IGNORECASE):
                header_hits.append((i, label))
                break
    # For each hit, body is lines until the NEXT ## header line of ANY kind.
    all_header_lines = [i for i, line in enumerate(lines)
                        if header_line_re.match(line)]
    bodies = {}
    for idx, (line_idx, label) in enumerate(header_hits):
        # next header line strictly after this one.
        nexts = [h for h in all_header_lines if h > line_idx]
        end = nexts[0] if nexts else len(lines)
        body = "\n".join(lines[line_idx + 1:end])
        bodies[label] = body
    return bodies


# --------------------------------------------------------------------------- #
# Checks.
# --------------------------------------------------------------------------- #

def check_artifacts_present(coverage_dir):
    missing = [name for name in _REQUIRED_ARTIFACTS
               if not os.path.isfile(os.path.join(coverage_dir, name))]
    if missing:
        return _result("artifacts_present", False,
                       "missing artifact(s): " + ", ".join(missing))
    return _result("artifacts_present", True,
                   f"all {len(_REQUIRED_ARTIFACTS)} coverage artifacts present")


def check_manifest_shape(coverage_dir, mode):
    path = os.path.join(coverage_dir, "coverage_manifest.json")
    if not os.path.isfile(path):
        return _result("manifest_shape", False, "coverage_manifest.json absent")
    try:
        manifest = _load_json(path)
    except (OSError, ValueError) as exc:
        return _result("manifest_shape", False,
                       f"coverage_manifest.json does not parse: {exc}")
    if not isinstance(manifest, dict):
        return _result("manifest_shape", False, "manifest is not a JSON object")

    problems = []
    for key in _MANIFEST_KEYS:
        if key not in manifest:
            problems.append(f"missing key: {key}")
    # type-shape the present keys.
    if "skills_invoked" in manifest:
        si = manifest["skills_invoked"]
        if not isinstance(si, list) or not all(
                isinstance(e, dict) and "skill" in e and "args_summary" in e
                for e in si):
            problems.append("skills_invoked must be a list of "
                            "{skill, args_summary}")
    if "data_endpoints" in manifest:
        de = manifest["data_endpoints"]
        if not isinstance(de, list) or not all(isinstance(e, str) for e in de):
            problems.append("data_endpoints must be a list of str")
    if "artifacts" in manifest:
        ar = manifest["artifacts"]
        if not isinstance(ar, list) or not all(isinstance(e, str) for e in ar):
            problems.append("artifacts must be a list of str")
    if "generated_utc" in manifest:
        gu = manifest["generated_utc"]
        if not isinstance(gu, str) or not gu.strip():
            problems.append("generated_utc must be a non-empty ISO string")

    depth = manifest.get("depth_mode")
    if depth not in _LEGAL_DEPTH_MODES:
        problems.append(f"depth_mode {depth!r} not one of {_LEGAL_DEPTH_MODES}")
    else:
        # The --mode flag and the recorded depth_mode MUST agree — a full-mode
        # gate over a shallow-recorded run (or vice versa) is a provenance lie.
        expected = _DEPTH_FULL if mode == "full" else _DEPTH_SHALLOW
        if depth != expected:
            problems.append(
                f"depth_mode {depth!r} disagrees with --mode {mode} "
                f"(expected {expected!r})")

    if problems:
        return _result("manifest_shape", False, "; ".join(problems))
    return _result("manifest_shape", True,
                   f"manifest well-formed; depth_mode={depth} agrees with "
                   f"--mode {mode}")


def _skills_invoked(coverage_dir):
    """The skill strings from the manifest's skills_invoked (best-effort; [] if
    the manifest is missing/malformed — the manifest_shape check owns that failure)."""
    path = os.path.join(coverage_dir, "coverage_manifest.json")
    try:
        manifest = _load_json(path)
    except (OSError, ValueError):
        return []
    si = manifest.get("skills_invoked") if isinstance(manifest, dict) else None
    if not isinstance(si, list):
        return []
    out = []
    for e in si:
        if isinstance(e, dict) and isinstance(e.get("skill"), str):
            out.append(e["skill"])
    return out


def check_fsi_invoked(coverage_dir):
    skills = _skills_invoked(coverage_dir)
    if any(_FSI_INITIATION_SKILL in s for s in skills):
        return _result("fsi_invoked", True,
                       f"equity-research {_FSI_INITIATION_SKILL} invoked")
    return _result("fsi_invoked", False,
                   f"skills_invoked does not name the equity-research "
                   f"{_FSI_INITIATION_SKILL} skill")


def check_subskills_invoked(coverage_dir, mode):
    if mode == "shallow":
        # Auto-pass on the explicit user-requested shallow floor: FSI sub-skills
        # are a FULL-depth requirement; a shallow run legitimately skips them.
        return _result("subskills_invoked", True,
                       "auto-pass (shallow, user-requested): FSI sub-skills not "
                       "required on the shallow floor")
    skills = _skills_invoked(coverage_dir)
    subs = set()
    for s in skills:
        if s.startswith(_SUBSKILL_PREFIX):
            subs.add(s[len(_SUBSKILL_PREFIX):])
    if len(subs) >= 2:
        return _result("subskills_invoked", True,
                       f">=2 distinct financial-analysis sub-skills invoked: "
                       + ", ".join(sorted(subs)))
    return _result("subskills_invoked", False,
                   f"only {len(subs)} distinct financial-analysis:* sub-skill(s) "
                   f"invoked (need >=2): " + (", ".join(sorted(subs)) or "none"))


def check_research_depth(coverage_dir, mode):
    path = os.path.join(coverage_dir, "research.md")
    if not os.path.isfile(path):
        return _result("research_depth", False, "research.md absent")
    text = _read(path)
    total_words = len(text.split())

    bodies = _split_research_sections(text)
    present = set(bodies)
    all_labels = {label for label, _ in _RESEARCH_SECTION_SPECS}
    missing = sorted(all_labels - present)
    problems = []
    if missing:
        problems.append("missing FSI Task-1 section(s): " + ", ".join(missing))

    # per-section floor (mode-independent): each PRESENT section >= 150 words.
    thin = []
    for label in sorted(present):
        wc = len(bodies[label].split())
        if wc < _RESEARCH_SECTION_MIN:
            thin.append(f"{label} ({wc}w)")
    if thin:
        problems.append(
            f"section(s) below the {_RESEARCH_SECTION_MIN}-word floor: "
            + ", ".join(thin))

    total_floor = _RESEARCH_TOTAL_FULL if mode == "full" else _RESEARCH_TOTAL_SHALLOW
    if total_words < total_floor:
        problems.append(f"total {total_words}w < {mode} floor {total_floor}w")

    if problems:
        return _result("research_depth", False, "; ".join(problems))
    return _result("research_depth", True,
                   f"all 9 FSI Task-1 sections present, each >= "
                   f"{_RESEARCH_SECTION_MIN}w; total {total_words}w >= "
                   f"{total_floor}w ({mode})")


def _forward_year_count(text):
    """Count DISTINCT forward fiscal years in `text`.

    Heuristic (documented + pinned in tests): collect every year token via
    _YEAR_TOKEN_RE. A year with a trailing 'E' (2026E / FY2026E) is an unambiguous
    ESTIMATE and always counts as forward. A year with NO trailing 'E' (2025 /
    FY2025) is a candidate: the latest such year is treated as the latest HISTORICAL
    anchor, and any other non-E year strictly greater than it also counts as forward.
    Returns the count of DISTINCT forward years. When no non-E year anchors the
    history, every E-marked year still counts.
    """
    est_years = set()    # years with a trailing E (unambiguous estimates)
    plain_years = set()  # 20xx (optionally FY-prefixed) with NO trailing E
    for m in _YEAR_TOKEN_RE.finditer(text):
        year = int(m.group(1))
        if m.group(2) == "E":
            est_years.add(year)
        else:
            plain_years.add(year)
    hist_max = max(plain_years) if plain_years else None
    forward = set(est_years)
    if hist_max is not None:
        forward |= {y for y in plain_years if y > hist_max}
    return len(forward)


def check_model_depth(coverage_dir, mode):
    path = os.path.join(coverage_dir, "model.md")
    if not os.path.isfile(path):
        return _result("model_depth", False, "model.md absent")
    text = _read(path)
    low = text.lower()

    problems = []
    # 3-statement structure required in BOTH modes.
    missing_stmts = [label for label, rx in _STATEMENT_SPECS
                     if not re.search(rx, low)]
    if missing_stmts:
        problems.append("missing statement(s): " + ", ".join(missing_stmts))

    fwd = _forward_year_count(text)
    floor = _MODEL_FWD_YEARS_FULL if mode == "full" else _MODEL_FWD_YEARS_SHALLOW
    if fwd < floor:
        problems.append(f"{fwd} forward fiscal year(s) < {mode} floor {floor}")

    if problems:
        return _result("model_depth", False, "; ".join(problems))
    return _result("model_depth", True,
                   f"3-statement structure present; {fwd} forward year(s) >= "
                   f"{floor} ({mode})")


def check_valuation_depth(coverage_dir, mode):
    path = os.path.join(coverage_dir, "valuation.md")
    if not os.path.isfile(path):
        return _result("valuation_depth", False, "valuation.md absent")
    text = _read(path)
    low = text.lower()

    problems = []
    # DCF section naming wacc/discount rate AND terminal growth.
    has_wacc = ("wacc" in low) or ("discount rate" in low)
    has_terminal = ("terminal growth" in low) or ("terminal value" in low) \
        or ("perpetuity growth" in low)
    if not has_wacc:
        problems.append("DCF section names neither 'wacc' nor 'discount rate'")
    if not has_terminal:
        problems.append("DCF section names no terminal-growth / terminal-value")

    # comps table with >= N ticker-first rows.
    comps_rows = 0
    for line in text.splitlines():
        if _COMPS_ROW_RE.match(line.strip()):
            comps_rows += 1
    comps_floor = _COMPS_ROWS_FULL if mode == "full" else _COMPS_ROWS_SHALLOW
    if comps_rows < comps_floor:
        problems.append(
            f"comps table has {comps_rows} comparable row(s) < {mode} floor "
            f"{comps_floor}")

    # bull/base/bear OR low/mid/high scenario values.
    has_bbb = all(w in low for w in ("bull", "base", "bear"))
    has_lmh = all(w in low for w in ("low", "mid", "high"))
    if not (has_bbb or has_lmh):
        problems.append("no bull/base/bear (or low/mid/high) scenario values")

    if problems:
        return _result("valuation_depth", False, "; ".join(problems))
    return _result("valuation_depth", True,
                   f"DCF (wacc + terminal), {comps_rows} comps rows >= "
                   f"{comps_floor}, scenario values present ({mode})")


def check_anchors_coherent(coverage_dir):
    apath = os.path.join(coverage_dir, "valuation_anchors.json")
    vpath = os.path.join(coverage_dir, "valuation.md")
    if not os.path.isfile(apath):
        return _result("anchors_coherent", False, "valuation_anchors.json absent")
    try:
        anchors = _load_json(apath)
    except (OSError, ValueError) as exc:
        return _result("anchors_coherent", False,
                       f"valuation_anchors.json does not parse: {exc}")

    issues = validate_anchors(anchors)
    if issues:
        return _result("anchors_coherent", False,
                       "invalid anchors: " + "; ".join(issues))

    # Each DCF scenario anchor must be TRANSCRIBED into valuation.md (a number
    # within +/-0.5% or the exact string). Anchors are transcriptions, not inventions.
    if not os.path.isfile(vpath):
        return _result("anchors_coherent", False, "valuation.md absent")
    vtext = _read(vpath)
    vnums = []
    for m in _NUM_RE.finditer(vtext):
        val = _canonical_num(m.group(0))
        if val is not None:
            vnums.append(val)

    absent = []
    for key in _ANCHOR_TRANSCRIBED:
        target = float(anchors[key])
        exact = ("%.2f" % target) in vtext or str(target) in vtext
        near = any(abs(n - target) <= _TRANSCRIPTION_TOL * abs(target)
                   for n in vnums)
        if not (exact or near):
            absent.append(f"{key}={target:g}")
    if absent:
        return _result("anchors_coherent", False,
                       "anchor(s) not transcribed into valuation.md: "
                       + ", ".join(absent))
    return _result("anchors_coherent", True,
                   "anchors valid and dcf_base/bear/bull transcribed into "
                   "valuation.md")


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #

def run_coverage_qc(coverage_dir, mode="full"):
    """Run all coverage checks for `mode` ("full"|"shallow"); return result dicts."""
    return [
        check_artifacts_present(coverage_dir),
        check_manifest_shape(coverage_dir, mode),
        check_fsi_invoked(coverage_dir),
        check_subskills_invoked(coverage_dir, mode),
        check_research_depth(coverage_dir, mode),
        check_model_depth(coverage_dir, mode),
        check_valuation_depth(coverage_dir, mode),
        check_anchors_coherent(coverage_dir),
    ]


# --------------------------------------------------------------------------- #
# Waivers + CLI (mirrors report_qc.py / qc_gate.py exactly).
# --------------------------------------------------------------------------- #

def _parse_waivers(raw_waivers):
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
        description="Coverage QC gate (B1/B2, blocking): verify an FSI-initiation "
                    "coverage/ directory carries FULL initiation depth and a "
                    "provenance manifest. Exits 0 (pass) or 1 (fail).")
    parser.add_argument("--coverage", required=True,
                        help="the coverage/ directory (research/model/valuation + "
                             "anchors + manifest)")
    parser.add_argument("--mode", choices=("full", "shallow"), default="full",
                        help="depth mode; default full. 'shallow' is ONLY for an "
                             "explicit per-run user request (manifest depth_mode "
                             "must read 'shallow (user-requested)').")
    parser.add_argument("--waive", action="append", default=[],
                        metavar="check_name:reason",
                        help="waive a named check (repeatable)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.coverage):
        print(f"ERROR: coverage directory not found: {args.coverage}",
              file=sys.stderr)
        return 1

    waiver_reasons = _parse_waivers(args.waive)
    results = run_coverage_qc(args.coverage, mode=args.mode)
    results, unwaived = _apply_waivers(results, waiver_reasons)

    print(_render_table(results, set(waiver_reasons)))
    print()
    passed = unwaived == 0
    print("COVERAGE QC: " + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
