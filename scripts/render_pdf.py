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
    """Money string with the sign OUTSIDE the dollar sign.

    -182.44 -> '-$182.44' (not '$-182.44'); 5.0 -> '$5'; with plus=True a
    positive value gains a leading '+' ('+$5') for signed delta columns. Zero
    is never signed. PURE (formatting only) so it is unit-tested directly.
    """
    if v is None or not isinstance(v, (int, float)) or isinstance(v, bool):
        return "n/a"
    if v < 0:
        return "-$%s" % _fmt(-v)
    sign = "+" if (plus and v > 0) else ""
    return "%s$%s" % (sign, _fmt(v))


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

    return {
        "composite": composite, "grade": grade, "dimensions": dims,
        "entry_1": entry_1, "ev_at_current": ev_at_current,
        "invalidation": invalidation,
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
        foot = ("Source: verified snapshot %s  ·  QC %s  ·  "
                "rubrics %s  ·  %s  ·  p %d/%d" % (
                    fb.get("snapshot_date", "?"), fb.get("qc", "?"),
                    fb.get("rubrics", "?"), _DISCLAIMER_SHORT,
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

    return {"snapshot_date": date, "qc": counts, "rubrics": rubric,
            "as_of": as_of}


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
    doc.text(x + 8, y_top - 15, doc.truncate("%s · %s" % (grade, action),
             doc.FONT_B, 12, w - 12), font=doc.FONT_B, size=12, rgb=doc.WHITE)
    doc.text(x + 8, y_top - 28, "Composite %s/100 · %s" % (
        _fmt(score), profile), font=doc.FONT, size=7.6, rgb=light)
    if ev_cur is not None:
        hz = " vs hurdle %+.1f%%" % (hurdle * 100) if hurdle is not None else ""
        doc.text(x + 8, y_top - 39, "EV(current) %+.1f%%%s" % (ev_cur * 100, hz),
                 font=doc.FONT, size=7.6, rgb=light)
    if breakeven is not None:
        doc.text(x + 8, y_top - 50, "Breakeven entry $%s" % _fmt(breakeven),
                 font=doc.FONT, size=7.6, rgb=light)
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
        return "$%s" % _fmt(v)

    def pct(v):
        return "%.1f%%" % (v * 100) if isinstance(v, (int, float)) else "n/a"

    rows = [
        ("Mkt cap", money(price.get("mktcap_computed"))),
        ("52wk hi / lo", "%s / %s" % (_fmt(price.get("wk52_high")),
                                      _fmt(price.get("wk52_low")))),
        ("ADV (3m)", money(price.get("adv_dollar_3m"))),
        ("Beta", _fmt(bench.get("beta"))),
        ("Realized vol 30", pct(tech.get("rv30_ann"))),
        ("Short interest", pct((sent.get("short_interest_pct") or 0) / 100)
         if sent.get("short_interest_pct") is not None else "n/a"),
        ("P/E ttm", _fmt(val.get("pe_ttm"))),
        ("P/E fwd", _fmt(val.get("pe_fwd"))),
        ("PEG", _fmt(val.get("peg"))),
        ("FCF yield", pct(val.get("fcf_yield"))),
        ("EPS ttm", _fmt(fund.get("eps_ttm"))),
        ("IV percentile", _fmt(sent.get("iv_pctile_1yr"))),
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
            _fmt(dc.get("above")), dc.get("convention", "")), ""))
    for i, e in enumerate(sp.get("entries", []) or [], start=1):
        ev = e.get("ev_at_level")
        ev_s = ("%+.1f%%" % (ev * 100)) if isinstance(ev, (int, float)) else ""
        rows.append(("Entry %d" % i, "%s · %s" % (
            _fmt(e.get("level")), e.get("condition", "")), ev_s))
    exits = sp.get("exits", {}) or {}
    pt = exits.get("profit_take") or {}
    if pt:
        rows.append(("Profit-take", "%s (%s)" % (_fmt(pt.get("level")),
                     pt.get("type", "")), ""))
    bt = exits.get("bull_target") or {}
    if bt:
        note = " · %s" % bt.get("note") if bt.get("note") else ""
        rows.append(("Bull target", "%s%s" % (_fmt(bt.get("level")), note), ""))
    inv = sp.get("invalidation", {}) or {}
    tl = inv.get("technical_leg") or {}
    fl = inv.get("fundamental_leg") or {}
    rows.append(("Invalidation", "%s %s; %s %s" % (
        tl.get("condition", ""), _fmt(tl.get("level")),
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


def _draw_exec_page1(doc, bundle, docs, slots, diff, prev_date):
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

    # Header band.
    hb_top = top - 10
    doc.text(M, hb_top - 26, doc.ticker, font=doc.FONT_B, size=34, rgb=doc.ACCENT)
    tk_w = doc.string_width(doc.ticker, doc.FONT_B, 34)
    last = price.get("last")
    prev = price.get("prev_close")
    doc.text(M + tk_w + 12, hb_top - 20, "$%s" % _fmt(last), font=doc.FONT_B,
             size=12, rgb=doc.INK)
    if last is not None and prev:
        chg = (last / prev - 1) * 100
        pw = doc.string_width("$%s" % _fmt(last), doc.FONT_B, 12)
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
    _trade_plan_table(doc, M, tp_top, main_w, tp)

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

def _draw_dimension_section(doc, bundle, docs, dim, module_key, chart_names, y_top):
    """One per-dimension section: brief prose + subscore table + charts.

    Returns the y reached; the caller starts a new page when it runs low.
    """
    M = doc.MARGIN
    label = _DIM_LABELS.get(dim, dim.title())
    module = docs.get(module_key) or {}
    doc.section_head(M, y_top, "EVIDENCE — %s (score %s)" % (
        label, _fmt(module.get("score"))), w=doc.CONTENT_W)

    y = y_top - 16
    brief = _load_brief(bundle, dim)
    text_w = doc.CONTENT_W * 0.58
    if brief:
        # Render the brief prose paragraphs (wrapped) in the left column.
        for para in brief.split("\n\n"):
            para = " ".join(para.split())
            if not para:
                continue
            for ln in doc.wrap(para, doc.FONT, 8, text_w):
                doc.text(M, y, ln, font=doc.FONT, size=8, rgb=doc.GRAY_DK)
                y -= 10.5
            y -= 4
    else:
        # No brief -> show the module's scored rationale (arithmetic strings).
        note = module.get("renormalization_note")
        if note:
            for ln in doc.wrap(str(note), doc.FONT_I, 7.6, text_w):
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
    _subscore_table(doc, st_x, y_top - 16, st_w, module, "SUBSCORES")

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
    """Options section: verdict line, recommended/declined tables, hedge."""
    M = doc.MARGIN
    opt = docs.get("module_options") or {}
    vd = opt.get("vol_dashboard", {}) or {}
    doc.section_head(M, y_top, "OPTIONS & VOLATILITY", w=doc.CONTENT_W)
    y = y_top - 15

    verdict = vd.get("verdict", "n/a")
    iv30, rv20 = vd.get("iv30"), vd.get("rv20")
    vline = "Vol verdict: %s" % str(verdict).replace("_", " ")
    if iv30 is not None and rv20 is not None:
        vline += "  —  IV30 %.0f%% vs RV20 %.0f%%" % (iv30 * 100, rv20 * 100)
    doc.text(M, y, vline, font=doc.FONT_B, size=8.4, rgb=doc.INK)
    y -= 16

    # Recommended structures table.
    rec = opt.get("recommended_structures") or []
    doc.text(M, y, "Recommended structures", font=doc.FONT_B, size=8,
             rgb=doc.ACCENT)
    doc.hairline(M, y - 3, M + doc.CONTENT_W, rgb=doc.GRAY_LT)
    y -= 12
    cols = [M, M + 150, M + 230, M + 320, M + 400]
    for j, hd in enumerate(("Structure", "Strikes", "Net", "PoP", "PoP method")):
        doc.text(cols[j], y, hd, font=doc.FONT_B, size=6.6, rgb=doc.GRAY_MD)
    y -= 10
    for st in rec:
        strikes = "/".join(_fmt(s) for s in (st.get("strikes") or []))
        net = st.get("net_credit") or st.get("net_debit")
        pop = st.get("pop")
        pop_s = "%.0f%%" % (pop * 100) if isinstance(pop, (int, float)) else "n/a"
        pm = str(st.get("pop_method", "")).split("(")[0].strip()
        doc.text(cols[0], y, doc.truncate(st.get("name", ""), doc.FONT, 7.4, 145),
                 font=doc.FONT, size=7.4, rgb=doc.INK)
        doc.text(cols[1], y, strikes, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
        doc.text(cols[2], y, _fmt(net), font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
        doc.text(cols[3], y, pop_s, font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
        doc.text(cols[4], y, doc.truncate(pm, doc.FONT, 7.4,
                 doc.CONTENT_W - 400 + M - 4), font=doc.FONT, size=7.4,
                 rgb=doc.GRAY_DK)
        y -= 11
    y -= 6

    # Declined table.
    declined = opt.get("declined") or []
    if declined:
        doc.text(M, y, "Declined", font=doc.FONT_B, size=8, rgb=doc.ACCENT)
        doc.hairline(M, y - 3, M + doc.CONTENT_W, rgb=doc.GRAY_LT)
        y -= 12
        for d in declined:
            doc.text(M, y, doc.truncate(d.get("name", ""), doc.FONT_B, 7.4, 140),
                     font=doc.FONT_B, size=7.4, rgb=doc.INK)
            doc.text(M + 150, y, doc.truncate(d.get("reason", ""), doc.FONT, 7.4,
                     doc.CONTENT_W - 150), font=doc.FONT, size=7.4, rgb=doc.GRAY_DK)
            y -= 11
        y -= 6

    # Hedge line.
    hedge = opt.get("hedge_structure")
    if isinstance(hedge, dict):
        cost = hedge.get("cost")
        cpp = hedge.get("cost_pct_of_spot")
        htxt = "Hedge: %s" % hedge.get("type", "n/a")
        if cost is not None:
            htxt += " · cost %s" % _fmt(cost)
        if isinstance(cpp, (int, float)):
            htxt += " (%.1f%% of spot)" % (cpp * 100)
        doc.text(M, y, htxt, font=doc.FONT_I, size=7.6, rgb=doc.GRAY_DK)
        y -= 12
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
        "Technical stop: %s %s" % (tl.get("condition", ""), _fmt(tl.get("level"))),
        "Fundamental leg: %s %s" % (fl.get("metric", ""), fl.get("threshold", "")),
    ):
        for ln in doc.wrap(line, doc.FONT, 7.6, doc.CONTENT_W * 0.5 - 20):
            doc.text(mx, my, ln, font=doc.FONT, size=7.6, rgb=doc.GRAY_DK)
            my -= 10
        my -= 3
    return min(y - 160, my)


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


def _measure_dimension_height(docs_ticker_asof, footer_bits, bundle, docs, dim,
                              key, chart_names, y_top):
    """Consumed height of a dimension section, WITHOUT drawing to the real doc.

    Draws the section onto a throwaway canvas (deterministic, same layout code)
    and returns ``y_top - y_reached``. Used to pack two small sections per page
    while keeping the p N/M footer count exact -- no layout logic is duplicated,
    so the measured height can never drift from the real render.
    """
    scratch = Doc(os.devnull, docs_ticker_asof[0], "Detail",
                  docs_ticker_asof[1], footer_bits)
    scratch.total_pages = 1
    scratch.begin_page()
    y_end = _draw_dimension_section(scratch, bundle, docs, dim, key,
                                    chart_names, y_top)
    # Do NOT save -- the scratch canvas is discarded (os.devnull sink).
    return y_top - y_end


def render_detail(bundle, docs, slots, diff, prev_date, out_path):
    ticker, as_of = _ticker_as_of(docs)
    doc = Doc(out_path, ticker, "Detail", as_of, _footer_bits(docs))

    # Page count is known ahead: 2 exec + dimension pages + options + downside +
    # appendix/integrity. Dimension sections are PACKED two-per-page when both
    # fit (they are typically ~40-50% of a page -- review: excess whitespace),
    # so the dimension-page count is derived from a measurement pass, not fixed.
    #
    # Dimension sections -> their detail charts.
    dim_charts = {
        "technical": ["drawdown_history", "vol_regime"],
        "fundamental": ["revisions", "pe_band"],
        "sentiment": ["vol_term_structure", "skew"],
        "risk": ["expected_move_cone", "oi_walls"],
    }
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
                                      dim_charts.get(dim, []), page_top)
        if cur and (y_avail - SECTION_GAP - h) < page_bottom:
            pages.append(cur)
            cur, y_avail = [], page_top
        cur.append((dim, key, h))
        y_avail -= h + (SECTION_GAP if len(cur) > 1 else 0)
    if cur:
        pages.append(cur)

    # 2 exec + packed dim pages + 1 options + 1 downside/monitoring + 1 appendix.
    doc.total_pages = 2 + len(pages) + 3

    # Exec pages first (repeated).
    _draw_exec_page1(doc, bundle, docs, slots, diff, prev_date)
    _draw_exec_page2(doc, bundle, docs, slots)

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
                                        dim_charts.get(dim, []), y)
        doc.end_page()

    # Options page.
    top = doc.begin_page()
    _draw_options_section(doc, bundle, docs, top - 12)
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
                "fundamental leg carried." % _fmt(tech["new"]))
    parts = []
    if not tech_same:
        parts.append("technical stop %s → %s" % (_fmt(tech["old"]),
                                                      _fmt(tech["new"])))
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
