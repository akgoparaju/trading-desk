"""Docket renderer (exec 2pp / detail ~10-15pp / delta 1pp) for the trading-desk.

WHY THIS MODULE EXISTS: the docket is the institutional PDF render of an
already-QC'd bundle. It carries TWO hard invariants, both enforced here:

  * ZERO LLM ARITHMETIC. Every number on the page is script-minted -- from the
    bundle's module JSONs (tables), the deterministic chart pack (charts/*.png),
    or the ``diff_bundles`` What-Changed computation. The only LLM content is the
    prose in ``pdf_slots.json`` (thesis bullets, desk read, positioning, delta
    interpretation), and even that is number-provenance-gated BEFORE it reaches
    this renderer.

  * THE SLOTS GATE CANNOT BE BYPASSED. ``report_qc.py --pdf-slots`` stamps
    ``{"qc_passed": true}`` into pdf_slots.json only after its prose passes
    number_provenance. ``render_pdf`` REFUSES to render exec/detail unless that
    stamp is present (exit 2 with the fix command). An un-gated slots file can
    never be embedded.

DESIGN CONSTRAINT (stdlib-first): reportlab + matplotlib are OPTIONAL,
venv-bootstrapped deps. This module and its PURE functions (``diff_bundles``,
``slots_gate_ok``, the bundle/slot loaders) import cleanly WITHOUT them; only the
draw path imports reportlab, LAZILY, raising a clear exit-3 message (pointing at
``render_env.py``) when it is absent. The base ``unittest`` suite runs sans-deps.

The layout components (grade box, key-stats sidebar, what-changed table, plan
table, timeline band, two-column note grid) are mined from the mockup
(docs/mockups/exec_mockup.py) and adapted into reusable functions on a shared
``Canvas``-wrapping ``Doc`` helper. The spec-§8 nits are fixed: labeled callouts
and collision-free timelines live in the chart pack (R2); here the estimates
double-caption, page-2 whitespace, and score-panel weight ticks are handled by
using the R2 charts and the single-caption sidebar.

Outputs land in the bundle PARENT under the detail_reports rule (mirrors
render_report._output_dir): ``<T>_Trade_Report_<date>.pdf`` / ``<T>_Detail_<date>.pdf``
/ ``<T>_Delta_Note_<date>.pdf``. Every page carries the integrity footer.

CLI: ``render_pdf.py --bundle <dir> --doc exec|detail|delta [--previous <bundle>]
[--out <path>]``.

stdlib-only at import; reportlab/matplotlib only inside the draw path; >=3.10 guard.
"""

import argparse
import glob
import json
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)"
             % sys.version_info[:2])

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import render_report, tdstyle
# score_composite / score_fundamental are stdlib-only PURE modules (no reportlab/
# matplotlib): the METHODOLOGY page PINS its convention constants (weights, horizon
# years, grade bands, the anchored/snapshot component maxima) by IMPORTING them from
# the rubric of record, so the appendix can never drift from the arithmetic it
# documents. sector_scales is likewise pure (used to locate + read the active scale).
from scripts import score_composite, score_fundamental, sector_scales

_DISCLAIMER_SHORT = "educational research, not investment advice"

_ABSENT_MSG = (
    "reportlab (and matplotlib) are required to render the docket but are not "
    "installed in this environment. Bootstrap the render venv with:\n"
    "    python3 scripts/render_env.py --check\n"
    "then invoke this renderer with the venv's python (the path it prints)."
)

# The four scoring dimensions in canonical display order (+ conviction).
_DIM_LABELS = {
    "technical": "Technical", "fundamental": "Fundamental",
    "sentiment": "Sentiment", "risk": "Risk",
    "thesis_conviction": "Conviction",
}


# --------------------------------------------------------------------------- #
# Bundle + slots loading (pure; no reportlab).
# --------------------------------------------------------------------------- #

def load_docs(bundle):
    """Load snapshot + module JSONs via render_report.load_bundle.

    Returns the same ``{snapshot, module_<x>}`` dict the report layer uses, so the
    docket reads exactly the shapes the whole pipeline emits.
    """
    return render_report.load_bundle(bundle)


def _slots_path(bundle):
    return os.path.join(bundle, "pdf_slots.json")


def load_slots(bundle):
    """Load pdf_slots.json (or {} if absent/unreadable)."""
    path = _slots_path(bundle)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def slots_gate_ok(bundle):
    """True iff pdf_slots.json exists AND carries the qc_passed=true stamp.

    This is the enforcement hook: render_pdf refuses exec/detail unless this is
    True. A missing file, a parse error, or an absent/false stamp all return False.

    NOTE: the stamp is trust-on-write -- an accidental-bypass guard (you can't
    render a slots file that never passed report_qc), NOT forgery-resistant: a
    hand-edited ``{"qc_passed": true}`` would pass. It defends against mistakes,
    not a determined author editing their own bundle.
    """
    return load_slots(bundle).get("qc_passed") is True


def _context_path(bundle):
    return os.path.join(bundle, "module_context.json")


def load_context(bundle):
    """Load module_context.json (or None if absent/unreadable/unparseable).

    The company-context module (C2) is OPTIONAL in a bundle: only a coverage-first
    run authors it. When present AND stamped (``qc.qc_passed``) the detail report
    renders the CONTEXT NARRATIVE sections from it; when absent or unstamped, those
    sections are omitted with a one-line disclosure (see ``context_gate_ok``).
    """
    path = _context_path(bundle)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def context_gate_ok(context):
    """True iff a loaded context module carries qc.qc_passed=true.

    The CONTEXT NARRATIVE sections render ONLY from a context module that passed
    its own provenance+structure gate (report_qc --context stamps ``qc.qc_passed``
    INTO module.qc). An un-stamped module — authored but never gated — is treated
    the same as an absent one: the sections are omitted with a disclosure line, so
    ungated prose can never reach the page. Mirrors slots_gate_ok's trust-on-write
    posture (an accidental-bypass guard, not forgery-resistant).
    """
    if not isinstance(context, dict):
        return False
    qc = context.get("qc")
    return isinstance(qc, dict) and qc.get("qc_passed") is True


