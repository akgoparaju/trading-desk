"""Tests for scripts/tdstyle.py -- the shared render style module.

WHY: tdstyle is the single source of truth for the docket's visual identity
(orange accent, bank-note axes, kicker/why annotation helpers). Its constants
are pinned by contract and MUST match the design spec exactly, so a drift in a
hex code or the accent RGB tuple is a test failure. The module must also be
IMPORTABLE on a machine WITHOUT matplotlib -- only the style-applying helpers may
touch matplotlib, and they must do so lazily and raise a clear, actionable error
when it is absent. matplotlib-dependent assertions are guarded by
``skipUnless(find_spec("matplotlib"))`` so the base suite is green without it.

stdlib-only for the unguarded tests; unittest.
"""

import importlib.util
import unittest

from scripts import tdstyle


_HAS_MPL = importlib.util.find_spec("matplotlib") is not None


class TestConstants(unittest.TestCase):
    """The pinned palette + layout constants are exact (contract)."""

    def test_accent_is_burnt_orange(self):
        self.assertEqual(tdstyle.ACCENT, "#BF5700")

    def test_semantic_colors(self):
        self.assertEqual(tdstyle.RED, "#C00000")
        self.assertEqual(tdstyle.GREEN, "#2E7D32")

    def test_gray_ramp(self):
        self.assertEqual(tdstyle.GRAY_TXT, "#444444")
        self.assertEqual(tdstyle.GRAY_MID, "#888888")
        self.assertEqual(tdstyle.HAIRLINE, "#DDDDDD")

    def test_accent_rgb_tuple_matches_hex(self):
        # ACCENT_RGB is a 3-tuple of 0-1 floats for reportlab; must equal the hex.
        self.assertEqual(len(tdstyle.ACCENT_RGB), 3)
        self.assertAlmostEqual(tdstyle.ACCENT_RGB[0], 0xBF / 255)
        self.assertAlmostEqual(tdstyle.ACCENT_RGB[1], 0x57 / 255)
        self.assertAlmostEqual(tdstyle.ACCENT_RGB[2], 0x00 / 255)

    def test_dpi_and_figwidth(self):
        self.assertEqual(tdstyle.DPI, 300)
        self.assertEqual(tdstyle.FIG_W, 10)


class TestImportableWithoutMatplotlib(unittest.TestCase):
    """The module imports cleanly regardless of matplotlib, and the style
    helpers fail LOUDLY (clear RuntimeError pointing at render_env) when it is
    absent rather than raising an opaque ImportError deep in a draw call."""

    def test_module_imports(self):
        # If we got here the import at top of file already succeeded.
        self.assertTrue(hasattr(tdstyle, "apply_mpl_style"))
        self.assertTrue(hasattr(tdstyle, "kicker"))
        self.assertTrue(hasattr(tdstyle, "why"))

    @unittest.skipIf(_HAS_MPL, "matplotlib present; cannot exercise the absent path")
    def test_apply_style_raises_actionable_error_when_absent(self):
        with self.assertRaises(RuntimeError) as ctx:
            tdstyle.apply_mpl_style()
        msg = str(ctx.exception)
        self.assertIn("matplotlib", msg)
        self.assertIn("render_env", msg)


@unittest.skipUnless(_HAS_MPL, "matplotlib not installed")
class TestApplyStyleRcParams(unittest.TestCase):
    """apply_mpl_style sets the bank-note rcParams (white bg, small font)."""

    def test_sets_rcparams(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams.update(plt.rcParamsDefault)  # start clean
        tdstyle.apply_mpl_style()
        self.assertEqual(plt.rcParams["figure.facecolor"], "white")
        self.assertEqual(plt.rcParams["axes.facecolor"], "white")
        self.assertEqual(plt.rcParams["savefig.facecolor"], "white")
        self.assertEqual(plt.rcParams["font.size"], 8)

    def test_kicker_and_why_draw_without_error(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        tdstyle.apply_mpl_style()
        fig, ax = plt.subplots()
        # kicker draws on the axes; why draws on the figure. Both return a Text.
        t1 = tdstyle.kicker(ax, "Test Kicker")
        t2 = tdstyle.why(fig, "why it matters")
        # kicker uppercases its text.
        self.assertEqual(t1.get_text(), "TEST KICKER")
        self.assertEqual(t2.get_text(), "why it matters")
        plt.close(fig)

    def test_bank_axes_hides_top_right_spines(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        tdstyle.apply_mpl_style()
        fig, ax = plt.subplots()
        tdstyle.bank_axes(ax)
        self.assertFalse(ax.spines["top"].get_visible())
        self.assertFalse(ax.spines["right"].get_visible())
        self.assertTrue(ax.spines["left"].get_visible())
        self.assertTrue(ax.spines["bottom"].get_visible())
        plt.close(fig)


if __name__ == "__main__":
    unittest.main()
