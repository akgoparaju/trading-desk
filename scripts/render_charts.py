"""Deterministic chart pack for the PDF docket (exec 8 + detail 8).

WHY THIS MODULE EXISTS: charts carry NUMBERS onto the page, so -- exactly like the
md report -- the architecture forbids the LLM from touching them. Every chart is
minted in Python from the bundle's QC'd JSONs. Each chart is a pair:

  ``extract_<name>(docs) -> dict | None``   PURE. Pulls the exact arrays from the
      bundle (unit-tested against fixtures for exact values). Returns ``None`` when
      a required input is absent -> the chart is SKIPPED (never fabricated), with a
      reason recorded in ``charts_manifest.json``.
  ``draw_<name>(data, path)``               Paints the extracted dict to a PNG
      using the shared ``tdstyle`` bank-note style. matplotlib is imported LAZILY
      (via tdstyle) so this module -- and its extract functions -- import cleanly
      on a machine without matplotlib. Only ``draw_*`` / the CLI need it.

``docs`` is the loaded bundle: ``{"snapshot", "module_<x>" ..., "daily"}`` where
``daily`` is the ascending daily rows (reused from build_snapshot's parser). The
chart data sources are pinned by contract; where the plan says ``technicals.*`` /
``fundamentals.*`` / ``valuation.*`` those live in the SNAPSHOT blocks, not the
module files.

The mockup-nit fixes are REQUIREMENTS here: LABELED event callouts (no bare "E"),
collision-free timeline labels (alternate above/below), a single caption per
chart, and visible weight ticks on the score bars.

CLI: ``render_charts.py --bundle <dir> --set exec|detail|all [--out <dir>]``.
Writes the PNGs + a ``charts_manifest.json`` with an entry per chart
(``{"chart","status":"ok","png"}`` or ``{"chart","status":"skipped","reason"}``).

stdlib-only at import; matplotlib only inside draw_*; >=3.10 guard.
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

from scripts import tdstyle
from scripts.build_snapshot import load_daily_raw, parse_daily_rows

# Charts by set (order = render order = manifest order).
EXEC_CHARTS = [
    "price_volume", "range52w", "scenario_fan", "football_field",
    "score_bars", "revisions", "pe_band", "catalyst_timeline",
]
DETAIL_CHARTS = [
    "downside_ladder", "drawdown_history", "vol_regime", "vol_term_structure",
    "skew", "expected_move_cone", "oi_walls", "subscore_breakdown",
]

# ~1 trading year window for the price/pe series.
_YEAR_SESSIONS = 252
# Cap price-chart ladder shelves to the few nearest the last price (label
# legibility). Reduced from 4 -> 3: with tightly-clustered real ladders four
# right-edge labels overprint each other and the price dot (review finding #1).
_MAX_SHELVES = 3


# --------------------------------------------------------------------------- #
# Pure geometry helpers (NO matplotlib) -- unit-tested for exact positions.
# --------------------------------------------------------------------------- #

def stagger_positions(values, min_gap):
    """Push a set of label y-values apart so adjacent labels clear ``min_gap``.

    PURE. Given label positions (any order) and a minimum vertical gap, return a
    list -- ALIGNED to the input order -- of adjusted positions that (a) preserve
    the original ordering of the values, (b) guarantee every adjacent pair is at
    least ``min_gap`` apart, and (c) push symmetrically so the adjusted cluster
    keeps the same mean as the input (labels spread up AND down, never all one
    way). Values already spaced >= ``min_gap`` are returned unchanged.

    Examples (exact, pinned by unit test):
      stagger_positions([850, 864], 40) -> [837.0, 877.0]   # span 14 -> 40, recentred
      stagger_positions([100, 200], 40) -> [100.0, 200.0]   # already spaced: unchanged
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [float(values[0])]

    # Work in value-sorted order, remembering the original slot of each item.
    order = sorted(range(n), key=lambda i: values[i])
    sorted_vals = [float(values[i]) for i in order]

    # Forward pass: shove each label up to at least prev + min_gap.
    adjusted = [sorted_vals[0]]
    for v in sorted_vals[1:]:
        adjusted.append(max(v, adjusted[-1] + min_gap))

    # Re-centre the whole adjusted stack onto the original mean so the spread is
    # symmetric (up and down) rather than only-upward.
    shift = (sum(sorted_vals) - sum(adjusted)) / n
    adjusted = [a + shift for a in adjusted]

    # Scatter back to the caller's original ordering.
    out = [0.0] * n
    for slot, val in zip(order, adjusted):
        out[slot] = val
    return out


def clamp_callout_y(y, ylim, pad_frac=0.04):
    """Clamp a callout's y so its text stays inside the axes with a small pad.

    PURE. ``ylim`` is ``(low, high)``; ``pad_frac`` is a fraction of the span
    reserved at each edge. Returns ``y`` unchanged when already inside the padded
    band, else the nearest padded edge -- so an event tag lifted toward the top of
    the figure no longer clips the upper margin (review finding #2).
    """
    lo, hi = float(ylim[0]), float(ylim[1])
    if hi < lo:
        lo, hi = hi, lo
    pad = (hi - lo) * pad_frac
    lo_p, hi_p = lo + pad, hi - pad
    if lo_p > hi_p:  # degenerate span: pin to the midpoint.
        return (lo + hi) / 2.0
    return min(max(float(y), lo_p), hi_p)


# --------------------------------------------------------------------------- #
# Bundle loading.
# --------------------------------------------------------------------------- #

def _load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _find_snapshot(bundle):
    hits = sorted(glob.glob(os.path.join(bundle, "snapshot_*.json")))
    return hits[0] if hits else None


def _find_daily(bundle):
    for cand in (os.path.join(bundle, "raw", "daily_adjusted.json"),
                 os.path.join(bundle, "daily_adjusted.json")):
        if os.path.isfile(cand):
            return cand
    return None


def load_docs(bundle):
    """Load a bundle into the ``docs`` dict the extract functions consume.

    Keys: ``snapshot`` (dict|None), ``module_<name>`` for each module file found,
    and ``daily`` (ascending rows list, possibly empty). Missing pieces are
    tolerated -- individual extracts decide whether they can proceed.
    """
    docs = {"snapshot": None, "daily": []}
    snap_path = _find_snapshot(bundle)
    if snap_path:
        docs["snapshot"] = _load_json(snap_path)
    for path in sorted(glob.glob(os.path.join(bundle, "module_*.json"))):
        name = os.path.splitext(os.path.basename(path))[0]  # module_technical
        docs[name] = _load_json(path)
    daily_path = _find_daily(bundle)
    if daily_path:
        raw = load_daily_raw(daily_path)
        if raw is not None:
            try:
                docs["daily"] = parse_daily_rows(raw)
            except Exception:  # noqa: BLE001 - a bad daily file just yields no series
                docs["daily"] = []
    return docs


