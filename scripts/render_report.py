"""Report renderer (L4 output layer) for the trading-desk plugin.

WHY THIS MODULE EXISTS: this is the FINAL output layer -- the 3-page trade decision
report. Its architecture kills LLM-number leakage BY CONSTRUCTION: this script
generates the ENTIRE report skeleton (every table, header, and number) from the
bundle's module JSONs. Every figure printed here is minted in Python from a module
output; the LLM layer fills ONLY the marked ``<!-- SLOT:... -->`` prose slots and
invents no numbers. ``scripts/report_qc.py`` then verifies the FINAL document
(after prose fill) numerically against the bundle, so a report can never ship with
a number that is not in the bundle.

Two modes:
- FULL (default): requires snapshot + module_{technical,risk,sentiment,fundamental,
  composite,tradeplan,options}.json. Any missing file -> exit 2 naming it
  (renormalized absences INSIDE modules are fine; the files must exist). Writes
  ``<TICKER>_Trade_Report_<date>.md``.
- DELTA (``--delta --previous <old_bundle>``): both bundles must have
  module_composite (+ whatever else exists). Writes
  ``<TICKER>_Delta_Report_<date>.md`` with composite/EV/level/structure
  deltas old-vs-new and a delta-interpretation slot.

Default output DIRECTORY (both modes): if the bundle dir's basename starts with
``detail_reports`` (the new ``trading_desk_<T>/detail_reports_<date>/`` layout),
the report is written to the bundle's PARENT dir; otherwise it is written inside
the bundle (legacy layout). ``--out`` overrides the path entirely.

stdlib-only; >=3.10 guard. The page/table builders are pure over parsed inputs;
the CLI is thin I/O.
"""

import argparse
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation: ensure the repo root is importable.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Required module files for a FULL report (each missing -> exit 2 naming it).
_REQUIRED_MODULES = [
    "module_technical.json",
    "module_risk.json",
    "module_sentiment.json",
    "module_fundamental.json",
    "module_composite.json",
    "module_tradeplan.json",
    "module_options.json",
]

_DISCLAIMER = ("_This report is for educational and research purposes only. It is "
               "not financial advice, not a recommendation, and not an offer or "
               "solicitation to buy or sell any security. Do your own research._")


# --------------------------------------------------------------------------- #
# Formatting helpers.
# --------------------------------------------------------------------------- #

def _fmt(x):
    """Compact number formatting (stable across runs; mirrors the scorers)."""
    if x is None:
        return "n/a"
    if isinstance(x, bool):
        return str(x)
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    if isinstance(x, float):
        return f"{x:g}"
    return str(x)


def _pct(frac, dp=1):
    """A fraction as a percent string (e.g. 0.085 -> '8.5%'), or 'n/a'."""
    if frac is None:
        return "n/a"
    return f"{frac * 100:.{dp}f}%"


def _read(x):
    """Score-band one-word read (scripted, never a prose slot)."""
    if x is None:
        return "n/a"
    if x >= 70:
        return "strong"
    if x >= 55:
        return "constructive"
    if x >= 45:
        return "mixed"
    return "weak"


def _days_between(a, b):
    """Calendar days from date string a to b (YYYY-MM-DD), or None."""
    from datetime import date
    def parse(s):
        if not isinstance(s, str) or len(s) < 10:
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    da, db = parse(a), parse(b)
    if da is None or db is None:
        return None
    return (db - da).days


def _plugin_version():
    """Read ../.claude-plugin/plugin.json version relative to this file; fallback."""
    path = os.path.join(_REPO_ROOT, ".claude-plugin", "plugin.json")
    try:
        with open(path) as fh:
            return json.load(fh).get("version", "unknown")
    except (OSError, ValueError):
        return "unknown"


# --------------------------------------------------------------------------- #
# Bundle I/O.
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest snapshot_*.json in the bundle, or None."""
    matches = glob.glob(os.path.join(bundle, "snapshot_*.json"))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _load_json(path):
    """Load a JSON file, or None if absent/unreadable."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_bundle(bundle):
    """Load snapshot + all present module JSONs. Returns a dict of parsed docs
    (missing modules map to None). ``snapshot`` may be None."""
    snap_path = _find_snapshot(bundle)
    out = {"snapshot": _load_json(snap_path) if snap_path else None}
    for name in _REQUIRED_MODULES:
        key = name[:-5]  # strip ".json"
        out[key] = _load_json(os.path.join(bundle, name))
    return out


# --------------------------------------------------------------------------- #
# Markdown table helper.
# --------------------------------------------------------------------------- #

def _table(headers, rows):
    """Render a GitHub-flavored markdown table. rows is a list of cell-lists."""
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join(["---"] * len(headers)) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# O11: Brief-transclusion helper.
# --------------------------------------------------------------------------- #

def _read_brief_span(bundle, dim, kind):
    """Read the STRIPPED text between ``<!-- {kind}:START -->`` and
    ``<!-- {kind}:END -->`` in ``<bundle>/brief_<dim>.md``.

    Returns the stripped span string, or ``None`` if:
    - the file is missing;
    - either marker is absent;
    - the extracted span is empty after stripping.

    ``kind`` is ``"BRIEF"`` or ``"SIGNAL"``.
    """
    if bundle is None:
        return None
    path = os.path.join(bundle, f"brief_{dim}.md")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return None
    start_marker = f"<!-- {kind}:START -->"
    end_marker = f"<!-- {kind}:END -->"
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return None
    end_idx = text.find(end_marker, start_idx + len(start_marker))
    if end_idx == -1:
        return None
    span = text[start_idx + len(start_marker):end_idx].strip()
    return span if span else None