def load_refresh_plan(bundle):
    """Load the refresh plan JSON governing this bundle, or None.

    The refresh planner writes ``refresh_plan.json`` to the TICKER dir (the bundle
    PARENT under the detail_reports layout), so we look there first, then in the
    bundle itself (legacy). Returns the parsed dict, or None when absent/unreadable.
    The banners on Detail p1 + the Delta note read ``scale_review_required`` and
    ``pending_proposals`` from it.
    """
    candidates = [
        os.path.join(os.path.dirname(os.path.normpath(bundle)),
                     "refresh_plan.json"),
        os.path.join(bundle, "refresh_plan.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path) as fh:
                    plan = json.load(fh)
                return plan if isinstance(plan, dict) else None
            except (OSError, ValueError):
                return None
    return None


def _scale_review_name(plan):
    """The '<name>@<version>' of the first scale whose falsifier tripped, or None.

    Reads ``plan['scales']`` (a list of {scale, any_tripped, ...}); returns the
    first tripped scale's label so the SCALE REVIEW banner can name it. Falls back
    to None (the banner then omits the name) when no tripped scale is identifiable.
    """
    if not isinstance(plan, dict):
        return None
    for entry in (plan.get("scales") or []):
        if isinstance(entry, dict) and entry.get("any_tripped"):
            return entry.get("scale")
    return None


def _charts_dir(bundle):
    return os.path.join(bundle, "charts")


def _chart_png(bundle, name):
    """Path to a rendered chart PNG, or None if it is absent (skipped chart)."""
    path = os.path.join(_charts_dir(bundle), "%s.png" % name)
    return path if os.path.isfile(path) else None


# --------------------------------------------------------------------------- #
# Small numeric helpers.
# --------------------------------------------------------------------------- #

def _fmt(x):
    """Delegate to render_report._fmt for run-stable number formatting."""
    return render_report._fmt(x)


def fmt_money_delta(v, plus=False):
    """Money string with the sign OUTSIDE the dollar sign, 2dp + separators.

    -182.44 -> '-$182.44' (not '$-182.44'); 5.0 -> '$5.00'; 1254.81 ->
    '$1,254.81'; with plus=True a positive value gains a leading '+' ('+$5.00')
    for signed delta columns. Zero is never signed. The magnitude is formatted
    to 2dp with thousands separators (display-precision discipline). PURE
    (formatting only) so it is unit-tested directly.
    """
    if v is None or not isinstance(v, (int, float)) or isinstance(v, bool):
        return "n/a"
    if v < 0:
        return "-%s" % fmt_price(-v)
    sign = "+" if (plus and v > 0) else ""
    return "%s%s" % (sign, fmt_price(v))


def _delta(o, n):
    """Numeric delta n - o when both are numbers, else None."""
    if isinstance(o, bool) or isinstance(n, bool):
        return None
    if isinstance(o, (int, float)) and isinstance(n, (int, float)):
        return round(n - o, 4)
    return None


def _grade_for(score):
    """The fixture/pipeline grade banding (C < 60 <= B)."""
    if score is None:
        return "?"
    return "B" if score >= 60 else "C"


# --------------------------------------------------------------------------- #
# Display-precision formatters (the "amateur tell" fix). PURE, unit-tested.
#
# WHY: raw floats ($853.2, Beta 1.65828, IV percentile 92.3077) leak an
# unfinished look into an institutional PDF. Every DISPLAYED number in the
# script-minted exec/detail/delta chrome (tables, sidebar, boxes, footer) is
# routed through one of these three formatters. They format for DISPLAY only --
# they never touch the bundle values the provenance gate checks (that gate reads
# the md/slots prose, not the PDF), and its rounding-expansion already tolerates
# these 0-2dp roundings, so the slots gate is unaffected.
# --------------------------------------------------------------------------- #

def fmt_price(v):
    """A price to 2 decimal places with thousands separators, '$'-prefixed.

    853.2 -> '$853.20'; 1254.81 -> '$1,254.81'; 681.436 -> '$681.44'. Non-numbers
    (or bool) -> 'n/a'. Negative prices keep the sign inside ('-$5.00') -- price
    columns are unsigned in practice, but this stays well-defined.
    """
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return "n/a"
    return "${:,.2f}".format(v)


def fmt_ratio(v):
    """A dimensionless ratio to 2 decimal places (no unit, no separators).

    18.3879 -> '18.39'; 0.138 -> '0.14'; 1.65828 -> '1.66'. Non-numbers -> 'n/a'.
    Used for P/E, PEG, beta, and other bare multiples where 2dp is enough.
    """
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return "n/a"
    return "{:.2f}".format(v)


def fmt_pct_int(v):
    """A percentile / integer-percent to 0 decimal places (rounded, no '%').

    92.3077 -> '92'; 8.7 -> '9'; 103.0 -> '103'. Non-numbers -> 'n/a'. Used for
    percentile displays (IV percentile) where fractional precision is noise.
    """
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return "n/a"
    return "{:.0f}".format(v)


# Grade-box action labels are long ("Hold/Accumulate-on-weakness") and were being
# truncated mid-phrase in the fixed-width box. This maps the canonical pipeline
# actions to compact, fully-visible short forms; unknown actions fall back to an
# uppercased form fitted by stringWidth at draw time (see _grade_box).
_ACTION_SHORT = {
    "Buy/Add": "BUY / ADD",
    "Hold/Accumulate-on-weakness": "HOLD / ACCUMULATE",
    "Hold/Trim": "HOLD / TRIM",
    "Reduce/Avoid": "REDUCE / AVOID",
}


def action_short(action):
    """Compact grade-box action label. PURE (map lookup), unit-tested.

    Known canonical actions map to their short form; anything else is uppercased
    (the caller then stringWidth-fits it into the box). None/empty -> '?'.
    """
    if not action:
        return "?"
    return _ACTION_SHORT.get(action, str(action).upper())


# --------------------------------------------------------------------------- #
# diff_bundles -- the What-Changed computation (PURE, zero LLM arithmetic).
# --------------------------------------------------------------------------- #

def diff_bundles(prev_docs, new_docs):
    """Compute the docket's What-Changed diff from two bundles' module JSONs.

    Returns a dict of old/new/Δ for the metrics the What-Changed box shows:
      composite {old,new,delta}, grade {old,new}, dimensions{<name>{old,new,delta}},
      entry_1 {old,new,delta}, ev_at_current {old,new,delta}, invalidation
      {technical{old,new}, fundamental{old,new}}.

    This is the SAME source as render_report's md delta (composite dimension
    scores, composite score, EV metrics, tradeplan levels/invalidation), so the
    PDF What-Changed and the md delta never disagree. Missing pieces map to None
    (a fresh entry with no prior bundle simply has None olds).
    """
    oc = (prev_docs.get("module_composite") or {}) if prev_docs else {}
    nc = new_docs.get("module_composite") or {}
    otp = (prev_docs.get("module_tradeplan") or {}) if prev_docs else {}
    ntp = new_docs.get("module_tradeplan") or {}

    # Composite score + grade.
    o_score, n_score = oc.get("score"), nc.get("score")
    composite = {"old": o_score, "new": n_score, "delta": _delta(o_score, n_score)}
    grade = {"old": oc.get("grade") if prev_docs else None,
             "new": nc.get("grade")}

    # Per-dimension scores.
    old_dims = {d.get("name"): d.get("score") for d in (oc.get("dimensions") or [])}
    new_dims = {d.get("name"): d.get("score") for d in (nc.get("dimensions") or [])}
    dims = {}
    for name in list(dict.fromkeys(list(new_dims) + list(old_dims))):
        o, n = old_dims.get(name), new_dims.get(name)
        dims[name] = {"old": o, "new": n, "delta": _delta(o, n)}

    # Entry 1 (first stock-plan entry level).
    def _entry1(tp):
        entries = ((tp.get("stock_plan") or {}).get("entries") or [])
        return entries[0].get("level") if entries else None
    o_e1, n_e1 = _entry1(otp), _entry1(ntp)
    entry_1 = {"old": o_e1, "new": n_e1, "delta": _delta(o_e1, n_e1)}

    # EV at current.
    o_ev = (oc.get("ev") or {}).get("ev_at_current")
    n_ev = (nc.get("ev") or {}).get("ev_at_current")
    ev_at_current = {"old": o_ev, "new": n_ev, "delta": _delta(o_ev, n_ev)}

    # Invalidation legs (technical level + fundamental metric text).
    def _inv(tp):
        inv = ((tp.get("stock_plan") or {}).get("invalidation") or {})
        tech = (inv.get("technical_leg") or {}).get("level")
        fund = (inv.get("fundamental_leg") or {}).get("metric")
        return tech, fund
    o_tech, o_fund = _inv(otp)
    n_tech, n_fund = _inv(ntp)
    invalidation = {
        "technical": {"old": o_tech, "new": n_tech},
        "fundamental": {"old": o_fund, "new": n_fund},
    }

    # Weight-set transition: composite carries the weight_set stamp
    # ("standard v1" | "CUSTOM <set>@<ver>"). A change flags the machinery a report
    # was weighted under moving between runs (a governance-visible transition).
    weight_set = {"old": oc.get("weight_set") if prev_docs else None,
                  "new": nc.get("weight_set")}

    # Sector-scale transition: the fundamental module's sector_scale stamp
    # ("<name>@<version>" | null). A change (including on/off) surfaces that the
    # anchored valuation's sector anchor moved between runs.
    of = (prev_docs.get("module_fundamental") or {}) if prev_docs else {}
    nf = new_docs.get("module_fundamental") or {}
    sector_scale = {"old": of.get("sector_scale") if prev_docs else None,
                    "new": nf.get("sector_scale")}

    return {
        "composite": composite, "grade": grade, "dimensions": dims,
        "entry_1": entry_1, "ev_at_current": ev_at_current,
        "invalidation": invalidation,
        "weight_set": weight_set, "sector_scale": sector_scale,
    }


def _what_changed_rows(diff):
    """The 3 What-Changed rows the exec box shows: (metric, old, new, Δ, is_down)."""
    def row(label, blk, money=False, pct=False):
        o, n, d = blk.get("old"), blk.get("new"), blk.get("delta")
        def f(v, lead=""):
            if v is None:
                return "n/a"
            if pct and isinstance(v, (int, float)):
                return "%s%.1f%%" % (lead, v * 100)
            if money and isinstance(v, (int, float)):
                return fmt_money_delta(v, plus=(lead == "+"))
            return "%s%s" % (lead, _fmt(v))
        dlead = "+" if (isinstance(d, (int, float)) and d > 0) else ""
        is_down = isinstance(d, (int, float)) and d < 0
        return (label, f(o), f(n), f(d, dlead) if d is not None else "n/a", is_down)

    return [
        row("Composite", diff["composite"]),
        row("Technical", diff["dimensions"].get("technical", {})),
        row("Entry 1", diff["entry_1"], money=True),
    ]


def _transition_rows(diff):
    """Weight-set + sector-scale transition rows for the What-Changed table.

    PURE (no drawing): returns a list of ``(label, "<prev> → <current>")`` rows,
    one per machinery field that CHANGED between the two bundles. A field whose
    old == new (including both None) yields NO row -- only genuine transitions are
    surfaced. ``None`` renders as "n/a" so a fresh-vs-prior (old None) transition
    reads "n/a → standard v1". Unit-tested in both directions + no-change.
    """
    rows = []
    for key, label in (("weight_set", "Weight set"),
                       ("sector_scale", "Scale")):
        blk = diff.get(key) or {}
        o, n = blk.get("old"), blk.get("new")
        if o != n:
            rows.append((label, "%s → %s"
                         % (o if o is not None else "n/a",
                            n if n is not None else "n/a")))
    return rows


# --------------------------------------------------------------------------- #
# Reportlab lazy-import + Doc canvas wrapper (mined from the mockup primitives).
# --------------------------------------------------------------------------- #

def _require_reportlab():
    """Import reportlab lazily; raise a clear RuntimeError if absent."""
    try:
        from reportlab.lib.pagesizes import letter  # noqa: F401
        from reportlab.pdfgen import canvas  # noqa: F401
        from reportlab.pdfbase.pdfmetrics import stringWidth  # noqa: F401
        from reportlab.lib.utils import ImageReader  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only sans-reportlab
        raise RuntimeError(_ABSENT_MSG) from exc
    import reportlab
    return reportlab


class Doc:
    """A thin reportlab-canvas wrapper carrying the docket's shared primitives.

    Holds the page geometry + palette (from tdstyle) and the masthead/footer/text
    helpers the mockup proved out, so every page is laid out identically. Created
    only on the draw path (reportlab imported lazily by the module functions).
    """

    FONT = "Helvetica"
    FONT_B = "Helvetica-Bold"
    FONT_I = "Helvetica-Oblique"
    MARGIN = 32

    def __init__(self, path, ticker, name, as_of, footer_bits):
        _require_reportlab()
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        self._letter = letter
        self.W, self.H = letter
        self.CONTENT_W = self.W - 2 * self.MARGIN
        self.c = canvas.Canvas(path, pagesize=letter)
        self.ticker = ticker
        self.name = name
        self.as_of = as_of
        self.footer_bits = footer_bits  # dict: qc, rubrics, snapshot_date
        self.total_pages = 0  # set before finalize for the p N/M footer
        self._page_no = 0

        # Palette (reportlab 0-1 tuples from tdstyle).
        self.ACCENT = tdstyle.ACCENT_RGB
        self.RED = tdstyle.RED_RGB
        self.GREEN = tdstyle.GREEN_RGB
        self.INK = tdstyle.INK_RGB
        self.GRAY_DK = tdstyle.GRAY_TXT_RGB
        self.GRAY_MD = tdstyle.GRAY_MID_RGB
        self.GRAY_LT = tdstyle.HAIRLINE_RGB
        self.WHITE = tdstyle.WHITE_RGB
        self.TRACK = (0.93, 0.93, 0.93)

    # ---- low-level drawing ---- #
    def set_fill(self, rgb):
        self.c.setFillColorRGB(*rgb)

    def set_stroke(self, rgb):
        self.c.setStrokeColorRGB(*rgb)

    def hairline(self, x1, y, x2, rgb=None, w=0.5):
        self.c.setLineWidth(w)
        self.set_stroke(rgb or self.GRAY_LT)
        self.c.line(x1, y, x2, y)

    def vline(self, x, y1, y2, rgb=None, w=0.5):
        self.c.setLineWidth(w)
        self.set_stroke(rgb or self.GRAY_LT)
        self.c.line(x, y1, x, y2)

    def rect(self, x, y, w, h, fill_rgb=None, stroke_rgb=None, line_w=0.5):
        if fill_rgb is not None:
            self.set_fill(fill_rgb)
        if stroke_rgb is not None:
            self.set_stroke(stroke_rgb)
            self.c.setLineWidth(line_w)
        self.c.rect(x, y, w, h, fill=1 if fill_rgb is not None else 0,
                    stroke=1 if stroke_rgb is not None else 0)

    def text(self, x, y, s, font=None, size=8, rgb=None, align="left"):
        self.c.setFont(font or self.FONT, size)
        self.set_fill(rgb if rgb is not None else self.INK)
        s = str(s)
        if align == "left":
            self.c.drawString(x, y, s)
        elif align == "right":
            self.c.drawRightString(x, y, s)
        else:
            self.c.drawCentredString(x, y, s)

    def string_width(self, s, font, size):
        from reportlab.pdfbase.pdfmetrics import stringWidth
        return stringWidth(str(s), font, size)

    def wrap(self, s, font, size, max_w):
        """Greedy word-wrap by measured width -> list of lines."""
        words = str(s).split()
        lines, cur = [], ""
        for w in words:
            trial = w if not cur else cur + " " + w
            if self.string_width(trial, font, size) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    def truncate(self, s, font, size, max_w):
        s = str(s)
        if self.string_width(s, font, size) <= max_w:
            return s
        ell = "…"
        while s and self.string_width(s + ell, font, size) > max_w:
            s = s[:-1]
        return s + ell

    def draw_wrapped(self, x, y, s, font, size, rgb, max_w, leading):
        for i, ln in enumerate(self.wrap(s, font, size, max_w)):
            self.text(x, y - i * leading, ln, font=font, size=size, rgb=rgb)
        n = len(self.wrap(s, font, size, max_w))
        return y - max(n - 1, 0) * leading

    def place_image(self, path, x, y_top, w, h):
        """Place a PNG with top-left anchor at (x, y_top), fit within w x h."""
        from reportlab.lib.utils import ImageReader
        img = ImageReader(path)
        iw, ih = img.getSize()
        ar = iw / ih
        draw_w, draw_h = w, w / ar
        if draw_h > h:
            draw_h, draw_w = h, h * ar
        self.c.drawImage(path, x, y_top - draw_h, width=draw_w, height=draw_h,
                         preserveAspectRatio=True, mask="auto")
        return draw_w, draw_h

    # ---- page chrome ---- #
    def masthead(self):
        y = self.H - self.MARGIN
        self.text(self.MARGIN, y - 8, "TRADING DESK", font=self.FONT_B,
                  size=10, rgb=self.ACCENT)
        right = "%s  ·  %s  ·  %s" % (self.ticker, self.name,
                                                (self.as_of or "")[:10])
        self.text(self.W - self.MARGIN, y - 8, right, font=self.FONT,
                  size=8, rgb=self.GRAY_DK, align="right")
        self.hairline(self.MARGIN, y - 15, self.W - self.MARGIN,
                      rgb=self.ACCENT, w=1.0)
        return y - 15

    def footer(self):
        """The contract-pinned footer on every page.

        Source: verified snapshot <as_of> · QC <counts> · rubrics <versions> ·
        educational research, not investment advice · p N/M.
        """
        y = self.MARGIN
        self.hairline(self.MARGIN, y + 10, self.W - self.MARGIN,
                      rgb=self.GRAY_LT, w=0.5)
        fb = self.footer_bits
        total = self.total_pages or self._page_no
        # Weights stamp always shown; scale stamp only when a sector scale is active.
        machinery = "Weights: %s" % fb.get("weight_set", "standard v1")
        if fb.get("scale"):
            machinery += "  ·  Scale: %s" % fb["scale"]
        foot = ("Source: verified snapshot %s  ·  QC %s  ·  "
                "rubrics %s  ·  %s  ·  %s  ·  p %d/%d" % (
                    fb.get("snapshot_date", "?"), fb.get("qc", "?"),
                    fb.get("rubrics", "?"), machinery, _DISCLAIMER_SHORT,
                    self._page_no, total))
        # Truncate to the content width so it never runs off the page.
        foot = self.truncate(foot, self.FONT, 6.2, self.CONTENT_W)
        self.text(self.MARGIN, y + 2, foot, font=self.FONT, size=6.2,
                  rgb=self.GRAY_MD)

    def section_head(self, x, y, s, w=None):
        self.text(x, y, s, font=self.FONT_B, size=10, rgb=self.ACCENT)
        if w:
            self.hairline(x, y - 3.5, x + w, rgb=self.ACCENT, w=0.7)
        return y

    def begin_page(self):
        self._page_no += 1
        return self.masthead()

    def end_page(self):
        self.footer()
        self.c.showPage()

    def save(self):
        self.c.save()


# --------------------------------------------------------------------------- #
# Footer-bit assembly (script-minted counts + versions from the bundle).
# --------------------------------------------------------------------------- #

def _footer_bits(docs):
    snap = docs.get("snapshot") or {}
    meta = snap.get("meta", {}) or {}
    as_of = meta.get("as_of_utc", "unknown")
    date = (as_of or "")[:10]

    qc = meta.get("qc", {}) or {}
    # A compact passed/failed/waived counts string. Prefer parsing an attestation
    # sentence if present; else derive counts from the checks + waivers lists
    # (real bundles carry those, not a prose attestation). Fall back to a status
    # word only when neither is available.
    counts = "PASS" if qc.get("passed") else "status unknown"
    attest = qc.get("attestation")
    if isinstance(attest, str):
        import re as _re
        m = _re.search(r"(\d+)\s+passed\s*/\s*(\d+)\s+failed\s*/\s*(\d+)\s+waived",
                       attest)
        if m:
            counts = "%s/%s/%s" % (m.group(1), m.group(2), m.group(3))
    else:
        checks = qc.get("checks")
        if isinstance(checks, list) and checks:
            waived_names = {w.get("check") for w in (qc.get("waivers") or [])
                            if isinstance(w, dict)}
            passed = failed = waived = 0
            for ch in checks:
                if not isinstance(ch, dict):
                    continue
                if ch.get("passed") is False and ch.get("check") in waived_names:
                    waived += 1
                elif ch.get("passed") is False:
                    failed += 1
                else:
                    passed += 1
            counts = "%d/%d/%d" % (passed, failed, waived)

    rubric = "?"
    comp = docs.get("module_composite") or {}
    if comp.get("rubric_version"):
        rubric = "v%s" % comp["rubric_version"]

    # Weight-set stamp (standard v1 | CUSTOM <set>@<ver>) from the composite module,
    # and the active sector-scale stamp (<name>@<version>) from the fundamental
    # module — both surfaced in the footer so the machinery a page was rendered
    # under is always disclosed on the page itself.
    weight_set = comp.get("weight_set") or score_composite.STANDARD_WEIGHT_SET
    fund = docs.get("module_fundamental") or {}
    scale_stamp = fund.get("sector_scale")  # "<name>@<version>" | None

    return {"snapshot_date": date, "qc": counts, "rubrics": rubric,
            "as_of": as_of, "weight_set": weight_set, "scale": scale_stamp}


def _ticker_as_of(docs):
    snap = docs.get("snapshot") or {}
    meta = snap.get("meta", {}) or {}
    return meta.get("ticker", "UNKNOWN"), meta.get("as_of_utc", "")


# --------------------------------------------------------------------------- #
# Reusable content blocks (mined + adapted from the mockup).
# --------------------------------------------------------------------------- #

def _grade_box(doc, x, y_top, w, comp):
    """The grade box: grade/action, composite, EV, breakeven. Height returned."""
    h = 58
    grade = comp.get("grade", "?")
    action = comp.get("action", "?")
    score = comp.get("score")
    profile = comp.get("profile", "?")
    ev = (comp.get("ev") or {})
    ev_cur = ev.get("ev_at_current")
    hurdle = ev.get("hurdle_total")
    breakeven = ev.get("ev_breakeven_entry")

    doc.rect(x, y_top - h, w, h, fill_rgb=doc.ACCENT)
    light = (0.96, 0.90, 0.84)
    # Short-form action so the grade line ("B · HOLD / ACCUMULATE") is never
    # truncated mid-phrase; still stringWidth-fitted as a final safety net.
    doc.text(x + 8, y_top - 15, doc.truncate("%s · %s" % (grade,
             action_short(action)), doc.FONT_B, 12, w - 12), font=doc.FONT_B,
             size=12, rgb=doc.WHITE)
    doc.text(x + 8, y_top - 28, "Composite %s/100 · %s" % (
        _fmt(score), profile), font=doc.FONT, size=7.6, rgb=light)
    if ev_cur is not None:
        hz = " vs hurdle %+.1f%%" % (hurdle * 100) if hurdle is not None else ""
        doc.text(x + 8, y_top - 39, "EV(current) %+.1f%%%s" % (ev_cur * 100, hz),
                 font=doc.FONT, size=7.6, rgb=light)
    if breakeven is not None:
        doc.text(x + 8, y_top - 50, "Breakeven entry %s" % fmt_price(breakeven),
                 font=doc.FONT, size=7.6, rgb=light)
    # CUSTOM weight-set tag near the grade box (top-right corner) when this report
    # was scored under a non-standard weight column — the tuning is never hidden.
    weight_set = comp.get("weight_set")
    if weight_set and weight_set != score_composite.STANDARD_WEIGHT_SET:
        tag = "CUSTOM WEIGHTS"
        tw = doc.string_width(tag, doc.FONT_B, 6.0) + 6
        doc.rect(x + w - tw - 4, y_top - 12, tw, 9, fill_rgb=doc.WHITE)
        doc.text(x + w - tw - 1, y_top - 10, tag, font=doc.FONT_B, size=6.0,
                 rgb=doc.ACCENT)
    return h


def _sensitivity_strip(doc, x, y_top, w, comp):
    """The 3-profile sensitivity strip under the grade box. Height returned."""
    sens = comp.get("sensitivity", {}) or {}
    h = 13
    doc.rect(x, y_top - h, w, h, stroke_rgb=doc.GRAY_LT)
    order = ("trader", "balanced", "long-term")
    labels = {"trader": "Trader", "balanced": "Balanced", "long-term": "LT"}
    seg = w / 3
    for i, key in enumerate(order):
        s = sens.get(key) or {}
        txt = "%s %s %s" % (labels[key], s.get("grade", "?"), _fmt(s.get("score")))
        cx = x + seg * i + seg / 2
        doc.text(cx, y_top - 9, doc.truncate(txt, doc.FONT, 6.4, seg - 4),
                 font=doc.FONT, size=6.4, rgb=doc.GRAY_DK, align="center")
        if i > 0:
            doc.vline(x + seg * i, y_top - h + 2, y_top - 2, rgb=doc.GRAY_LT)
    return h


def _draw_scale_banners(doc, plan, y_top):
    """Draw the scale-governance banner(s) for a bundle's refresh plan. Returns y.

    Two one-line banners, drawn top-down when their trigger is present in ``plan``:
      * scale_review_required True -> an ACCENT-colored banner:
        "SCALE REVIEW REQUIRED — falsifier tripped on <name>@<version>; see
         methodology page." (the name comes from the first tripped scale).
      * pending_proposals non-empty -> a NEUTRAL (gray) banner:
        "Pending scale proposal(s) awaiting ratification: <names>".
    No plan / neither trigger -> nothing drawn, y_top returned unchanged. Placed on
    Detail p1 (below the header band) and on the Delta note.
    """
    if not isinstance(plan, dict):
        return y_top
    M = doc.MARGIN
    W = doc.CONTENT_W
    y = y_top
    if plan.get("scale_review_required"):
        name = _scale_review_name(plan)
        msg = ("SCALE REVIEW REQUIRED — falsifier tripped on %s; see methodology "
               "page." % (name or "an active scale"))
        h = 14
        doc.rect(M, y - h, W, h, fill_rgb=doc.ACCENT)
        doc.text(M + 6, y - 10, doc.truncate(msg, doc.FONT_B, 8, W - 12),
                 font=doc.FONT_B, size=8, rgb=doc.WHITE)
        y -= h + 5
    proposals = plan.get("pending_proposals") or []
    if proposals:
        names = ", ".join(str(p) for p in proposals)
        msg = "Pending scale proposal(s) awaiting ratification: %s" % names
        h = 13
        doc.rect(M, y - h, W, h, fill_rgb=(0.94, 0.94, 0.94),
                 stroke_rgb=doc.GRAY_LT)
        doc.text(M + 6, y - 9.5, doc.truncate(msg, doc.FONT, 7.4, W - 12),
                 font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
        y -= h + 5
    return y


def _what_changed_box(doc, x, y_top, w, diff, prev_date):
    """The What-Changed box (only when --previous). Returns box height."""
    h = 78
    doc.rect(x, y_top - h, w, h, stroke_rgb=doc.GRAY_LT)
    doc.text(x + 6, y_top - 12, "WHAT CHANGED", font=doc.FONT_B, size=8.5,
             rgb=doc.ACCENT)
    if prev_date:
        doc.text(x + w - 6, y_top - 12, "since prior note (%s)" % prev_date,
                 font=doc.FONT_I, size=6.4, rgb=doc.GRAY_MD, align="right")
    doc.hairline(x + 6, y_top - 16, x + w - 6, rgb=doc.GRAY_LT)

    cols = [x + 8, x + 130, x + 205, x + 280]
    ry = y_top - 28
    for j, hd in enumerate(("Metric", "Prior", "New", "Δ")):
        doc.text(cols[j], ry, hd, font=doc.FONT_B, size=6.6, rgb=doc.GRAY_MD,
                 align="left" if j == 0 else "right")
    for i, (m, o, n, d, is_down) in enumerate(_what_changed_rows(diff)):
        yy = ry - 10 - i * 9.0
        dc = doc.RED if is_down else doc.GREEN
        doc.text(cols[0], yy, m, font=doc.FONT, size=7.4, rgb=doc.INK)
        doc.text(cols[1], yy, o, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK,
                 align="right")
        doc.text(cols[2], yy, n, font=doc.FONT_B, size=7.4, rgb=doc.INK,
                 align="right")
        doc.text(cols[3], yy, d, font=doc.FONT_B, size=7.4, rgb=dc, align="right")
    return h


def _thesis_bullets(doc, x, y_top, w, bullets):
    """Thesis bullets ('Lead — rest' bold-lead format). Returns y after block."""
    doc.section_head(x, y_top, "THESIS", w=w)
    by = y_top - 15
    for b in bullets[:3]:
        # split on the em-dash lead separator.
        if "—" in b:
            lead, rest = b.split("—", 1)
            lead, rest = lead.strip(), rest.strip()
        else:
            lead, rest = b.strip(), ""
        doc.set_fill(doc.ACCENT)
        doc.c.rect(x + 1, by - 6, 4, 4, fill=1, stroke=0)
        lead_s = lead + " — " if rest else lead
        doc.text(x + 10, by - 4.5, lead_s, font=doc.FONT_B, size=8.2, rgb=doc.INK)
        lead_w = doc.string_width(lead_s, doc.FONT_B, 8.2)
        avail = w - 10 - lead_w
        rest_lines = doc.wrap(rest, doc.FONT, 8.2, avail) if rest else []
        if rest_lines:
            doc.text(x + 10 + lead_w, by - 4.5, rest_lines[0], font=doc.FONT,
                     size=8.2, rgb=doc.GRAY_DK)
            remaining = rest[len(rest_lines[0]):].strip()
            yy = by - 4.5
            if remaining:
                for ln in doc.wrap(remaining, doc.FONT, 8.2, w - 10):
                    yy -= 10.5
                    doc.text(x + 10, yy, ln, font=doc.FONT, size=8.2,
                             rgb=doc.GRAY_DK)
            by = yy - 13
        else:
            by -= 13
    return by


def _key_stats(doc, x, y_top, w, docs):
    """Key-statistics sidebar box. Returns y after the box."""
    snap = docs.get("snapshot") or {}
    price = snap.get("price", {}) or {}
    tech = snap.get("technicals", {}) or {}
    val = snap.get("valuation", {}) or {}
    fund = snap.get("fundamentals", {}) or {}
    sent = snap.get("sentiment", {}) or {}
    bench = snap.get("benchmark", {}) or {}
    events = snap.get("events", {}) or {}
    ne = events.get("next_earnings", {}) or {}

    def money(v):
        if v is None:
            return "n/a"
        if v >= 1e9:
            return "$%.0fB" % (v / 1e9)
        if v >= 1e6:
            return "$%.0fM" % (v / 1e6)
        return fmt_price(v)

    def pct(v):
        return "%.1f%%" % (v * 100) if isinstance(v, (int, float)) else "n/a"

    rows = [
        ("Mkt cap", money(price.get("mktcap_computed"))),
        ("52wk hi / lo", "%s / %s" % (fmt_price(price.get("wk52_high")),
                                      fmt_price(price.get("wk52_low")))),
        ("ADV (3m)", money(price.get("adv_dollar_3m"))),
        ("Beta", fmt_ratio(bench.get("beta"))),
        ("Realized vol 30", pct(tech.get("rv30_ann"))),
        ("Short interest", pct((sent.get("short_interest_pct") or 0) / 100)
         if sent.get("short_interest_pct") is not None else "n/a"),
        ("P/E ttm", fmt_ratio(val.get("pe_ttm"))),
        ("P/E fwd", fmt_ratio(val.get("pe_fwd"))),
        ("PEG", fmt_ratio(val.get("peg"))),
        ("FCF yield", pct(val.get("fcf_yield"))),
        ("EPS ttm", fmt_ratio(fund.get("eps_ttm"))),
        ("IV percentile", fmt_pct_int(sent.get("iv_pctile_1yr"))),
        ("Next earnings", ne.get("date") or "n/a"),
    ]
    doc.text(x, y_top, "KEY STATISTICS", font=doc.FONT_B, size=8.5, rgb=doc.ACCENT)
    ks_top = y_top - 6
    row_h = 13.6
    h = row_h * len(rows) + 8
    doc.rect(x, ks_top - h, w, h, stroke_rgb=doc.GRAY_LT)
    for i, (lab, v) in enumerate(rows):
        yy = ks_top - 12 - i * row_h
        doc.text(x + 6, yy, lab, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
        doc.text(x + w - 6, yy, doc.truncate(v, doc.FONT_B, 7.4, w * 0.55),
                 font=doc.FONT_B, size=7.4, rgb=doc.INK, align="right")
        if i < len(rows) - 1:
            doc.hairline(x + 4, yy - 4.5, x + w - 4, rgb=(0.94, 0.94, 0.94))
    return ks_top - h


def _desk_read(doc, x, y_top, w, desk):
    """The DESK READ block (setup/edge/trigger/risk). Returns y after block."""
    doc.section_head(x, y_top, "DESK READ", w=w)
    order = (("Setup", "setup"), ("Edge", "edge"),
             ("Trigger", "trigger"), ("Risk", "risk"))
    ry = y_top - 14
    for label, key in order:
        body = desk.get(key, "") or ""
        doc.text(x, ry, label, font=doc.FONT_B, size=7.4, rgb=doc.INK)
        lab_w = doc.string_width(label, doc.FONT_B, 7.4)
        first_avail = w - lab_w - 5
        words = str(body).split()
        first, k = "", 0
        for k, wd in enumerate(words):
            trial = wd if not first else first + " " + wd
            if doc.string_width(trial, doc.FONT, 7.4) <= first_avail:
                first = trial
            else:
                break
        else:
            k = len(words)
        if first:
            # Some words fit beside the label; wrap the remainder full-width below.
            doc.text(x + lab_w + 5, ry, first, font=doc.FONT, size=7.4,
                     rgb=doc.GRAY_DK)
            rest = " ".join(words[k:]) if k < len(words) else ""
        else:
            # Label too wide for any inline text -> whole body wraps below.
            rest = str(body)
        if rest:
            for ln in doc.wrap(rest, doc.FONT, 7.4, w - 4):
                ry -= 9.5
                doc.text(x, ry, ln, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
        ry -= 12.5
    return ry


def _positioning_grid(doc, x, y_top, w, gutter, positioning):
    """Two-column POSITIONING & EXECUTION note grid. Returns min y reached."""
    doc.section_head(x, y_top, "POSITIONING & EXECUTION", w=w)
    order = (("Entry discipline", "entry_discipline"),
             ("Sizing & Kelly", "sizing_kelly"),
             ("Path dependency", "path_dependency"),
             ("Monitoring", "monitoring"))
    col_w = (w - gutter) / 2
    col_x = [x, x + col_w + gutter]
    col_y = [y_top - 14, y_top - 14]
    for i, (head, key) in enumerate(order):
        body = positioning.get(key, "") or ""
        cxi = i % 2
        cx = col_x[cxi]
        cy = col_y[cxi]
        doc.set_fill(doc.ACCENT)
        doc.c.rect(cx, cy - 6, 3.5, 3.5, fill=1, stroke=0)
        doc.text(cx + 8, cy - 4.5, head, font=doc.FONT_B, size=8, rgb=doc.INK)
        yb = cy - 4.5 - 11
        for ln in doc.wrap(body, doc.FONT, 7.4, col_w - 8):
            doc.text(cx + 8, yb, ln, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
            yb -= 9.7
        col_y[cxi] = yb - 9
    return min(col_y)


def _trade_plan_table(doc, x, y_top, w, tradeplan):
    """The trade-plan table (don't-chase, entries, exits, invalidation, size,
    hedge, expression). Returns y after the table."""
    sp = tradeplan.get("stock_plan", {}) or {}
    expr = tradeplan.get("expression", {}) or {}
    rows = []
    dc = sp.get("dont_chase", {}) or {}
    if dc.get("above") is not None:
        rows.append(("Don't-chase", "above %s (%s)" % (
            fmt_price(dc.get("above")), dc.get("convention", "")), ""))
    for i, e in enumerate(sp.get("entries", []) or [], start=1):
        ev = e.get("ev_at_level")
        ev_s = ("%+.1f%%" % (ev * 100)) if isinstance(ev, (int, float)) else ""
        rows.append(("Entry %d" % i, "%s · %s" % (
            fmt_price(e.get("level")), e.get("condition", "")), ev_s))
    exits = sp.get("exits", {}) or {}
    pt = exits.get("profit_take") or {}
    if pt:
        rows.append(("Profit-take", "%s (%s)" % (fmt_price(pt.get("level")),
                     pt.get("type", "")), ""))
    bt = exits.get("bull_target") or {}
    if bt:
        note = " · %s" % bt.get("note") if bt.get("note") else ""
        rows.append(("Bull target", "%s%s" % (fmt_price(bt.get("level")), note),
                     ""))
    inv = sp.get("invalidation", {}) or {}
    tl = inv.get("technical_leg") or {}
    fl = inv.get("fundamental_leg") or {}
    rows.append(("Invalidation", "%s %s; %s %s" % (
        tl.get("condition", ""), fmt_price(tl.get("level")),
        fl.get("metric", ""), fl.get("threshold", "")), ""))
    sz = sp.get("sizing", {}) or {}
    if sz.get("recommended_pct") is not None:
        rows.append(("Size", "%.1f%% · cap %.1f%%" % (
            sz["recommended_pct"] * 100, (sz.get("cap_pct") or 0) * 100), ""))
    hedge = sp.get("hedge", {}) or {}
    if hedge.get("required"):
        rows.append(("Hedge", "%s · %s" % (
            hedge.get("structure", ""), hedge.get("trigger", "")), ""))
    else:
        rows.append(("Hedge", "not required at this size", ""))
    rec = expr.get("recommended_for_profile")
    if rec:
        rows.append(("Expression", rec, ""))

    doc.section_head(x, y_top, "TRADE PLAN", w=w)
    tc0, tc1 = x, x + 78
    tc_ev = x + w - 4
    rule_max_w = w - 78 - 40
    ty = y_top - 11
    doc.text(tc0, ty, "Item", font=doc.FONT_B, size=6.6, rgb=doc.GRAY_MD)
    doc.text(tc1, ty, "Level / Rule", font=doc.FONT_B, size=6.6, rgb=doc.GRAY_MD)
    doc.text(tc_ev, ty, "EV", font=doc.FONT_B, size=6.6, rgb=doc.GRAY_MD,
             align="right")
    doc.hairline(x, ty - 3, x + w, rgb=doc.GRAY_LT)
    row_h = 13.5
    ty -= 5
    for i, (item, rule, ev) in enumerate(rows):
        yy = ty - (i + 1) * row_h + 4
        doc.text(tc0, yy, item, font=doc.FONT_B, size=8, rgb=doc.INK)
        doc.text(tc1, yy, doc.truncate(rule, doc.FONT, 8, rule_max_w),
                 font=doc.FONT, size=8, rgb=doc.GRAY_DK)
        if ev:
            doc.text(tc_ev, yy, ev, font=doc.FONT_B, size=8, rgb=doc.GREEN,
                     align="right")
        doc.hairline(x, yy - 4, x + w, rgb=(0.93, 0.93, 0.93))
    return ty - len(rows) * row_h


def _subscore_table(doc, x, y_top, w, module, title):
    """A per-dimension subscore table (name · points/max). Returns y after."""
    subs = (module or {}).get("subscores") or []
    doc.text(x, y_top, title, font=doc.FONT_B, size=8, rgb=doc.ACCENT)
    doc.hairline(x, y_top - 3, x + w, rgb=doc.GRAY_LT)
    ry = y_top - 13
    for s in subs:
        name = str(s.get("name", "")).replace("_", " ")
        pts, mx = s.get("points"), s.get("max")
        doc.text(x, ry, doc.truncate(name, doc.FONT, 7.4, w - 40),
                 font=doc.FONT, size=7.4, rgb=doc.INK)
        doc.text(x + w - 2, ry, "%s / %s" % (_fmt(pts), _fmt(mx)),
                 font=doc.FONT_B, size=7.4, rgb=doc.GRAY_DK, align="right")
        ry -= 11
    return ry


# --------------------------------------------------------------------------- #
# EXEC document (2 pages) -- mockup layout with real bundle data.
# --------------------------------------------------------------------------- #

def _load_brief(bundle, dim):
    """Load an LLM-authored per-dimension evidence brief (brief_<dim>.md), or ''.

    These are optional prose files the SKILL writes; absent -> the detail section
    shows the module's scored rationale instead (arithmetic strings), never blank.
    """
    path = os.path.join(bundle, "brief_%s.md" % dim)
    if os.path.isfile(path):
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            return ""
    return ""


def _draw_exec_page1(doc, bundle, docs, slots, diff, prev_date, plan=None):
    top = doc.begin_page()
    M = doc.MARGIN
    gutter = 14
    main_w = doc.CONTENT_W * 0.66
    side_x = M + main_w + gutter
    side_w = doc.W - M - side_x

    comp = docs.get("module_composite") or {}
    tp = docs.get("module_tradeplan") or {}
    snap = docs.get("snapshot") or {}
    price = snap.get("price", {}) or {}
    meta = snap.get("meta", {}) or {}

    # Scale-governance banner(s) — Detail p1 only (``plan`` is threaded there; the
    # standalone exec doc passes None so its layout is untouched). When a banner
    # renders, the whole header band shifts DOWN by the consumed height so nothing
    # overlaps.
    top = _draw_scale_banners(doc, plan, top - 2) + 2 if plan else top

    # Header band.
    hb_top = top - 10
    doc.text(M, hb_top - 26, doc.ticker, font=doc.FONT_B, size=34, rgb=doc.ACCENT)
    tk_w = doc.string_width(doc.ticker, doc.FONT_B, 34)
    last = price.get("last")
    prev = price.get("prev_close")
    doc.text(M + tk_w + 12, hb_top - 20, fmt_price(last), font=doc.FONT_B,
             size=12, rgb=doc.INK)
    if last is not None and prev:
        chg = (last / prev - 1) * 100
        pw = doc.string_width(fmt_price(last), doc.FONT_B, 12)
        col = doc.RED if chg < 0 else doc.GREEN
        doc.text(M + tk_w + 12 + pw + 8, hb_top - 20, "%+.2f%% (1d)" % chg,
                 font=doc.FONT_B, size=9, rgb=col)

    # Grade box + sensitivity strip (right).
    _grade_box(doc, side_x, hb_top, side_w, comp)
    ps_top = hb_top - 58 - 4
    _sensitivity_strip(doc, side_x, ps_top, side_w, comp)

    # What-Changed box (only when --previous), else thesis moves up.
    if diff is not None:
        wc_top = hb_top - 46
        _what_changed_box(doc, M, wc_top, main_w, diff, prev_date)
        th_top = wc_top - 78 - 16
    else:
        th_top = hb_top - 52

    # Thesis bullets.
    bullets = slots.get("thesis_bullets") or []
    by = _thesis_bullets(doc, M, th_top, main_w, bullets)

    # Price chart (main col).
    pc_top = by - 6
    png = _chart_png(bundle, "price_volume")
    pc_bottom = pc_top - 224
    if png:
        _, dh = doc.place_image(png, M, pc_top, main_w, 224)
        pc_bottom = pc_top - dh

    # 52-week range bar (from the chart pack if present, else a drawn bar).
    rb_top = pc_bottom - 12
    r_png = _chart_png(bundle, "range52w")
    if r_png:
        _, dh = doc.place_image(r_png, M, rb_top, main_w, 60)
        rb_bottom = rb_top - dh
    else:
        rb_bottom = rb_top - 30

    # Trade-plan table.
    tp_top = rb_bottom - 14
    tp_bottom = _trade_plan_table(doc, M, tp_top, main_w, tp)

    # Deep-entry commentary line (fix 4d) — a scripted static line under the plan
    # when any entry sits >25% below last (trigger computed, sentence fixed).
    _draw_deep_entry_line(doc, docs, tp_bottom - 8)

    # Sidebar: key stats + score panel + desk read.
    sb_top = ps_top - 13 - 12
    ks_bottom = _key_stats(doc, side_x, sb_top, side_w, docs)
    sp_top = ks_bottom - 14
    doc.text(side_x, sp_top, "SCORE PANEL", font=doc.FONT_B, size=8.5,
             rgb=doc.ACCENT)
    doc.hairline(side_x, sp_top - 3.5, side_x + side_w, rgb=doc.ACCENT, w=0.6)
    s_png = _chart_png(bundle, "score_bars")
    sp_img_top = sp_top - 10
    dh = 120
    if s_png:
        _, dh = doc.place_image(s_png, side_x, sp_img_top, side_w, 130)
    cap_y = sp_img_top - dh - 3
    doc.text(side_x, cap_y, "contribution-weighted composite %s" % _fmt(
        comp.get("score")), font=doc.FONT_I, size=6.6, rgb=doc.GRAY_MD)

    rv_top = cap_y - 16
    _desk_read(doc, side_x, rv_top, side_w, slots.get("desk_read") or {})

    doc.end_page()


def _draw_exec_page2(doc, bundle, docs, slots):
    top = doc.begin_page()
    M = doc.MARGIN
    gutter = 16
    col_w = (doc.CONTENT_W - gutter) / 2
    lx, rx = M, M + col_w + gutter

    # Scenario fan (left) + football field (right).
    sc_top = top - 12
    doc.section_head(lx, sc_top, "BULL · BASE · BEAR", w=col_w)
    sc_dh = 0
    p = _chart_png(bundle, "scenario_fan")
    if p:
        _, sc_dh = doc.place_image(p, lx, sc_top - 8, col_w, 236)
    ff_top = top - 12
    doc.section_head(rx, ff_top, "VALUATION ANCHORS", w=col_w)
    ff_dh = 0
    p = _chart_png(bundle, "football_field")
    if p:
        _, ff_dh = doc.place_image(p, rx, ff_top - 8, col_w, 236)

    charts_bottom = (sc_top - 8) - max(sc_dh, ff_dh, 40)

    # Catalyst timeline band (full width).
    tl_top = charts_bottom - 20
    doc.section_head(lx, tl_top, "CATALYST TIMELINE", w=doc.CONTENT_W)
    tl_bottom = tl_top - 8 - 92
    p = _chart_png(bundle, "catalyst_timeline")
    if p:
        _, dh = doc.place_image(p, lx, tl_top - 8, doc.CONTENT_W, 92)
        tl_bottom = tl_top - 8 - dh

    # Revisions (left) + P/E band (right) mini-panels (fixes page-2 whitespace).
    er_top = tl_bottom - 16
    doc.section_head(lx, er_top, "ESTIMATES & REVISIONS", w=col_w)
    rev_bottom = er_top - 8 - 96
    p = _chart_png(bundle, "revisions")
    if p:
        _, dh = doc.place_image(p, lx, er_top - 8, col_w, 96)
        rev_bottom = er_top - 8 - dh
    doc.section_head(rx, er_top, "VALUATION HISTORY", w=col_w)
    pe_bottom = er_top - 8 - 96
    p = _chart_png(bundle, "pe_band")
    if p:
        _, dh = doc.place_image(p, rx, er_top - 8, col_w, 96)
        pe_bottom = er_top - 8 - dh

    # Positioning & Execution grid.
    pe_top = min(rev_bottom, pe_bottom) - 18
    _positioning_grid(doc, lx, pe_top, doc.CONTENT_W, gutter,
                      slots.get("positioning") or {})

    doc.end_page()


def render_exec(bundle, docs, slots, diff, prev_date, out_path):
    ticker, as_of = _ticker_as_of(docs)
    doc = Doc(out_path, ticker, "Trade Report", as_of, _footer_bits(docs))
    doc.total_pages = 2
    _draw_exec_page1(doc, bundle, docs, slots, diff, prev_date)
    _draw_exec_page2(doc, bundle, docs, slots)
    doc.save()
    return out_path


# --------------------------------------------------------------------------- #
# DETAIL document -- exec pages + per-dimension + options + downside +
# monitoring + integrity + appendix.
# --------------------------------------------------------------------------- #

def _draw_dimension_section(doc, bundle, docs, dim, module_key, chart_names,
                            y_top, slots=None):
    """One per-dimension EVIDENCE section: note-as-body + SCORING TRAIL + charts.

    Body priority (fix): the ~200-word evidence NOTE from pdf_slots is the body;
    when present, the arithmetic strings are DEMOTED into a small-type "SCORING
    TRAIL" exhibit box (they prove the score but no longer masquerade as the
    argument). Fallback chain when no note is authored (older bundles): the
    brief_<dim>.md prose, else the arithmetic strings inline — the CURRENT
    behavior, preserved.

    Returns the y reached; the caller starts a new page when it runs low.
    """
    M = doc.MARGIN
    label = _DIM_LABELS.get(dim, dim.title())
    module = docs.get(module_key) or {}
    doc.section_head(M, y_top, "EVIDENCE — %s (score %s)" % (
        label, _fmt(module.get("score"))), w=doc.CONTENT_W)

    y = y_top - 16
    text_w = doc.CONTENT_W * 0.58
    note = _evidence_note(slots, dim)
    brief = _load_brief(bundle, dim)

    if note:
        # NOTE is the body; arithmetic demoted to the SCORING TRAIL exhibit below.
        for para in note.split("\n\n"):
            para = " ".join(para.split())
            if not para:
                continue
            for ln in doc.wrap(para, doc.FONT, 8, text_w):
                doc.text(M, y, ln, font=doc.FONT, size=8, rgb=doc.GRAY_DK)
                y -= 10.5
            y -= 4
        y -= 2
        # SCORING TRAIL exhibit box (small-type, the arithmetic proof).
        trail = [s.get("arithmetic") for s in (module.get("subscores") or [])
                 if s.get("arithmetic")]
        if trail:
            doc.text(M, y, "SCORING TRAIL", font=doc.FONT_B, size=6.4,
                     rgb=doc.GRAY_MD)
            y -= 8.5
            box_top = y + 4
            ty = y
            for arith in trail:
                for ln in doc.wrap("• %s" % arith, doc.FONT, 6.6, text_w - 8):
                    doc.text(M + 4, ty, ln, font=doc.FONT, size=6.6,
                             rgb=doc.GRAY_MD)
                    ty -= 8.4
            doc.rect(M, ty + 2, text_w, box_top - (ty + 2), stroke_rgb=doc.GRAY_LT)
            y = ty - 4
    elif brief:
        # No note -> the brief prose (preserved fallback).
        for para in brief.split("\n\n"):
            para = " ".join(para.split())
            if not para:
                continue
            for ln in doc.wrap(para, doc.FONT, 8, text_w):
                doc.text(M, y, ln, font=doc.FONT, size=8, rgb=doc.GRAY_DK)
                y -= 10.5
            y -= 4
    else:
        # No note, no brief -> the module's scored rationale inline (preserved).
        rnote = module.get("renormalization_note")
        if rnote:
            for ln in doc.wrap(str(rnote), doc.FONT_I, 7.6, text_w):
                doc.text(M, y, ln, font=doc.FONT_I, size=7.6, rgb=doc.GRAY_MD)
                y -= 10
            y -= 4
        for s in (module.get("subscores") or []):
            arith = s.get("arithmetic")
            if arith:
                for ln in doc.wrap("• %s" % arith, doc.FONT, 7.6, text_w):
                    doc.text(M, y, ln, font=doc.FONT, size=7.6, rgb=doc.GRAY_DK)
                    y -= 9.7

    # Subscore table (right column) at the section top.
    st_x = M + text_w + 16
    st_w = doc.CONTENT_W - text_w - 16
    sub_y = _subscore_table(doc, st_x, y_top - 16, st_w, module, "SUBSCORES")

    # PEG display-only (anchored mode): where the snapshot-mode valuation exhibit
    # renders PEG as a SCORED subscore, anchored mode surfaces it here as
    # DISPLAY-ONLY with the exclusion note (it is not in the scored subscores). The
    # note is a bundle leaf from module_fundamental.peg_display.
    peg_disp = module.get("peg_display")
    if isinstance(peg_disp, dict):
        py = sub_y - 6
        doc.text(st_x, py, "PEG (display-only)", font=doc.FONT_B, size=6.6,
                 rgb=doc.GRAY_MD)
        py -= 9
        doc.text(st_x, py, "PEG %s" % _fmt(peg_disp.get("value")),
                 font=doc.FONT, size=7.2, rgb=doc.INK)
        py -= 9
        for ln in doc.wrap("excluded from scoring — %s"
                           % (peg_disp.get("note") or "unreliable for cyclicals"),
                           doc.FONT_I, 6.6, st_w):
            doc.text(st_x, py, ln, font=doc.FONT_I, size=6.6, rgb=doc.GRAY_MD)
            py -= 8.2

    # Charts for this dimension, placed SIDE BY SIDE (two per row) below the
    # text/table block. Halving the vertical chart cost (was stacked full-width)
    # roughly halves the section height, letting two dimension sections pack onto
    # one page instead of each burning ~55% of a page (review: excess
    # whitespace). Odd trailing charts fall to their own row.
    y = min(y, y_top - 16 - 14 * (len(module.get("subscores") or []) + 1)) - 10
    present = [p for p in (_chart_png(bundle, c) for c in chart_names) if p]
    chart_w = (doc.CONTENT_W - 12) / 2
    col_x = [M, M + chart_w + 12]
    i = 0
    while i < len(present):
        row = present[i:i + 2]
        row_h = 0.0
        for j, p in enumerate(row):
            _, dh = doc.place_image(p, col_x[j], y, chart_w, 132)
            row_h = max(row_h, dh)
        y -= row_h + 8
        i += 2
    return y


def _draw_options_section(doc, bundle, docs, y_top):
    """The FULL options module render (fix 4b) — not the 2-row summary it was.

    Renders every load-bearing part of module_options: the vol dashboard mini-table
    (verdict/iv30/rv20/diff/pctile/term), the recommended structures table with
    per-structure MANAGEMENT rules beneath each, the declined table WITH reasons,
    ``warnings_global``, and the hedge_structure when present. The options charts
    (vol_term_structure / skew / expected_move_cone / oi_walls) now live UNDER this
    section (mapping fix 4a), placed by the caller after the tables.
    """
    M = doc.MARGIN
    W = doc.CONTENT_W
    opt = docs.get("module_options") or {}
    vd = opt.get("vol_dashboard", {}) or {}
    doc.section_head(M, y_top, "OPTIONS & VOLATILITY", w=W)
    y = y_top - 15

    # -- Vol dashboard mini-table: verdict / IV30 / RV20 / diff / pctile / term. --
    doc.text(M, y, "Vol dashboard", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
    doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
    y -= 12

    def _pct0(v):
        return "%.0f%%" % (v * 100) if isinstance(v, (int, float)) else "n/a"

    def _ppt(v):  # a diff already expressed as a fraction -> signed percentage pts
        return "%+.0f%%" % (v * 100) if isinstance(v, (int, float)) else "n/a"

    vd_cells = [
        ("Verdict", str(vd.get("verdict", "n/a")).replace("_", " ")),
        ("IV30", _pct0(vd.get("iv30"))),
        ("RV20", _pct0(vd.get("rv20"))),
        ("IV-RV diff", _ppt(vd.get("diff"))),
        ("IV pctile (1y)", fmt_pct_int(vd.get("iv_pctile_1yr"))),
        ("Term", str(vd.get("term_structure", "n/a")).replace("_", " ")),
    ]
    seg = W / len(vd_cells)
    for i, (lab, val) in enumerate(vd_cells):
        cx = M + seg * i
        doc.text(cx, y, lab, font=doc.FONT, size=6.6, rgb=doc.GRAY_MD)
        doc.text(cx, y - 9, doc.truncate(val, doc.FONT_B, 7.6, seg - 4),
                 font=doc.FONT_B, size=7.6, rgb=doc.INK)
    y -= 24

    # -- Recommended structures + per-structure management rules. --
    rec = opt.get("recommended_structures") or []
    doc.text(M, y, "Recommended structures", font=doc.FONT_B, size=8,
             rgb=doc.ACCENT)
    doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
    y -= 12
    cols = [M, M + 150, M + 230, M + 320, M + 400]
    for j, hd in enumerate(("Structure", "Strikes", "Net", "PoP", "PoP method")):
        doc.text(cols[j], y, hd, font=doc.FONT_B, size=6.6, rgb=doc.GRAY_MD)
    y -= 10
    for st in rec:
        strikes = "/".join(fmt_price(s) for s in (st.get("strikes") or []))
        net = st.get("net_credit") or st.get("net_debit")
        pop = st.get("pop")
        pop_s = "%.0f%%" % (pop * 100) if isinstance(pop, (int, float)) else "n/a"
        pm = str(st.get("pop_method", "")).split("(")[0].strip()
        doc.text(cols[0], y, doc.truncate(st.get("name", ""), doc.FONT, 7.4, 145),
                 font=doc.FONT, size=7.4, rgb=doc.INK)
        doc.text(cols[1], y, strikes, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
        doc.text(cols[2], y, fmt_price(net), font=doc.FONT, size=7.4,
                 rgb=doc.GRAY_DK)
        doc.text(cols[3], y, pop_s, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
        doc.text(cols[4], y, doc.truncate(pm, doc.FONT, 7.4,
                 W - 400 + M - 4), font=doc.FONT, size=7.4,
                 rgb=doc.GRAY_DK)
        y -= 11
        # Per-structure management rules (the rich content the 2-row summary dropped).
        mgmt = st.get("management") or []
        if mgmt:
            rules = "Manage: " + " · ".join(str(r) for r in mgmt)
            for ln in doc.wrap(rules, doc.FONT_I, 7.0, W - 12):
                doc.text(M + 10, y, ln, font=doc.FONT_I, size=7.0, rgb=doc.GRAY_MD)
                y -= 9.2
        # Per-structure warnings, if any.
        for wn in (st.get("warnings") or []):
            for ln in doc.wrap("⚠ %s" % wn, doc.FONT_I, 7.0, W - 12):
                doc.text(M + 10, y, ln, font=doc.FONT_I, size=7.0, rgb=doc.RED)
                y -= 9.2
        y -= 3
    y -= 3

    # -- Declined table WITH reasons. --
    declined = opt.get("declined") or []
    if declined:
        doc.text(M, y, "Declined", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
        doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
        y -= 12
        for d in declined:
            doc.text(M, y, doc.truncate(d.get("name", ""), doc.FONT_B, 7.4, 140),
                     font=doc.FONT_B, size=7.4, rgb=doc.INK)
            doc.text(M + 150, y, doc.truncate(d.get("reason", ""), doc.FONT, 7.4,
                     W - 150), font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
            y -= 11
        y -= 6

    # -- Global warnings (binary-event / liquidity gates). --
    warnings = opt.get("warnings_global") or []
    for wn in warnings:
        for ln in doc.wrap("⚠ %s" % wn, doc.FONT_B, 7.4, W):
            doc.text(M, y, ln, font=doc.FONT_B, size=7.4, rgb=doc.RED)
            y -= 10
        y -= 2

    # -- Hedge structure (when present). --
    hedge = opt.get("hedge_structure")
    if isinstance(hedge, dict):
        cost = hedge.get("cost")
        cpp = hedge.get("cost_pct_of_spot")
        legs = hedge.get("legs") or []
        leg_s = " / ".join(
            "%s %s %s" % (lg.get("side", ""), lg.get("type", ""),
                          fmt_price(lg.get("strike"))) for lg in legs)
        htxt = "Hedge: %s" % hedge.get("type", "n/a")
        if leg_s:
            htxt += " (%s)" % leg_s
        if cost is not None:
            htxt += " · cost %s" % fmt_price(cost)
        if isinstance(cpp, (int, float)):
            htxt += " (%.1f%% of spot)" % (cpp * 100)
        for ln in doc.wrap(htxt, doc.FONT_I, 7.6, W):
            doc.text(M, y, ln, font=doc.FONT_I, size=7.6, rgb=doc.GRAY_DK)
            y -= 10
        y -= 2
    return y
    return y


def _draw_downside_monitoring(doc, bundle, docs, y_top):
    """Downside map + monitoring section."""
    M = doc.MARGIN
    doc.section_head(M, y_top, "DOWNSIDE MAP & MONITORING", w=doc.CONTENT_W)
    y = y_top - 15
    p = _chart_png(bundle, "downside_ladder")
    if p:
        _, dh = doc.place_image(p, M, y, doc.CONTENT_W * 0.5, 160)
    # Monitoring lines from the tradeplan invalidation legs.
    tp = docs.get("module_tradeplan") or {}
    inv = ((tp.get("stock_plan") or {}).get("invalidation") or {})
    tl = inv.get("technical_leg") or {}
    fl = inv.get("fundamental_leg") or {}
    mx = M + doc.CONTENT_W * 0.5 + 16
    my = y
    doc.text(mx, my, "MONITORING", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
    my -= 13
    for line in (
        "Technical stop: %s %s" % (tl.get("condition", ""),
                                   fmt_price(tl.get("level"))),
        "Fundamental leg: %s %s" % (fl.get("metric", ""), fl.get("threshold", "")),
    ):
        for ln in doc.wrap(line, doc.FONT, 7.6, doc.CONTENT_W * 0.5 - 20):
            doc.text(mx, my, ln, font=doc.FONT, size=7.6, rgb=doc.GRAY_DK)
            my -= 10
        my -= 3

    # Downside-map anchor table (full width, below the chart + monitoring block).
    # SUSPECT floors (approx_current_eps method breakdown) are shown GRAYED with
    # their reason -- kept for continuity, visibly discounted, never a live anchor
    # (fix 3). The chart above already omits suspect rungs.
    ty = _draw_downside_map_table(doc, docs, M, min(y - 160, my) - 16)
    return min(min(y - 160, my), ty)


def _nearest_downside_anchors(docs, n=5):
    """The ``n`` anchors NEAREST last (smallest |pct_from_last|), sorted by depth.

    Dedup fix (4c): the downside map was rendered BOTH as a chart AND as the full
    downside_map table below it — the same data twice. We keep the chart and demote
    the table to a COMPACT n-row companion of the anchors nearest the current price
    (the levels that matter first on a decline), not the full ladder. Rows are
    picked by smallest absolute distance from last, then displayed shallowest-first
    (least-negative pct_from_last at the top, most-negative at the bottom) so the
    table reads top-to-bottom like a descent.
    """
    risk = docs.get("module_risk") or {}
    dmap = ((risk.get("tables") or {}).get("downside_map") or [])
    if not dmap:
        return []

    def _dist(r):
        p = r.get("pct_from_last")
        return abs(p) if isinstance(p, (int, float)) else 1e9

    def _depth(r):
        p = r.get("pct_from_last")
        return p if isinstance(p, (int, float)) else 0.0

    nearest = sorted(dmap, key=_dist)[:n]
    return sorted(nearest, key=_depth, reverse=True)


def _draw_downside_map_table(doc, docs, x, y_top):
    """The compact NEAREST-anchors table (<=5 rows); suspect rows grayed + reason.

    Dedup fix (4c): only the 5 anchors nearest the current price are shown here —
    the chart above carries the full ladder, so this is a focused companion, not a
    duplicate of the whole downside_map.
    """
    dmap = _nearest_downside_anchors(docs, n=5)
    if not dmap:
        return y_top
    w = doc.CONTENT_W
    doc.text(x, y_top, "NEAREST DOWNSIDE ANCHORS", font=doc.FONT_B, size=8,
             rgb=doc.ACCENT)
    doc.hairline(x, y_top - 3, x + w, rgb=doc.GRAY_LT)
    cols = [x, x + 110, x + 175]
    y = y_top - 13
    for j, hd in enumerate(("Level", "Type", "% from last")):
        doc.text(cols[j], y, hd, font=doc.FONT_B, size=6.6, rgb=doc.GRAY_MD)
    y -= 11
    for r in dmap:
        suspect = bool(r.get("suspect"))
        rgb = doc.GRAY_MD if suspect else doc.INK
        rgb2 = doc.GRAY_MD if suspect else doc.GRAY_DK
        typ = str(r.get("type", "")).replace("_", " ")
        pct = r.get("pct_from_last")
        pct_s = "%+.1f%%" % (pct * 100) if isinstance(pct, (int, float)) else ""
        doc.text(cols[0], y, fmt_price(r.get("level")), font=doc.FONT_B, size=7.4,
                 rgb=rgb)
        doc.text(cols[1], y, typ, font=doc.FONT, size=7.4, rgb=rgb2)
        doc.text(cols[2], y, pct_s, font=doc.FONT, size=7.4, rgb=rgb2)
        if suspect:
            reason = "suspect — %s" % (r.get("suspect_reason") or "")
            doc.text(cols[2] + 70, y, doc.truncate(reason, doc.FONT_I, 7.0,
                     w - (cols[2] - x) - 70), font=doc.FONT_I, size=7.0,
                     rgb=doc.GRAY_MD)
        y -= 11
    return y


def _parse_grade_history(bundle):
    """Parse the grade history from ../thesis_entry.md dated sections.

    Each dated section header ('## ... (YYYY-MM-DD)') plus a '**Grade:** X → Y'
    and '**composite:** a/100 → b/100' line yields one history row. Absent file ->
    'first entry'. The thesis_entry.md lives in the bundle PARENT (next to the
    detail_reports folder), so we look there then in the bundle itself.
    """
    import re
    candidates = [
        os.path.join(os.path.dirname(os.path.normpath(bundle)), "thesis_entry.md"),
        os.path.join(bundle, "thesis_entry.md"),
    ]
    text = None
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path) as fh:
                    text = fh.read()
                break
            except OSError:
                continue
    if not text:
        return []

    rows = []
    date_re = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")
    grade_re = re.compile(r"\*\*Grade:\*\*\s*([A-F][+-]?)\s*→\s*([A-F][+-]?)")
    comp_re = re.compile(r"\*\*composite:\*\*\s*([\d.]+)/100\s*→\s*([\d.]+)/100")
    # split into sections on '## ' headers.
    for section in re.split(r"^##\s", text, flags=re.MULTILINE):
        dm = date_re.search(section)
        if not dm:
            continue
        gm = grade_re.search(section)
        cm = comp_re.search(section)
        rows.append({
            "date": dm.group(1),
            "grade": ("%s→%s" % (gm.group(1), gm.group(2))) if gm else "?",
            "composite": ("%s→%s" % (cm.group(1), cm.group(2))) if cm else "?",
        })
    return rows


# --------------------------------------------------------------------------- #
# METHODOLOGY appendix — the transparency page. 100% script-generated: every
# string is a pinned constant in THIS module or a value read from the bundle's
# module JSONs / the active scale JSON. ZERO LLM content. The convention constants
# (weights, horizon years, grade bands, anchored/snapshot component maxima) are
# IMPORTED from score_composite / score_fundamental — the rubric of record — so the
# appendix can never drift from the arithmetic it documents.
#
# ``assemble_methodology`` is the PURE data-assembly half (no reportlab): it turns
# the bundle module JSONs + an optional scale JSON into an ordered list of structured
# BLOCKS ({"kind", "title", ...}); the draw path (``_draw_methodology``) renders each
# block height-aware. Factoring the assembly out lets the anchored-vs-snapshot maxima
# table, the CUSTOM dual weight table, the scale block present/absent branches, and
# the peg_display line be unit-tested WITHOUT the render venv.
# --------------------------------------------------------------------------- #

def _fmt_const(v):
    """Compact numeric constant string (int when integral, else %g). PURE."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return "%g" % v


# The dimension -> (rubric field, source module) rows the RUBRIC VERSIONS block
# shows, in canonical order. Each reads ``rubric_version`` off its module JSON; the
# composite is the "expression version" of record, the context module version is
# shown only when a context module carries one.
_METHODOLOGY_RUBRIC_DIMENSIONS = (
    ("Technical", "module_technical"),
    ("Fundamental", "module_fundamental"),
    ("Sentiment", "module_sentiment"),
    ("Risk", "module_risk"),
    ("Composite (expression)", "module_composite"),
)

# Anchored-mode valuation component maxima (v1.2), PINNED from score_fundamental so
# the table matches the scorer exactly.
_ANCHORED_MAXIMA = (
    ("DCF-band position", score_fundamental._DCF_MAX),
    ("Comps-range position", score_fundamental._COMPS_MAX),
    ("Own-history multiple", score_fundamental._OWNHIST_MAX),
    ("FCF yield", score_fundamental._FCFY_ANCHORED_MAX),
    ("Justified sector-band", score_fundamental._JUSTIFIED_MAX),
)
# Snapshot-mode (v1.1 floor) valuation component maxima. Pinned inline (score_
# valuation uses literal 20/15/15); labeled so a reader sees PEG is SCORED here.
_SNAPSHOT_MAXIMA = (
    ("Fwd P/E vs own 5-yr median", 20),
    ("PEG", 15),
    ("FCF yield", 15),
)

# The DCF-vs-comps disagreement rule sentence, pinned from the scorer's constants.
_DISAGREEMENT_RULE = (
    "When DCF base vs comps-mid disagree by more than %d%%, the methods materially "
    "conflict: the DCF band is WIDENED to span both and a x%s confidence haircut "
    "scales the DCF component's max (%d → %s) — the estimates are never averaged."
    % (int(score_fundamental._DISAGREE_THRESHOLD * 100),
       _fmt_const(score_fundamental._CONFIDENCE_HAIRCUT),
       score_fundamental._DCF_MAX,
       _fmt_const(score_fundamental._DCF_MAX * score_fundamental._CONFIDENCE_HAIRCUT)))

# Enumerated judgment flags (script-applied points, mandatory justification) — the
# CONVENTIONS block lists these so a reader knows exactly which levers are asserted.
_JUDGMENT_FLAGS = (
    "variant (strong|some|none)",
    "catalyst_clarity (clear|partial|vague)",
    "invalidation (both-legs|one-leg|none)",
    "moat (wide|narrow|none, fundamental quality)",
)

# The four GOVERNANCE sentences — pinned, forward-only versioning discipline.
_GOVERNANCE_SENTENCES = (
    "Versioning is forward-only: a rubric or scale change gets a new version; "
    "historical reports are never recalculated under it.",
    "A falsifier's auto-consequence must be pre-registered in the scale JSON "
    "(on_trip) before it can fire — no consequence is invented after the trip.",
    "A scale change requires adversarial review and explicit user ratification; a "
    "drafted proposal is pending until ratified and is disclosed as such.",
    "History is append-only: prior thesis entries and grades stand as written; a "
    "re-score adds a dated entry, it does not overwrite the record.",
)


def _fmt_num(v):
    """Compact numeric string for methodology tables (int when integral, else g)."""
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (int, float)):
        return "%g" % v
    return str(v)


def assemble_methodology(bundle_modules, scale_json):
    """Assemble the METHODOLOGY page content as an ordered list of BLOCKS (PURE).

    ``bundle_modules`` is the ``{snapshot, module_<x>}`` docs dict (as ``load_docs``
    returns). ``scale_json`` is the active sector scale dict (from the fundamental
    module's ``sector_scale`` stamp, located + read) or None.

    Each block is a dict ``{"kind", "title", ...}``:
      - rubric_versions:  {"rows": [(dimension, "rubric v<x>" | "n/a", source_field)]}
      - composite_weights:{"weight_set", "custom", "profiles": [...],
                           "rows": [(profile, {dim: weight}, {dim: std_weight}|None)],
                           "dims": [dim order]}
      - valuation_formula:{"mode", "maxima": [(component, max)], "peg_line": str|None,
                           "disagreement_rule": str}
      - sector_scale:     {"present": bool, ...scale fields when present...}
      - conventions:      {"ev_hurdle", "grade_bands": [...], "horizon_years": [...],
                           "judgment_flags": [...]}
      - governance:       {"sentences": [...]}

    Every value is either a pinned module constant or a bundle/scale leaf — NO LLM
    content. This is the transparency contract the page renders verbatim.
    """
    modules = bundle_modules or {}
    blocks = []

    # -- 1. Rubric versions -------------------------------------------------
    rows = []
    for label, key in _METHODOLOGY_RUBRIC_DIMENSIONS:
        m = modules.get(key) or {}
        rv = m.get("rubric_version")
        rows.append((label, ("rubric v%s" % rv) if rv else "n/a",
                     "%s.rubric_version" % key.replace("module_", "")))
    # Context module version, only when a context module is present + versioned.
    ctx = modules.get("module_context") or {}
    ctx_ver = ctx.get("version") or ctx.get("rubric_version")
    if ctx_ver:
        rows.append(("Company context", "v%s" % ctx_ver, "context.version"))
    blocks.append({"kind": "rubric_versions", "title": "Rubric versions",
                   "rows": rows})

    # -- 2. Composite weights ----------------------------------------------
    comp = modules.get("module_composite") or {}
    weight_set = comp.get("weight_set") or score_composite.STANDARD_WEIGHT_SET
    is_custom = weight_set != score_composite.STANDARD_WEIGHT_SET
    dim_order = ["technical", "fundamental", "sentiment", "risk",
                 "thesis_conviction"]
    profile = comp.get("profile")
    # The weights ACTUALLY used, from the composite's dimensions rows (authoritative
    # for the profile this report was scored under).
    used_weights = {}
    for d in (comp.get("dimensions") or []):
        nm = d.get("name")
        if nm is not None:
            used_weights[nm] = d.get("weight")
    weight_rows = []
    if profile:
        # The report's profile row: used weights + (when custom) the standard column
        # for comparison, pinned from the WEIGHTS table.
        std = score_composite.WEIGHTS.get(profile)
        weight_rows.append((profile, used_weights, std if is_custom else None))
    blocks.append({
        "kind": "composite_weights", "title": "Composite weights",
        "weight_set": weight_set, "custom": is_custom, "profile": profile,
        "dims": dim_order, "rows": weight_rows,
    })

    # -- 3. Fundamental valuation formula set ------------------------------
    fund = modules.get("module_fundamental") or {}
    val_mode = None
    for s in (fund.get("subscores") or []):
        if s.get("name") == "valuation" and s.get("valuation_mode"):
            val_mode = s.get("valuation_mode")
            break
    if val_mode is None:
        # Fall back to the sector_scale stamp presence heuristic (anchored implies a
        # sector_scale field may be set); default to snapshot floor when unknown.
        val_mode = ("anchored_v1.2" if fund.get("sector_scale")
                    else "snapshot_v1.1")
    anchored = val_mode == "anchored_v1.2"
    maxima = list(_ANCHORED_MAXIMA if anchored else _SNAPSHOT_MAXIMA)
    peg_line = None
    peg = fund.get("peg_display")
    if isinstance(peg, dict):
        peg_line = ("PEG %s — display-only, excluded from scoring (unreliable for "
                    "cyclicals)" % _fmt_num(peg.get("value")))
    blocks.append({
        "kind": "valuation_formula", "title": "Fundamental valuation formula set",
        "mode": val_mode, "anchored": anchored, "maxima": maxima,
        "peg_line": peg_line, "disagreement_rule": _DISAGREEMENT_RULE,
    })

    # -- 4. Active sector scale --------------------------------------------
    scale_block = {"kind": "sector_scale", "title": "Active sector scale"}
    stamp = fund.get("sector_scale")  # "<name>@<version>" | None
    if isinstance(scale_json, dict):
        band = None
        try:
            band = sector_scales.compute_band(scale_json)
        except (KeyError, TypeError, ZeroDivisionError):
            band = None
        falsifiers = []
        for f in (scale_json.get("falsifiers") or []):
            if isinstance(f, dict):
                falsifiers.append((f.get("metric"), f.get("op"),
                                   f.get("value"), f.get("meaning")))
        params = []
        for k, v in (scale_json.get("parameters") or {}).items():
            params.append((k, v))
        prior = scale_json.get("prior")
        prior_ver = prior.get("version") if isinstance(prior, dict) else None
        scale_block.update({
            "present": True,
            "stamp": stamp or "%s@%s" % (scale_json.get("scale"),
                                         scale_json.get("version")),
            "name": scale_json.get("name") or scale_json.get("scale"),
            "version": scale_json.get("version"),
            "effective": scale_json.get("effective"),
            "basis": scale_json.get("basis"),
            "formula": scale_json.get("formula"),
            "parameters": params,
            "band": band,
            "evidence": list(scale_json.get("evidence") or []),
            "falsifiers": falsifiers,
            "prior_version": prior_ver,
        })
    else:
        scale_block["present"] = False
        scale_block["stamp"] = stamp  # may be a stamp we could not locate on disk
    blocks.append(scale_block)

    # -- 5. Conventions -----------------------------------------------------
    grade_bands = []
    for lo, letter, action in ((80, "A", "Buy/Add"),
                               (60, "B", "Hold/Accumulate-on-weakness"),
                               (45, "C", "Hold/Trim"),
                               (0, "D", "Reduce/Avoid")):
        # Verify the pinned band edges agree with the scorer (defensive, no drift).
        letter_check, action_check = score_composite.grade_for(lo)
        grade_bands.append((letter_check, lo, action_check))
    horizon_rows = [(p, score_composite.HORIZON_YEARS[p])
                    for p in score_composite.WEIGHTS]
    ev_hurdle = ("EV hurdle = %g × horizon-years (an %g%%/yr return bar); EV must "
                 "clear the hurdle to earn asymmetry points."
                 % (score_composite._HURDLE_RATE,
                    score_composite._HURDLE_RATE * 100))
    blocks.append({
        "kind": "conventions", "title": "Conventions",
        "ev_hurdle": ev_hurdle, "grade_bands": grade_bands,
        "horizon_years": horizon_rows, "judgment_flags": list(_JUDGMENT_FLAGS),
    })

    # -- 6. Governance ------------------------------------------------------
    blocks.append({"kind": "governance", "title": "Governance",
                   "sentences": list(_GOVERNANCE_SENTENCES)})

    return blocks


def _locate_scale_json(bundle, docs):
    """Locate + read the active sector scale JSON for the methodology page, or None.

    The fundamental module stamps ``sector_scale`` as "<name>@<version>" when an
    anchored valuation used a scale. We resolve the scale NAME from that stamp and
    look for ``trading_desk_config/scales/<name>.json`` under the bundle dir and the
    CWD (the two conventional locations, per sector_scales.find_scale_for). Returns
    the parsed+validated scale dict, or None when there is no stamp or the file is
    absent/invalid (the page then renders the "no sector scale active" branch).
    """
    fund = docs.get("module_fundamental") or {}
    stamp = fund.get("sector_scale")
    if not isinstance(stamp, str) or "@" not in stamp:
        return None
    name = stamp.split("@", 1)[0]
    for base in (bundle, os.getcwd()):
        path = sector_scales.find_scale_for(base, name)
        if path:
            try:
                return sector_scales.load_scale(path)
            except ValueError:
                continue
    return None


def _draw_methodology(doc, bundle, docs, y_top):
    """Draw the METHODOLOGY appendix from ``assemble_methodology`` blocks.

    100% script-generated (ZERO LLM content). Height-aware like _draw_findings_block:
    each block is measured (its wrapped height) before it is drawn; when the next
    block would cross the footer band the current page closes and a fresh one opens
    under a ``METHODOLOGY (continued)`` kicker. Returns the y reached on the final
    page. The layout matches the detail aesthetic (dense 8-9pt, hairline tables,
    accent kickers).
    """
    M = doc.MARGIN
    W = doc.CONTENT_W
    page_bottom = doc.MARGIN + 24
    scale_json = _locate_scale_json(bundle, docs)
    blocks = assemble_methodology(docs, scale_json)

    doc.section_head(M, y_top, "METHODOLOGY", w=W)
    y = y_top - 16

    def _kicker(yk, label):
        doc.text(M, yk, label, font=doc.FONT_B, size=8, rgb=doc.ACCENT)
        doc.hairline(M, yk - 3, M + W, rgb=doc.GRAY_LT)
        return yk - 12

    def _ensure(need):
        """Page-break helper: if ``need`` px won't fit, spill to a fresh page."""
        nonlocal y
        if y - need < page_bottom:
            doc.end_page()
            top = doc.begin_page()
            doc.section_head(M, top - 12, "METHODOLOGY (continued)", w=W)
            y = top - 12 - 16

    for block in blocks:
        kind = block["kind"]
        # A conservative per-block header budget so a kicker never orphans at the
        # very bottom of a page (block bodies then flow and _ensure re-checks rows).
        _ensure(40)
        y = _kicker(y, block["title"])

        if kind == "rubric_versions":
            for dim, ver, src in block["rows"]:
                _ensure(11)
                doc.text(M + 6, y, dim, font=doc.FONT, size=7.6, rgb=doc.INK)
                doc.text(M + 180, y, ver, font=doc.FONT_B, size=7.6,
                         rgb=doc.GRAY_DK)
                doc.text(M + W, y, src, font=doc.FONT_I, size=6.6,
                         rgb=doc.GRAY_MD, align="right")
                y -= 10.5
            y -= 6

        elif kind == "composite_weights":
            ws = block["weight_set"]
            note = ("%s — standard v1 shown for comparison" % ws
                    if block["custom"] else ws)
            doc.text(M + 6, y, "Weight set: %s" % note, font=doc.FONT_I,
                     size=7.0, rgb=doc.GRAY_MD)
            y -= 11
            dims = block["dims"]
            dim_short = {"technical": "Tech", "fundamental": "Fund",
                         "sentiment": "Sent", "risk": "Risk",
                         "thesis_conviction": "Conv"}
            # Column header row (profile + one column per dimension weight).
            col0 = M + 6
            colw = (W - 90) / len(dims)
            _ensure(11)
            doc.text(col0, y, "Profile", font=doc.FONT_B, size=6.6,
                     rgb=doc.GRAY_MD)
            for i, d in enumerate(dims):
                doc.text(M + 90 + colw * i + colw / 2, y, dim_short.get(d, d),
                         font=doc.FONT_B, size=6.6, rgb=doc.GRAY_MD,
                         align="center")
            doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
            y -= 11
            for profile, used, std in block["rows"]:
                _ensure(11 if std is None else 20)
                doc.text(col0, y, profile, font=doc.FONT_B, size=7.4, rgb=doc.INK)
                for i, d in enumerate(dims):
                    doc.text(M + 90 + colw * i + colw / 2, y,
                             _fmt_num(used.get(d)), font=doc.FONT, size=7.4,
                             rgb=doc.INK, align="center")
                y -= 10.5
                if std is not None:
                    doc.text(col0, y, "standard v1", font=doc.FONT_I, size=6.8,
                             rgb=doc.GRAY_MD)
                    for i, d in enumerate(dims):
                        doc.text(M + 90 + colw * i + colw / 2, y,
                                 _fmt_num(std.get(d)), font=doc.FONT_I, size=6.8,
                                 rgb=doc.GRAY_MD, align="center")
                    y -= 10.5
            y -= 6

        elif kind == "valuation_formula":
            doc.text(M + 6, y, "Valuation mode: %s" % block["mode"],
                     font=doc.FONT_B, size=7.4, rgb=doc.INK)
            y -= 11
            for comp_name, mx in block["maxima"]:
                _ensure(10)
                doc.text(M + 12, y, comp_name, font=doc.FONT, size=7.4,
                         rgb=doc.GRAY_DK)
                doc.text(M + 12 + 190, y, "max %s" % _fmt_num(mx),
                         font=doc.FONT_B, size=7.4, rgb=doc.INK)
                y -= 9.8
            if block["peg_line"]:
                _ensure(11)
                y -= 1
                for ln in doc.wrap(block["peg_line"], doc.FONT_I, 7.2, W - 12):
                    doc.text(M + 6, y, ln, font=doc.FONT_I, size=7.2,
                             rgb=doc.GRAY_MD)
                    y -= 9.4
            _ensure(20)
            y -= 2
            for ln in doc.wrap(block["disagreement_rule"], doc.FONT, 7.2, W - 6):
                doc.text(M + 6, y, ln, font=doc.FONT, size=7.2, rgb=doc.GRAY_DK)
                y -= 9.4
            y -= 6

        elif kind == "sector_scale":
            if not block.get("present"):
                stamp = block.get("stamp")
                msg = ("No sector scale active — standard bands."
                       if not stamp else
                       "Sector scale %s stamped but not locatable on disk — "
                       "standard bands." % stamp)
                for ln in doc.wrap(msg, doc.FONT_I, 7.4, W - 6):
                    doc.text(M + 6, y, ln, font=doc.FONT_I, size=7.4,
                             rgb=doc.GRAY_MD)
                    y -= 9.6
                y -= 6
            else:
                head = "%s  (effective %s)" % (block["stamp"],
                                               block.get("effective") or "?")
                doc.text(M + 6, y, head, font=doc.FONT_B, size=7.6, rgb=doc.INK)
                y -= 11
                basis = block.get("basis")
                if basis:
                    for ln in doc.wrap("Basis: %s" % basis, doc.FONT, 7.2,
                                       W - 12):
                        _ensure(9)
                        doc.text(M + 12, y, ln, font=doc.FONT, size=7.2,
                                 rgb=doc.GRAY_DK)
                        y -= 9.2
                doc.text(M + 6, y, "Formula: %s" % block.get("formula"),
                         font=doc.FONT, size=7.2, rgb=doc.GRAY_DK)
                y -= 10
                # Parameters table.
                params = block.get("parameters") or []
                if params:
                    doc.text(M + 6, y, "Parameters", font=doc.FONT_B, size=6.8,
                             rgb=doc.GRAY_MD)
                    y -= 9.5
                    for k, v in params:
                        _ensure(9)
                        doc.text(M + 12, y, str(k), font=doc.FONT, size=7.2,
                                 rgb=doc.GRAY_DK)
                        doc.text(M + 12 + 160, y, _fmt_num(v), font=doc.FONT_B,
                                 size=7.2, rgb=doc.INK)
                        y -= 9.0
                band = block.get("band")
                if isinstance(band, dict):
                    _ensure(9)
                    doc.text(M + 6, y, "Band [low/mid/high]: %s / %s / %s"
                             % (_fmt_num(round(band.get("low"), 4)
                                         if band.get("low") is not None else None),
                                _fmt_num(round(band.get("mid"), 4)
                                         if band.get("mid") is not None else None),
                                _fmt_num(round(band.get("high"), 4)
                                         if band.get("high") is not None else None)),
                             font=doc.FONT, size=7.2, rgb=doc.GRAY_DK)
                    y -= 10
                evidence = block.get("evidence") or []
                if evidence:
                    _ensure(9)
                    doc.text(M + 6, y, "Evidence: %s" % ", ".join(evidence),
                             font=doc.FONT_I, size=7.0, rgb=doc.GRAY_MD)
                    y -= 9.5
                # Falsifiers table.
                fals = block.get("falsifiers") or []
                if fals:
                    _ensure(11)
                    doc.text(M + 6, y, "Falsifiers", font=doc.FONT_B, size=6.8,
                             rgb=doc.GRAY_MD)
                    doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
                    y -= 10
                    cols = [M + 6, M + 6 + 150, M + 6 + 205]
                    for j, hd in enumerate(("Metric", "Op", "Value")):
                        doc.text(cols[j], y, hd, font=doc.FONT_B, size=6.4,
                                 rgb=doc.GRAY_MD)
                    y -= 9
                    for metric, op, value, meaning in fals:
                        _ensure(9)
                        doc.text(cols[0], y, doc.truncate(str(metric), doc.FONT,
                                 7.0, 140), font=doc.FONT, size=7.0, rgb=doc.INK)
                        doc.text(cols[1], y, str(op), font=doc.FONT, size=7.0,
                                 rgb=doc.GRAY_DK)
                        doc.text(cols[2], y, _fmt_num(value), font=doc.FONT,
                                 size=7.0, rgb=doc.GRAY_DK)
                        y -= 8.6
                        if meaning:
                            for ln in doc.wrap("— %s" % meaning, doc.FONT_I,
                                               6.6, W - 18):
                                _ensure(8)
                                doc.text(cols[0] + 6, y, ln, font=doc.FONT_I,
                                         size=6.6, rgb=doc.GRAY_MD)
                                y -= 8.0
                        y -= 1
                pv = block.get("prior_version")
                _ensure(9)
                doc.text(M + 6, y, "Prior version: %s"
                         % (pv if pv else "none (first version)"),
                         font=doc.FONT_I, size=7.0, rgb=doc.GRAY_MD)
                y -= 10
                y -= 4

        elif kind == "conventions":
            for ln in doc.wrap(block["ev_hurdle"], doc.FONT, 7.2, W - 6):
                _ensure(9)
                doc.text(M + 6, y, ln, font=doc.FONT, size=7.2, rgb=doc.GRAY_DK)
                y -= 9.4
            y -= 2
            # Grade bands row.
            _ensure(10)
            doc.text(M + 6, y, "Grade bands:", font=doc.FONT_B, size=7.2,
                     rgb=doc.INK)
            bands = "  ·  ".join("%s ≥ %s (%s)" % (letter, lo, action)
                                 for letter, lo, action in block["grade_bands"])
            for ln in doc.wrap(bands, doc.FONT, 7.0,
                               W - doc.string_width("Grade bands: ", doc.FONT_B,
                                                    7.2) - 10):
                doc.text(M + 6 + doc.string_width("Grade bands: ", doc.FONT_B,
                         7.2), y, ln, font=doc.FONT, size=7.0, rgb=doc.GRAY_DK)
                y -= 9.2
            y -= 2
            # Horizon years per profile.
            _ensure(9)
            hz = "  ·  ".join("%s %sy" % (p, _fmt_num(yrs))
                              for p, yrs in block["horizon_years"])
            doc.text(M + 6, y, "Horizon (years): %s" % hz, font=doc.FONT,
                     size=7.2, rgb=doc.GRAY_DK)
            y -= 10
            # Judgment flags.
            _ensure(11)
            doc.text(M + 6, y, "Judgment flags (script-applied points, mandatory "
                     "justification):", font=doc.FONT_B, size=7.0, rgb=doc.INK)
            y -= 9.5
            flags = "; ".join(block["judgment_flags"])
            for ln in doc.wrap(flags, doc.FONT, 7.0, W - 12):
                _ensure(9)
                doc.text(M + 12, y, ln, font=doc.FONT, size=7.0, rgb=doc.GRAY_DK)
                y -= 9.0
            y -= 6

        elif kind == "governance":
            for i, sentence in enumerate(block["sentences"], start=1):
                _ensure(11)
                lines = doc.wrap("%d. %s" % (i, sentence), doc.FONT, 7.2, W - 12)
                _ensure(len(lines) * 9.2)
                for ln in lines:
                    doc.text(M + 6, y, ln, font=doc.FONT, size=7.2,
                             rgb=doc.GRAY_DK)
                    y -= 9.2
                y -= 2
            y -= 4

    return y


def _measure_methodology_pages(docs_ticker_asof, footer_bits, bundle, docs, y_top):
    """Pages the METHODOLOGY appendix consumes, WITHOUT drawing to the real doc.

    Mirrors _measure_context_pages: draws onto a throwaway canvas (same layout
    code) so the p N/M footer count stays exact even when the appendix spills to a
    METHODOLOGY (continued) page. Returns the page count.
    """
    scratch = Doc(os.devnull, docs_ticker_asof[0], "Detail",
                  docs_ticker_asof[1], footer_bits)
    scratch.total_pages = 1
    scratch.begin_page()
    _draw_methodology(scratch, bundle, docs, y_top)
    return scratch._page_no


def _draw_appendix(doc, bundle, docs, y_top):
    """Appendix: rubric versions + grade history (from thesis_entry.md)."""
    M = doc.MARGIN
    doc.section_head(M, y_top, "APPENDIX — RUBRICS & GRADE HISTORY",
                     w=doc.CONTENT_W)
    y = y_top - 15
    doc.text(M, y, "Rubric versions", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
    y -= 12
    for key in ("module_technical", "module_fundamental", "module_sentiment",
                "module_risk", "module_composite", "module_tradeplan",
                "module_options"):
        m = docs.get(key) or {}
        rv = m.get("rubric_version")
        if rv:
            doc.text(M, y, "%s: v%s" % (key.replace("module_", ""), rv),
                     font=doc.FONT, size=7.6, rgb=doc.GRAY_DK)
            y -= 10
    y -= 8

    doc.text(M, y, "Grade history", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
    doc.hairline(M, y - 3, M + doc.CONTENT_W, rgb=doc.GRAY_LT)
    y -= 12
    hist = _parse_grade_history(bundle)
    if not hist:
        doc.text(M, y, "first entry — no prior thesis_entry.md sections found.",
                 font=doc.FONT_I, size=7.6, rgb=doc.GRAY_MD)
        y -= 11
    else:
        cols = [M, M + 140, M + 260]
        for j, hd in enumerate(("Date", "Grade", "Composite")):
            doc.text(cols[j], y, hd, font=doc.FONT_B, size=6.8, rgb=doc.GRAY_MD)
        y -= 11
        for row in hist:
            doc.text(cols[0], y, row["date"], font=doc.FONT, size=7.6, rgb=doc.INK)
            doc.text(cols[1], y, row["grade"], font=doc.FONT, size=7.6,
                     rgb=doc.GRAY_DK)
            doc.text(cols[2], y, row["composite"], font=doc.FONT, size=7.6,
                     rgb=doc.GRAY_DK)
            y -= 10
    return y


def _draw_integrity_footer_page(doc, docs, y_top):
    """The integrity footer content block (as body, not the page chrome)."""
    M = doc.MARGIN
    snap = docs.get("snapshot") or {}
    md = render_report.build_integrity_footer(snap, docs)
    doc.section_head(M, y_top, "DATA INTEGRITY", w=doc.CONTENT_W)
    y = y_top - 15
    for line in md.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.lstrip("- ").replace("**", "").replace("_", "")
        for ln in doc.wrap(line, doc.FONT, 7.4, doc.CONTENT_W):
            doc.text(M, y, ln, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
            y -= 9.5
        y -= 2
    return y


# --------------------------------------------------------------------------- #
# WHY THIS CALL — the argued case, rendered PURELY from module-JSON judgment
# fields (no new prose). One sub-block per module: the composite scenario
# reasoning, each conviction subscore with its flag value + justification, the
# trade-plan judgment flags, the fundamental moat flag, and the sentiment
# judgment flags. This is the section that turns pages 3-6 from arithmetic dumps
# into an argument: every captured justification the pipeline wrote, printed.
# --------------------------------------------------------------------------- #

def _flag_chip(doc, x, y, label, value):
    """Draw a compact 'label: VALUE' flag chip. Returns the chip's right edge.

    The value is drawn in a small tinted pill; unknown/None values render 'n/a'.
    """
    val_s = str(value) if value not in (None, "") else "n/a"
    label_s = "%s:" % label
    doc.text(x, y, label_s, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
    lw = doc.string_width(label_s, doc.FONT, 7.4)
    px = x + lw + 4
    pill_w = doc.string_width(val_s, doc.FONT_B, 7.4) + 8
    doc.rect(px, y - 2.5, pill_w, 11, fill_rgb=(0.93, 0.90, 0.85))
    doc.text(px + 4, y, val_s, font=doc.FONT_B, size=7.4, rgb=doc.ACCENT)
    return px + pill_w


def _why_subblock(doc, x, y, w, title, chip_label, chip_value, justification):
    """One WHY-THIS-CALL sub-block: title, an optional flag chip, and the
    justification text wrapped below. Returns the y reached.

    Pure module-JSON rendering: ``chip_value`` is the flag's captured value and
    ``justification`` the captured judgment text (may cite C-IDs — rendered as-is,
    they are chrome the reader follows into the CONTEXT NARRATIVE findings).
    """
    doc.text(x, y, title, font=doc.FONT_B, size=7.8, rgb=doc.INK)
    ty = y
    if chip_label is not None:
        lw = doc.string_width(title, doc.FONT_B, 7.8)
        _flag_chip(doc, x + lw + 10, y, chip_label, chip_value)
    ty -= 11
    if justification:
        for ln in doc.wrap(str(justification), doc.FONT, 7.4, w - 6):
            doc.text(x + 6, ty, ln, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
            ty -= 9.6
    return ty - 4


def _parse_conviction_subscore(s):
    """Split a conviction subscore string into (name, flag_value, justification).

    The composite writes each as e.g.
      "variant some -> 12/20 (Modestly differentiated: ...)"
      "catalyst_clarity clear -> 20/20 (Dated, identifiable ...)"
      "ev_asymmetry: ev 0.0664 / hurdle ... = ratio 0.55 -> 12/40"
    We take the leading token as the subscore name, the parenthetical (if any) as
    the captured justification, and the words between the name and '->' as the flag
    value. Best-effort + defensive: an unparseable string yields (raw, None, None).
    """
    import re as _re
    raw = str(s)
    just = None
    m = _re.search(r"\(([^)]*)\)\s*$", raw)
    head = raw
    if m:
        just = m.group(1).strip()
        head = raw[:m.start()].strip()
    # name is the first whitespace/':'-delimited token.
    nm = _re.match(r"\s*([A-Za-z_]+)", head)
    name = nm.group(1) if nm else head
    flag_val = None
    # value = the text between the name and the '->' arrow, if that segment is
    # short (a flag word like 'some'/'clear'/'both-legs'), not an arithmetic tail.
    seg = head[len(name):].split("->", 1)[0].strip().lstrip(":").strip()
    if seg and len(seg.split()) <= 2 and "/" not in seg and "=" not in seg:
        flag_val = seg
    return name, flag_val, just


def _draw_why_this_call(doc, docs, y_top):
    """The WHY THIS CALL section: the argued case from module-JSON judgment.

    Sub-blocks, in order:
      * composite ev.scenario_reasoning (the thesis in one paragraph);
      * each conviction subscore with its flag value + justification (variant,
        catalyst_clarity, invalidation — from module_composite);
      * trade-plan judgment flags (catalyst-in-thesis + justification,
        fundamental-invalidation justification) from module_tradeplan.flags;
      * fundamental moat flag + justification from module_fundamental.flags
        (may cite C-IDs — rendered as-is);
      * sentiment judgment flags (rating_actions / inst_flow / insider_baseline +
        justifications) from module_sentiment.flags.
    Returns the y reached.
    """
    M = doc.MARGIN
    W = doc.CONTENT_W
    doc.section_head(M, y_top, "WHY THIS CALL", w=W)
    y = y_top - 16

    comp = docs.get("module_composite") or {}
    tp = docs.get("module_tradeplan") or {}
    fund = docs.get("module_fundamental") or {}
    sent = docs.get("module_sentiment") or {}

    # -- Scenario reasoning (the thesis paragraph). --
    reasoning = (comp.get("ev") or {}).get("scenario_reasoning")
    if reasoning:
        doc.text(M, y, "Scenario reasoning", font=doc.FONT_B, size=7.8, rgb=doc.INK)
        y -= 11
        for ln in doc.wrap(str(reasoning), doc.FONT, 7.6, W):
            doc.text(M + 6, y, ln, font=doc.FONT, size=7.6, rgb=doc.GRAY_DK)
            y -= 9.8
        y -= 6

    # -- Conviction subscores (variant / catalyst_clarity / invalidation ...). --
    cflags = comp.get("flags") or {}
    conv = (comp.get("thesis_conviction") or {}).get("subscores") or []
    if conv:
        doc.text(M, y, "Conviction", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
        doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
        y -= 12
        for sub in conv:
            name, flag_val, just = _parse_conviction_subscore(sub)
            # Prefer the composite's flags dict for value + justification when the
            # subscore name matches a captured flag (richer, uncondensed text).
            if flag_val is None:
                flag_val = cflags.get(name)
            fj = cflags.get("%s_justification" % name)
            just = fj or just
            title = str(name).replace("_", " ")
            y = _why_subblock(doc, M, y, W, title, "flag", flag_val, just)

    # -- Trade-plan judgment flags. --
    tflags = tp.get("flags") or {}
    if tflags:
        doc.text(M, y, "Trade plan", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
        doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
        y -= 12
        y = _why_subblock(
            doc, M, y, W, "catalyst in thesis", "flag",
            tflags.get("catalyst_in_thesis"),
            tflags.get("catalyst_in_thesis_justification"))
        fund_metric = tflags.get("fund_invalidation_metric")
        fund_thresh = tflags.get("fund_invalidation_threshold")
        chip = None
        if fund_metric:
            chip = fund_metric + (" %s" % fund_thresh if fund_thresh else "")
        y = _why_subblock(
            doc, M, y, W, "fundamental invalidation", "metric", chip,
            tflags.get("fund_invalidation_justification"))

    # -- Fundamental moat flag (may cite C-IDs — rendered as-is). --
    fflags = fund.get("flags") or {}
    moat = fflags.get("moat")
    moat_just = fflags.get("moat_justification")
    if moat is not None or moat_just:
        doc.text(M, y, "Fundamental — moat", font=doc.FONT_B, size=8,
                 rgb=doc.ACCENT)
        doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
        y -= 12
        y = _why_subblock(doc, M, y, W, "moat", "rating", moat, moat_just)

    # -- Sentiment judgment flags. --
    sflags = sent.get("flags") or {}
    sent_keys = ("rating_actions", "inst_flow", "insider_baseline")
    if any(k in sflags for k in sent_keys):
        doc.text(M, y, "Sentiment", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
        doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
        y -= 12
        for k in sent_keys:
            if k not in sflags:
                continue
            title = k.replace("_", " ")
            y = _why_subblock(doc, M, y, W, title, "flag", sflags.get(k),
                              sflags.get("%s_justification" % k))
    return y


# --------------------------------------------------------------------------- #
# CONTEXT NARRATIVE — rendered ONLY from a stamped module_context.json (C2).
# Four sections (THE BUSINESS / WHAT'S MOVING THE STOCK / THE CASES / RISKS)
# plus a FINDINGS footnote block. Absent or un-stamped module -> one disclosure
# line, sections omitted. This is pure module-JSON rendering (no new prose).
# --------------------------------------------------------------------------- #

def _draw_context_disclosure(doc, y_top):
    """The single line shown when no stamped context module is in the bundle."""
    M = doc.MARGIN
    doc.section_head(M, y_top, "COMPANY CONTEXT", w=doc.CONTENT_W)
    y = y_top - 16
    doc.text(M, y, "No gated company-context module in this bundle — business, "
             "cases, and risk narrative omitted.", font=doc.FONT_I, size=7.6,
             rgb=doc.GRAY_MD)
    return y - 12


# The FINDINGS footnote type sizes (shared by the draw path and the pure
# height helper so a measured height can never drift from the render).
_FINDINGS_SIZE = 6.6
_FINDINGS_LEADING = 8.4


def _finding_lines(doc, finding, max_w):
    """Word-wrapped lines for a single finding footnote (pure; no drawing).

    Composes the ``id  claim  [source]`` line and greedy-wraps it to ``max_w``.
    Isolated from the draw loop so the wrapped height (``len * leading``) can be
    unit-tested and reused by the pagination measurement without duplicating the
    line composition.
    """
    fid = (finding or {}).get("id", "")
    claim = (finding or {}).get("claim", "")
    source = (finding or {}).get("source", "")
    line = "%s  %s  [%s]" % (fid, claim, source)
    return doc.wrap(line, doc.FONT, _FINDINGS_SIZE, max_w)


def _draw_findings_block(doc, findings, y, page_bottom):
    """Draw the FINDINGS footnote block, spilling to new pages when exhausted.

    Each finding is measured (wrapped height) before it is drawn; when the next
    finding would cross ``page_bottom`` (the footer band), the current page is
    closed and a fresh page opens under a ``FINDINGS (continued)`` kicker. This
    mirrors the measured two-up packing used for the evidence dimensions: the
    footer is never overlapped and no finding is truncated mid-sentence. Returns
    the y reached on the final page.
    """
    if not findings:
        return y
    M = doc.MARGIN
    W = doc.CONTENT_W

    def _kicker(yk, label):
        doc.text(M, yk, label, font=doc.FONT_B, size=7.4, rgb=doc.GRAY_MD)
        doc.hairline(M, yk - 3, M + W, rgb=doc.GRAY_LT)
        return yk - 11

    y = _kicker(y, "FINDINGS")
    for f in findings:
        lines = _finding_lines(doc, f, W - 4)
        block_h = len(lines) * _FINDINGS_LEADING + 1
        # Would this finding cross the footer band? Continue on a fresh page.
        if y - block_h < page_bottom:
            doc.end_page()
            top = doc.begin_page()
            y = _kicker(top - 12, "FINDINGS (continued)")
        for ln in lines:
            doc.text(M + 4, y, ln, font=doc.FONT, size=_FINDINGS_SIZE,
                     rgb=doc.GRAY_MD)
            y -= _FINDINGS_LEADING
        y -= 1
    return y


def _draw_context_narrative(doc, docs, context, y_top):
    """Render the CONTEXT NARRATIVE sections from a STAMPED module_context.

    THE BUSINESS (what_they_sell + revenue_drivers + segments), WHAT'S MOVING THE
    STOCK (live_tape dated entries), THE CASES (bull/base/bear narratives +
    conditions, values tied to the scenario fan by the composite's scenario
    reasoning above), RISKS (ARGUED) (risk/why/anchor table), and a FINDINGS
    footnote block (id -> claim -> source, small gray). The findings block is
    height-aware: it spills to a ``FINDINGS (continued)`` page rather than
    overrunning the footer. Returns y reached on the final page.
    """
    M = doc.MARGIN
    W = doc.CONTENT_W
    # Footer band: keep findings clear of the p N/M hairline (matches the packing
    # geometry in render_detail: page_bottom == MARGIN + 24).
    page_bottom = doc.MARGIN + 24
    doc.section_head(M, y_top, "COMPANY CONTEXT", w=W)
    y = y_top - 16

    biz = context.get("business") or {}

    # -- THE BUSINESS. --
    doc.text(M, y, "THE BUSINESS", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
    doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
    y -= 12
    wts = biz.get("what_they_sell")
    if wts:
        for ln in doc.wrap(str(wts), doc.FONT, 7.6, W):
            doc.text(M + 6, y, ln, font=doc.FONT, size=7.6, rgb=doc.GRAY_DK)
            y -= 9.8
        y -= 2
    for label, key in (("Revenue drivers", "revenue_drivers"),
                       ("Segments", "segments")):
        items = biz.get(key) or []
        if items:
            line = "%s: %s" % (label, "; ".join(str(i) for i in items))
            for ln in doc.wrap(line, doc.FONT, 7.4, W):
                doc.text(M + 6, y, ln, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
                y -= 9.4
            y -= 2
    y -= 4

    # -- WHAT'S MOVING THE STOCK (live tape). --
    tape = context.get("live_tape") or []
    if tape:
        doc.text(M, y, "WHAT'S MOVING THE STOCK", font=doc.FONT_B, size=8,
                 rgb=doc.ACCENT)
        doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
        y -= 12
        for ev in tape:
            date = ev.get("date", "")
            event = ev.get("event", "")
            why = ev.get("why_it_matters", "")
            head = "%s — %s" % (date, event)
            # Wrap the title on word boundaries (max 2 lines) instead of hard
            # mid-word truncation, so long tape headlines stay legible.
            head_lines = doc.wrap(head, doc.FONT_B, 7.4, W - 6)[:2]
            for hl in head_lines:
                doc.text(M + 6, y, hl, font=doc.FONT_B, size=7.4, rgb=doc.INK)
                y -= 9.6
            if why:
                for ln in doc.wrap(str(why), doc.FONT, 7.2, W - 12):
                    doc.text(M + 12, y, ln, font=doc.FONT, size=7.2,
                             rgb=doc.GRAY_DK)
                    y -= 9.0
            y -= 2
        y -= 4

    # -- THE CASES (bull / base / bear). --
    cases = context.get("cases") or {}
    if cases:
        doc.text(M, y, "THE CASES", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
        doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
        doc.text(M + W, y, "tied to the scenario fan above", font=doc.FONT_I,
                 size=6.4, rgb=doc.GRAY_MD, align="right")
        y -= 12
        for label, key in (("Bull", "bull"), ("Base", "base"), ("Bear", "bear")):
            case = cases.get(key) or {}
            narr = case.get("narrative")
            if not (narr or case.get("conditions")):
                continue
            doc.text(M + 6, y, "%s:" % label, font=doc.FONT_B, size=7.6,
                     rgb=doc.INK)
            lw = doc.string_width("%s:" % label, doc.FONT_B, 7.6)
            avail = W - 6 - lw - 4
            narr_lines = doc.wrap(str(narr), doc.FONT, 7.4, avail) if narr else []
            if narr_lines:
                doc.text(M + 6 + lw + 4, y, narr_lines[0], font=doc.FONT,
                         size=7.4, rgb=doc.GRAY_DK)
                y -= 9.6
                for ln in narr_lines[1:]:
                    doc.text(M + 12, y, ln, font=doc.FONT, size=7.4,
                             rgb=doc.GRAY_DK)
                    y -= 9.6
            else:
                y -= 9.6
            conds = case.get("conditions") or []
            if conds:
                cline = "Conditions: " + "; ".join(str(c) for c in conds)
                for ln in doc.wrap(cline, doc.FONT_I, 7.2, W - 12):
                    doc.text(M + 12, y, ln, font=doc.FONT_I, size=7.2,
                             rgb=doc.GRAY_MD)
                    y -= 9.0
            y -= 3
        y -= 3

    # -- RISKS (ARGUED). --
    risks = context.get("risks") or []
    if risks:
        doc.text(M, y, "RISKS (ARGUED)", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
        doc.hairline(M, y - 3, M + W, rgb=doc.GRAY_LT)
        y -= 12
        cols = [M, M + W * 0.34]
        for j, hd in enumerate(("Risk", "Why it matters")):
            doc.text(cols[j], y, hd, font=doc.FONT_B, size=6.6, rgb=doc.GRAY_MD)
        y -= 10
        for r in risks:
            risk_lines = doc.wrap(str(r.get("risk", "")), doc.FONT_B, 7.2,
                                  W * 0.34 - 8)
            why_lines = doc.wrap(str(r.get("why", "")), doc.FONT, 7.2,
                                 W * 0.64)
            rows_h = max(len(risk_lines), len(why_lines), 1)
            for i in range(rows_h):
                if i < len(risk_lines):
                    doc.text(cols[0], y, risk_lines[i], font=doc.FONT_B,
                             size=7.2, rgb=doc.INK)
                if i < len(why_lines):
                    doc.text(cols[1], y, why_lines[i], font=doc.FONT, size=7.2,
                             rgb=doc.GRAY_DK)
                y -= 9.2
            anchor = r.get("anchor")
            if anchor:
                doc.text(cols[1], y, doc.truncate("anchor: %s" % anchor,
                         doc.FONT_I, 6.6, W * 0.64), font=doc.FONT_I, size=6.6,
                         rgb=doc.GRAY_MD)
                y -= 8.4
            y -= 2
        y -= 4

    # -- FINDINGS footnote block (id -> claim -> source). Height-aware: spills to
    # a FINDINGS (continued) page rather than overrunning the footer. --
    findings = context.get("findings") or []
    y = _draw_findings_block(doc, findings, y, page_bottom)
    return y


# --------------------------------------------------------------------------- #
# Deep-entry commentary (fix 4d): a scripted static line printed under the trade
# plan when ANY entry sits >25% below last. The trigger is computed; the line is
# a fixed sentence (no LLM prose).
# --------------------------------------------------------------------------- #

_DEEP_ENTRY_THRESHOLD = 0.25
_DEEP_ENTRY_LINE = (
    "Deep entries reflect structural supports (200-DMA lag after a parabolic "
    "run); they are conditional adds if the thesis survives the decline, not "
    "predictions.")


def _has_deep_entry(docs):
    """True iff any stock-plan entry level sits >25% below the snapshot's last.

    Uses the entry's own ``level`` vs snapshot price.last. Missing last or entries
    -> False (no line). The threshold is strict (>25%, not >=).
    """
    snap = docs.get("snapshot") or {}
    last = (snap.get("price") or {}).get("last")
    if not isinstance(last, (int, float)) or last <= 0:
        return False
    entries = ((docs.get("module_tradeplan") or {}).get("stock_plan") or {}) \
        .get("entries") or []
    for e in entries:
        level = e.get("level")
        if isinstance(level, (int, float)):
            if (last - level) / last > _DEEP_ENTRY_THRESHOLD:
                return True
    return False


def _draw_deep_entry_line(doc, docs, y_top):
    """Print the deep-entry commentary line when the trigger fires. Returns y."""
    if not _has_deep_entry(docs):
        return y_top
    M = doc.MARGIN
    y = y_top
    for ln in doc.wrap(_DEEP_ENTRY_LINE, doc.FONT_I, 7.4, doc.CONTENT_W):
        doc.text(M, y, ln, font=doc.FONT_I, size=7.4, rgb=doc.GRAY_DK)
        y -= 9.6
    return y - 4


# --------------------------------------------------------------------------- #
# Evidence-note resolution (fix: per-dimension EVIDENCE body from slots).
# --------------------------------------------------------------------------- #

def _evidence_note(slots, dim):
    """The ~200-word evidence note for a dimension from pdf_slots, or '' if absent.

    Reads ``slots['evidence_notes'][dim]``. Absent key (older bundles) -> '' so the
    caller falls back to the brief / arithmetic strings (current behavior).
    """
    notes = (slots or {}).get("evidence_notes") or {}
    val = notes.get(dim)
    return str(val).strip() if isinstance(val, str) and val.strip() else ""


def _measure_dimension_height(docs_ticker_asof, footer_bits, bundle, docs, dim,
                              key, chart_names, y_top, slots=None):
    """Consumed height of a dimension section, WITHOUT drawing to the real doc.

    Draws the section onto a throwaway canvas (deterministic, same layout code)
    and returns ``y_top - y_reached``. Used to pack two small sections per page
    while keeping the p N/M footer count exact -- no layout logic is duplicated,
    so the measured height can never drift from the real render. ``slots`` is
    threaded through so the note-as-body path measures identically to the render.
    """
    scratch = Doc(os.devnull, docs_ticker_asof[0], "Detail",
                  docs_ticker_asof[1], footer_bits)
    scratch.total_pages = 1
    scratch.begin_page()
    y_end = _draw_dimension_section(scratch, bundle, docs, dim, key,
                                    chart_names, y_top, slots=slots)
    # Do NOT save -- the scratch canvas is discarded (os.devnull sink).
    return y_top - y_end


def _measure_context_pages(docs_ticker_asof, footer_bits, docs, context, y_top):
    """How many pages the CONTEXT NARRATIVE consumes, WITHOUT drawing to the doc.

    The findings block is height-aware and may spill onto FINDINGS (continued)
    pages, so the context page count is derived from a measurement pass on a
    throwaway canvas (same layout code) -- this keeps the p N/M footer count exact
    even when a long findings registry overflows a single page.
    """
    scratch = Doc(os.devnull, docs_ticker_asof[0], "Detail",
                  docs_ticker_asof[1], footer_bits)
    scratch.total_pages = 1
    scratch.begin_page()
    _draw_context_narrative(scratch, docs, context, y_top)
    # Do NOT save -- scratch canvas discarded. _page_no counts begin_page() calls,
    # which equals the number of context pages actually rendered.
    return scratch._page_no


# The chart-to-section mapping (fix 4a). subscore_breakdown is a grid; here we
# keep only the two per-dimension detail charts that belong under each EVIDENCE
# dimension. Charts that describe RISK (drawdown_history / vol_regime) go under
# the Risk dimension; the OPTIONS charts (vol_term_structure / skew /
# expected_move_cone / oi_walls) are rendered under the OPTIONS section, not a
# dimension (they were previously scattered under technical / sentiment / risk).
_DIM_CHARTS = {
    "technical": ["price_volume"],   # trend context lives on the exec price chart
    "fundamental": ["revisions", "pe_band"],
    "sentiment": [],                 # sentiment charts are exec-side (score/skew)
    "risk": ["drawdown_history", "vol_regime"],
}
# The options section's own charts (fix 4a: these describe implied vol / skew /
# expected move / OI walls — all OPTIONS concepts).
_OPTIONS_CHARTS = ["vol_term_structure", "skew", "expected_move_cone", "oi_walls"]


def render_detail(bundle, docs, slots, diff, prev_date, out_path):
    ticker, as_of = _ticker_as_of(docs)
    doc = Doc(out_path, ticker, "Detail", as_of, _footer_bits(docs))
    context = load_context(bundle)
    context_ok = context_gate_ok(context)

    # Page count is known ahead: 2 exec + WHY-THIS-CALL + context + dimension
    # pages + options + downside + appendix/integrity. Dimension sections are
    # PACKED two-per-page when both fit, so the dimension-page count is derived
    # from a measurement pass, not fixed.
    dim_charts = _DIM_CHARTS
    dim_pages = [
        ("technical", "module_technical"),
        ("fundamental", "module_fundamental"),
        ("sentiment", "module_sentiment"),
        ("risk", "module_risk"),
    ]

    # Page geometry for packing: a section starts at ``top - 12`` (top ==
    # H - MARGIN - 15 from begin_page) and must stay above the footer band.
    page_top = doc.H - doc.MARGIN - 15 - 12
    page_bottom = doc.MARGIN + 24          # keep clear of the footer hairline
    SECTION_GAP = 20                       # separator between stacked sections

    # Measurement pass: pack dimension sections greedily, two per page when the
    # second still fits below the first. ``pages`` is a list of section-lists.
    df = _footer_bits(docs)
    ticker_asof = (ticker, as_of)
    pages, cur, y_avail = [], [], page_top
    for dim, key in dim_pages:
        h = _measure_dimension_height(ticker_asof, df, bundle, docs, dim, key,
                                      dim_charts.get(dim, []), page_top,
                                      slots=slots)
        if cur and (y_avail - SECTION_GAP - h) < page_bottom:
            pages.append(cur)
            cur, y_avail = [], page_top
        cur.append((dim, key, h))
        y_avail -= h + (SECTION_GAP if len(cur) > 1 else 0)
    if cur:
        pages.append(cur)

    # Context page count: 0 without a stamped module (the one-line disclosure
    # rides on the why-this-call page); otherwise >=1, derived from a measurement
    # pass because a long FINDINGS registry spills onto FINDINGS (continued) pages.
    context_top = page_top
    context_pages = (
        _measure_context_pages(ticker_asof, df, docs, context, context_top)
        if context_ok else 0)

    # METHODOLOGY appendix page count: >=1, measured because a long sector-scale
    # falsifier table (or a CUSTOM dual weight table) can spill onto a
    # METHODOLOGY (continued) page.
    methodology_pages = _measure_methodology_pages(ticker_asof, df, bundle, docs,
                                                   page_top)

    # 2 exec + 1 why-this-call (carries the context DISCLOSURE inline when no
    # stamped module) + the measured context page(s) ONLY when the narrative
    # exists (fix 4e: a one-line disclosure never burns a near-empty page) +
    # packed dim pages + 1 options + 1 downside/monitoring + 1 appendix +
    # the measured METHODOLOGY page(s).
    doc.total_pages = 2 + 1 + context_pages + len(pages) + 3 + methodology_pages

    # Exec pages first (repeated). Detail p1 carries the scale-governance banner(s)
    # from the bundle's refresh plan (if present) — the standalone exec doc does not.
    plan = load_refresh_plan(bundle)
    _draw_exec_page1(doc, bundle, docs, slots, diff, prev_date, plan=plan)
    _draw_exec_page2(doc, bundle, docs, slots)

    # WHY THIS CALL page (right after the exec repeat) — the argued case. When no
    # stamped context module exists, the one-line context disclosure rides at the
    # bottom of this page rather than opening a near-empty page of its own.
    top = doc.begin_page()
    y = _draw_why_this_call(doc, docs, top - 12)
    if not context_ok:
        _draw_context_disclosure(doc, y - 24)
    doc.end_page()

    # COMPANY CONTEXT page — only when a stamped module supplies the narrative.
    if context_ok:
        top = doc.begin_page()
        _draw_context_narrative(doc, docs, context, top - 12)
        doc.end_page()

    # Per-dimension pages (packed).
    for page_sections in pages:
        top = doc.begin_page()
        y = top - 12
        for i, (dim, key, h) in enumerate(page_sections):
            if i > 0:
                y -= SECTION_GAP
                doc.hairline(doc.MARGIN, y + SECTION_GAP / 2,
                             doc.MARGIN + doc.CONTENT_W, rgb=doc.GRAY_LT)
            y = _draw_dimension_section(doc, bundle, docs, dim, key,
                                        dim_charts.get(dim, []), y, slots=slots)
        doc.end_page()

    # Options page — the FULL module render + the OPTIONS charts beneath it.
    top = doc.begin_page()
    y = _draw_options_section(doc, bundle, docs, top - 12)
    present = [p for p in (_chart_png(bundle, c) for c in _OPTIONS_CHARTS) if p]
    chart_w = (doc.CONTENT_W - 12) / 2
    col_x = [doc.MARGIN, doc.MARGIN + chart_w + 12]
    y -= 8
    i = 0
    while i < len(present):
        row = present[i:i + 2]
        row_h = 0.0
        for j, p in enumerate(row):
            _, dh = doc.place_image(p, col_x[j], y, chart_w, 120)
            row_h = max(row_h, dh)
        y -= row_h + 8
        i += 2
    doc.end_page()

    # Downside map + monitoring page.
    top = doc.begin_page()
    _draw_downside_monitoring(doc, bundle, docs, top - 12)
    doc.end_page()

    # Appendix + integrity page.
    top = doc.begin_page()
    y = _draw_appendix(doc, bundle, docs, top - 12)
    _draw_integrity_footer_page(doc, docs, y - 20)
    doc.end_page()

    # METHODOLOGY appendix (transparency) — the final page(s). 100% script-generated
    # from the module JSONs + the active scale; height-aware (spills to a
    # METHODOLOGY (continued) page when the scale/weight tables are long).
    top = doc.begin_page()
    _draw_methodology(doc, bundle, docs, top - 12)
    doc.end_page()

    doc.save()
    return out_path


# --------------------------------------------------------------------------- #
# DELTA note (1 page).
# --------------------------------------------------------------------------- #

def _draw_score_delta_bars(doc, x, y_top, w, diff):
    """Horizontal score-delta bars drawn with reportlab primitives.

    Each dimension's Δ is a bar left/right of a zero axis (red down / green up),
    labeled with the signed value. Pure reportlab (no chart PNG needed).
    """
    dims = [(name, blk) for name, blk in diff["dimensions"].items()
            if isinstance(blk.get("delta"), (int, float))]
    doc.text(x, y_top, "SCORE DELTAS", font=doc.FONT_B, size=8.5, rgb=doc.ACCENT)
    doc.hairline(x, y_top - 3.5, x + w, rgb=doc.ACCENT, w=0.6)
    if not dims:
        doc.text(x, y_top - 16, "no comparable dimensions", font=doc.FONT_I,
                 size=7.4, rgb=doc.GRAY_MD)
        return y_top - 24

    labels = {name: _DIM_LABELS.get(name, name.title()) for name, _ in dims}
    # Reserve a label column wide enough for the longest row name so the chart
    # zone (bars + value text) NEVER reaches back into the labels. The bar
    # extent is clamped to this zone -- a full-magnitude down bar stops clear of
    # the labels rather than overprinting them.
    label_w = max(doc.string_width(t, doc.FONT, 7.4) for t in labels.values())
    chart_x0 = x + label_w + 8            # start of the plotting zone
    val_w = 40                            # room reserved for the +/-value text
    zero_x = chart_x0 + (x + w - chart_x0) / 2
    half = min(zero_x - chart_x0, x + w - zero_x) - val_w
    if half < 6:                          # degenerate-narrow column safety net
        half = max(6, (x + w - chart_x0) / 2 - val_w)
    max_abs = max(abs(blk["delta"]) for _, blk in dims) or 1.0
    row_h = 16
    y = y_top - 16
    for name, blk in dims:
        d = blk["delta"]
        doc.text(x, y, labels[name], font=doc.FONT, size=7.4, rgb=doc.INK)
        bar_len = (abs(d) / max_abs) * half
        if d >= 0:
            doc.rect(zero_x, y - 3, bar_len, 6, fill_rgb=doc.GREEN)
            doc.text(zero_x + bar_len + 3, y - 2.5, "+%s" % _fmt(d),
                     font=doc.FONT_B, size=6.8, rgb=doc.GREEN)
        else:
            doc.rect(zero_x - bar_len, y - 3, bar_len, 6, fill_rgb=doc.RED)
            doc.text(zero_x - bar_len - 3, y - 2.5, _fmt(d), font=doc.FONT_B,
                     size=6.8, rgb=doc.RED, align="right")
        y -= row_h
    doc.vline(zero_x, y + row_h - 8, y_top - 14, rgb=doc.GRAY_MD, w=0.7)
    return y


def _draw_what_changed_table(doc, x, y_top, w, diff, prev_date):
    """The full What-Changed table (all tracked metrics) for the delta note."""
    doc.text(x, y_top, "WHAT CHANGED", font=doc.FONT_B, size=8.5, rgb=doc.ACCENT)
    if prev_date:
        doc.text(x + w, y_top, "since %s" % prev_date, font=doc.FONT_I, size=6.6,
                 rgb=doc.GRAY_MD, align="right")
    doc.hairline(x, y_top - 3.5, x + w, rgb=doc.ACCENT, w=0.6)
    cols = [x, x + w * 0.45, x + w * 0.68, x + w]
    y = y_top - 14
    for j, hd in enumerate(("Metric", "Prior", "New", "Δ")):
        doc.text(cols[j], y, hd, font=doc.FONT_B, size=6.8, rgb=doc.GRAY_MD,
                 align="right" if j == 3 else "left")
    y -= 11

    def emit(label, blk, money=False, pct=False):
        nonlocal y
        o, n, d = blk.get("old"), blk.get("new"), blk.get("delta")

        def f(v, lead=""):
            if v is None:
                return "n/a"
            if pct and isinstance(v, (int, float)):
                return "%s%.1f%%" % (lead, v * 100)
            if money and isinstance(v, (int, float)):
                return fmt_money_delta(v, plus=(lead == "+"))
            return "%s%s" % (lead, _fmt(v))
        dc = doc.GRAY_DK
        dtxt = "n/a"
        if isinstance(d, (int, float)):
            dc = doc.RED if d < 0 else doc.GREEN
            dtxt = f(d, "+" if d > 0 else "")
        doc.text(cols[0], y, label, font=doc.FONT, size=7.6, rgb=doc.INK)
        doc.text(cols[1], y, f(o), font=doc.FONT, size=7.6, rgb=doc.GRAY_DK)
        doc.text(cols[2], y, f(n), font=doc.FONT_B, size=7.6, rgb=doc.INK)
        doc.text(cols[3], y, dtxt, font=doc.FONT_B, size=7.6, rgb=dc,
                 align="right")
        y -= 11

    emit("Composite", diff["composite"])
    emit("Grade", {"old": diff["grade"]["old"], "new": diff["grade"]["new"],
                   "delta": None})
    for name in ("technical", "fundamental", "sentiment", "risk"):
        if name in diff["dimensions"]:
            emit(_DIM_LABELS[name], diff["dimensions"][name])
    emit("Entry 1", diff["entry_1"], money=True)
    emit("EV(current)", diff["ev_at_current"], pct=True)

    # Machinery transitions (weight-set / sector-scale) — one row each only when the
    # stamp CHANGED between runs (a governance-visible transition). The old→new
    # string sits in the Prior column, spanning to the New column (it is a label,
    # not a numeric delta), so the Δ column stays "—".
    for label, transition in _transition_rows(diff):
        doc.text(cols[0], y, label, font=doc.FONT, size=7.6, rgb=doc.INK)
        doc.text(cols[1], y, doc.truncate(transition, doc.FONT, 7.6,
                 cols[3] - cols[1]), font=doc.FONT_B, size=7.6, rgb=doc.ACCENT)
        doc.text(cols[3], y, "—", font=doc.FONT, size=7.6, rgb=doc.GRAY_MD,
                 align="right")
        y -= 11
    return y


def _invalidation_status_line(diff):
    """Whether both invalidation legs are unchanged (structurally intact) text."""
    inv = diff["invalidation"]
    tech = inv["technical"]
    fund = inv["fundamental"]
    tech_same = tech["old"] == tech["new"]
    fund_same = fund["old"] == fund["new"]
    if tech_same and fund_same:
        return ("Invalidation: both legs UNCHANGED — technical stop %s, "
                "fundamental leg carried." % fmt_price(tech["new"]))
    parts = []
    if not tech_same:
        parts.append("technical stop %s → %s" % (fmt_price(tech["old"]),
                                                      fmt_price(tech["new"])))
    if not fund_same:
        parts.append("fundamental leg revised")
    return "Invalidation: " + "; ".join(parts) + "."


def render_delta(bundle, docs, slots, diff, prev_date, out_path):
    ticker, as_of = _ticker_as_of(docs)
    doc = Doc(out_path, ticker, "Delta Note", as_of, _footer_bits(docs))
    doc.total_pages = 1
    top = doc.begin_page()
    M = doc.MARGIN
    gutter = 16
    col_w = (doc.CONTENT_W - gutter) / 2

    # Scale-governance banner(s) from the bundle's refresh plan, at the top of the
    # note (below the masthead). Shifts the body down when it renders.
    plan = load_refresh_plan(bundle)
    top = _draw_scale_banners(doc, plan, top - 2) + 2 if plan else top

    # What-Changed table (left) + score-delta bars (right).
    tbl_bottom = _draw_what_changed_table(doc, M, top - 14, col_w, diff, prev_date)
    bars_bottom = _draw_score_delta_bars(doc, M + col_w + gutter, top - 14,
                                         col_w, diff)

    # Invalidation status line (full width).
    y = min(tbl_bottom, bars_bottom) - 18
    doc.hairline(M, y + 8, M + doc.CONTENT_W, rgb=doc.GRAY_LT)
    for ln in doc.wrap(_invalidation_status_line(diff), doc.FONT_B, 8.4,
                       doc.CONTENT_W):
        doc.text(M, y, ln, font=doc.FONT_B, size=8.4, rgb=doc.INK)
        y -= 12
    y -= 8

    # Delta interpretation slot (LLM prose, gated).
    interp = slots.get("delta_interpretation")
    doc.section_head(M, y, "INTERPRETATION", w=doc.CONTENT_W)
    y -= 15
    if interp:
        for para in str(interp).split("\n\n"):
            for ln in doc.wrap(" ".join(para.split()), doc.FONT, 8, doc.CONTENT_W):
                doc.text(M, y, ln, font=doc.FONT, size=8, rgb=doc.GRAY_DK)
                y -= 11
            y -= 4
    else:
        doc.text(M, y, "No interpretation authored for this delta.",
                 font=doc.FONT_I, size=7.6, rgb=doc.GRAY_MD)

    doc.end_page()
    doc.save()
    return out_path


# --------------------------------------------------------------------------- #
# Output path resolution (mirrors render_report._output_dir / _default_out).
# --------------------------------------------------------------------------- #

_DOC_KINDS = {"exec": "Trade_Report", "detail": "Detail", "delta": "Delta_Note"}


def _default_out(bundle, docs, doc_kind):
    ticker, as_of = _ticker_as_of(docs)
    date = (as_of or "")[:10] or "undated"
    kind = _DOC_KINDS[doc_kind]
    out_dir = render_report._output_dir(bundle)
    return os.path.join(out_dir, "%s_%s_%s.pdf" % (ticker, kind, date))


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render the trading-desk docket (exec 2pp / detail ~10-15pp / "
                    "delta 1pp) deterministically from a QC'd bundle. Every number "
                    "is script-minted; LLM prose comes only from the gated "
                    "pdf_slots.json. Runs inside the render venv (matplotlib + "
                    "reportlab); exits 3 with a fix line when those are absent.")
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--doc", required=True, choices=["exec", "detail", "delta"],
                        help="which docket document to render")
    parser.add_argument("--previous", default=None,
                        help="the older bundle dir (exec/detail: What-Changed box; "
                             "delta: required comparison base)")
    parser.add_argument("--out", default=None,
                        help="output path (default derived under the detail_reports "
                             "rule)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundle):
        print("ERROR: bundle directory not found: %s" % args.bundle,
              file=sys.stderr)
        return 1

    docs = load_docs(args.bundle)
    if docs.get("snapshot") is None:
        print("ERROR: no snapshot in bundle: %s" % args.bundle, file=sys.stderr)
        return 1

    slots = load_slots(args.bundle)

    # Delta requires a previous bundle.
    if args.doc == "delta" and not (args.previous and os.path.isdir(args.previous)):
        print("ERROR: --doc delta requires --previous <older bundle dir>",
              file=sys.stderr)
        return 1

    # SLOTS GATE (exec/detail): refuse unless pdf_slots.json carries qc_passed.
    if args.doc in ("exec", "detail") and not slots_gate_ok(args.bundle):
        print("REFUSED: pdf_slots.json is missing or not gated (qc_passed absent).\n"
              "Run the slots provenance gate first:\n"
              "    python3 scripts/report_qc.py --pdf-slots %s --bundle %s\n"
              "which stamps qc_passed=true on pass; then re-run this renderer."
              % (_slots_path(args.bundle), args.bundle))
        return 2

    # Previous bundle + diff (What-Changed / delta comparison).
    diff = None
    prev_date = None
    if args.previous and os.path.isdir(args.previous):
        prev_docs = load_docs(args.previous)
        diff = diff_bundles(prev_docs, docs)
        prev_meta = (prev_docs.get("snapshot") or {}).get("meta", {}) or {}
        prev_date = (prev_meta.get("as_of_utc", "") or "")[:10] or None

    out_path = args.out or _default_out(args.bundle, docs, args.doc)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    try:
        if args.doc == "exec":
            render_exec(args.bundle, docs, slots, diff, prev_date, out_path)
        elif args.doc == "detail":
            render_detail(args.bundle, docs, slots, diff, prev_date, out_path)
        else:
            render_delta(args.bundle, docs, slots, diff, prev_date, out_path)
    except RuntimeError as exc:  # reportlab/matplotlib absent -> actionable exit 3
        print(str(exc), file=sys.stderr)
        return 3

    print("wrote %s (%s)" % (out_path, args.doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