# --------------------------------------------------------------------------- #
# Small helpers shared by extracts.
# --------------------------------------------------------------------------- #

def _snap(docs):
    return docs.get("snapshot") or {}


def _last_price(docs):
    price = _snap(docs).get("price", {}) or {}
    last = price.get("last")
    if last is not None:
        return float(last)
    daily = docs.get("daily") or []
    if daily:
        return float(daily[-1].get("adjusted_close") or daily[-1].get("close"))
    return None


def _window_daily(docs, n=_YEAR_SESSIONS):
    daily = docs.get("daily") or []
    return daily[-n:] if daily else []


# --------------------------------------------------------------------------- #
# EXEC extracts.
# --------------------------------------------------------------------------- #

def extract_price_volume(docs):
    """1yr daily price + volume + labeled event callouts + ladder shelves."""
    window = _window_daily(docs)
    if len(window) < 2:
        return None
    dates = [r["date"] for r in window]
    closes = [float(r.get("adjusted_close") or r.get("close")) for r in window]
    vols = [float(r.get("volume") or 0.0) for r in window]

    # Ladder shelves LABELED (mockup-nit fix: no bare markers). A real ladder can
    # carry a dozen rungs, whose right-edge labels would overlap into an
    # unreadable stack -- keep only the few nearest the current price.
    last_px = closes[-1]
    all_shelves = []
    tech = docs.get("module_technical") or {}
    for lvl in (tech.get("ladder") or []):
        level = lvl.get("level")
        typ = lvl.get("type", "")
        if level is None:
            continue
        all_shelves.append({"level": float(level),
                            "label": "%s %s" % (_money(level),
                                                typ.replace("_", " "))})
    all_shelves.sort(key=lambda s: abs(s["level"] - last_px))
    shelves = sorted(all_shelves[:_MAX_SHELVES], key=lambda s: s["level"])

    # Event callouts placed at their date if it falls inside the window. On the
    # price chart the label sits over the series, so keep it SHORT (nit fix: a
    # compact tag, not the full catalyst sentence).
    date_set = set(dates)
    events = []
    for cat in (_snap(docs).get("events", {}).get("catalysts") or []):
        d = cat.get("date")
        if d in date_set:
            # Tag truncated at 24 chars w/ ellipsis (review finding #2): short
            # enough to sit over the series without running off the top margin.
            events.append({"date": d, "label": _short_event(cat.get("event", ""),
                                                            limit=24)})
    ne = _snap(docs).get("events", {}).get("next_earnings") or {}
    if ne.get("date") in date_set:
        events.append({"date": ne["date"], "label": "Earnings"})

    return {"dates": dates, "closes": closes, "volumes": vols,
            "shelves": shelves, "events": events, "last": closes[-1]}


def extract_range52w(docs):
    """52-week range bar with the entry band from the tradeplan entries."""
    price = _snap(docs).get("price", {}) or {}
    lo, hi = price.get("wk52_low"), price.get("wk52_high")
    last = _last_price(docs)
    if lo is None or hi is None or last is None:
        return None
    tp = docs.get("module_tradeplan") or {}
    entries = ((tp.get("stock_plan") or {}).get("entries") or [])
    levels = [float(e["level"]) for e in entries if e.get("level") is not None]
    entry_low = min(levels) if levels else None
    entry_high = max(levels) if levels else None
    return {"low": float(lo), "high": float(hi), "last": float(last),
            "entry_low": entry_low, "entry_high": entry_high}


def extract_scenario_fan(docs):
    """Bull/base/bear fan from composite.ev.scenarios; prob-weighted EV endpoint."""
    comp = docs.get("module_composite") or {}
    scen = ((comp.get("ev") or {}).get("scenarios") or [])
    last = _last_price(docs)
    if not scen or last is None:
        return None
    scenarios = [{"name": s.get("name"), "prob": float(s.get("prob")),
                  "price_target": float(s.get("price_target"))} for s in scen]
    ev_price = sum(s["prob"] * s["price_target"] for s in scenarios)
    return {"scenarios": scenarios, "last": float(last), "ev_price": ev_price}


def extract_football_field(docs):
    """Valuation anchors: ladder supports + valuation floor + consensus PT +
    scenario targets, all vs the current price."""
    last = _last_price(docs)
    if last is None:
        return None
    rows = []

    # Ladder-support anchor (review finding #5): the OLD contract spanned the
    # band from the LOWEST to the HIGHEST support below the price -- on a real
    # ladder that is min..max of a dozen rungs (MU: 370..850), a ~480-wide bar
    # that says nothing about where support actually sits. The anchor that
    # matters is the floor DIRECTLY beneath the price, so the NEW contract is the
    # band between the TWO nearest proven supports below last (MU: 803.5..850);
    # with only one support below, it is a single dot at that level.
    tech = docs.get("module_technical") or {}
    supports = sorted(
        [float(l["level"]) for l in (tech.get("ladder") or [])
         if l.get("level") is not None and float(l["level"]) < last])
    nearest = supports[-2:]  # up to the two closest below the price
    if len(nearest) >= 2:
        rows.append({"label": "Ladder support", "lo": nearest[0],
                     "hi": nearest[1], "kind": "band", "color": "accent"})
    elif len(nearest) == 1:
        rows.append({"label": "Ladder support", "lo": nearest[0],
                     "hi": nearest[0], "kind": "dot", "color": "accent"})

    # Valuation floor from the risk downside_map.
    risk = docs.get("module_risk") or {}
    dmap = ((risk.get("tables") or {}).get("downside_map") or [])
    floors = [float(r["level"]) for r in dmap
              if r.get("type") == "valuation_floor" and r.get("level") is not None]
    if floors:
        rows.append({"label": "Valuation floor", "lo": min(floors),
                     "hi": max(floors), "kind": "band", "color": "gray"})

    # Scenario targets (bull / bear) from composite.
    comp = docs.get("module_composite") or {}
    scen = {s.get("name"): s for s in ((comp.get("ev") or {}).get("scenarios") or [])}
    if "bear" in scen:
        bt = float(scen["bear"]["price_target"])
        rows.append({"label": "Bear target", "lo": bt, "hi": bt,
                     "kind": "dot", "color": "red"})
    if "base" in scen:
        bt = float(scen["base"]["price_target"])
        rows.append({"label": "Base target", "lo": bt, "hi": bt,
                     "kind": "dot", "color": "accent"})
    if "bull" in scen:
        bt = float(scen["bull"]["price_target"])
        rows.append({"label": "Bull target", "lo": bt, "hi": bt,
                     "kind": "dot", "color": "green"})

    # Consensus price target from snapshot sentiment.
    cpt = _snap(docs).get("sentiment", {}).get("consensus_pt")
    if cpt is not None:
        rows.append({"label": "Consensus PT", "lo": float(cpt),
                     "hi": float(cpt), "kind": "dot", "color": "accent"})

    if not rows:
        return None
    return {"rows": rows, "last": float(last)}


