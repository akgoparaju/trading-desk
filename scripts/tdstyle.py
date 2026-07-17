"""Shared render style for the PDF docket (charts + reportlab pages).

WHY THIS MODULE EXISTS: the docket's visual identity -- burnt-orange accent,
sell-side "bank note" axes, kicker/why annotation helpers -- must be defined ONCE
so every chart and every PDF page is consistent, and so a palette change is a
single edit. The aesthetic target is a sell-side research note (Goldman / Morgan
Stanley), NOT a fintech dashboard.

DESIGN CONSTRAINT (stdlib-first): matplotlib is an OPTIONAL, venv-bootstrapped
dependency. This module MUST import cleanly on a machine without matplotlib -- the
base ``unittest`` suite runs there. Only the style-applying helpers touch
matplotlib, and they import it LAZILY and raise a clear, actionable RuntimeError
(pointing the user at ``render_env.py``) when it is absent. The colour constants
and the reportlab RGB tuples are pure stdlib and always available.

Public surface (pinned by contract):
  ACCENT, RED, GREEN, GRAY_TXT, GRAY_MID, HAIRLINE  -- hex strings
  ACCENT_RGB, RED_RGB, GREEN_RGB, GRAY_TXT_RGB, ... -- 0-1 float tuples (reportlab)
  DPI = 300, FIG_W = 10
  apply_mpl_style()      -- set the bank-note rcParams
  bank_axes(ax)          -- left/bottom gray spines + subtle y-grid
  kicker(ax, text)       -- bold-uppercase accent kicker, top-left above axes
  why(fig, text)         -- one-line italic-gray "why it matters", bottom-left
"""

# --------------------------------------------------------------------------- #
# Palette (contract-pinned hex strings).
# --------------------------------------------------------------------------- #
ACCENT = "#BF5700"    # burnt orange -- the single brand accent
RED = "#C00000"       # loss / invalidation / warning
GREEN = "#2E7D32"     # gain / positive
GRAY_TXT = "#444444"  # dark gray body text on charts
GRAY_MID = "#888888"  # mid gray: spines, secondary labels, "why" annotation
HAIRLINE = "#DDDDDD"  # light gray hairlines / grid / track bars
INK = "#222222"       # near-black for primary chart ink
TRACK = "#EDEDED"     # score-bar track fill


def _hex_to_rgb(h):
    """Convert ``#RRGGBB`` to a 0-1 float 3-tuple for reportlab / matplotlib."""
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


# reportlab colour tuples (0-1 floats).
ACCENT_RGB = _hex_to_rgb(ACCENT)
RED_RGB = _hex_to_rgb(RED)
GREEN_RGB = _hex_to_rgb(GREEN)
GRAY_TXT_RGB = _hex_to_rgb(GRAY_TXT)
GRAY_MID_RGB = _hex_to_rgb(GRAY_MID)
HAIRLINE_RGB = _hex_to_rgb(HAIRLINE)
INK_RGB = _hex_to_rgb(INK)
WHITE_RGB = (1.0, 1.0, 1.0)

# --------------------------------------------------------------------------- #
# Layout constants (contract-pinned).
# --------------------------------------------------------------------------- #
DPI = 300      # print-quality raster
FIG_W = 10     # nominal figure width in inches (charts scale from this)

FONT_FAMILY = "Helvetica"

_ABSENT_MSG = (
    "matplotlib is required to render charts but is not installed in this "
    "environment. Bootstrap the render venv with:\n"
    "    python3 scripts/render_env.py --check\n"
    "then invoke the renderers with the venv's python (the path it prints)."
)


def _require_mpl():
    """Import matplotlib lazily; raise a clear RuntimeError if it is absent.

    Keeping this behind a function means ``import scripts.tdstyle`` never needs
    matplotlib -- only the drawing helpers do.
    """
    try:
        import matplotlib  # noqa: F401
        import matplotlib.pyplot as plt  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only sans-mpl
        raise RuntimeError(_ABSENT_MSG) from exc
    return matplotlib, plt


def apply_mpl_style():
    """Apply the bank-note matplotlib rcParams (white bg, small Helvetica).

    Idempotent; safe to call before every chart. Raises RuntimeError (via
    ``_require_mpl``) with an actionable message if matplotlib is not installed.
    """
    _matplotlib, plt = _require_mpl()
    plt.rcParams.update({
        "font.family": FONT_FAMILY,
        "font.size": 8,
        "axes.edgecolor": GRAY_MID,
        "axes.linewidth": 0.6,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
        "text.color": INK,
        "axes.labelcolor": INK,
        "xtick.color": GRAY_TXT,
        "ytick.color": GRAY_TXT,
        "xtick.labelsize": 7.5,
        "ytick.labelsize": 7.5,
    })


def bank_axes(ax, grid_axis="y"):
    """Left+bottom gray spines only, subtle grid on ``grid_axis``.

    The sell-side look: no top/right box, thin gray spines, a faint grid behind
    the data. ``grid_axis`` is "y" for most charts, "x" for horizontal bars.
    """
    _require_mpl()
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRAY_MID)
        ax.spines[side].set_linewidth(0.6)
    ax.tick_params(length=2.5, width=0.5, colors=GRAY_TXT)
    if grid_axis:
        ax.grid(axis=grid_axis, color=HAIRLINE, linewidth=0.5, alpha=0.8, zorder=0)
    ax.set_axisbelow(True)
    return ax


def kicker(ax, text):
    """Small bold-uppercase accent kicker, top-left, above the axes.

    Returns the created Text artist so callers/tests can inspect it.
    """
    _require_mpl()
    return ax.text(0.0, 1.045, text.upper(), transform=ax.transAxes,
                   fontsize=8.5, fontweight="bold", color=ACCENT,
                   ha="left", va="bottom", family=FONT_FAMILY)


def why(fig, text, x=0.008, y=0.012):
    """One-line italic-gray 'why it matters' annotation, figure bottom-left.

    Returns the created Text artist.
    """
    _require_mpl()
    return fig.text(x, y, text, fontsize=6.6, style="italic",
                    color=GRAY_MID, ha="left", va="bottom", family=FONT_FAMILY)