# --------------------------------------------------------------------------- #
# Page 1 -- Decision.
# --------------------------------------------------------------------------- #

def build_header_block(snapshot):
    """Header table: ticker · last (+as-of) · mktcap · 52wk range · next event."""
    meta = snapshot.get("meta", {})
    price = snapshot.get("price", {})
    events = snapshot.get("events", {})
    ticker = meta.get("ticker", "UNKNOWN")
    as_of = meta.get("as_of_utc", "")
    last = price.get("last")
    mktcap = price.get("mktcap") or price.get("mktcap_computed")
    wk_hi, wk_lo = price.get("wk52_high"), price.get("wk52_low")

    ne = events.get("next_earnings") if isinstance(events, dict) else None
    ev_date = ne.get("date") if isinstance(ne, dict) else None
    as_of_date = as_of[:10] if isinstance(as_of, str) else None
    days = _days_between(as_of_date, ev_date)
    event_cell = "n/a"
    if ev_date is not None:
        event_cell = ev_date + (f" ({days}d)" if days is not None else "")

    rows = [
        [f"**{ticker}**",
         f"last {_fmt(last)} (as of {as_of_date})",
         f"mktcap {_fmt(mktcap)}",
         f"52wk {_fmt(wk_lo)}–{_fmt(wk_hi)}",
         f"next event {event_cell}"],
    ]
    return _table(["Ticker", "Last", "Market Cap", "52-Week Range", "Next Binary Event"],
                  rows)


def build_the_call(composite):
    """The call line + a tension slot.

    Appends the composite confidence roll-up badge when the composite carries a
    ``confidence`` block.  Badge text is word-only (digit-free) to pass QC.
    """
    grade = composite.get("grade", "?")
    action = composite.get("action", "?")
    score = composite.get("score")
    profile = composite.get("profile", "?")
    line = (f"**{grade} — {action}** (composite {_fmt(score)}/100, "
            f"{profile} profile)")
    # Roll-up confidence badge (word-only).
    conf = composite.get("confidence") if isinstance(composite, dict) else None
    if isinstance(conf, dict) and conf.get("level"):
        glyph = _confidence_glyph(conf.get("level"))
        # The composite rollup block carries a top-level 'why' (not per-axis).
        why = conf.get("why", "")
        if why:
            line += f" · Confidence: {glyph} ({why})"
        else:
            line += f" · Confidence: {glyph}"
    return line + "\n\n<!-- SLOT:tension -->"


def build_composite_table(composite):
    """Composite table: per-dimension rows (score/weight/contribution/read) +
    composite row + sensitivity row (all three profiles; bold when grades differ)."""
    dims = composite.get("dimensions", [])
    rows = []
    for d in dims:
        rows.append([d.get("name"), _fmt(d.get("score")), _fmt(d.get("weight")),
                     _fmt(d.get("contribution")), _read(d.get("score"))])
    rows.append(["**composite**", f"**{_fmt(composite.get('score'))}**", "**1.0**",
                 f"**{_fmt(composite.get('score'))}**",
                 f"**{_read(composite.get('score'))}**"])

    table = _table(["Dimension", "Score", "Weight", "Contribution", "Read"], rows)

    sens = composite.get("sensitivity", {}) or {}
    # sensitivity carries the three profile dicts PLUS a "weight_set" string label
    # (score_composite stamps it). Iterate only the dict-valued profile entries —
    # calling .get() on the weight_set string would crash (real-data E2E finding).
    grades = {p: v.get("grade") for p, v in sens.items() if isinstance(v, dict)}
    differ = len(set(g for g in grades.values() if g is not None)) > 1
    cells = []
    for p in ("trader", "balanced", "long-term"):
        s = sens.get(p) or {}
        cell = f"{p}: {_fmt(s.get('score'))}/{s.get('grade', '?')}"
        cells.append(f"**{cell}**" if differ else cell)
    sens_line = "**Sensitivity** (per profile): " + " · ".join(cells)
    if differ:
        sens_line += "  _(grades differ across profiles)_"
    return table + "\n\n" + sens_line