def extract_score_bars(docs):
    """5 dimension scores + contribution weights (visible ticks)."""
    comp = docs.get("module_composite") or {}
    dims = comp.get("dimensions") or []
    if not dims:
        return None
    label_map = {
        "technical": "Technical", "fundamental": "Fundamental",
        "sentiment": "Sentiment", "risk": "Risk",
        "thesis_conviction": "Conviction",
    }
    bars = []
    for d in dims:
        name = d.get("name")
        bars.append({
            "label": label_map.get(name, str(name).title()),
            "score": d.get("score"),
            "weight_pct": round(float(d.get("weight", 0.0)) * 100, 4),
        })
    return {"bars": bars, "composite": comp.get("score")}


def extract_revisions(docs):
    """NTM EPS revisions from snapshot.fundamentals.revisions_90d."""
    rev = (_snap(docs).get("fundamentals", {}) or {}).get("revisions_90d")
    if not rev or rev.get("eps_now") is None or rev.get("eps_90d_ago") is None:
        return None
    return {
        "eps_now": float(rev["eps_now"]),
        "eps_90d_ago": float(rev["eps_90d_ago"]),
        "pct": float(rev.get("pct")) if rev.get("pct") is not None else None,
        "up_30d": float(rev.get("up_30d")) if rev.get("up_30d") is not None else None,
        "down_30d": (float(rev.get("down_30d"))
                     if rev.get("down_30d") is not None else None),
    }


def extract_pe_band(docs):
    """Trailing P/E series (daily close / eps_ttm) vs the 5yr median.

    Method label from valuation.pe_median_method (disclosed on the chart).
    """
    window = _window_daily(docs)
    fund = _snap(docs).get("fundamentals", {}) or {}
    val = _snap(docs).get("valuation", {}) or {}
    eps_ttm = fund.get("eps_ttm")
    median_pe = val.get("pe_5yr_median")
    if len(window) < 2 or not eps_ttm or median_pe is None:
        return None
    dates = [r["date"] for r in window]
    pe_series = [float(r.get("adjusted_close") or r.get("close")) / float(eps_ttm)
                 for r in window]
    return {"dates": dates, "pe_series": pe_series,
            "median_pe": float(median_pe), "eps_ttm": float(eps_ttm),
            "method": val.get("pe_median_method")}


def extract_catalyst_timeline(docs):
    """Forward catalyst events; alternate above/below to avoid label collisions."""
    events = []
    ne = _snap(docs).get("events", {}).get("next_earnings") or {}
    cats = _snap(docs).get("events", {}).get("catalysts") or []
    seen = set()
    # Prefer the catalyst list (richer), then ensure next_earnings is present.
    for cat in cats:
        d = cat.get("date")
        if not d or d in seen:
            continue
        seen.add(d)
        events.append({"date": d, "label": _short_event(cat.get("event", ""))})
    if ne.get("date") and ne["date"] not in seen:
        events.append({"date": ne["date"], "label": "Earnings"})
        seen.add(ne["date"])
    if not events:
        return None
    events.sort(key=lambda e: e["date"])
    # Alternate placement side so adjacent labels never overlap (nit fix).
    for i, e in enumerate(events):
        e["side"] = "above" if i % 2 == 0 else "below"
    return {"events": events}


# --------------------------------------------------------------------------- #
# DETAIL extracts.
# --------------------------------------------------------------------------- #

def extract_downside_ladder(docs):
    """Downside anchors from risk.tables.downside_map (rungs below current)."""
    risk = docs.get("module_risk") or {}
    dmap = ((risk.get("tables") or {}).get("downside_map") or [])
    last = _last_price(docs)
    if not dmap or last is None:
        return None
    rungs = []
    for r in dmap:
        lvl = r.get("level")
        if lvl is None:
            continue
        rungs.append({"level": float(lvl), "type": r.get("type", ""),
                      "pct_from_last": r.get("pct_from_last")})
    if not rungs:
        return None
    rungs.sort(key=lambda x: x["level"], reverse=True)
    return {"rungs": rungs, "last": float(last)}


def extract_drawdown_history(docs):
    """Per-year max drawdown from snapshot.technicals.drawdowns_by_year."""
    ddy = (_snap(docs).get("technicals", {}) or {}).get("drawdowns_by_year")
    if not ddy:
        return None
    bars = [{"year": int(b["year"]), "max_dd": float(b["max_dd"])}
            for b in ddy if b.get("year") is not None and b.get("max_dd") is not None]
    if not bars:
        return None
    bars.sort(key=lambda b: b["year"])
    return {"bars": bars}


def extract_vol_regime(docs):
    """Realized-vol regime: rv30 vs its 10yr percentile + beta note."""
    tech = _snap(docs).get("technicals", {}) or {}
    rv30 = tech.get("rv30_ann")
    pctile = tech.get("rv30_vs_10yr_pctile")
    if rv30 is None or pctile is None:
        return None
    beta = (_snap(docs).get("benchmark", {}) or {}).get("beta")
    if beta is None:
        # fall back to risk vol_profile beta if present.
        beta = (((docs.get("module_risk") or {}).get("tables") or {})
                .get("vol_profile", {}) or {}).get("beta")
    return {"rv30": float(rv30), "pctile": float(pctile),
            "rv20": (float(tech["rv20_ann"]) if tech.get("rv20_ann") is not None
                     else None),
            "beta": float(beta) if beta is not None else None}


def extract_vol_term_structure(docs):
    """ATM IV by expiry (tenor-windowed) from options.vol_dashboard."""
    opt = docs.get("module_options") or {}
    atm = ((opt.get("vol_dashboard") or {}).get("atm_iv_by_expiry") or [])
    pts = [{"expiry": p.get("expiry"), "atm_iv": float(p["atm_iv"])}
           for p in atm if p.get("atm_iv") is not None and p.get("expiry")]
    if len(pts) < 2:
        return None
    return {"points": pts,
            "term_structure": (opt.get("vol_dashboard") or {}).get("term_structure")}


def extract_skew(docs):
    """25-delta 30-day skew + verdict context from options.vol_dashboard."""
    vd = (docs.get("module_options") or {}).get("vol_dashboard") or {}
    skew = vd.get("skew_25d_30d")
    if skew is None:
        return None
    return {"skew_25d_30d": float(skew), "verdict": vd.get("verdict"),
            "iv30": (float(vd["iv30"]) if vd.get("iv30") is not None else None)}


