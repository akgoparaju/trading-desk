"""Tests for scripts/render_pdf.py + report_qc.py --pdf-slots -- the docket layer.

WHY: the docket (exec 2pp / detail ~10-15pp / delta 1pp) is the PDF render of the
already-QC'd bundle. Two invariants are load-bearing and tested here:

  1. NO LLM ARITHMETIC. The What-Changed box is computed script-side by
     ``diff_bundles(prev_docs, new_docs)`` -- a PURE function over the two bundles'
     module JSONs (same source as the md delta). Its exact old/new/Δ values are
     asserted against fixture bundles so a mis-wired diff is a test failure.

  2. THE SLOTS GATE CANNOT BE BYPASSED. ``report_qc.py --pdf-slots`` runs
     number_provenance over every LLM-authored prose slot with the SAME allowed-set
     machinery as the report gate; a fabricated number in a slot orphans (exit 1).
     On PASS it stamps ``{"qc_passed": true, "checked_utc": ...}`` INTO the slots
     file. ``render_pdf.py`` REFUSES to render exec/detail unless that stamp is
     present (exit 2 with the fix command), so an un-gated slots file can never be
     embedded.

The render smoke tests (exec 2pp, detail full docket >=6pp with packed dimension
pages, delta 1pp, footer strings) require
reportlab + matplotlib and are guarded by ``skipUnless(find_spec(...))`` so the base
suite stays green without the render venv. Page counts are read from the PDF bytes
(``/Type /Pages ... /Count N``) -- no pypdf dependency.

The fixture builders are REUSED from tests/test_report_renderer.py (the same minimal
bundle the report layer tests), so the docket is exercised against the identical
shapes the whole pipeline emits.

stdlib-only for the pure tests; unittest.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from scripts import render_pdf as rp
from scripts import report_qc as rq

# Reuse the report-layer fixture bundle builders (identical module shapes).
from tests.test_report_renderer import (
    _mk_bundle, _composite_doc, _tradeplan_doc, _snapshot_doc,
    _fundamental_doc, _LAST, _EV_AT_CURRENT,
)

# Convention constants the METHODOLOGY page PINS by import — the tests assert the
# assembled page reflects THESE exact values (a drift is a test failure).
from scripts import score_composite as _sc, score_fundamental as _sf


_HAS_MPL = importlib.util.find_spec("matplotlib") is not None
_HAS_RL = importlib.util.find_spec("reportlab") is not None
_CAN_RENDER = _HAS_MPL and _HAS_RL

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDER_PDF = os.path.join(_REPO_ROOT, "scripts", "render_pdf.py")
QC = os.path.join(_REPO_ROOT, "scripts", "report_qc.py")


# --------------------------------------------------------------------------- #
# Minimal, bundle-cited slots (every number traces to the fixture bundle).
#   last = 100; entries 95 / 90; composite 59.4; ev_at_current 0.175 (=17.5%);
#   revisions +8/-1 are NOT in the fixture, so slots avoid them.
# --------------------------------------------------------------------------- #

def _clean_slots():
    """pdf_slots.json whose every number is a fixture-bundle leaf."""
    return {
        "thesis_bullets": [
            "Support intact — price 100 holds above the 95 entry shelf.",
            "Post-dip entry — EV clears the hurdle only at 95 or below.",
            "Falsifier — a weekly close below 82 breaks the technical leg.",
        ],
        "desk_read": {
            "setup": "Constructive dip into the 95 support; do not chase.",
            "edge": "EV improves at 90; patience is the edge here.",
            "trigger": "Rest a limit at 95; add on a reclaim of 90.",
            "risk": "Trend cracks below the 82 stop level.",
        },
        "positioning": {
            "entry_discipline": "Half at the 95 limit, half reserved for 90.",
            "sizing_kelly": "Target 4.0% of book on a quarter-Kelly frame.",
            "path_dependency": "Bull case needs the print to confirm; bear below 82.",
            "monitoring": "Watch the 82 stop and the 95 support shelf.",
        },
        "delta_interpretation": None,
    }


def _clean_slots_with_notes():
    """_clean_slots plus an evidence_notes map whose numbers are fixture leaves.

    Every number cited (100 / 95 / 90 / 82) is a fixture-bundle leaf, so the
    slots gate PASSES; a fabricated number in a note (tested separately) orphans.
    """
    slots = _clean_slots()
    slots["evidence_notes"] = {
        "technical": ("Trend structure holds above the 95 support shelf; "
                      "momentum is constructive but not extended into the print."),
        "fundamental": "Valuation sits below the 5-year median; growth intact.",
        "sentiment": "Street constructive; positioning is not crowded.",
        "risk": "Realized vol elevated; the downside is anchored at 82.",
        "options": "IV is cheap vs realized; defined-risk is favored into 90.",
    }
    return slots


def _context_doc(stamped=True):
    """A minimal, gate-shaped module_context.json for the docket tests.

    Numbers in the prose are kept to fixture-bundle leaves (100/95/90) so the
    context gate would pass; ``stamped`` controls whether the qc attestation is
    written (a stamped module renders the CONTEXT NARRATIVE; an unstamped one is
    omitted with a disclosure line).
    """
    mod = {
        "skill": "company-context", "version": "1.0.0", "ticker": "MU",
        "as_of": "2026-07-16", "mode": "web_compressed",
        "business": {
            "what_they_sell": "Memory and storage semiconductors.",
            "revenue_drivers": ["DRAM pricing", "HBM ramp"],
            "segments": ["Compute & Networking", "Mobile"],
        },
        "competitive": {
            "position": "A scale cost leader (C1).",
            "moat_evidence": ["cost-curve lead"],
            "competitors": ["Samsung", "SK Hynix"],
        },
        "live_tape": [
            {"date": "2026-07-15", "event": "HBM qualification headline",
             "why_it_matters": "validates the ramp (C2)"},
        ],
        "cases": {
            "bull": {"narrative": "The HBM ramp drives margins (C1).",
                     "conditions": ["revisions stay positive"]},
            "base": {"narrative": "Consolidation below the shelf.",
                     "conditions": ["support at 95 holds"]},
            "bear": {"narrative": "A DRAM-oversupply air-pocket.",
                     "conditions": ["pricing rolls over"]},
        },
        "risks": [
            {"risk": "DRAM oversupply", "why": "cyclical pricing risk",
             "anchor": "https://example.com/dram"},
        ],
        "findings": [
            {"id": "C1", "claim": "Scale cost leader", "source": "coverage/research.md"},
            {"id": "C2", "claim": "HBM qualified", "source": "https://example.com/hbm"},
        ],
        "qc": ({"qc_passed": True, "checked_utc": "2026-07-16T00:00:00Z"}
               if stamped else None),
    }
    return mod


def _context_doc_many_findings(n=20):
    """A stamped context module whose FINDINGS registry is long enough to spill.

    Each finding carries a long, multi-line claim so that ``n`` of them cannot fit
    in the footnote band of a single page -- the height-aware block must continue
    on a FINDINGS (continued) page. Also gives the first live_tape entry a very
    long title to exercise word-boundary wrapping (no mid-word 'ami…' truncation).
    """
    mod = _context_doc(stamped=True)
    long_claim = (
        "This is a deliberately long finding claim engineered so that each "
        "individual finding wraps across roughly three measured lines, "
        "guaranteeing that a registry of twenty such findings comfortably "
        "exceeds the footnote band of any single page and must therefore "
        "continue on a dedicated FINDINGS continuation page without ever "
        "colliding with or overrunning the contract-pinned page footer band")
    mod["findings"] = [
        {"id": "C%d" % i, "claim": "%s (item %d)." % (long_claim, i),
         "source": "coverage/research.md §Section-%d / snapshot leaf %d" % (i, i)}
        for i in range(1, n + 1)
    ]
    mod["live_tape"] = [
        {"date": "2026-07-15",
         "event": ("Second consecutive down day: MU fell another -5.65% "
                   "(904.28 to 853.20), extending the prior drop to roughly "
                   "-13% over two sessions amid a semis-wide selloff (C1)"),
         "why_it_matters": "momentum break confirms the risk-off tape (C1)"},
    ]
    return mod


def _write_context(bundle, module):
    path = os.path.join(bundle, "module_context.json")
    with open(path, "w") as fh:
        json.dump(module, fh)
    return path


# --------------------------------------------------------------------------- #
# METHODOLOGY fixtures: anchored fundamental / custom composite / a scale JSON /
# a refresh plan (for banners). All numbers are self-contained fixture leaves.
# --------------------------------------------------------------------------- #

def _fundamental_anchored():
    """A v1.2 anchored-mode fundamental doc: valuation subscore carries
    valuation_mode 'anchored_v1.2', a sector_scale stamp, and a peg_display block."""
    fund = _fundamental_doc()
    fund["rubric_version"] = "1.2.0"
    fund["subscores"] = [
        {"name": "quality", "points": 30, "max": 50, "arithmetic": "q"},
        {"name": "valuation", "points": 40, "max": 50,
         "valuation_mode": "anchored_v1.2", "arithmetic": "v"},
    ]
    fund["sector_scale"] = "memory_semis@2026.1"
    fund["peg_display"] = {
        "value": 0.8,
        "note": ("display-only; excluded from scoring (institutional practice; "
                 "unreliable for cyclicals)"),
    }
    return fund


def _composite_custom():
    """A composite doc scored under a CUSTOM weight set (long-term profile)."""
    comp = _composite_doc(profile="long-term")
    comp["weight_set"] = "CUSTOM deep-value@1.0"
    # long-term custom column (sums to 1.0) recorded on the dimensions rows.
    custom = {"technical": 0.05, "fundamental": 0.50, "sentiment": 0.10,
              "risk": 0.15, "thesis_conviction": 0.20}
    for d in comp["dimensions"]:
        d["weight"] = custom[d["name"]]
    return comp


def _memory_semis_scale():
    """A valid justified_pb sector scale (mirrors the sector_scales test fixture)."""
    return {
        "scale": "memory_semis", "name": "Memory Semis", "version": "2026.1",
        "effective": "2026-07-01",
        "basis": "Gordon residual-income justified P/B for a mid-cycle DRAM name.",
        "formula": "justified_pb",
        "parameters": {"roe_normalized": 0.35, "r": 0.12, "g": 0.04},
        "evidence": ["C1", "C3"],
        "falsifiers": [
            {"metric": "fundamentals.roe", "op": "<", "value": 0.10,
             "consecutive_quarters": 2,
             "meaning": "structural ROE collapse below cost of equity"},
        ],
        "prior": {"version": "2025.4"},
    }


def _write_scale(bundle, scale):
    """Write a scale under <bundle>/trading_desk_config/scales/<scale>.json."""
    d = os.path.join(bundle, "trading_desk_config", "scales")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.json" % scale["scale"])
    with open(path, "w") as fh:
        json.dump(scale, fh)
    return path


def _write_refresh_plan(ticker_dir, plan):
    """Write refresh_plan.json to the TICKER dir (bundle parent)."""
    path = os.path.join(ticker_dir, "refresh_plan.json")
    with open(path, "w") as fh:
        json.dump(plan, fh)
    return path


def _write_slots(bundle, slots):
    path = os.path.join(bundle, "pdf_slots.json")
    with open(path, "w") as fh:
        json.dump(slots, fh)
    return path


def _qc_slots(bundle, slots_path, extra=None):
    proc = subprocess.run(
        [sys.executable, QC, "--pdf-slots", slots_path, "--bundle", bundle]
        + (extra or []),
        capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _render(bundle, doc, extra=None):
    argv = ["--bundle", bundle, "--doc", doc] + (extra or [])
    py = sys.executable
    proc = subprocess.run([py, RENDER_PDF] + argv, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _pdf_page_count(path):
    """Page count from PDF bytes without pypdf.

    Prefer the page-tree ``/Type /Pages ... /Count N``; fall back to counting
    ``/Type /Page`` objects (each page is one) if the tree form is absent.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    m = re.search(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", data, re.DOTALL)
    if m:
        return int(m.group(1))
    m = re.search(rb"/Count\s+(\d+)\s*/Type\s*/Pages\b", data, re.DOTALL)
    if m:
        return int(m.group(1))
    # Fallback: count leaf page objects.
    return len(re.findall(rb"/Type\s*/Page\b(?!s)", data))


def _pdf_text(path):
    """Best-effort decompressed text from a PDF's content streams.

    reportlab Flate-compresses page content streams, so raw-byte matching fails.
    We locate each stream via its ``/Length N`` dict entry (the correct PDF way to
    bound a stream, robust against the literal 'stream' bytes appearing inside
    image data), zlib-inflate it, and pull the strings shown by ``(...) Tj`` and
    ``[...] TJ`` text operators. Returns the concatenated visible text.
    """
    import base64
    import zlib
    with open(path, "rb") as fh:
        data = fh.read()
    out = []
    # Each stream object dict '<< ... >>' precedes 'stream\r?\n<bytes>endstream'.
    # reportlab encodes page content as ASCII85Decode + FlateDecode, so we honor
    # the /Filter chain (a85 then inflate) before pulling text operators.
    for m in re.finditer(rb"(<<.*?>>)\s*stream\r?\n", data, re.DOTALL):
        dict_bytes = m.group(1)
        lm = re.search(rb"/Length\s+(\d+)", dict_bytes)
        if not lm:
            continue
        length = int(lm.group(1))
        start = m.end()
        blob = data[start:start + length]
        try:
            if b"ASCII85Decode" in dict_bytes:
                a85 = blob.rstrip(b"\r\n")
                if a85.endswith(b"~>"):
                    a85 = a85[:-2]
                blob = base64.a85decode(a85)
            if b"FlateDecode" in dict_bytes:
                blob = zlib.decompress(blob)
        except (zlib.error, ValueError):
            continue
        # (literal string) Tj  -- unescape the common PDF escapes.
        for tok in re.findall(rb"\((?:[^()\\]|\\.)*\)", blob):
            s = tok[1:-1]
            s = s.replace(rb"\(", b"(").replace(rb"\)", b")").replace(rb"\\", b"\\")
            out.append(s)
    return b" ".join(out)


# --------------------------------------------------------------------------- #
# diff_bundles -- EXACT old/new/Δ on two fixture bundles (pure function).
# --------------------------------------------------------------------------- #

class TestDiffBundles(unittest.TestCase):
    def _two_bundles(self):
        """Return (old_docs, new_docs) with a known, computable delta.

        old: composite 59.4 (Σ contributions), technical 70, entry_1 95, ev 0.175.
        new: technical 90 (contribution 22.5 -> composite 65.0), entry_1 92.
        """
        old_comp = _composite_doc()
        new_comp = _composite_doc()
        new_comp["score"] = 65.0
        new_comp["grade"] = "B"
        new_comp["dimensions"][0]["score"] = 90
        new_comp["dimensions"][0]["contribution"] = 22.5
        new_comp["ev"]["ev_at_current"] = 0.20

        old_tp = _tradeplan_doc(entry1=95.0)
        new_tp = _tradeplan_doc(entry1=92.0)

        old_docs = {"module_composite": old_comp, "module_tradeplan": old_tp}
        new_docs = {"module_composite": new_comp, "module_tradeplan": new_tp}
        return old_docs, new_docs

    def test_composite_old_new_delta(self):
        old, new = self._two_bundles()
        d = rp.diff_bundles(old, new)
        self.assertEqual(d["composite"]["old"], 59.4)
        self.assertEqual(d["composite"]["new"], 65.0)
        self.assertAlmostEqual(d["composite"]["delta"], 5.6, places=4)

    def test_grade_old_new(self):
        old, new = self._two_bundles()
        d = rp.diff_bundles(old, new)
        # old composite 59.4 -> grade C in the fixture; new 65 -> B.
        self.assertEqual(d["grade"]["old"], "C")
        self.assertEqual(d["grade"]["new"], "B")

    def test_per_dimension_scores(self):
        old, new = self._two_bundles()
        d = rp.diff_bundles(old, new)
        tech = d["dimensions"]["technical"]
        self.assertEqual(tech["old"], 70)
        self.assertEqual(tech["new"], 90)
        self.assertEqual(tech["delta"], 20)
        # an unchanged dimension has delta 0.
        self.assertEqual(d["dimensions"]["risk"]["delta"], 0)

    def test_entry_1_old_new_delta(self):
        old, new = self._two_bundles()
        d = rp.diff_bundles(old, new)
        self.assertEqual(d["entry_1"]["old"], 95.0)
        self.assertEqual(d["entry_1"]["new"], 92.0)
        self.assertAlmostEqual(d["entry_1"]["delta"], -3.0, places=4)

    def test_ev_at_current_old_new_delta(self):
        old, new = self._two_bundles()
        d = rp.diff_bundles(old, new)
        self.assertAlmostEqual(d["ev_at_current"]["old"], 0.175, places=4)
        self.assertAlmostEqual(d["ev_at_current"]["new"], 0.20, places=4)
        self.assertAlmostEqual(d["ev_at_current"]["delta"], 0.025, places=4)

    def test_invalidation_legs(self):
        old, new = self._two_bundles()
        d = rp.diff_bundles(old, new)
        inv = d["invalidation"]
        # technical leg level 82 in both; fundamental metric text present.
        self.assertEqual(inv["technical"]["old"], 82.0)
        self.assertEqual(inv["technical"]["new"], 82.0)
        self.assertIn("HBM", inv["fundamental"]["new"])

    def test_diff_matches_md_delta_composite_values(self):
        # diff_bundles composite old/new/Δ must equal render_report's md delta
        # (same source: composite dimension scores / composite score).
        from scripts import report_qc as _rq
        old, new = self._two_bundles()
        d = rp.diff_bundles(old, new)
        derived = _rq.derived_delta_values(old, new)
        # the composite-score delta (5.6) is among the md delta's derived values.
        self.assertTrue(any(abs(v - d["composite"]["delta"]) < 1e-6
                            for v in derived))


# --------------------------------------------------------------------------- #
# fmt_money_delta: sign OUTSIDE the dollar sign (review polish, fix 1).
# --------------------------------------------------------------------------- #

class TestFmtMoneyDelta(unittest.TestCase):
    # NOTE (fix 1 display precision): the money magnitude now formats to 2dp with
    # thousands separators, so '$5' is '$5.00' and a 4-figure value gains commas.
    def test_negative_puts_sign_before_dollar(self):
        # the defect: '$-182.44'. The fix: '-$182.44'.
        self.assertEqual(rp.fmt_money_delta(-182.44), "-$182.44")
        self.assertEqual(rp.fmt_money_delta(-3.0), "-$3.00")

    def test_positive_plain(self):
        self.assertEqual(rp.fmt_money_delta(5.0), "$5.00")
        self.assertEqual(rp.fmt_money_delta(182.44), "$182.44")
        self.assertEqual(rp.fmt_money_delta(1254.81), "$1,254.81")

    def test_positive_with_plus_lead(self):
        self.assertEqual(rp.fmt_money_delta(5.0, plus=True), "+$5.00")
        # negatives never gain a '+', even with plus=True.
        self.assertEqual(rp.fmt_money_delta(-5.0, plus=True), "-$5.00")

    def test_zero_is_never_signed(self):
        self.assertEqual(rp.fmt_money_delta(0), "$0.00")
        self.assertEqual(rp.fmt_money_delta(0.0, plus=True), "$0.00")

    def test_non_number_is_na(self):
        self.assertEqual(rp.fmt_money_delta(None), "n/a")
        self.assertEqual(rp.fmt_money_delta(True), "n/a")

    def test_what_changed_rows_money_negative_sign_outside(self):
        # Entry 1 dropping 95 -> 92 => a money row with a -$3.00 delta and the
        # value cells sign-correct (no '$-' anywhere in the money row).
        old, new = TestDiffBundles()._two_bundles()
        diff = rp.diff_bundles(old, new)
        rows = rp._what_changed_rows(diff)
        entry_row = [r for r in rows if r[0] == "Entry 1"][0]
        _, old_s, new_s, delta_s, is_down = entry_row
        self.assertEqual(old_s, "$95.00")
        self.assertEqual(new_s, "$92.00")
        self.assertEqual(delta_s, "-$3.00")
        self.assertTrue(is_down)
        for cell in (old_s, new_s, delta_s):
            self.assertNotIn("$-", cell)


# --------------------------------------------------------------------------- #
# Display-precision formatters + action short-map (fix 1 + fix 2).
# --------------------------------------------------------------------------- #

class TestDisplayFormatters(unittest.TestCase):
    def test_fmt_price_2dp_and_separators(self):
        self.assertEqual(rp.fmt_price(853.2), "$853.20")
        self.assertEqual(rp.fmt_price(1254.81), "$1,254.81")
        self.assertEqual(rp.fmt_price(681.436), "$681.44")   # rounds to 2dp
        self.assertEqual(rp.fmt_price(103.21), "$103.21")

    def test_fmt_price_na_for_non_numbers(self):
        self.assertEqual(rp.fmt_price(None), "n/a")
        self.assertEqual(rp.fmt_price(True), "n/a")
        self.assertEqual(rp.fmt_price("x"), "n/a")

    def test_fmt_ratio_2dp(self):
        self.assertEqual(rp.fmt_ratio(18.3879), "18.39")
        self.assertEqual(rp.fmt_ratio(0.138), "0.14")
        self.assertEqual(rp.fmt_ratio(1.65828), "1.66")
        self.assertEqual(rp.fmt_ratio(4.5), "4.50")

    def test_fmt_ratio_na_for_non_numbers(self):
        self.assertEqual(rp.fmt_ratio(None), "n/a")
        self.assertEqual(rp.fmt_ratio(True), "n/a")

    def test_fmt_pct_int_0dp(self):
        self.assertEqual(rp.fmt_pct_int(92.3077), "92")
        self.assertEqual(rp.fmt_pct_int(8.7), "9")       # rounds
        self.assertEqual(rp.fmt_pct_int(103.0), "103")

    def test_fmt_pct_int_na_for_non_numbers(self):
        self.assertEqual(rp.fmt_pct_int(None), "n/a")
        self.assertEqual(rp.fmt_pct_int(True), "n/a")


class TestActionShort(unittest.TestCase):
    def test_known_actions_map_to_short_forms(self):
        self.assertEqual(rp.action_short("Buy/Add"), "BUY / ADD")
        self.assertEqual(rp.action_short("Hold/Accumulate-on-weakness"),
                         "HOLD / ACCUMULATE")
        self.assertEqual(rp.action_short("Hold/Trim"), "HOLD / TRIM")
        self.assertEqual(rp.action_short("Reduce/Avoid"), "REDUCE / AVOID")

    def test_unknown_action_uppercased_fallback(self):
        self.assertEqual(rp.action_short("Watch"), "WATCH")

    def test_empty_action_is_question_mark(self):
        self.assertEqual(rp.action_short(""), "?")
        self.assertEqual(rp.action_short(None), "?")


# --------------------------------------------------------------------------- #
# C4 pure functions: chart mapping, deep-entry trigger, nearest-anchors dedup,
# evidence-note resolution, context gate, conviction subscore parsing.
# --------------------------------------------------------------------------- #

class TestChartSectionMapping(unittest.TestCase):
    """The chart-to-section mapping (fix 4a): risk vs options vs per-dimension."""

    def test_risk_charts_map_to_risk_dimension(self):
        # drawdown_history + vol_regime describe RISK, not technical.
        self.assertEqual(rp._DIM_CHARTS["risk"],
                         ["drawdown_history", "vol_regime"])
        # they are NOT under technical anymore.
        self.assertNotIn("drawdown_history", rp._DIM_CHARTS["technical"])
        self.assertNotIn("vol_regime", rp._DIM_CHARTS["technical"])

    def test_options_charts_are_the_options_set(self):
        # vol_term_structure / skew / expected_move_cone / oi_walls are OPTIONS.
        self.assertEqual(
            rp._OPTIONS_CHARTS,
            ["vol_term_structure", "skew", "expected_move_cone", "oi_walls"])

    def test_options_charts_not_under_any_dimension(self):
        # None of the options charts leaks back into a dimension mapping.
        for c in rp._OPTIONS_CHARTS:
            for dim, charts in rp._DIM_CHARTS.items():
                self.assertNotIn(c, charts,
                                 "%s must not be under dimension %s" % (c, dim))

    def test_fundamental_keeps_its_valuation_charts(self):
        self.assertEqual(rp._DIM_CHARTS["fundamental"], ["revisions", "pe_band"])


class TestDeepEntryTrigger(unittest.TestCase):
    """The deep-entry commentary line trigger (fix 4d): any entry >25% below last."""

    def _docs(self, last, entry_levels):
        return {
            "snapshot": {"price": {"last": last}},
            "module_tradeplan": {"stock_plan": {
                "entries": [{"level": lv} for lv in entry_levels]}},
        }

    def test_no_deep_entry_when_all_shallow(self):
        # 95 / 90 vs last 100 -> deepest is 10% below -> no line.
        self.assertFalse(rp._has_deep_entry(self._docs(100.0, [95.0, 90.0])))

    def test_deep_entry_fires_past_25pct(self):
        # 70 vs last 100 -> 30% below -> fires.
        self.assertTrue(rp._has_deep_entry(self._docs(100.0, [95.0, 70.0])))

    def test_boundary_exactly_25pct_does_not_fire(self):
        # 75 vs 100 -> exactly 25% -> strict '>' means NO line.
        self.assertFalse(rp._has_deep_entry(self._docs(100.0, [75.0])))

    def test_missing_last_is_no_line(self):
        self.assertFalse(rp._has_deep_entry(self._docs(None, [70.0])))

    def test_line_text_is_the_scripted_static_sentence(self):
        # It is a fixed sentence, not LLM prose.
        self.assertIn("structural supports", rp._DEEP_ENTRY_LINE)
        self.assertIn("conditional adds", rp._DEEP_ENTRY_LINE)
        self.assertIn("not", rp._DEEP_ENTRY_LINE.lower())


class TestNearestDownsideAnchors(unittest.TestCase):
    """The downside dedup (fix 4c): a compact <=5 nearest-anchor companion."""

    def _docs(self, rows):
        return {"module_risk": {"tables": {"downside_map": rows}}}

    def test_caps_at_five_nearest_by_distance(self):
        rows = [{"level": 100 - i, "pct_from_last": -i / 100.0}
                for i in range(1, 9)]  # 8 rows, 1%..8% down
        near = rp._nearest_downside_anchors(self._docs(rows), n=5)
        self.assertEqual(len(near), 5)
        # the five nearest are the 1%..5% rows (levels 99..95).
        levels = {r["level"] for r in near}
        self.assertEqual(levels, {99, 98, 97, 96, 95})

    def test_sorted_shallowest_first_descent(self):
        # Displayed shallowest (least-negative) at the top, deepest at the bottom,
        # so the table reads top-to-bottom like a descent.
        rows = [{"level": 82, "pct_from_last": -0.18},
                {"level": 95, "pct_from_last": -0.05},
                {"level": 90, "pct_from_last": -0.10}]
        near = rp._nearest_downside_anchors(self._docs(rows), n=5)
        pcts = [r["pct_from_last"] for r in near]
        self.assertEqual(pcts, [-0.05, -0.10, -0.18])  # descending -> descent

    def test_empty_map_returns_empty(self):
        self.assertEqual(rp._nearest_downside_anchors({}, n=5), [])


class TestEvidenceNoteResolution(unittest.TestCase):
    """The evidence-note body resolver (fix 3): note present vs older-bundle fallback."""

    def test_returns_note_when_present(self):
        slots = {"evidence_notes": {"risk": "The downside is anchored."}}
        self.assertEqual(rp._evidence_note(slots, "risk"),
                         "The downside is anchored.")

    def test_absent_evidence_notes_returns_empty(self):
        # Older bundle with no evidence_notes key -> '' (fallback preserved).
        self.assertEqual(rp._evidence_note({"thesis_bullets": []}, "risk"), "")

    def test_absent_dimension_returns_empty(self):
        slots = {"evidence_notes": {"risk": "x"}}
        self.assertEqual(rp._evidence_note(slots, "technical"), "")

    def test_blank_note_returns_empty(self):
        self.assertEqual(rp._evidence_note({"evidence_notes": {"risk": "  "}},
                                           "risk"), "")


class TestContextGate(unittest.TestCase):
    """The context render gate (deliverable 2): only a STAMPED module renders."""

    def test_stamped_module_gates_true(self):
        self.assertTrue(rp.context_gate_ok(_context_doc(stamped=True)))

    def test_unstamped_module_gates_false(self):
        # authored but never gated -> treated as absent (disclosure path).
        self.assertFalse(rp.context_gate_ok(_context_doc(stamped=False)))

    def test_none_gates_false(self):
        self.assertFalse(rp.context_gate_ok(None))

    def test_load_context_absent_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(rp.load_context(d))

    def test_load_context_reads_module(self):
        with tempfile.TemporaryDirectory() as d:
            _write_context(d, _context_doc(stamped=True))
            mod = rp.load_context(d)
            self.assertIsInstance(mod, dict)
            self.assertTrue(rp.context_gate_ok(mod))


class TestConvictionSubscoreParse(unittest.TestCase):
    """The conviction-subscore parser feeding WHY THIS CALL (deliverable 1)."""

    def test_flag_style_subscore(self):
        name, val, just = rp._parse_conviction_subscore(
            "variant some -> 12/20 (Modestly differentiated: consensus underrates)")
        self.assertEqual(name, "variant")
        self.assertEqual(val, "some")
        self.assertIn("Modestly differentiated", just)

    def test_hyphenated_flag_value(self):
        name, val, just = rp._parse_conviction_subscore(
            "invalidation both-legs -> 20/20 (stop + metric)")
        self.assertEqual(name, "invalidation")
        self.assertEqual(val, "both-legs")
        self.assertEqual(just, "stop + metric")

    def test_arithmetic_subscore_has_no_flag_value(self):
        name, val, just = rp._parse_conviction_subscore(
            "ev_asymmetry: ev 0.06 / hurdle 0.12 = ratio 0.55 -> 12/40")
        self.assertEqual(name, "ev_asymmetry")
        self.assertIsNone(val)  # the arithmetic tail is not a flag word.
        self.assertIsNone(just)


# --------------------------------------------------------------------------- #
# V4: diff_bundles weight-set + sector-scale transition rows (PURE).
# --------------------------------------------------------------------------- #

class TestDiffTransitions(unittest.TestCase):
    """diff_bundles surfaces weight-set + sector-scale stamps and _transition_rows
    emits a row only on a genuine change (both directions + no-change)."""

    def _docs(self, weight_set=None, sector_scale=None):
        comp = _composite_doc()
        if weight_set is not None:
            comp["weight_set"] = weight_set
        fund = _fundamental_doc()
        if sector_scale is not None:
            fund["sector_scale"] = sector_scale
        return {"module_composite": comp, "module_fundamental": fund}

    def test_weight_set_and_scale_captured_in_diff(self):
        old = self._docs(weight_set="standard v1", sector_scale=None)
        new = self._docs(weight_set="CUSTOM deep-value@1.0",
                         sector_scale="memory_semis@2026.1")
        d = rp.diff_bundles(old, new)
        self.assertEqual(d["weight_set"]["old"], "standard v1")
        self.assertEqual(d["weight_set"]["new"], "CUSTOM deep-value@1.0")
        self.assertIsNone(d["sector_scale"]["old"])
        self.assertEqual(d["sector_scale"]["new"], "memory_semis@2026.1")

    def test_transition_rows_on_change_forward(self):
        old = self._docs(weight_set="standard v1", sector_scale=None)
        new = self._docs(weight_set="CUSTOM deep-value@1.0",
                         sector_scale="memory_semis@2026.1")
        rows = rp._transition_rows(rp.diff_bundles(old, new))
        labels = {r[0]: r[1] for r in rows}
        self.assertEqual(labels["Weight set"],
                         "standard v1 → CUSTOM deep-value@1.0")
        self.assertEqual(labels["Scale"], "n/a → memory_semis@2026.1")

    def test_transition_rows_on_change_reverse(self):
        # A scale turning OFF and weights reverting to standard also surface.
        old = self._docs(weight_set="CUSTOM x@1.0",
                         sector_scale="memory_semis@2026.1")
        new = self._docs(weight_set="standard v1", sector_scale=None)
        rows = rp._transition_rows(rp.diff_bundles(old, new))
        labels = {r[0]: r[1] for r in rows}
        self.assertEqual(labels["Weight set"], "CUSTOM x@1.0 → standard v1")
        self.assertEqual(labels["Scale"], "memory_semis@2026.1 → n/a")

    def test_no_transition_rows_when_unchanged(self):
        old = self._docs(weight_set="standard v1", sector_scale=None)
        new = self._docs(weight_set="standard v1", sector_scale=None)
        self.assertEqual(rp._transition_rows(rp.diff_bundles(old, new)), [])

    def test_no_previous_bundle_old_is_none(self):
        # A fresh bundle (no prior) has None olds; a stamped new -> a transition.
        new = self._docs(weight_set="standard v1",
                         sector_scale="memory_semis@2026.1")
        d = rp.diff_bundles({}, new)
        self.assertIsNone(d["weight_set"]["old"])
        rows = dict(rp._transition_rows(d))
        self.assertEqual(rows["Weight set"], "n/a → standard v1")
        self.assertEqual(rows["Scale"], "n/a → memory_semis@2026.1")


# --------------------------------------------------------------------------- #
# V4: assemble_methodology — the PURE content-assembly function.
# --------------------------------------------------------------------------- #

class TestAssembleMethodology(unittest.TestCase):
    """The methodology page's data assembly: anchored vs snapshot maxima, the
    CUSTOM dual weight table, scale present/absent, the peg_display line, and that
    the convention constants MATCH the score_composite imports (no drift)."""

    def _block(self, blocks, kind):
        return [b for b in blocks if b["kind"] == kind][0]

    def _docs(self, fund, comp):
        return {"module_technical": {"rubric_version": "1.0.0"},
                "module_fundamental": fund, "module_sentiment": {"rubric_version": "1.0.0"},
                "module_risk": {"rubric_version": "1.0.0"}, "module_composite": comp}

    def test_blocks_in_contract_order(self):
        blocks = rp.assemble_methodology(
            self._docs(_fundamental_doc(), _composite_doc()), None)
        kinds = [b["kind"] for b in blocks]
        self.assertEqual(kinds, ["rubric_versions", "composite_weights",
                                 "valuation_formula", "sector_scale",
                                 "conventions", "governance"])

    def test_snapshot_mode_maxima_and_peg_scored(self):
        # snapshot mode -> 20/15/15 maxima with PEG SCORED (no display line).
        fund = _fundamental_doc()  # v1.1 snapshot, no peg_display
        blocks = rp.assemble_methodology(self._docs(fund, _composite_doc()), None)
        val = self._block(blocks, "valuation_formula")
        self.assertFalse(val["anchored"])
        self.assertEqual(val["maxima"],
                         [("Fwd P/E vs own 5-yr median", 20),
                          ("PEG", 15), ("FCF yield", 15)])
        self.assertIsNone(val["peg_line"])

    def test_anchored_mode_maxima_match_scorer_and_peg_display(self):
        # anchored mode -> 17/13/8/7/5 (pinned from score_fundamental) + PEG line.
        fund = _fundamental_anchored()
        blocks = rp.assemble_methodology(self._docs(fund, _composite_custom()),
                                         _memory_semis_scale())
        val = self._block(blocks, "valuation_formula")
        self.assertTrue(val["anchored"])
        self.assertEqual(
            val["maxima"],
            [("DCF-band position", _sf._DCF_MAX),
             ("Comps-range position", _sf._COMPS_MAX),
             ("Own-history multiple", _sf._OWNHIST_MAX),
             ("FCF yield", _sf._FCFY_ANCHORED_MAX),
             ("Justified sector-band", _sf._JUSTIFIED_MAX)])
        self.assertIsNotNone(val["peg_line"])
        self.assertIn("0.8", val["peg_line"])
        self.assertIn("display-only", val["peg_line"])
        self.assertIn("excluded from scoring", val["peg_line"])

    def test_disagreement_rule_pins_scorer_constants(self):
        blocks = rp.assemble_methodology(
            self._docs(_fundamental_anchored(), _composite_doc()), None)
        val = self._block(blocks, "valuation_formula")
        # 25% threshold, x0.75 haircut, 17 -> 12.75, never averaged.
        self.assertIn("25%", val["disagreement_rule"])
        self.assertIn("0.75", val["disagreement_rule"])
        self.assertIn("12.75", val["disagreement_rule"])
        self.assertIn("never averaged", val["disagreement_rule"])

    def test_standard_weights_single_row(self):
        comp = _composite_doc()  # no weight_set -> standard v1 default
        blocks = rp.assemble_methodology(self._docs(_fundamental_doc(), comp), None)
        cw = self._block(blocks, "composite_weights")
        self.assertFalse(cw["custom"])
        self.assertEqual(cw["weight_set"], _sc.STANDARD_WEIGHT_SET)
        # one row, its standard comparison column is None (no dual table).
        self.assertEqual(len(cw["rows"]), 1)
        profile, used, std = cw["rows"][0]
        self.assertIsNone(std)

    def test_custom_weights_dual_table_matches_standard_import(self):
        comp = _composite_custom()  # long-term custom
        blocks = rp.assemble_methodology(
            self._docs(_fundamental_anchored(), comp), _memory_semis_scale())
        cw = self._block(blocks, "composite_weights")
        self.assertTrue(cw["custom"])
        self.assertEqual(cw["weight_set"], "CUSTOM deep-value@1.0")
        profile, used, std = cw["rows"][0]
        self.assertEqual(profile, "long-term")
        # the custom column is the dimensions' weights; the standard column is
        # PINNED from score_composite.WEIGHTS (no retype).
        self.assertEqual(std, _sc.WEIGHTS["long-term"])
        self.assertEqual(used["fundamental"], 0.50)  # the custom tilt

    def test_custom_comparison_scores_surfaced_from_sensitivity(self):
        # When custom, sensitivity.standard_comparison lands on the page as
        # (profile, custom score/grade, standard score/grade) tuples.
        comp = _composite_custom()
        comp["sensitivity"] = {
            "trader": {"score": 58.2, "grade": "C",
                       "standard_comparison": {"score": 57.0, "grade": "C"}},
            "balanced": {"score": 60.1, "grade": "C",
                         "standard_comparison": {"score": 59.9, "grade": "C"}},
            "long-term": {"score": 63.6, "grade": "B",
                          "standard_comparison": {"score": 61.0, "grade": "C"}},
            "weight_set": "CUSTOM deep-value@1.0",
        }
        blocks = rp.assemble_methodology(
            self._docs(_fundamental_doc(), comp), None)
        cw = self._block(blocks, "composite_weights")
        self.assertEqual(cw["comparison"],
                         [("trader", 58.2, "C", 57.0, "C"),
                          ("balanced", 60.1, "C", 59.9, "C"),
                          ("long-term", 63.6, "B", 61.0, "C")])

    def test_standard_weights_carry_no_comparison(self):
        blocks = rp.assemble_methodology(
            self._docs(_fundamental_doc(), _composite_doc()), None)
        cw = self._block(blocks, "composite_weights")
        self.assertEqual(cw["comparison"], [])

    def test_scale_block_present_carries_scale_fields(self):
        scale = _memory_semis_scale()
        blocks = rp.assemble_methodology(
            self._docs(_fundamental_anchored(), _composite_doc()), scale)
        sb = self._block(blocks, "sector_scale")
        self.assertTrue(sb["present"])
        self.assertEqual(sb["stamp"], "memory_semis@2026.1")
        self.assertEqual(sb["effective"], "2026-07-01")
        self.assertEqual(sb["formula"], "justified_pb")
        self.assertEqual(sb["evidence"], ["C1", "C3"])
        self.assertEqual(sb["prior_version"], "2025.4")
        # a computed band + a falsifier row (metric/op/value/meaning).
        self.assertIsInstance(sb["band"], dict)
        self.assertEqual(sb["falsifiers"][0][0], "fundamentals.roe")
        self.assertEqual(sb["falsifiers"][0][1], "<")
        self.assertIn("ROE collapse", sb["falsifiers"][0][3])

    def test_scale_block_absent_when_no_scale(self):
        blocks = rp.assemble_methodology(
            self._docs(_fundamental_doc(), _composite_doc()), None)
        sb = self._block(blocks, "sector_scale")
        self.assertFalse(sb["present"])

    def test_conventions_pin_composite_constants(self):
        blocks = rp.assemble_methodology(
            self._docs(_fundamental_doc(), _composite_doc()), None)
        conv = self._block(blocks, "conventions")
        # horizon years MATCH the imported HORIZON_YEARS (no retype).
        horizon = dict(conv["horizon_years"])
        self.assertEqual(horizon, dict(_sc.HORIZON_YEARS))
        # grade bands agree with score_composite.grade_for at each edge.
        for letter, lo, action in conv["grade_bands"]:
            self.assertEqual(_sc.grade_for(lo), (letter, action))
        # the EV hurdle sentence names the imported hurdle rate.
        self.assertIn("%g" % _sc._HURDLE_RATE, conv["ev_hurdle"])

    def test_rubric_versions_read_from_modules(self):
        blocks = rp.assemble_methodology(
            self._docs(_fundamental_anchored(), _composite_doc()), None)
        rv = self._block(blocks, "rubric_versions")
        rows = {r[0]: r[1] for r in rv["rows"]}
        self.assertEqual(rows["Fundamental"], "rubric v1.2.0")
        self.assertEqual(rows["Composite (expression)"], "rubric v1.0.0")

    def test_governance_four_pinned_sentences(self):
        blocks = rp.assemble_methodology(
            self._docs(_fundamental_doc(), _composite_doc()), None)
        gov = self._block(blocks, "governance")
        self.assertGreaterEqual(len(gov["sentences"]), 4)
        joined = " ".join(gov["sentences"]).lower()
        self.assertIn("forward-only", joined)
        self.assertIn("pre-registered", joined)
        self.assertIn("ratification", joined)
        self.assertIn("append-only", joined)


# --------------------------------------------------------------------------- #
# V4: refresh-plan loader + banner name resolution (PURE).
# --------------------------------------------------------------------------- #

class TestRefreshPlanLoad(unittest.TestCase):
    def test_loads_from_ticker_dir_parent(self):
        with tempfile.TemporaryDirectory() as parent:
            bundle = os.path.join(parent, "detail_reports_2026-07-16")
            os.makedirs(bundle)
            _write_refresh_plan(parent, {"scale_review_required": True})
            plan = rp.load_refresh_plan(bundle)
            self.assertIsInstance(plan, dict)
            self.assertTrue(plan["scale_review_required"])

    def test_absent_plan_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(rp.load_refresh_plan(d))

    def test_scale_review_name_picks_tripped_scale(self):
        plan = {"scales": [
            {"scale": "software_saas@2026.1", "any_tripped": False},
            {"scale": "memory_semis@2026.1", "any_tripped": True}]}
        self.assertEqual(rp._scale_review_name(plan), "memory_semis@2026.1")

    def test_scale_review_name_none_when_no_trip(self):
        plan = {"scales": [{"scale": "x@1", "any_tripped": False}]}
        self.assertIsNone(rp._scale_review_name(plan))


# --------------------------------------------------------------------------- #
# report_qc --pdf-slots: orphan fails, clean passes + stamp written.
# --------------------------------------------------------------------------- #

class TestSlotsProvenance(unittest.TestCase):
    def test_clean_slots_pass_and_stamp_written(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            slots = _write_slots(d, _clean_slots())
            rc, out, err = _qc_slots(d, slots)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("PASS", out)
            # Stamp written INTO the slots file.
            with open(slots) as fh:
                stamped = json.load(fh)
            self.assertIs(stamped.get("qc_passed"), True)
            self.assertIn("checked_utc", stamped)
            # Original slot content preserved.
            self.assertEqual(len(stamped["thesis_bullets"]), 3)

    def test_fabricated_number_in_desk_read_fails_and_names_it(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            slots = _clean_slots()
            slots["desk_read"]["setup"] = "Targets a hidden 4444 handle."
            path = _write_slots(d, slots)
            rc, out, err = _qc_slots(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("number_provenance", out)
            self.assertIn("4444", out)
            # No stamp written on failure.
            with open(path) as fh:
                unstamped = json.load(fh)
            self.assertNotIn("qc_passed", unstamped)

    def test_fabricated_number_in_thesis_bullet_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            slots = _clean_slots()
            slots["thesis_bullets"][0] = "Targets 7231 on the tape — absent number."
            path = _write_slots(d, slots)
            rc, out, err = _qc_slots(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("7231", out)

    def test_fabricated_number_in_positioning_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            slots = _clean_slots()
            slots["positioning"]["monitoring"] = "Watch the 6197 pivot."
            path = _write_slots(d, slots)
            rc, out, err = _qc_slots(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("6197", out)

    def test_delta_interpretation_slot_checked(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            slots = _clean_slots()
            slots["delta_interpretation"] = "The 8888 shift dominates the delta."
            path = _write_slots(d, slots)
            rc, out, err = _qc_slots(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("8888", out)

    def test_delta_slot_with_previous_bundle_deltas_pass(self):
        # A delta_interpretation citing a script-computed Δ passes when --previous
        # is supplied (the Δ is in-bundle via derived_delta_values).
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            _mk_bundle(old)
            new_comp = _composite_doc()
            new_comp["score"] = 65.0
            new_comp["grade"] = "B"
            new_comp["dimensions"][0]["score"] = 90
            new_comp["dimensions"][0]["contribution"] = 22.5
            _mk_bundle(new, composite_override=new_comp)
            slots = _clean_slots()
            # composite Δ = 65 - 59.4 = 5.6 -> cite it.
            slots["delta_interpretation"] = "Composite moved 5.6 on the technical leg."
            path = _write_slots(new, slots)
            rc, out, err = _qc_slots(new, path, ["--previous", old])
            self.assertEqual(rc, 0, out + err)

    def test_bogus_date_in_slot_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            slots = _clean_slots()
            slots["desk_read"]["trigger"] = "The real catalyst lands 2031-01-01."
            path = _write_slots(d, slots)
            rc, out, err = _qc_slots(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("2031-01-01", out)

    # -- evidence_notes slots (C4): scanned like every other prose slot. --

    def test_clean_evidence_notes_pass_and_stamp(self):
        # A slots file with in-bundle evidence_notes passes and stamps.
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            path = _write_slots(d, _clean_slots_with_notes())
            rc, out, err = _qc_slots(d, path)
            self.assertEqual(rc, 0, out + err)
            with open(path) as fh:
                stamped = json.load(fh)
            self.assertIs(stamped.get("qc_passed"), True)
            # evidence_notes preserved through the stamp write.
            self.assertEqual(len(stamped["evidence_notes"]), 5)

    def test_fabricated_number_in_evidence_note_orphans(self):
        # A fabricated number in an evidence note must orphan (the note is prose).
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            slots = _clean_slots_with_notes()
            slots["evidence_notes"]["risk"] = "A hidden 5150 stop lurks below."
            path = _write_slots(d, slots)
            rc, out, err = _qc_slots(d, path)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("number_provenance", out)
            self.assertIn("5150", out)
            with open(path) as fh:
                self.assertNotIn("qc_passed", json.load(fh))

    def test_collect_slot_strings_includes_evidence_notes(self):
        # The collect function scans every evidence note (contract pin).
        slots = _clean_slots_with_notes()
        collected = rq.collect_slot_strings(slots)
        for note in slots["evidence_notes"].values():
            self.assertIn(note, collected)


# --------------------------------------------------------------------------- #
# render_pdf refuses exec/detail when the slots stamp is absent (exit 2).
# --------------------------------------------------------------------------- #

class TestSlotsGateEnforced(unittest.TestCase):
    def test_exec_refuses_without_stamp(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _write_slots(d, _clean_slots())  # present but UNSTAMPED
            rc, out, err = _render(d, "exec")
            self.assertEqual(rc, 2, out + err)
            # The fix command names the slots gate.
            self.assertIn("--pdf-slots", out + err)

    def test_detail_refuses_without_stamp(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            _write_slots(d, _clean_slots())
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 2, out + err)
            self.assertIn("--pdf-slots", out + err)

    def test_exec_refuses_when_slots_missing_entirely(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)  # no pdf_slots.json at all
            rc, out, err = _render(d, "exec")
            self.assertEqual(rc, 2, out + err)

    def test_stamp_helper_reads_qc_passed(self):
        # Pure-function contract: slots_gate_ok(bundle) reflects the stamp.
        with tempfile.TemporaryDirectory() as d:
            _mk_bundle(d)
            path = _write_slots(d, _clean_slots())
            self.assertFalse(rp.slots_gate_ok(d))
            # stamp it and re-check.
            with open(path) as fh:
                s = json.load(fh)
            s["qc_passed"] = True
            with open(path, "w") as fh:
                json.dump(s, fh)
            self.assertTrue(rp.slots_gate_ok(d))


# --------------------------------------------------------------------------- #
# Render smokes (venv-guarded): page counts + footer strings.
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_CAN_RENDER, "reportlab+matplotlib required for render smoke")
class TestRenderSmoke(unittest.TestCase):
    def _prep_stamped(self, d, charts=True, slots=None, context=None):
        _mk_bundle(d)
        path = _write_slots(d, slots or _clean_slots())
        # Run the real slots gate to write the stamp (also exercises the gate).
        rc, out, err = _qc_slots(d, path)
        self.assertEqual(rc, 0, out + err)
        if context is not None:
            _write_context(d, context)
        if charts:
            from scripts import render_charts
            docs = render_charts.load_docs(d)
            render_charts.render_set(docs, render_charts._chart_names("all"),
                                     os.path.join(d, "charts"))

    def test_exec_renders_two_pages(self):
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d)
            rc, out, err = _render(d, "exec")
            self.assertEqual(rc, 0, out + err)
            pdf = os.path.join(d, "MU_Trade_Report_2026-07-16.pdf")
            self.assertTrue(os.path.isfile(pdf), out + err)
            self.assertEqual(_pdf_page_count(pdf), 2)

    def test_detail_renders_full_docket(self):
        # The detail (C4) is: 2 exec pages + 1 WHY-THIS-CALL page (which also
        # carries the context DISCLOSURE line when no stamped context module) +
        # >=1 PACKED dimension page + options + downside/monitoring + appendix.
        # No dedicated context page renders in the no-context branch (fix 4e: a
        # one-line disclosure never opens a near-empty page). The floor is
        # therefore 2 exec + 1 why + >=1 dim + 3 tail = 7. A regression that
        # dropped a whole section-block would fall below.
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d)
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            pdf = os.path.join(d, "MU_Detail_2026-07-16.pdf")
            self.assertTrue(os.path.isfile(pdf), out + err)
            self.assertGreaterEqual(_pdf_page_count(pdf), 7)

    def test_detail_renders_why_this_call_from_flags(self):
        # WHY THIS CALL renders the captured judgment: the composite flag values
        # AND their justification text (deliverable 1) — pure module-JSON prose.
        # The fixture sentiment flags are empty and the fixture tradeplan lacks a
        # catalyst-in-thesis justification, so we ENRICH those two module JSONs
        # in-bundle (without touching the shared fixture builders) to exercise the
        # trade-plan + sentiment sub-blocks.
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d)
            # Enrich the sentiment flags (rating_actions/inst_flow/insider_baseline).
            sent_path = os.path.join(d, "module_sentiment.json")
            with open(sent_path) as fh:
                sent = json.load(fh)
            sent["flags"] = {
                "rating_actions": "neutral",
                "rating_actions_justification": "no rating changes this cycle",
                "inst_flow": "unknown", "inst_flow_justification": None,
                "insider_baseline": "normal", "insider_baseline_justification": None,
            }
            with open(sent_path, "w") as fh:
                json.dump(sent, fh)
            # Enrich the tradeplan catalyst-in-thesis justification.
            tp_path = os.path.join(d, "module_tradeplan.json")
            with open(tp_path) as fh:
                tp = json.load(fh)
            tp["flags"]["catalyst_in_thesis_justification"] = \
                "bull case rests on the print"
            with open(tp_path, "w") as fh:
                json.dump(tp, fh)

            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            text = _pdf_text(os.path.join(d, "MU_Detail_2026-07-16.pdf"))
            self.assertIn(b"WHY THIS CALL", text)
            # scenario reasoning paragraph (from module_composite.ev).
            self.assertIn(b"HBM demand asymmetric", text)
            # a conviction flag JUSTIFICATION (not just the value).
            self.assertIn(b"consensus underrates HBM", text)
            # a trade-plan judgment justification.
            self.assertIn(b"bull case rests on the print", text)
            # a fundamental-invalidation justification from tradeplan flags.
            self.assertIn(b"HBM is the margin thesis", text)
            # sentiment flags render (rating actions justification present).
            self.assertIn(b"rating actions", text)
            self.assertIn(b"no rating changes this cycle", text)

    def test_context_sections_omitted_without_module(self):
        # No module_context in the bundle -> narrative omitted, disclosure shown.
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d)  # no context written
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            text = _pdf_text(os.path.join(d, "MU_Detail_2026-07-16.pdf"))
            self.assertIn(b"No gated company-context", text)
            # the narrative headers are NOT present.
            self.assertNotIn(b"THE BUSINESS", text)
            self.assertNotIn(b"THE CASES", text)

    def test_context_sections_omitted_when_unstamped(self):
        # An authored-but-UNSTAMPED module is treated as absent (disclosure path).
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d, context=_context_doc(stamped=False))
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            text = _pdf_text(os.path.join(d, "MU_Detail_2026-07-16.pdf"))
            self.assertIn(b"No gated company-context", text)
            self.assertNotIn(b"THE BUSINESS", text)

    def test_context_sections_render_when_stamped(self):
        # A STAMPED module renders the full CONTEXT NARRATIVE: business, cases,
        # risks, and the findings footnote block (deliverable 2).
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d, context=_context_doc(stamped=True))
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            text = _pdf_text(os.path.join(d, "MU_Detail_2026-07-16.pdf"))
            self.assertIn(b"THE BUSINESS", text)
            self.assertIn(b"Memory and storage", text)
            self.assertIn(b"THE CASES", text)
            self.assertIn(b"RISKS", text)
            self.assertIn(b"FINDINGS", text)
            # a live_tape event rides under WHAT'S MOVING THE STOCK.
            self.assertIn(b"HBM qualification", text)
            # the findings registry (id + source) appears.
            self.assertIn(b"C1", text)
            # the stamped module adds a dedicated page (>= the no-context floor).
            self.assertGreaterEqual(
                _pdf_page_count(os.path.join(d, "MU_Detail_2026-07-16.pdf")), 8)

    def test_findings_block_spills_to_continuation_page(self):
        # A long FINDINGS registry (20 multi-line findings) can't fit the footnote
        # band of one context page, so the height-aware block must spill onto a
        # FINDINGS (continued) page -- growing the total page count vs the small
        # (2-finding) fixture -- without raising and without truncating findings.
        with tempfile.TemporaryDirectory() as small, \
                tempfile.TemporaryDirectory() as big:
            self._prep_stamped(small, context=_context_doc(stamped=True))
            self._prep_stamped(big, context=_context_doc_many_findings(20))
            rc_s, out_s, err_s = _render(small, "detail")
            rc_b, out_b, err_b = _render(big, "detail")
            self.assertEqual(rc_s, 0, out_s + err_s)
            self.assertEqual(rc_b, 0, out_b + err_b)
            small_pdf = os.path.join(small, "MU_Detail_2026-07-16.pdf")
            big_pdf = os.path.join(big, "MU_Detail_2026-07-16.pdf")
            # The findings overflow adds at least one page.
            self.assertGreater(_pdf_page_count(big_pdf), _pdf_page_count(small_pdf))
            # The continuation kicker rendered; every finding id is present (none
            # dropped or truncated away by the spill).
            text = _pdf_text(big_pdf)
            self.assertIn(b"FINDINGS (continued)", text)
            self.assertIn(b"C1", text)
            self.assertIn(b"C20", text)

    def test_finding_lines_wraps_to_multiple_lines(self):
        # The pure per-finding wrap helper: a long claim wraps to >1 line, each
        # within the max width, so the block's measured height is len*leading.
        doc = rp.Doc(os.devnull, "MU", "Detail", "2026-07-16", {})
        max_w = doc.CONTENT_W - 4
        finding = {"id": "C1",
                   "claim": ("A deliberately long finding claim that must wrap "
                             "across several measured lines because it clearly "
                             "exceeds the available footnote width by a wide "
                             "margin and keeps going well past a single line"),
                   "source": "coverage/research.md §Overview / snapshot leaf"}
        lines = rp._finding_lines(doc, finding, max_w)
        self.assertGreater(len(lines), 1)
        for ln in lines:
            self.assertLessEqual(
                doc.string_width(ln, doc.FONT, rp._FINDINGS_SIZE), max_w)
        # composition: the id leads and the source is bracketed at the tail.
        joined = " ".join(lines)
        self.assertTrue(joined.startswith("C1"))
        self.assertIn("[coverage/research.md", joined)

    def test_options_section_renders_full_module(self):
        # The options section renders the FULL module (fix 4b), not 2 rows:
        # vol dashboard mini-table, per-structure management rules, declined
        # reasons, and warnings_global.
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d)
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            text = _pdf_text(os.path.join(d, "MU_Detail_2026-07-16.pdf"))
            self.assertIn(b"Vol dashboard", text)
            self.assertIn(b"IV pctile", text)
            # per-structure management rules ('Manage: ...').
            self.assertIn(b"Manage:", text)
            # a declined structure WITH its reason.
            self.assertIn(b"cash_secured_put", text)
            self.assertIn(b"earnings within 30d", text)
            # the global binary-event warning.
            self.assertIn(b"BINARY EVENT", text)

    def test_downside_map_dedup_uses_nearest_table(self):
        # The dedup (fix 4c): the compact NEAREST-anchors table replaces the full
        # downside_map dump (chart carries the full ladder).
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d)
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            text = _pdf_text(os.path.join(d, "MU_Detail_2026-07-16.pdf"))
            self.assertIn(b"NEAREST DOWNSIDE ANCHORS", text)

    def test_evidence_note_becomes_body_with_scoring_trail(self):
        # With evidence_notes present, the note is the BODY and the arithmetic is
        # demoted to a small-type SCORING TRAIL exhibit (fix 3).
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d, slots=_clean_slots_with_notes())
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            text = _pdf_text(os.path.join(d, "MU_Detail_2026-07-16.pdf"))
            # the note prose appears as body.
            self.assertIn(b"constructive but not extended", text)
            # the SCORING TRAIL exhibit label appears.
            self.assertIn(b"SCORING TRAIL", text)

    def test_delta_renders_one_page(self):
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            _mk_bundle(old)
            self._prep_stamped(new)
            rc, out, err = _render(new, "delta", ["--previous", old])
            self.assertEqual(rc, 0, out + err)
            pdf = os.path.join(new, "MU_Delta_Note_2026-07-16.pdf")
            self.assertTrue(os.path.isfile(pdf), out + err)
            self.assertEqual(_pdf_page_count(pdf), 1)

    # -- V4: METHODOLOGY appendix + stamps + banners (venv-guarded smokes). --

    def test_detail_has_methodology_page_and_grows(self):
        # The METHODOLOGY appendix header renders and the detail grows past the
        # pre-methodology floor (was >=7; now >=8 with the appendix page).
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d)
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            pdf = os.path.join(d, "MU_Detail_2026-07-16.pdf")
            self.assertGreaterEqual(_pdf_page_count(pdf), 8)
            text = _pdf_text(pdf)
            self.assertIn(b"METHODOLOGY", text)
            self.assertIn(b"Rubric versions", text)
            self.assertIn(b"Composite weights", text)
            self.assertIn(b"Fundamental valuation", text)
            self.assertIn(b"Governance", text)
            # standard weight-set footer stamp is present.
            self.assertIn(b"Weights: standard v1", text)
            # no scale active -> the standard-bands line, and no Scale: stamp.
            self.assertIn(b"No sector scale active", text)

    def test_detail_methodology_anchored_custom_scale(self):
        # Anchored fundamental + CUSTOM weights + an active scale render the
        # anchored maxima, the dual weight table, the scale block, and the footer
        # CUSTOM + Scale stamps.
        with tempfile.TemporaryDirectory() as parent:
            d = os.path.join(parent, "detail_reports_2026-07-16")
            os.makedirs(d)
            self._prep_stamped(d)
            # Overwrite the composite + fundamental with the anchored/custom fixtures.
            with open(os.path.join(d, "module_composite.json"), "w") as fh:
                json.dump(_composite_custom(), fh)
            with open(os.path.join(d, "module_fundamental.json"), "w") as fh:
                json.dump(_fundamental_anchored(), fh)
            _write_scale(d, _memory_semis_scale())
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            pdf = os.path.join(parent, "MU_Detail_2026-07-16.pdf")
            text = _pdf_text(pdf)
            # anchored maxima table.
            self.assertIn(b"DCF-band position", text)
            self.assertIn(b"Justified sector-band", text)
            # CUSTOM dual weight table.
            self.assertIn(b"standard v1 shown for comparison", text)
            # the scale block (name + a falsifier meaning).
            self.assertIn(b"Falsifiers", text)
            self.assertIn(b"structural ROE collapse", text)
            # PEG display-only line under the valuation subscores.
            self.assertIn(b"display-only", text)
            # footer machinery stamps: CUSTOM weights + Scale.
            self.assertIn(b"Weights: CUSTOM deep-value@1.0", text)
            self.assertIn(b"Scale: memory_semis@2026.1", text)

    def test_detail_banners_when_plan_trips(self):
        # A refresh plan with scale_review_required + a pending proposal renders
        # both banners on Detail p1.
        with tempfile.TemporaryDirectory() as parent:
            d = os.path.join(parent, "detail_reports_2026-07-16")
            os.makedirs(d)
            self._prep_stamped(d)
            _write_refresh_plan(parent, {
                "scale_review_required": True,
                "scales": [{"scale": "memory_semis@2026.1", "any_tripped": True}],
                "pending_proposals": ["ep_upstream.json"],
            })
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            text = _pdf_text(os.path.join(parent, "MU_Detail_2026-07-16.pdf"))
            self.assertIn(b"SCALE REVIEW REQUIRED", text)
            self.assertIn(b"memory_semis@2026.1", text)
            self.assertIn(b"Pending scale proposal", text)

    def test_delta_banner_and_transition_rows(self):
        # The delta note carries the banner (from the new bundle's plan) and a
        # weight-set transition row when the stamp changed between runs.
        with tempfile.TemporaryDirectory() as parent_old, \
                tempfile.TemporaryDirectory() as parent_new:
            old = os.path.join(parent_old, "detail_reports_2026-07-15")
            new = os.path.join(parent_new, "detail_reports_2026-07-16")
            os.makedirs(old)
            os.makedirs(new)
            _mk_bundle(old)  # old: standard weight set (no weight_set key)
            self._prep_stamped(new)
            # new: CUSTOM weight set -> a Weight set transition row.
            with open(os.path.join(new, "module_composite.json"), "w") as fh:
                json.dump(_composite_custom(), fh)
            _write_refresh_plan(parent_new, {
                "scale_review_required": True,
                "scales": [{"scale": "memory_semis@2026.1", "any_tripped": True}],
                "pending_proposals": [],
            })
            rc, out, err = _render(new, "delta", ["--previous", old])
            self.assertEqual(rc, 0, out + err)
            text = _pdf_text(os.path.join(parent_new, "MU_Delta_Note_2026-07-16.pdf"))
            self.assertIn(b"SCALE REVIEW REQUIRED", text)
            # the weight-set transition row (standard v1 -> CUSTOM).
            self.assertIn(b"Weight set", text)
            self.assertIn(b"CUSTOM deep-value@1.0", text)

    def test_exec_doc_has_no_methodology_or_banner(self):
        # The standalone exec doc is UNCHANGED: no methodology page, and no banner
        # even if a plan exists (banners are Detail/Delta only).
        with tempfile.TemporaryDirectory() as parent:
            d = os.path.join(parent, "detail_reports_2026-07-16")
            os.makedirs(d)
            self._prep_stamped(d)
            _write_refresh_plan(parent, {"scale_review_required": True,
                                         "pending_proposals": ["x.json"]})
            rc, out, err = _render(d, "exec")
            self.assertEqual(rc, 0, out + err)
            pdf = os.path.join(parent, "MU_Trade_Report_2026-07-16.pdf")
            self.assertEqual(_pdf_page_count(pdf), 2)
            text = _pdf_text(pdf)
            self.assertNotIn(b"METHODOLOGY", text)
            self.assertNotIn(b"SCALE REVIEW REQUIRED", text)

    def test_exec_output_lands_in_bundle_parent_under_detail_reports(self):
        # detail_reports_<date> bundle -> PDF lands in the PARENT (mirrors md rule).
        with tempfile.TemporaryDirectory() as parent:
            bundle = os.path.join(parent, "detail_reports_2026-07-17")
            os.makedirs(bundle)
            self._prep_stamped(bundle)
            rc, out, err = _render(bundle, "exec")
            self.assertEqual(rc, 0, out + err)
            self.assertTrue(
                os.path.isfile(os.path.join(parent, "MU_Trade_Report_2026-07-16.pdf")),
                out + err)

    def test_exec_footer_string_present(self):
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d)
            _render(d, "exec")
            pdf = os.path.join(d, "MU_Trade_Report_2026-07-16.pdf")
            text = _pdf_text(pdf)
            # The contract-pinned footer chrome must appear on the page.
            self.assertIn(b"not investment advice", text)
            self.assertIn(b"verified snapshot", text)
            # The page-N-of-M chrome is present too.
            self.assertIn(b"p 1/2", text)

    def test_delta_without_previous_errors(self):
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d)
            rc, out, err = _render(d, "delta")
            # delta requires --previous.
            self.assertNotEqual(rc, 0)

    def test_what_changed_only_with_previous(self):
        # exec with --previous embeds the What-Changed reason; without it, no box.
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            _mk_bundle(old)
            self._prep_stamped(new)
            rc, out, err = _render(new, "exec", ["--previous", old])
            self.assertEqual(rc, 0, out + err)
            self.assertTrue(os.path.isfile(
                os.path.join(new, "MU_Trade_Report_2026-07-16.pdf")))


if __name__ == "__main__":
    unittest.main()