def build_tradeplan_table(tradeplan):
    """Trade plan table: don't-chase, entries, exits, invalidation, size, hedge,
    expression."""
    sp = tradeplan.get("stock_plan", {}) or {}
    expr = tradeplan.get("expression", {}) or {}
    rows = []

    dc = sp.get("dont_chase", {}) or {}
    rows.append(["Don't-chase", f"above {_fmt(dc.get('above'))} ({dc.get('convention', '')})"])

    for i, e in enumerate(sp.get("entries", []) or [], start=1):
        tag = " (sized down)" if e.get("sized_down") else ""
        rows.append([f"Entry {i}",
                     f"{_fmt(e.get('level'))} — {e.get('condition', '')}; "
                     f"EV-at-level {_fmt(e.get('ev_at_level'))}{tag}"])

    exits = sp.get("exits", {}) or {}
    pt = exits.get("profit_take") or {}
    if pt:
        rows.append(["Profit-take", f"{_fmt(pt.get('level'))} ({pt.get('type', '')})"])
    bt = exits.get("bull_target") or {}
    if bt:
        note = f" ({bt.get('note')})" if bt.get("note") else ""
        rows.append(["Bull target", f"{_fmt(bt.get('level'))}{note}"])

    inv = sp.get("invalidation", {}) or {}
    tl = inv.get("technical_leg") or {}
    fl = inv.get("fundamental_leg") or {}
    rows.append(["Invalidation (technical)",
                 f"{tl.get('condition', '')} {_fmt(tl.get('level'))}"])
    rows.append(["Invalidation (fundamental)",
                 f"{fl.get('metric', '')} {fl.get('threshold', '')}"])

    sz = sp.get("sizing", {}) or {}
    rows.append(["Size",
                 f"recommended {_pct(sz.get('recommended_pct'))}, "
                 f"cap {_pct(sz.get('cap_pct'))}, f* {_pct(sz.get('f_star'))}"])

    hedge = sp.get("hedge", {}) or {}
    if hedge.get("required"):
        strikes = hedge.get("strikes_from") or []
        strike_txt = ", ".join(_fmt(s) for s in strikes)
        rows.append(["Hedge",
                     f"required — {hedge.get('trigger', '')}; "
                     f"{hedge.get('structure', '')} from {strike_txt}"])
    else:
        rows.append(["Hedge", "not required"])

    expr_cell = expr.get("recommended_for_profile", "")
    extras = []
    if expr.get("selector_fired"):
        extras.append(f"selector: {expr['selector_fired']}")
    if expr.get("executable") is False:
        extras.append(f"NOT executable — {expr.get('executability_note', '')}")
    if extras:
        expr_cell += " (" + "; ".join(extras) + ")"
    rows.append(["**Expression**", expr_cell])

    return _table(["Plan Row", "Value"], rows)


def build_event_playbook(snapshot, tradeplan):
    """Event playbook box: scripted skeleton (event date + implied move) + slot."""
    events = snapshot.get("events", {}) or {}
    sentiment = snapshot.get("sentiment", {}) or {}
    ne = events.get("next_earnings") if isinstance(events, dict) else None
    ev_date = ne.get("date") if isinstance(ne, dict) else None
    implied = sentiment.get("implied_move_next_earnings_pct")
    parts = ["**Event playbook**"]
    line = f"- Next event: {ev_date or 'n/a'}"
    if implied is not None:
        line += f"; implied move ±{_pct(implied)}"
    parts.append(line)
    parts.append("<!-- SLOT:event_playbook -->")
    return "\n".join(parts)