def extract_expected_move_cone(docs):
    """Expected-move cone from options.expected_moves around the current price."""
    opt = docs.get("module_options") or {}
    moves = opt.get("expected_moves") or []
    last = _last_price(docs)
    if not moves or last is None:
        return None
    out = []
    for m in moves:
        if m.get("one_sigma") is None or not m.get("expiry"):
            continue
        out.append({"expiry": m["expiry"], "one_sigma": float(m["one_sigma"]),
                    "straddle": (float(m["straddle"])
                                 if m.get("straddle") is not None else None)})
    if not out:
        return None
    return {"moves": out, "last": float(last)}


def extract_oi_walls(docs):
    """Open-interest walls / near-money clusters from options.flow.oi_walls."""
    opt = docs.get("module_options") or {}
    walls_src = (opt.get("flow") or {}).get("oi_walls")
    last = _last_price(docs)
    if not walls_src or last is None:
        return None
    walls = []
    for c in (walls_src.get("near_money_clusters") or []):
        if c.get("strike") is None:
            continue
        walls.append({"strike": float(c["strike"]), "oi": c.get("oi"),
                      "type": c.get("type", "")})
    # Ensure the named call/put walls are represented.
    for key, typ in (("call_wall", "call"), ("put_wall", "put")):
        w = walls_src.get(key)
        if w and w.get("strike") is not None:
            if not any(x["strike"] == float(w["strike"]) for x in walls):
                walls.append({"strike": float(w["strike"]), "oi": w.get("oi"),
                              "type": typ})
    if not walls:
        return None
    walls.sort(key=lambda x: x["strike"])
    return {"walls": walls, "last": float(last)}


def extract_subscore_breakdown(docs):
    """Per-dimension subscore bars grid across the four scoring modules."""
    panels = []
    for dim, key in (("technical", "module_technical"),
                     ("fundamental", "module_fundamental"),
                     ("sentiment", "module_sentiment"),
                     ("risk", "module_risk")):
        mod = docs.get(key) or {}
        subs = mod.get("subscores") or []
        rows = [{"name": s.get("name"), "points": s.get("points"),
                 "max": s.get("max")}
                for s in subs if s.get("name") is not None]
        if rows:
            panels.append({"dimension": dim, "subscores": rows})
    if not panels:
        return None
    return {"panels": panels}


# --------------------------------------------------------------------------- #
# Formatting helpers.
# --------------------------------------------------------------------------- #

def _money(v):
    return "${:,.0f}".format(v) if v >= 100 else "${:.2f}".format(v)


def _short_event(text, limit=42):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Draw functions (matplotlib, lazy). Each paints an extracted dict to ``path``.
# --------------------------------------------------------------------------- #

def _new_fig(w, h):
    tdstyle.apply_mpl_style()
    import matplotlib.pyplot as plt
    return plt, plt.subplots(figsize=(w, h))


def _save(plt, fig, path):
    fig.savefig(path, dpi=tdstyle.DPI, bbox_inches="tight", pad_inches=0.06,
                facecolor="white")
    plt.close(fig)


def draw_price_volume(data, path):
    tdstyle.apply_mpl_style()
    import matplotlib.pyplot as plt
    fig, (axp, axv) = plt.subplots(
        2, 1, figsize=(tdstyle.FIG_W * 0.62, 3.1), sharex=True,
        gridspec_kw={"height_ratios": [4.2, 1.0], "hspace": 0.06})
    n = len(data["closes"])
    x = list(range(n))
    axp.plot(x, data["closes"], color=tdstyle.ACCENT, linewidth=1.15, zorder=5)
    date_to_x = {d: i for i, d in enumerate(data["dates"])}

    # Reserve right-margin room so shelf labels sit clear of the series.
    axp.set_xlim(-2, n + n * 0.16)

    # LABELED ladder shelves (no bare markers). The dashed lines stay at their
    # TRUE levels, but the right-edge LABELS are pushed apart with the pure
    # stagger helper so a tight cluster (real MU bundle: shelves within ~$96) no
    # longer overprints itself or the price dot (review finding #1). A leader
    # line links each label back to its true level when the two differ.
    ordered = sorted(data["shelves"], key=lambda s: s["level"])
    levels = [sh["level"] for sh in ordered]
    price_span = (max(data["closes"]) - min(data["closes"])) or 1.0
    label_gap = price_span * 0.06  # min vertical gap between adjacent labels
    label_ys = stagger_positions(levels, label_gap)
    for sh, lvl, ly in zip(ordered, levels, label_ys):
        axp.axhline(lvl, color=tdstyle.GRAY_MID, linewidth=0.7,
                    linestyle=(0, (4, 3)), zorder=2)
        if abs(ly - lvl) > 1e-6:
            axp.plot([n - 0.5, n + 0.8], [lvl, ly], color=tdstyle.GRAY_MID,
                     linewidth=0.5, alpha=0.6, zorder=2)
        axp.text(n + 1, ly, sh["label"], va="center", ha="left",
                 fontsize=6.0, color=tdstyle.GRAY_TXT)

    # LABELED event callouts (no bare "E"); lifted above the local high so the
    # tag clears the price line, but CLAMPED inside the axes so the rotated text
    # no longer clips the top figure margin (review finding #2).
    ymin, ymax = min(data["closes"]), max(data["closes"])
    ylim_lo = ymin - price_span * 0.05
    ylim_hi = ymax + price_span * 0.16
    axp.set_ylim(ylim_lo, ylim_hi)
    callout_y = clamp_callout_y(ymax + price_span * 0.14, (ylim_lo, ylim_hi))
    for ev in data["events"]:
        xi = date_to_x.get(ev["date"])
        if xi is None:
            continue
        yv = data["closes"][xi]
        axp.scatter([xi], [yv], s=16, marker="^", facecolor="white",
                    edgecolor=tdstyle.ACCENT, linewidth=0.8, zorder=6)
        axp.annotate(ev["label"], xy=(xi, yv),
                     xytext=(xi, callout_y), fontsize=6.0,
                     color=tdstyle.ACCENT, ha="center", va="top",
                     rotation=90,
                     arrowprops=dict(arrowstyle="-", color=tdstyle.ACCENT,
                                     lw=0.5, alpha=0.6))

    axp.scatter([n - 1], [data["last"]], s=22, color=tdstyle.RED, zorder=7)
    tdstyle.bank_axes(axp)
    axp.tick_params(axis="x", labelbottom=False, length=0)
    axp.spines["bottom"].set_visible(False)

    axv.bar(x, data["volumes"], width=1.0, color=tdstyle.HAIRLINE,
            edgecolor="none", zorder=3)
    axv.bar([n - 1], [data["volumes"][-1]], width=1.0, color=tdstyle.RED,
            alpha=0.55, zorder=4)
    axv.set_yticks([])
    for side in ("top", "right", "left"):
        axv.spines[side].set_visible(False)
    axv.spines["bottom"].set_color(tdstyle.GRAY_MID)
    _month_ticks(axv, data["dates"])
    axv.text(0.0, 0.86, "VOLUME", transform=axv.transAxes, fontsize=6.0,
             color=tdstyle.GRAY_MID, fontweight="bold", va="top")

    tdstyle.kicker(axp, "1-Year Daily Price & Volume")
    tdstyle.why(fig, "Price vs the support shelves that frame the entry.")
    fig.subplots_adjust(left=0.08, right=0.98, top=0.90, bottom=0.11)
    _save(plt, fig, path)


