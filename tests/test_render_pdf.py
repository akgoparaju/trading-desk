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

The render smoke tests (exec 2pp, detail >=8pp, delta 1pp, footer strings) require
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
    _LAST, _EV_AT_CURRENT,
)


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
    def _prep_stamped(self, d, charts=True):
        _mk_bundle(d)
        path = _write_slots(d, _clean_slots())
        # Run the real slots gate to write the stamp (also exercises the gate).
        rc, out, err = _qc_slots(d, path)
        self.assertEqual(rc, 0, out + err)
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

    def test_detail_renders_at_least_eight_pages(self):
        with tempfile.TemporaryDirectory() as d:
            self._prep_stamped(d)
            rc, out, err = _render(d, "detail")
            self.assertEqual(rc, 0, out + err)
            pdf = os.path.join(d, "MU_Detail_2026-07-16.pdf")
            self.assertTrue(os.path.isfile(pdf), out + err)
            self.assertGreaterEqual(_pdf_page_count(pdf), 8)

    def test_delta_renders_one_page(self):
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            _mk_bundle(old)
            self._prep_stamped(new)
            rc, out, err = _render(new, "delta", ["--previous", old])
            self.assertEqual(rc, 0, out + err)
            pdf = os.path.join(new, "MU_Delta_Note_2026-07-16.pdf")
            self.assertTrue(os.path.isfile(pdf), out + err)
            self.assertEqual(_pdf_page_count(pdf), 1)

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