def build_page1(snapshot, composite, tradeplan):
    parts = [
        "## Page 1 — Decision",
        "",
        build_header_block(snapshot),
        "",
        "### The Call",
        "",
        build_the_call(composite),
        "",
        "### Composite",
        "",
        build_composite_table(composite),
        "",
        "### Trade Plan",
        "",
        build_tradeplan_table(tradeplan),
        "",
        build_event_playbook(snapshot, tradeplan),
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Page 2 -- Evidence.
# --------------------------------------------------------------------------- #

def _confidence_glyph(level):
    """Unicode glyph + level text for a confidence level string (digit-free)."""
    return {"HIGH": "● HIGH", "MEDIUM": "◐ MEDIUM", "LOW": "○ LOW"}.get(
        str(level).upper(), "○ UNKNOWN")


def _confidence_badge(module):
    """Per-dimension confidence badge string from module.get('confidence').

    Returns a space-prefixed '· <glyph> <LEVEL> (<why>)' string, or '' when
    the confidence block is absent (graceful for older bundles). The why tag is
    the weakest-axis why from the block (already digit-free by confidence.py contract).
    """
    if not isinstance(module, dict):
        return ""
    conf = module.get("confidence")
    if not isinstance(conf, dict):
        return ""
    level = conf.get("level")
    if level is None:
        return ""
    # The weakest-axis why: the axis whose level == the module level.
    why = None
    for axis in ("source", "depth", "staleness"):
        ax = conf.get(axis)
        if isinstance(ax, dict) and ax.get("level") == level:
            why = ax.get("why")
            break
    glyph = _confidence_glyph(level)
    if why:
        return f" · {glyph} ({why})"
    return f" · {glyph}"


def _score_headline(label, module, slot_suffix):
    """A scripted score-headline line for an evidence dimension.

    Appends a per-dimension confidence badge when the module carries a
    ``confidence`` block (graceful for older bundles without one).
    Badge text is word-only (digit-free) so report_qc number_provenance passes.
    """
    score = module.get("score") if isinstance(module, dict) else None
    ver = module.get("rubric_version", "?") if isinstance(module, dict) else "?"
    badge = _confidence_badge(module)
    return f"### {label} — {_fmt(score)}/100 (rubric v{ver}){badge}"


def build_technical_evidence(technical, bundle=None):
    ladder = technical.get("ladder", []) or []
    below = sorted([e for e in ladder if e.get("pct_from_last") is not None
                    and e["pct_from_last"] < 0],
                   key=lambda e: e["level"], reverse=True)[:3]
    above = sorted([e for e in ladder if e.get("pct_from_last") is not None
                    and e["pct_from_last"] >= 0],
                   key=lambda e: e["level"])[:3]
    rows = []
    for e in below:
        rows.append(["support", _fmt(e.get("level")), e.get("type", ""),
                     _pct(e.get("pct_from_last"))])
    for e in above:
        rows.append(["resistance", _fmt(e.get("level")), e.get("type", ""),
                     _pct(e.get("pct_from_last"))])
    table = _table(["Side", "Level", "Type", "% from last"], rows)
    brief_span = _read_brief_span(bundle, "technical", "BRIEF")
    signal_span = _read_brief_span(bundle, "technical", "SIGNAL")
    return "\n".join([
        _score_headline("Technical", technical, "technical"),
        "",
        brief_span if brief_span is not None else "<!-- SLOT:brief_technical -->",
        "",
        table,
        "",
        signal_span if signal_span is not None else "<!-- SLOT:signal_technical -->",
    ])


def build_fundamental_evidence(fundamental, bundle=None):
    subs = fundamental.get("subscores", []) or []
    rows = []
    for s in subs:
        rows.append([s.get("name", ""), _fmt(s.get("points")), _fmt(s.get("max"))])
    table = _table(["Sub-dimension", "Points", "Max"], rows)
    brief_span = _read_brief_span(bundle, "fundamental", "BRIEF")
    signal_span = _read_brief_span(bundle, "fundamental", "SIGNAL")
    return "\n".join([
        _score_headline("Fundamental", fundamental, "fundamental"),
        "",
        brief_span if brief_span is not None else "<!-- SLOT:brief_fundamental -->",
        "",
        table,
        "",
        signal_span if signal_span is not None else "<!-- SLOT:signal_fundamental -->",
    ])


def build_sentiment_evidence(sentiment_mod, bundle=None):
    tables = sentiment_mod.get("tables", {}) or {}
    pos = tables.get("positioning", {}) or {}
    rows = [
        ["short interest %", _fmt(pos.get("short_interest_pct"))],
        ["put/call (full chain)", _fmt(pos.get("put_call_ratio_full_chain"))],
        ["IV percentile (1yr)", _fmt(pos.get("iv_pctile_1yr"))],
    ]
    table = _table(["Positioning", "Value"], rows)
    brief_span = _read_brief_span(bundle, "sentiment", "BRIEF")
    signal_span = _read_brief_span(bundle, "sentiment", "SIGNAL")
    return "\n".join([
        _score_headline("Sentiment / Positioning", sentiment_mod, "sentiment"),
        "",
        brief_span if brief_span is not None else "<!-- SLOT:brief_sentiment -->",
        "",
        table,
        "",
        signal_span if signal_span is not None else "<!-- SLOT:signal_sentiment -->",
    ])


def build_risk_evidence(risk, bundle=None):
    dm = ((risk.get("tables", {}) or {}).get("downside_map") or [])[:5]
    rows = []
    for r in dm:
        rows.append([_fmt(r.get("level")), r.get("type", ""),
                     _pct(r.get("pct_from_last"))])
    table = _table(["Level", "Type", "% from last"], rows)
    brief_span = _read_brief_span(bundle, "risk", "BRIEF")
    signal_span = _read_brief_span(bundle, "risk", "SIGNAL")
    return "\n".join([
        _score_headline("Risk", risk, "risk"),
        "",
        brief_span if brief_span is not None else "<!-- SLOT:brief_risk -->",
        "",
        table,
        "",
        signal_span if signal_span is not None else "<!-- SLOT:signal_risk -->",
    ])


def build_thesis_evidence(composite, bundle=None):
    ev = composite.get("ev", {}) or {}
    scenarios = ev.get("scenarios", []) or []
    rows = []
    for sc in scenarios:
        rows.append([sc.get("name", ""), _fmt(sc.get("prob")),
                     _fmt(sc.get("price_target"))])
    table = _table(["Scenario", "Probability", "Price Target"], rows)
    ev_line = (f"EV at current: {_fmt(ev.get('ev_at_current'))} · "
               f"hurdle {_fmt(ev.get('hurdle_total'))} · "
               f"EV-breakeven entry {_fmt(ev.get('ev_breakeven_entry'))}")
    # The composite-score skill writes brief_composite.md (not brief_thesis.md);
    # its part-2 BRIEF span is the tension sentence that belongs here.
    # No SIGNAL marker exists in brief_composite.md, so the signal slot falls
    # back to the open mark unconditionally.
    brief_span = _read_brief_span(bundle, "composite", "BRIEF")
    return "\n".join([
        _score_headline("Thesis / EV", composite, "thesis").replace(
            "/100 (rubric", " conviction (rubric"),
        "",
        brief_span if brief_span is not None else "<!-- SLOT:brief_thesis -->",
        "",
        table,
        "",
        ev_line,
        "",
        "<!-- SLOT:signal_thesis -->",
    ])


def build_page2(technical, fundamental, sentiment_mod, risk, composite,
                bundle=None):
    parts = [
        "## Page 2 — Evidence",
        "",
        build_technical_evidence(technical, bundle=bundle),
        "",
        build_fundamental_evidence(fundamental, bundle=bundle),
        "",
        build_sentiment_evidence(sentiment_mod, bundle=bundle),
        "",
        build_risk_evidence(risk, bundle=bundle),
        "",
        build_thesis_evidence(composite, bundle=bundle),
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Page 3 -- Context & Protocol.
# --------------------------------------------------------------------------- #

def build_sr_and_downside(technical, risk):
    ladder = technical.get("ladder", []) or []
    ladder_rows = []
    for e in sorted(ladder, key=lambda e: e.get("level", 0), reverse=True):
        basis = e.get("basis", "")
        ladder_rows.append([_fmt(e.get("level")), e.get("type", ""), basis,
                            _pct(e.get("pct_from_last"))])
    ladder_tbl = _table(["Level", "Type", "Basis", "% from last"], ladder_rows)

    dm = (risk.get("tables", {}) or {}).get("downside_map") or []
    dm_rows = []
    for r in dm:
        basis = r.get("basis", "")
        method = f" ({r.get('method')})" if r.get("method") else ""
        dm_rows.append([_fmt(r.get("level")), r.get("type", ""),
                        basis + method, _pct(r.get("pct_from_last"))])
    dm_tbl = _table(["Level", "Type", "Basis", "% from last"], dm_rows)

    return "\n".join(["### Support / Resistance Ladder", "", ladder_tbl, "",
                      "### Downside Map", "", dm_tbl])


def build_catalyst_calendar(snapshot):
    # QF4: compute as_of date from snapshot.meta so we can label past rows.
    meta = snapshot.get("meta", {}) or {}
    as_of_utc = meta.get("as_of_utc") or ""
    as_of_date = as_of_utc[:10] if isinstance(as_of_utc, str) and len(as_of_utc) >= 10 else ""

    events = snapshot.get("events", {}) or {}
    rows = []
    ne = events.get("next_earnings") if isinstance(events, dict) else None
    if isinstance(ne, dict) and ne.get("date"):
        cons = ne.get("consensus_eps")
        note = f"consensus EPS {_fmt(cons)}" if cons is not None else ""
        # QF4: empty note -> em-dash; past event -> append " (past)".
        note = note or "—"
        if as_of_date and ne.get("date", "") < as_of_date:
            note += " (past)"
        rows.append(["next earnings", ne.get("date"), note])
    for c in events.get("catalysts", []) or []:
        if isinstance(c, dict):
            note = c.get("note", "") or "—"
            if as_of_date and (c.get("date") or "") < as_of_date:
                note += " (past)"
            rows.append([c.get("name", "catalyst"), c.get("date", ""), note])
    if not rows:
        rows.append(["—", "—", "no scheduled catalysts"])
    tbl = _table(["Catalyst", "Date", "Note"], rows)
    return "\n".join(["### Catalyst Calendar", "", tbl, "",
                      "<!-- SLOT:catalyst_notes -->"])


def build_scenario_ev(composite):
    ev = composite.get("ev", {}) or {}
    scenarios = ev.get("scenarios", []) or []
    rows = []
    for sc in scenarios:
        rows.append([sc.get("name", ""), _fmt(sc.get("prob")),
                     _fmt(sc.get("price_target"))])
    tbl = _table(["Scenario", "Probability", "Price Target"], rows)
    ev_line = (f"EV at current {_fmt(ev.get('ev_at_current'))} · "
               f"EV-breakeven entry {_fmt(ev.get('ev_breakeven_entry'))} · "
               f"hurdle {_fmt(ev.get('hurdle_total'))}")
    return "\n".join(["### Scenario & Expected Value", "", tbl, "", ev_line])


def build_options_expression(options, tradeplan):
    vd = options.get("vol_dashboard", {}) or {}
    verdict = vd.get("verdict", "unknown")
    verdict_line = (f"**Vol dashboard**: IV-vs-realized verdict is *{verdict}* "
                    f"(iv30 {_fmt(vd.get('iv30'))}, rv20 {_fmt(vd.get('rv20'))}, "
                    f"diff {_fmt(vd.get('diff'))}, IV pctile {_fmt(vd.get('iv_pctile_1yr'))}).")

    rec = options.get("recommended_structures", []) or []
    rec_rows = []
    for st in rec:
        legs = "; ".join(f"{lg.get('side')} {lg.get('type')} {_fmt(lg.get('strike'))}"
                         for lg in st.get("legs", []))
        net = st.get("net_credit")
        net_txt = (f"credit {_fmt(net)}" if net is not None
                   else f"debit {_fmt(st.get('net_debit'))}")
        rec_rows.append([st.get("name", ""), legs, net_txt,
                         _fmt(st.get("max_loss")),
                         "; ".join(_fmt(b) for b in st.get("breakevens", [])),
                         f"{_fmt(st.get('pop'))} ({st.get('pop_method', '')})"])
    rec_tbl = (_table(["Structure", "Legs", "Net", "Max Loss", "Breakevens",
                       "PoP (method)"], rec_rows)
               if rec_rows else "_No structures recommended._")

    declined = options.get("declined", []) or []
    dec_rows = [[d.get("name", ""), d.get("reason", "")] for d in declined]
    dec_tbl = (_table(["Declined", "Reason"], dec_rows)
               if dec_rows else "_No structures declined._")

    hedge = options.get("hedge_structure")
    hedge_block = ""
    if isinstance(hedge, dict):
        legs = "; ".join(f"{lg.get('side')} {lg.get('type')} {_fmt(lg.get('strike'))}"
                         for lg in hedge.get("legs", []))
        hedge_block = (f"\n\n**Hedge structure**: {hedge.get('type', '')} "
                       f"[{legs}], cost {_fmt(hedge.get('cost'))}.")

    expr = tradeplan.get("expression", {}) or {}
    mpp = expr.get("mode_per_profile", {}) or {}
    matrix_rows = [[p, mpp.get(p, "")]
                   for p in ("trader", "balanced", "long-term")]
    matrix_tbl = _table(["Profile", "Expression"], matrix_rows)

    return "\n".join([
        "### Options Expression",
        "",
        verdict_line,
        "",
        "**Recommended structures**",
        "",
        rec_tbl,
        "",
        "**Declined structures**",
        "",
        dec_tbl + hedge_block,
        "",
        "**Expression matrix (all profiles)**",
        "",
        matrix_tbl,
    ])


def build_monitoring(tradeplan):
    sp = tradeplan.get("stock_plan", {}) or {}
    inv = sp.get("invalidation", {}) or {}
    tl = inv.get("technical_leg") or {}
    fl = inv.get("fundamental_leg") or {}
    alerts = [
        f"- Technical alert: {tl.get('condition', '')} {_fmt(tl.get('level'))}",
        f"- Fundamental alert: {fl.get('metric', '')} {fl.get('threshold', '')}",
        "- Review cadence: reassess on the next earnings print or a both-leg breach.",
    ]
    return "\n".join(["### Monitoring Protocol", ""] + alerts
                     + ["", "<!-- SLOT:monitoring_notes -->"])


def build_integrity_footer(snapshot, modules):
    meta = snapshot.get("meta", {}) or {}
    as_of = meta.get("as_of_utc", "unknown")
    qc = meta.get("qc", {}) or {}
    attest = qc.get("attestation") or (
        "QC passed" if qc.get("passed") else "QC status unknown")

    src_bits = []
    for src in meta.get("sources", []) or []:
        grp = src.get("field_group", "?")
        ret = src.get("retrieved_utc", "?")
        src_bits.append(f"{grp} @ {ret}")
    src_line = "; ".join(src_bits) if src_bits else "no sources listed"

    missing = meta.get("missing", []) or []
    missing_line = (", ".join(missing) if missing else "none")

    api_notes = meta.get("api_tier_notes", []) or []
    api_line = "; ".join(str(n) for n in api_notes) if api_notes else "none"

    # rubric versions per module + expression rule version + schema version.
    ver_bits = []
    for key in ("module_technical", "module_risk", "module_sentiment",
                "module_fundamental", "module_composite", "module_tradeplan",
                "module_options"):
        m = modules.get(key)
        if isinstance(m, dict) and m.get("rubric_version"):
            ver_bits.append(f"{key.replace('module_', '')} v{m['rubric_version']}")
    tp = modules.get("module_tradeplan") or {}
    expr_ver = (tp.get("expression", {}) or {}).get("rule_version")
    if expr_ver:
        ver_bits.append(f"expression {expr_ver}")
    schema = meta.get("schema_version")
    if schema:
        ver_bits.append(f"snapshot schema {schema}")
    # confidence-version travels in the footer (rubric-version-travels contract).
    # Read from any module's confidence block; fall back to the module constant.
    conf_ver = None
    for key in ("module_technical", "module_risk", "module_sentiment",
                "module_fundamental", "module_composite"):
        m = modules.get(key)
        if isinstance(m, dict):
            c = m.get("confidence")
            if isinstance(c, dict) and c.get("version"):
                conf_ver = c["version"]
                break
    if conf_ver is None:
        try:
            from scripts import confidence as _conf_mod
            conf_ver = _conf_mod.CONFIDENCE_VERSION
        except ImportError:
            pass
    if conf_ver:
        ver_bits.append(f"confidence-v{conf_ver}")
    ver_line = "; ".join(ver_bits)

    # Provisional disclosures: surface each module's PROVISIONAL note verbatim so a
    # reader sees that a v1.1.0 rubric is UNRATIFIED, not just its version number
    # (code-review fix — the notes were stamped into the module JSONs but never
    # rendered). The only numeric token in these notes is the rubric version, which
    # already travels in the Rubric-versions line above.
    prov_bits = []
    for key in ("module_technical", "module_risk", "module_sentiment",
                "module_composite", "module_tradeplan"):
        m = modules.get(key)
        if not isinstance(m, dict):
            continue
        note = m.get("module_note") or m.get("note")
        if note and "PROVISIONAL" in str(note).upper():
            prov_bits.append(str(note))
    prov_line = " · ".join(prov_bits) if prov_bits else "none"

    plugin_ver = _plugin_version()

    lines = [
        "### Data Integrity",
        "",
        f"- Snapshot as of: {as_of}",
        f"- Sources (field group @ retrieved): {src_line}",
        f"- QC attestation: {attest}",
        f"- API tier notes: {api_line}",
        f"- Staleness / missing disclosures: {missing_line}",
        f"- Rubric versions: {ver_line}",
        f"- Provisional disclosures: {prov_line}",
        f"- Plugin version: {plugin_ver}",
        "",
        _DISCLAIMER,
    ]
    return "\n".join(lines)


def build_page3(snapshot, technical, risk, composite, options, tradeplan, modules):
    parts = [
        "## Page 3 — Context & Protocol",
        "",
        build_sr_and_downside(technical, risk),
        "",
        build_catalyst_calendar(snapshot),
        "",
        build_scenario_ev(composite),
        "",
        build_options_expression(options, tradeplan),
        "",
        build_monitoring(tradeplan),
        "",
        build_integrity_footer(snapshot, modules),
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Full report assembly.
# --------------------------------------------------------------------------- #

def build_full_report(bundle_docs, bundle=None):
    snapshot = bundle_docs["snapshot"]
    ticker = (snapshot.get("meta", {}) or {}).get("ticker", "UNKNOWN")
    as_of_date = (snapshot.get("meta", {}) or {}).get("as_of_utc", "")[:10]

    page1 = build_page1(snapshot, bundle_docs["module_composite"],
                        bundle_docs["module_tradeplan"])
    page2 = build_page2(bundle_docs["module_technical"],
                        bundle_docs["module_fundamental"],
                        bundle_docs["module_sentiment"],
                        bundle_docs["module_risk"],
                        bundle_docs["module_composite"],
                        bundle=bundle)
    page3 = build_page3(snapshot, bundle_docs["module_technical"],
                        bundle_docs["module_risk"], bundle_docs["module_composite"],
                        bundle_docs["module_options"],
                        bundle_docs["module_tradeplan"], bundle_docs)

    title = f"# {ticker} — Trade Report ({as_of_date})"
    return "\n\n".join([title, page1, page2, page3]) + "\n"


# --------------------------------------------------------------------------- #
# Delta report.
# --------------------------------------------------------------------------- #

def _absent_line(section, which):
    return f"_{section}: n/a (module absent in {which})._"


def build_delta_composite_table(old_comp, new_comp):
    if old_comp is None or new_comp is None:
        which = "previous" if old_comp is None else "current"
        return _absent_line("Composite delta", which)
    old_dims = {d.get("name"): d for d in (old_comp.get("dimensions") or [])}
    new_dims = {d.get("name"): d for d in (new_comp.get("dimensions") or [])}
    names = list(dict.fromkeys(list(old_dims) + list(new_dims)))
    rows = []
    for name in names:
        o = old_dims.get(name, {}).get("score")
        n = new_dims.get(name, {}).get("score")
        delta = (round(n - o, 4) if (o is not None and n is not None) else None)
        rows.append([name, _fmt(o), _fmt(n), _fmt(delta)])
    # composite row.
    o_s, n_s = old_comp.get("score"), new_comp.get("score")
    d_s = round(n_s - o_s, 4) if (o_s is not None and n_s is not None) else None
    rows.append(["**composite**", f"**{_fmt(o_s)}**", f"**{_fmt(n_s)}**",
                 f"**{_fmt(d_s)}**"])
    tbl = _table(["Dimension", "Old", "New", "Δ"], rows)

    og, ng = old_comp.get("grade"), new_comp.get("grade")
    grade_line = f"Grade: {og} → {ng}"
    if og != ng:
        grade_line = f"**Grade: {og} → {ng}**"
    return tbl + "\n\n" + grade_line


def build_delta_ev(old_comp, new_comp):
    if old_comp is None or new_comp is None:
        which = "previous" if old_comp is None else "current"
        return _absent_line("EV delta", which)
    oe = old_comp.get("ev", {}) or {}
    ne = new_comp.get("ev", {}) or {}
    rows = [
        ["ev_at_current", _fmt(oe.get("ev_at_current")), _fmt(ne.get("ev_at_current"))],
        ["ev_breakeven_entry", _fmt(oe.get("ev_breakeven_entry")),
         _fmt(ne.get("ev_breakeven_entry"))],
    ]
    return _table(["EV Metric", "Old", "New"], rows)


def _level_map(tradeplan):
    """Extract named levels from a tradeplan for the delta level table."""
    out = {}
    if not isinstance(tradeplan, dict):
        return out
    sp = tradeplan.get("stock_plan", {}) or {}
    for i, e in enumerate(sp.get("entries", []) or [], start=1):
        out[f"entry_{i}"] = e.get("level")
    inv = sp.get("invalidation", {}) or {}
    out["invalidation_technical"] = (inv.get("technical_leg") or {}).get("level")
    hedge = sp.get("hedge", {}) or {}
    sf = hedge.get("strikes_from") or []
    for i, s in enumerate(sf, start=1):
        out[f"hedge_strike_{i}"] = s
    return out


def build_delta_levels(old_tp, new_tp):
    if old_tp is None or new_tp is None:
        which = "previous" if old_tp is None else "current"
        return _absent_line("Level changes", which)
    old_m = _level_map(old_tp)
    new_m = _level_map(new_tp)
    names = list(dict.fromkeys(list(old_m) + list(new_m)))
    rows = [[name, _fmt(old_m.get(name)), _fmt(new_m.get(name))] for name in names]
    return _table(["Level", "Old", "New"], rows)


def _structure_names(tradeplan):
    if not isinstance(tradeplan, dict):
        return set()
    expr = tradeplan.get("expression", {}) or {}
    return {s.get("name") for s in (expr.get("structures_selected") or [])
            if s.get("name")}


def build_delta_structures(old_tp, new_tp):
    if old_tp is None or new_tp is None:
        which = "previous" if old_tp is None else "current"
        return _absent_line("Structure changes", which)
    old_s = _structure_names(old_tp)
    new_s = _structure_names(new_tp)
    added = sorted(new_s - old_s)
    removed = sorted(old_s - new_s)
    lines = [
        f"- Added: {', '.join(added) if added else 'none'}",
        f"- Removed: {', '.join(removed) if removed else 'none'}",
    ]
    return "\n".join(lines)


def build_delta_report(old_docs, new_docs):
    snapshot = new_docs["snapshot"]
    old_snap = old_docs["snapshot"]
    ticker = (snapshot.get("meta", {}) or {}).get("ticker", "UNKNOWN")
    new_as_of = (snapshot.get("meta", {}) or {}).get("as_of_utc", "")
    old_as_of = (old_snap.get("meta", {}) or {}).get("as_of_utc", "") if old_snap else "?"
    as_of_date = new_as_of[:10]

    title = f"# {ticker} — Delta Report ({as_of_date})"
    header = f"**Comparison**: {old_as_of} → {new_as_of}"

    body = [
        title,
        "",
        header,
        "",
        "## Composite Delta",
        "",
        build_delta_composite_table(old_docs["module_composite"],
                                    new_docs["module_composite"]),
        "",
        "## Expected-Value Delta",
        "",
        build_delta_ev(old_docs["module_composite"], new_docs["module_composite"]),
        "",
        "## Level Changes",
        "",
        build_delta_levels(old_docs["module_tradeplan"],
                           new_docs["module_tradeplan"]),
        "",
        "## Structure Changes",
        "",
        build_delta_structures(old_docs["module_tradeplan"],
                               new_docs["module_tradeplan"]),
        "",
        "## Interpretation",
        "",
        "<!-- SLOT:delta_interpretation -->",
        "",
        "## Data Integrity",
        "",
        build_integrity_footer(snapshot, new_docs),
    ]
    return "\n".join(body) + "\n"


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #

def _require_full_modules(bundle):
    """Return the first missing required file name, or None if all present."""
    if _find_snapshot(bundle) is None:
        return "snapshot_*.json"
    for name in _REQUIRED_MODULES:
        if not os.path.isfile(os.path.join(bundle, name)):
            return name
    return None


def _output_dir(bundle):
    """The directory a default-named report is written to.

    New layout ``trading_desk_<T>/detail_reports_<date>/``: when the bundle dir's
    basename starts with ``detail_reports`` the report is a sibling of the bundle,
    so it lands in the bundle's PARENT directory (next to the data folder, not
    buried inside it). Legacy bundle names keep the report inside the bundle. An
    explicit ``--out`` bypasses this entirely (handled by the caller).
    """
    base = os.path.basename(os.path.normpath(bundle))
    if base.startswith("detail_reports"):
        return os.path.dirname(os.path.normpath(bundle))
    return bundle


def _default_out(bundle, snapshot, delta=False):
    meta = snapshot.get("meta", {}) or {}
    ticker = meta.get("ticker", "UNKNOWN")
    date = (meta.get("as_of_utc", "") or "")[:10] or "undated"
    kind = "Delta_Report" if delta else "Trade_Report"
    return os.path.join(_output_dir(bundle), f"{ticker}_{kind}_{date}.md")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render the 3-page trade decision report (or a delta report) "
                    "entirely from a bundle's module JSONs. Every number is "
                    "script-written; LLM prose fills only <!-- SLOT:... --> marks.")
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--out", default=None, help="output path (default derived)")
    parser.add_argument("--delta", action="store_true",
                        help="render a delta report vs --previous")
    parser.add_argument("--previous", default=None,
                        help="the older bundle directory (required with --delta)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 2

    if args.delta:
        if not args.previous:
            print("ERROR: --previous <old_bundle> is required with --delta",
                  file=sys.stderr)
            return 2
        if not os.path.isdir(args.previous):
            print(f"ERROR: previous bundle not found: {args.previous}",
                  file=sys.stderr)
            return 2
        new_docs = load_bundle(args.bundle)
        old_docs = load_bundle(args.previous)
        if new_docs["snapshot"] is None:
            print(f"ERROR: no snapshot_*.json in {args.bundle}", file=sys.stderr)
            return 2
        # Delta requires module_composite in both bundles.
        if new_docs["module_composite"] is None:
            print("ERROR: module_composite.json missing in the current bundle "
                  "(delta requires composite in both bundles).", file=sys.stderr)
            return 2
        if old_docs["module_composite"] is None:
            print("ERROR: module_composite.json missing in the previous bundle "
                  "(delta requires composite in both bundles).", file=sys.stderr)
            return 2
        report = build_delta_report(old_docs, new_docs)
        out = args.out or _default_out(args.bundle, new_docs["snapshot"], delta=True)
        with open(out, "w") as fh:
            fh.write(report)
        print(out)
        return 0

    # -- full report -------------------------------------------------------
    missing = _require_full_modules(args.bundle)
    if missing is not None:
        print(f"ERROR: required file missing: {missing} "
              f"(run the upstream skills first).", file=sys.stderr)
        return 2

    docs = load_bundle(args.bundle)
    if docs["snapshot"] is None:
        print(f"ERROR: no readable snapshot in {args.bundle}", file=sys.stderr)
        return 2

    report = build_full_report(docs, bundle=args.bundle)
    out = args.out or _default_out(args.bundle, docs["snapshot"], delta=False)
    with open(out, "w") as fh:
        fh.write(report)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