def _month_ticks(ax, dates, k=6):
    n = len(dates)
    if n == 0:
        return
    idxs = [int(round(i * (n - 1) / (k - 1))) for i in range(k)]
    ax.set_xticks(idxs)
    ax.set_xticklabels([dates[i][:7] for i in idxs], fontsize=6.0)


def draw_range52w(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.62, 0.9)
    lo, hi, last = data["low"], data["high"], data["last"]
    span = hi - lo or 1.0
    ax.barh([0], [hi - lo], left=[lo], height=0.4, color=tdstyle.HAIRLINE,
            zorder=1)
    if data["entry_low"] is not None and data["entry_high"] is not None:
        elo, ehi = data["entry_low"], data["entry_high"]
        ax.barh([0], [max(ehi - elo, span * 0.01)], left=[elo], height=0.4,
                color=tdstyle.ACCENT, alpha=0.28, zorder=2)
        ax.text((elo + ehi) / 2, -0.42, "entry %.0f-%.0f" % (elo, ehi),
                ha="center", va="top", fontsize=6.2, color=tdstyle.ACCENT)
    ax.plot([last, last], [-0.25, 0.25], color=tdstyle.RED, linewidth=2.0,
            zorder=4)
    ax.text(lo, 0.42, "$%.0f" % lo, ha="left", va="bottom", fontsize=6.4,
            color=tdstyle.GRAY_TXT)
    ax.text(hi, 0.42, "$%.0f" % hi, ha="right", va="bottom", fontsize=6.4,
            color=tdstyle.GRAY_TXT)
    ax.text(last, 0.30, "$%.0f" % last, ha="center", va="bottom", fontsize=6.6,
            color=tdstyle.RED, fontweight="bold")
    ax.set_ylim(-0.7, 0.8)
    ax.set_yticks([])
    ax.set_xticks([])
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    tdstyle.kicker(ax, "52-Week Range")
    fig.subplots_adjust(left=0.05, right=0.97, top=0.72, bottom=0.16)
    _save(plt, fig, path)


def draw_scenario_fan(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.42, 3.15)
    last = data["last"]
    color_map = {"bull": tdstyle.GREEN, "base": tdstyle.ACCENT, "bear": tdstyle.RED}
    for s in data["scenarios"]:
        yv = s["price_target"]
        col = color_map.get(s["name"], tdstyle.ACCENT)
        ls = "-" if s["name"] != "bear" else (0, (5, 2))
        ax.plot([0.0, 1.0], [last, yv], color=col, linewidth=1.2, alpha=0.9,
                zorder=3, linestyle=ls)
        ax.scatter([1.0], [yv], s=34, color=col, zorder=5)
        chg = (yv / last - 1) * 100
        ax.text(1.02, yv, "%s  $%.0f\n%+.0f%%  p=%.0f%%" % (
            s["name"].title(), yv, chg, s["prob"] * 100),
            va="center", ha="left", fontsize=7.0, color=tdstyle.INK,
            linespacing=1.25)
    ax.scatter([0.0], [last], s=40, color=tdstyle.INK, zorder=6)
    ax.text(-0.02, last, "Now\n$%.0f" % last, va="center", ha="right",
            fontsize=7.0, color=tdstyle.INK, fontweight="bold", linespacing=1.2)
    ev = data["ev_price"]
    ax.axhline(ev, color=tdstyle.GRAY_MID, linewidth=0.7, linestyle=(0, (2, 2)))
    ax.text(0.5, ev, "prob-weighted $%.0f (%+.1f%%)" % (ev, (ev / last - 1) * 100),
            fontsize=6.4, color=tdstyle.GRAY_TXT, ha="center", va="bottom",
            style="italic")
    ax.set_xlim(-0.30, 1.75)
    ax.set_xticks([])
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(tdstyle.GRAY_MID)
    ax.grid(axis="y", color=tdstyle.HAIRLINE, linewidth=0.5, alpha=0.8)
    ax.set_axisbelow(True)
    tdstyle.kicker(ax, "12-Month Scenario Fan")
    tdstyle.why(fig, "Probability-weighted skew of the 12-month outcomes.")
    fig.subplots_adjust(left=0.12, right=0.99, top=0.90, bottom=0.08)
    _save(plt, fig, path)


def draw_football_field(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.42, 3.15)
    color_map = {"accent": tdstyle.ACCENT, "gray": tdstyle.GRAY_MID,
                 "red": tdstyle.RED, "green": tdstyle.GREEN}
    rows = data["rows"]
    last = data["last"]
    # Anchor endpoint labels that fall within 2% of the now-line x are dropped
    # so they do not collide with the "$<last> now" label (review finding #3).
    all_vals = [v for r in rows for v in (r["lo"], r["hi"])]
    span = (max(all_vals + [last]) - min(all_vals + [last])) or 1.0
    near_now = span * 0.02

    def _endpoint_label(xval, yi, ha, text):
        if abs(xval - last) <= near_now:
            return  # too close to the now-line -> skip to avoid overprint
        ax.text(xval, yi, text, va="center", ha=ha, fontsize=6.0,
                color=tdstyle.GRAY_TXT)

    y = list(range(len(rows)))[::-1]
    for yi, r in zip(y, rows):
        col = color_map.get(r["color"], tdstyle.ACCENT)
        if r["kind"] == "band" and r["hi"] > r["lo"]:
            ax.plot([r["lo"], r["hi"]], [yi, yi], color=col, linewidth=7.0,
                    solid_capstyle="butt", alpha=0.85, zorder=3)
            _endpoint_label(r["lo"], yi, "right", "%.0f  " % r["lo"])
            _endpoint_label(r["hi"], yi, "left", "  %.0f" % r["hi"])
        else:
            ax.scatter([r["lo"]], [yi], s=42, color=col, zorder=5)
            _endpoint_label(r["lo"], yi, "left", "  %.0f" % r["lo"])
    ax.set_yticks(y)
    ax.set_yticklabels([r["label"] for r in rows], fontsize=7.0,
                       color=tdstyle.INK)
    ax.axvline(last, color=tdstyle.INK, linewidth=0.9,
               linestyle=(0, (3, 2)), zorder=4)
    # "now" label offset to the RIGHT of the dashed line at the TOP so it never
    # sits on top of a bar-endpoint label (review finding #3).
    ax.text(last + span * 0.015, len(rows) - 0.4, "$%.0f now" % last,
            rotation=90, fontsize=6.2, color=tdstyle.INK, va="top", ha="left",
            fontweight="bold")
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(tdstyle.GRAY_MID)
    ax.grid(axis="x", color=tdstyle.HAIRLINE, linewidth=0.5, alpha=0.8)
    ax.set_axisbelow(True)
    tdstyle.kicker(ax, "Valuation Anchors")
    tdstyle.why(fig, "Where the price sits relative to every anchor.")
    # bottom >= 0.16 so the why() caption clears the x-tick labels (finding #4).
    fig.subplots_adjust(left=0.24, right=0.97, top=0.90, bottom=0.16)
    _save(plt, fig, path)


def draw_score_bars(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.32, 2.1)
    bars = data["bars"]
    y = list(range(len(bars)))[::-1]
    ax.barh(y, [100] * len(bars), color=tdstyle.TRACK, height=0.62, zorder=1)
    ax.barh(y, [b["score"] for b in bars], color=tdstyle.ACCENT, height=0.62,
            zorder=2)
    for yi, b in zip(y, bars):
        ax.text(b["score"] + 2.5, yi, "%s" % b["score"], va="center", ha="left",
                fontsize=7.0, color=tdstyle.INK, fontweight="bold")
        # VISIBLE weight tick on the track (nit fix).
        w = b["weight_pct"]
        ax.plot([w, w], [yi - 0.33, yi + 0.33], color=tdstyle.GRAY_MID,
                linewidth=1.0, zorder=4)
    ax.set_yticks(y)
    ax.set_yticklabels([b["label"] for b in bars], fontsize=7.2,
                       color=tdstyle.INK)
    ax.set_xlim(0, 110)
    ax.set_xticks([])
    for side in ("top", "right", "bottom", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)
    ax.text(1.0, 1.08, "| = weight", transform=ax.transAxes, fontsize=5.8,
            color=tdstyle.GRAY_MID, ha="right", va="bottom")
    fig.subplots_adjust(left=0.30, right=0.95, top=0.90, bottom=0.04)
    _save(plt, fig, path)


def draw_revisions(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.42, 1.55)
    days = [-90, 0]
    eps = [data["eps_90d_ago"], data["eps_now"]]
    ax.plot(days, eps, color=tdstyle.ACCENT, linewidth=1.3, marker="o",
            markersize=3.5, markerfacecolor="white",
            markeredgecolor=tdstyle.ACCENT, markeredgewidth=0.9, zorder=4)
    ax.scatter([0], [data["eps_now"]], s=30, color=tdstyle.GREEN, zorder=5)
    if data["pct"] is not None:
        ax.annotate("%+.1f%% / 90d" % (data["pct"] * 100),
                    xy=(0, data["eps_now"]),
                    xytext=(-45, data["eps_now"]), fontsize=7.0,
                    color=tdstyle.GREEN, fontweight="bold", ha="left",
                    va="center")
    ax.set_xticks([-90, 0])
    ax.set_xticklabels(["90d ago", "now"], fontsize=6.6)
    tdstyle.bank_axes(ax)
    tdstyle.kicker(ax, "NTM EPS Revisions")
    # SINGLE caption (nit fix: no double caption).
    if data["up_30d"] is not None and data["down_30d"] is not None:
        tdstyle.why(fig, "Estimate momentum: %d up / %d down (30d)." % (
            int(data["up_30d"]), int(data["down_30d"])))
    fig.subplots_adjust(left=0.12, right=0.97, top=0.82, bottom=0.20)
    _save(plt, fig, path)


def draw_pe_band(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.42, 1.7)
    n = len(data["pe_series"])
    x = list(range(n))
    ax.plot(x, data["pe_series"], color=tdstyle.ACCENT, linewidth=1.15, zorder=4)
    ax.axhline(data["median_pe"], color=tdstyle.GRAY_MID, linewidth=0.8,
               linestyle=(0, (4, 3)), zorder=2)
    ax.text(n - 1, data["median_pe"], "  5yr median %.1fx" % data["median_pe"],
            va="center", ha="left", fontsize=6.2, color=tdstyle.GRAY_TXT)
    tdstyle.bank_axes(ax)
    _month_ticks(ax, data["dates"])
    tdstyle.kicker(ax, "Trailing P/E vs History")
    method = data.get("method")
    if method:
        tdstyle.why(fig, "P/E = close / EPS(ttm); median method: %s." % method)
    fig.subplots_adjust(left=0.10, right=0.97, top=0.86, bottom=0.18)
    _save(plt, fig, path)


def draw_catalyst_timeline(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.9, 1.2)
    events = data["events"]
    n = len(events)
    ax.axhline(0, color=tdstyle.GRAY_MID, linewidth=0.9, zorder=1)
    xs = [i for i in range(n)] if n > 1 else [0]
    for xi, e in zip(xs, events):
        ax.scatter([xi], [0], s=40, color=tdstyle.ACCENT, zorder=5)
        up = e["side"] == "above"
        yo = 0.30 if up else -0.30
        va = "bottom" if up else "top"
        ax.plot([xi, xi], [0, yo * 0.6], color=tdstyle.ACCENT, linewidth=0.7)
        ax.text(xi, yo, "%s\n%s" % (e["date"][5:], e["label"]), ha="center",
                va=va, fontsize=6.2, color=tdstyle.INK, linespacing=1.1)
    ax.set_xlim(-0.6, (n - 1) + 0.6 if n > 1 else 0.6)
    ax.set_ylim(-0.8, 0.8)
    ax.axis("off")
    tdstyle.kicker(ax, "Catalyst Timeline")
    fig.subplots_adjust(left=0.02, right=0.98, top=0.80, bottom=0.10)
    _save(plt, fig, path)


def draw_downside_ladder(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.5, 2.6)
    rungs = data["rungs"]
    y = list(range(len(rungs)))[::-1]
    for yi, r in zip(y, rungs):
        below = r["level"] < data["last"]
        col = tdstyle.RED if below else tdstyle.GRAY_MID
        ax.plot([data["last"], r["level"]], [yi, yi], color=col, linewidth=1.0,
                alpha=0.6, zorder=2)
        ax.scatter([r["level"]], [yi], s=30, color=col, zorder=4)
        pct = r.get("pct_from_last")
        pct_s = ("  %+.0f%%" % (pct * 100)) if pct is not None else ""
        ax.text(r["level"], yi, "  $%.0f%s" % (r["level"], pct_s), va="center",
                ha="left", fontsize=6.4, color=tdstyle.GRAY_TXT)
    ax.axvline(data["last"], color=tdstyle.INK, linewidth=0.9,
               linestyle=(0, (3, 2)), zorder=3)
    ax.set_yticks(y)
    ax.set_yticklabels([r["type"].replace("_", " ") for r in rungs],
                       fontsize=6.6, color=tdstyle.INK)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(tdstyle.GRAY_MID)
    ax.grid(axis="x", color=tdstyle.HAIRLINE, linewidth=0.5, alpha=0.8)
    ax.set_axisbelow(True)
    tdstyle.kicker(ax, "Downside Map")
    tdstyle.why(fig, "Support ladder below the current price.")
    fig.subplots_adjust(left=0.24, right=0.95, top=0.88, bottom=0.10)
    _save(plt, fig, path)


def draw_drawdown_history(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.5, 2.0)
    bars = data["bars"]
    x = list(range(len(bars)))
    ax.bar(x, [b["max_dd"] * 100 for b in bars], color=tdstyle.RED, alpha=0.7,
           width=0.7, zorder=3)
    ax.set_xticks(x)
    ax.set_xticklabels([str(b["year"]) for b in bars], fontsize=6.4, rotation=0)
    tdstyle.bank_axes(ax)
    ax.axhline(0, color=tdstyle.GRAY_MID, linewidth=0.6)
    tdstyle.kicker(ax, "Max Drawdown by Year")
    tdstyle.why(fig, "This name is structurally high-beta -- drawdowns are the norm.")
    fig.subplots_adjust(left=0.10, right=0.97, top=0.88, bottom=0.14)
    _save(plt, fig, path)


def draw_vol_regime(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.5, 1.7)
    ax.barh([0], [100], color=tdstyle.TRACK, height=0.5, zorder=1)
    ax.barh([0], [data["pctile"]], color=tdstyle.ACCENT, height=0.5, zorder=2)
    ax.plot([data["pctile"], data["pctile"]], [-0.3, 0.3], color=tdstyle.RED,
            linewidth=1.2, zorder=4)
    ax.text(data["pctile"], 0.4, "%.0fth pctile" % data["pctile"], ha="center",
            va="bottom", fontsize=6.6, color=tdstyle.RED, fontweight="bold")
    ax.set_ylim(-0.6, 0.9)
    ax.set_yticks([])
    ax.set_xlim(0, 105)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0", "25", "50", "75", "100"], fontsize=6.2)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(tdstyle.GRAY_MID)
    tdstyle.kicker(ax, "Realized-Vol Regime")
    note = "RV30 %.0f%% (10yr pctile %.0f)" % (data["rv30"] * 100, data["pctile"])
    if data["beta"] is not None:
        note += " -- beta %.2f" % data["beta"]
    tdstyle.why(fig, note + ".")
    fig.subplots_adjust(left=0.05, right=0.97, top=0.80, bottom=0.18)
    _save(plt, fig, path)


def draw_vol_term_structure(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.5, 1.9)
    pts = data["points"]
    x = list(range(len(pts)))
    ax.plot(x, [p["atm_iv"] * 100 for p in pts], color=tdstyle.ACCENT,
            linewidth=1.3, marker="o", markersize=3.5, markerfacecolor="white",
            markeredgecolor=tdstyle.ACCENT, markeredgewidth=0.9, zorder=4)
    ax.set_xticks(x)
    ax.set_xticklabels([p["expiry"][5:] for p in pts], fontsize=6.0, rotation=45,
                       ha="right")
    tdstyle.bank_axes(ax)
    tdstyle.kicker(ax, "ATM IV Term Structure")
    ts = data.get("term_structure")
    tdstyle.why(fig, "Term structure: %s." % ts if ts else
                "ATM implied vol across expiries.")
    fig.subplots_adjust(left=0.10, right=0.97, top=0.86, bottom=0.26)
    _save(plt, fig, path)


def draw_skew(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.5, 1.7)
    skew = data["skew_25d_30d"]
    col = tdstyle.RED if skew > 0 else tdstyle.GREEN
    ax.barh([0], [skew * 100], color=col, alpha=0.75, height=0.5, zorder=3)
    ax.axvline(0, color=tdstyle.GRAY_MID, linewidth=0.7)
    ax.text(skew * 100, 0, "  %+.1f pts" % (skew * 100), va="center",
            ha="left" if skew >= 0 else "right", fontsize=7.0,
            color=col, fontweight="bold")
    ax.set_yticks([])
    ax.set_ylim(-0.6, 0.6)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(tdstyle.GRAY_MID)
    ax.tick_params(labelsize=6.2)
    tdstyle.kicker(ax, "25-Delta Put/Call Skew (30d)")
    verdict = data.get("verdict")
    tdstyle.why(fig, "Positive skew = puts bid over calls%s." % (
        " (%s)" % verdict if verdict else ""))
    fig.subplots_adjust(left=0.05, right=0.97, top=0.80, bottom=0.16)
    _save(plt, fig, path)


def draw_expected_move_cone(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.5, 2.0)
    last = data["last"]
    moves = data["moves"]
    x = list(range(len(moves) + 1))
    upper = [last] + [last + m["one_sigma"] for m in moves]
    lower = [last] + [last - m["one_sigma"] for m in moves]
    ax.fill_between(x, lower, upper, color=tdstyle.ACCENT, alpha=0.18, zorder=1)
    ax.plot(x, upper, color=tdstyle.ACCENT, linewidth=1.0, zorder=3)
    ax.plot(x, lower, color=tdstyle.ACCENT, linewidth=1.0, zorder=3)
    ax.axhline(last, color=tdstyle.INK, linewidth=0.8, linestyle=(0, (2, 2)),
               zorder=2)
    ax.set_xticks(x)
    ax.set_xticklabels(["now"] + [m["expiry"][5:] for m in moves], fontsize=6.0,
                       rotation=45, ha="right")
    tdstyle.bank_axes(ax)
    tdstyle.kicker(ax, "Expected-Move Cone")
    tdstyle.why(fig, "1-sigma option-implied range by expiry.")
    fig.subplots_adjust(left=0.12, right=0.97, top=0.86, bottom=0.26)
    _save(plt, fig, path)


def draw_oi_walls(data, path):
    plt, (fig, ax) = _new_fig(tdstyle.FIG_W * 0.5, 2.0)
    walls = data["walls"]
    strikes = [w["strike"] for w in walls]
    ois = [(w["oi"] or 0) for w in walls]
    colors = [tdstyle.RED if w.get("type") == "put" else tdstyle.GREEN
              if w.get("type") == "call" else tdstyle.ACCENT for w in walls]
    ax.bar(range(len(walls)), ois, color=colors, alpha=0.75, width=0.7, zorder=3)
    ax.set_xticks(range(len(walls)))
    ax.set_xticklabels(["$%.0f" % s for s in strikes], fontsize=6.0, rotation=0)
    tdstyle.bank_axes(ax)
    tdstyle.kicker(ax, "Open-Interest Walls")
    tdstyle.why(fig, "Strikes where dealer positioning concentrates (spot $%.0f)."
                % data["last"])
    fig.subplots_adjust(left=0.12, right=0.97, top=0.88, bottom=0.14)
    _save(plt, fig, path)


def draw_subscore_breakdown(data, path):
    tdstyle.apply_mpl_style()
    import matplotlib.pyplot as plt
    panels = data["panels"]
    ncol = 2
    nrow = (len(panels) + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol,
                             figsize=(tdstyle.FIG_W * 0.9, 1.7 * nrow))
    axes = axes.flatten() if hasattr(axes, "flatten") else [axes]
    for i, panel in enumerate(panels):
        ax = axes[i]
        subs = panel["subscores"]
        y = list(range(len(subs)))[::-1]
        ax.barh(y, [s["max"] for s in subs], color=tdstyle.TRACK, height=0.6,
                zorder=1)
        ax.barh(y, [s["points"] for s in subs], color=tdstyle.ACCENT,
                height=0.6, zorder=2)
        for yi, s in zip(y, subs):
            ax.text(s["max"], yi, "  %d/%d" % (s["points"], s["max"]),
                    va="center", ha="left", fontsize=5.6, color=tdstyle.GRAY_TXT)
        ax.set_yticks(y)
        ax.set_yticklabels([s["name"].replace("_", " ") for s in subs],
                           fontsize=6.0, color=tdstyle.INK)
        ax.set_xticks([])
        for side in ("top", "right", "bottom", "left"):
            ax.spines[side].set_visible(False)
        ax.tick_params(length=0)
        tdstyle.kicker(ax, panel["dimension"])
    for j in range(len(panels), len(axes)):
        axes[j].axis("off")
    fig.subplots_adjust(left=0.14, right=0.92, top=0.92, bottom=0.05,
                        hspace=0.55, wspace=0.6)
    _save(plt, fig, path)


# --------------------------------------------------------------------------- #
# Chart registry: name -> (extract, draw, missing-input reason template).
# --------------------------------------------------------------------------- #

_REGISTRY = {
    "price_volume": (extract_price_volume, draw_price_volume,
                     "no daily price series in bundle"),
    "range52w": (extract_range52w, draw_range52w,
                 "missing 52-week range or last price"),
    "scenario_fan": (extract_scenario_fan, draw_scenario_fan,
                     "no composite.ev.scenarios"),
    "football_field": (extract_football_field, draw_football_field,
                       "no valuation anchors available"),
    "score_bars": (extract_score_bars, draw_score_bars,
                   "no composite dimensions"),
    "revisions": (extract_revisions, draw_revisions,
                  "no fundamentals.revisions_90d"),
    "pe_band": (extract_pe_band, draw_pe_band,
                "missing daily series, eps_ttm, or pe_5yr_median"),
    "catalyst_timeline": (extract_catalyst_timeline, draw_catalyst_timeline,
                          "no catalyst events"),
    "downside_ladder": (extract_downside_ladder, draw_downside_ladder,
                        "no risk downside_map"),
    "drawdown_history": (extract_drawdown_history, draw_drawdown_history,
                         "no technicals.drawdowns_by_year"),
    "vol_regime": (extract_vol_regime, draw_vol_regime,
                   "missing rv30 / percentile"),
    "vol_term_structure": (extract_vol_term_structure, draw_vol_term_structure,
                           "fewer than 2 atm_iv_by_expiry points (no chain?)"),
    "skew": (extract_skew, draw_skew, "no skew_25d_30d (no chain?)"),
    "expected_move_cone": (extract_expected_move_cone, draw_expected_move_cone,
                           "no options.expected_moves (no chain?)"),
    "oi_walls": (extract_oi_walls, draw_oi_walls,
                 "no options.flow.oi_walls (no chain?)"),
    "subscore_breakdown": (extract_subscore_breakdown, draw_subscore_breakdown,
                           "no module subscores"),
}


def _chart_names(which):
    if which == "exec":
        return list(EXEC_CHARTS)
    if which == "detail":
        return list(DETAIL_CHARTS)
    return list(EXEC_CHARTS) + list(DETAIL_CHARTS)


def render_set(docs, names, out_dir):
    """Render each named chart to ``out_dir``; return the manifest list.

    A chart whose extract returns None is SKIPPED (reason recorded). A draw
    failure (e.g. matplotlib absent) is also recorded as skipped with the error.
    """
    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    for name in names:
        extract, draw, reason = _REGISTRY[name]
        try:
            data = extract(docs)
        except Exception as exc:  # noqa: BLE001 - a bad module shouldn't abort the pack
            manifest.append({"chart": name, "status": "skipped",
                             "reason": "extract error: %s" % exc})
            continue
        if data is None:
            manifest.append({"chart": name, "status": "skipped",
                             "reason": reason})
            continue
        png_name = "%s.png" % name
        try:
            draw(data, os.path.join(out_dir, png_name))
        except RuntimeError as exc:  # matplotlib absent -> actionable message
            manifest.append({"chart": name, "status": "skipped",
                             "reason": str(exc).splitlines()[0]})
            continue
        except Exception as exc:  # noqa: BLE001
            manifest.append({"chart": name, "status": "skipped",
                             "reason": "draw error: %s" % exc})
            continue
        manifest.append({"chart": name, "status": "ok", "png": png_name})
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render the deterministic docket chart pack (exec 8 + "
                    "detail 8) from a bundle. Every number is script-minted from "
                    "the bundle JSONs; charts with missing inputs are skipped.")
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--set", dest="which", required=True,
                        choices=["exec", "detail", "all"],
                        help="which chart set to render")
    parser.add_argument("--out", default=None,
                        help="output dir (default <bundle>/charts)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundle):
        print("error: bundle dir not found: %s" % args.bundle, file=sys.stderr)
        return 2

    out_dir = args.out or os.path.join(args.bundle, "charts")
    docs = load_docs(args.bundle)
    names = _chart_names(args.which)
    manifest = render_set(docs, names, out_dir)

    with open(os.path.join(out_dir, "charts_manifest.json"), "w") as fh:
        json.dump({"set": args.which, "charts": manifest}, fh, indent=2)

    ok = sum(1 for m in manifest if m["status"] == "ok")
    skipped = sum(1 for m in manifest if m["status"] == "skipped")
    print("charts: %d ok, %d skipped -> %s" % (ok, skipped, out_dir))
    for m in manifest:
        if m["status"] == "skipped":
            print("  skipped %s: %s" % (m["chart"], m["reason"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
