"""Options-strategy decision skill (L3) for the trading-desk plugin.

WHY THIS MODULE EXISTS: the composite says WHETHER a name is a buy; the trade-plan says
HOW to put the stock position on and picks the EXPRESSION (stock vs options). This
module turns a DIRECTION + the REAL options chain into concrete, defined-risk option
STRUCTURES -- real strikes only, economics minted from chain marks, probabilities shown
as LABELED delta approximations, and a battery of MECHANICAL honesty gates. The LLM
layer narrates; every strike, credit, breakeven, and PoP here is minted in Python from
chain contracts and snapshot facts -- nothing is invented.

THE CENTRAL LESSON THIS MODULE ENCODES (MU 2026-07-15 prototype session): IV LEVEL alone
NEVER selects a strategy. IV-vs-REALIZED is the PRIMARY GATE. A 96% IV that LOOKS rich
but sits 14 points BELOW 110-116% realized is CHEAP vs realized -- premium sellers are
NOT being paid for delivered vol, and a naive "sell premium" call would have been wrong.
So `vol_verdict(iv_minus_rv20)` gates the whole selection matrix, not the IV percentile.

DESIGN CONTRACT (project-wide, mirrors composite-score / trade-plan):
- ``INPUT_FIELDS`` is EMPTY. This module scores NO snapshot field directly -- it reads a
  handful of snapshot facts (options.iv_minus_rv20, sentiment.iv30/iv_pctile, the
  expected_moves, the flow block) as STRUCTURE references, never as scored rubric inputs.
  The single-mapping rule is preserved BY CONSTRUCTION (an empty scored set collides with
  nothing); the module is added to tests/test_single_mapping.py SKILLS for uniformity.
- The chain is loaded ONLY via scripts.chain.load_contracts and NEVER printed or returned
  whole -- this module reads it on disk and emits compact derived structures.
- Two modes. ``pipeline`` derives direction from module_composite grade (A|B -> bullish,
  C -> neutral, D -> bearish) and aligns to module_tradeplan (entry_1 CSP alignment,
  hedge spec) -- both files REQUIRED (exit 2 if missing). ``standalone`` REQUIRES an
  explicit ``--direction`` (exit 2 if absent) and needs neither module.

stdlib-only; >=3.10 guard. The structure-building functions are pure over parsed inputs.
"""

import argparse
import glob
import json
import os
import sys
from datetime import date

if sys.version_info < (3, 10):
    sys.exit("trading-desk requires Python >= 3.10 (found %d.%d)" % sys.version_info[:2])

# Allow direct invocation (``python3 scripts/options_strategy.py``): ensure the repo
# root is importable so ``from scripts import ...`` resolves the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import chain as chain_mod
from scripts._artifact import emit_json

RUBRIC_VERSION = "1.1.0"
SKILL_NAME = "options-strategy"

# This module scores NO snapshot field directly (it reads snapshot facts only as
# structure references). Empty by construction -> single-mapping safe.
INPUT_FIELDS = set()

# No fields gate/cap a scoring branch here (there is no scoring), so GUARD_FIELDS is
# empty (declared for parity with the scorers and the governance test's getattr).
GUARD_FIELDS = set()

# -- Vol-verdict thresholds (IV minus realized-vol; the PRIMARY GATE) --------- #
_CHEAP_THRESHOLD = -0.03
_RICH_THRESHOLD = 0.03

# -- Term-structure classification band -------------------------------------- #
_TERM_BAND = 0.02

# -- Delta targets (selection off the REAL chain) ---------------------------- #
_SHORT_DELTA = 0.30            # short put / short call ~ 0.30 delta
_LONG_CALL_DELTA = 0.55        # long call (debit vertical) ~ 0.55 delta
_LONG_PUT_DELTA = 0.55         # long put (debit vertical) ~ 0.55 delta
_CONDOR_SHORT_DELTA = 0.25     # condor short strikes ~ 0.25-0.30 delta each side

# -- Expiry selection -------------------------------------------------------- #
_TARGET_DTE = 45
_MIN_DTE = 30
_MAX_DTE = 90
_CATALYST_SELECTOR_DAYS = 60

# -- Liquidity gate ---------------------------------------------------------- #
_MIN_OI = 100
_SPREAD_FLOOR = 0.10           # absolute spread floor
_SPREAD_PCT = 0.10             # or 10% of mark, whichever is larger

# -- Event gates ------------------------------------------------------------- #
_EVENT_DAYS = 30
_CSP_ALIGNMENT_PCT = 0.02      # entry_1 within 2% of a listed put strike -> align

# -- Collar-alternative short-call delta ------------------------------------- #
_COLLAR_CALL_DELTA = 0.20

# -- Wave 4B: skew-informed routing ------------------------------------------ #
# 25-delta risk-reversal threshold. rr_25d = IV(25Δ put) - IV(25Δ call): positive =
# puts richer (downside skew / fear), negative = calls richer. 0.04 is a PROVISIONAL
# default (equity RR typically spans 0.01-0.10); it is documented + falsifiable (see
# SKILL.md). When |rr| exceeds the threshold, routing prefers SELLING the rich wing.
_SKEW_THRESHOLD = 0.04
# Per-side condor deltas when a wing is skew-rich: sell the rich wing NEARER the money
# (harvest the richer premium) and push the CHEAP wing FURTHER out (widen it).
_CONDOR_RICH_DELTA = 0.30      # rich wing short, nearer the money
_CONDOR_CHEAP_DELTA = 0.20     # cheap wing short, further out (the widened wing)
# Adjacent deltas retried when a delta-picked SHORT strike is illiquid (low OI).
_DELTA_RETRIES = (0.25, 0.35)

# -- Wave 4B: IV-crush simulation -------------------------------------------- #
# Post-earnings implied-vol collapse factor. iv_post_crush = iv_leg * IV_CRUSH_FACTOR.
# 0.62 is a CITED PROVISIONAL constant (review R4: avg post-print IV crush ~38.2% ->
# residual 0.618 ~= 0.62), Philosophy-A. It is disclosed + falsifiable (see SKILL.md):
# if the crush-EV gate declines structures that would have been profitable (too
# aggressive) or passes structures that lose on realized crush (too lax), 0.62 is
# refuted and re-set -- ideally calibrated from bracketing IV-history samples.
IV_CRUSH_FACTOR = 0.62
# Scenario spots for the crush sim: -2sigma, -1sigma, spot, +1sigma, +2sigma. The
# probability weights are a simple SYMMETRIC discrete approximation of the standard
# normal over those five buckets (sums to 1.0); documented Philosophy-A choice.
_CRUSH_SCENARIO_SIGMAS = (-2.0, -1.0, 0.0, 1.0, 2.0)
_CRUSH_SCENARIO_PROBS = (0.05, 0.24, 0.42, 0.24, 0.05)
# Minimum post-event time-to-expiry (years) so the crushed leg still carries time
# value in bs_price (a same-day expiry after the print collapses to intrinsic).
_CRUSH_MIN_T = 1.0 / 365.0


def skew_verdict(rr_25d, threshold=_SKEW_THRESHOLD):
    """Classify the 25-delta risk-reversal (put IV - call IV) into a routing verdict.

    rr_25d = IV(25Δ put) - IV(25Δ call) at the working expiry (chain.skew_25d):
      rr >  +threshold -> "puts_rich"  (downside skew / fear; prefer SELLING puts);
      rr <  -threshold -> "calls_rich" (prefer SELLING calls);
      |rr| <= threshold -> "balanced";
      rr is None        -> "unknown" (no skew read; routing falls back to balanced).

    threshold default 0.04 is PROVISIONAL (documented + falsifiable in SKILL.md).
    """
    if rr_25d is None:
        return "unknown"
    if rr_25d > threshold:
        return "puts_rich"
    if rr_25d < -threshold:
        return "calls_rich"
    return "balanced"


# --------------------------------------------------------------------------- #
# Formatting helpers (mirror the scorers' _fmt/_clean conventions).
# --------------------------------------------------------------------------- #

def _fmt(x):
    """Compact number formatting for arithmetic strings (stable across runs)."""
    if x is None:
        return "n/a"
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return f"{x:g}"


def _clean(x):
    """Normalize a numeric to int when integral, else round to 4 dp for stable JSON."""
    if x is None:
        return None
    xf = float(x)
    if xf.is_integer():
        return int(xf)
    return round(xf, 4)


def _parse_date(s):
    """YYYY-MM-DD -> date, or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        y, m, d = (int(p) for p in s.split("-"))
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def _mark(contract):
    """Mid price: contract mark, else (bid+ask)/2, else None."""
    if contract is None:
        return None
    m = contract.get("mark")
    if m is not None:
        return m
    bid, ask = contract.get("bid"), contract.get("ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return None


def _leg_record(contract, side):
    """One leg record for a structure: strike/delta/mark/oi + bid/ask when present.

    bid/ask are carried through so the liquidity gate (which runs on the assembled
    structure) can still see the raw spread. They are dropped from the JSON when
    absent to keep the module compact.
    """
    rec = {
        "side": side,
        "type": contract.get("type"),
        "strike": _clean(contract.get("strike")),
        "delta": _clean(contract.get("delta")),
        "mark": _clean(_mark(contract)),
        "oi": contract.get("oi"),
    }
    if contract.get("bid") is not None:
        rec["bid"] = _clean(contract.get("bid"))
    if contract.get("ask") is not None:
        rec["ask"] = _clean(contract.get("ask"))
    return rec


# --------------------------------------------------------------------------- #
# Vol dashboard + PRIMARY GATE (IV-vs-realized).
# --------------------------------------------------------------------------- #

def vol_verdict(iv_minus_rv):
    """The PRIMARY GATE: classify IV vs realized vol (never IV level alone).

    diff <= -0.03 -> "cheap_vs_realized" (no premium-selling edge; long premium viable);
    -0.03 < diff < 0.03 -> "fair";
    diff >= 0.03 -> "rich_vs_realized" (premium selling favored);
    diff is None -> "unknown" (treat as fair + disclose).
    """
    if iv_minus_rv is None:
        return "unknown"
    if iv_minus_rv <= _CHEAP_THRESHOLD:
        return "cheap_vs_realized"
    if iv_minus_rv >= _RICH_THRESHOLD:
        return "rich_vs_realized"
    return "fair"


def term_structure(atm_iv_by_expiry, as_of=None):
    """Front-vs-back ATM IV: 'backwardation' | 'contango' | 'flat'.

    front > back + 0.02 -> backwardation; back > front + 0.02 -> contango; else flat.
    A single (or empty) expiry list is 'flat' by convention.

    Rows are restricted to the TRADEABLE tenor window [7, 365] days when ``as_of``
    is parseable (Gate-3 finding, MU: comparing a 0-DTE stub against a 2-year LEAP
    reported 'contango' while the tradeable curve was backwarded), and sorted by
    expiry date before the front/back comparison.
    """
    rows = [r for r in (atm_iv_by_expiry or []) if r.get("atm_iv") is not None]
    # as_of may be a full ISO timestamp (meta.as_of_utc) — slice to the date part.
    as_of_d = _parse_date(str(as_of)[:10]) if as_of else None
    if as_of_d is not None:
        windowed = []
        for r in rows:
            d = _parse_date(r.get("expiry"))
            if d is not None and 7 <= (d - as_of_d).days <= 365:
                windowed.append(r)
        if len(windowed) >= 2:
            rows = windowed
    rows.sort(key=lambda r: r.get("expiry") or "")
    if len(rows) < 2:
        return "flat"
    front = rows[0]["atm_iv"]
    back = rows[-1]["atm_iv"]
    if front > back + _TERM_BAND:
        return "backwardation"
    if back > front + _TERM_BAND:
        return "contango"
    return "flat"


def build_vol_dashboard(snapshot):
    """Assemble the vol dashboard from the snapshot (options + sentiment blocks).

    verdict is the PRIMARY GATE (vol_verdict over options.iv_minus_rv20). Also carries
    iv30, rv20, diff, iv_pctile_1yr, atm_iv_by_expiry passthrough, term structure, skew.
    """
    options = snapshot.get("options") or {}
    sentiment = snapshot.get("sentiment") or {}

    diff = options.get("iv_minus_rv20")
    atm = options.get("atm_iv_by_expiry") or []
    verdict = vol_verdict(diff)
    as_of = (snapshot.get("meta") or {}).get("as_of_utc")

    ev = options.get("event_vol") or {}
    dash = {
        "verdict": verdict,
        "iv30": sentiment.get("iv30"),
        "rv20": options.get("rv20_for_iv_comparison"),
        "diff": _clean(diff),
        "iv_pctile_1yr": sentiment.get("iv_pctile_1yr"),
        "atm_iv_by_expiry": atm,
        "term_structure": term_structure(atm, as_of),
        "skew_25d_30d": options.get("skew_25d_30d"),
        # Wave 4B: the gate uses the ex-earnings RV; surface whether the print was
        # actually stripped and the isolated event-implied move for the reader.
        "rv20_ex_earnings_used": bool(options.get("rv20_ex_earnings_stripped")),
        "event_implied_move": _clean(ev.get("event_implied_move")),
    }
    if options.get("rv20_ex_earnings_stripped"):
        dash["rv_note"] = ("IV-vs-realized gate used the ex-earnings RV "
                           "(a recent print was stripped from the RV20 window)")
    if verdict == "unknown":
        dash["disclosure"] = ("iv_minus_rv20 is null -- IV-vs-realized gate cannot be "
                              "evaluated; treated as fair, no premium-selling edge asserted")
    return dash


# --------------------------------------------------------------------------- #
# Expiry selection.
# --------------------------------------------------------------------------- #

def is_monthlyish(expiry):
    """3rd-Friday heuristic: expiry day in [15,21] AND weekday is Friday."""
    d = _parse_date(expiry)
    if d is None:
        return False
    return 15 <= d.day <= 21 and d.weekday() == 4   # Friday == 4


def select_expiry(expiries, as_of, catalyst_date, days_to_catalyst):
    """Choose the working expiry.

    - Pipeline with a near catalyst (days_to_catalyst <= 60): the first MONTHLYISH
      expiry strictly AFTER the catalyst date (fall back to the first listed expiry
      after the catalyst; then to the nearest-to-45 rule).
    - Otherwise: the expiry nearest to 45 DTE within [30, 90]; if none in-window,
      the nearest listed expiry to 45 DTE overall.
    """
    as_of_d = _parse_date(as_of)
    parsed = [(e, _parse_date(e)) for e in expiries]
    parsed = [(e, d) for e, d in parsed if d is not None]
    if not parsed:
        return None

    cat_d = _parse_date(catalyst_date)
    if (days_to_catalyst is not None and days_to_catalyst <= _CATALYST_SELECTOR_DAYS
            and cat_d is not None):
        after = [(e, d) for e, d in parsed if d > cat_d]
        monthly_after = [(e, d) for e, d in after if is_monthlyish(e)]
        if monthly_after:
            return min(monthly_after, key=lambda ed: ed[1])[0]
        if after:
            return min(after, key=lambda ed: ed[1])[0]
        # no expiry after the catalyst -> fall through to the DTE rule.

    if as_of_d is None:
        return parsed[0][0]

    def dte(d):
        return (d - as_of_d).days

    in_window = [(e, d) for e, d in parsed if _MIN_DTE <= dte(d) <= _MAX_DTE]
    # MONTHLYISH FIRST within the window (Gate-3 finding, MU: nearest-to-45 picked
    # an illiquid weekly 2 days closer to target than the liquid monthly, and every
    # structure then failed the liquidity gate). Weeklies are a fallback, not a peer.
    monthly_in_window = [(e, d) for e, d in in_window if is_monthlyish(e)]
    pool = monthly_in_window or in_window or parsed
    return min(pool, key=lambda ed: abs(dte(ed[1]) - _TARGET_DTE))[0]


# --------------------------------------------------------------------------- #
# Strike selection off the REAL chain (by delta).
# --------------------------------------------------------------------------- #

def _legs(contracts, expiry, opt_type):
    """Contracts of ``opt_type`` at ``expiry`` that carry a delta, sorted by strike."""
    out = [c for c in contracts
           if c.get("expiration") == expiry and c.get("type") == opt_type
           and c.get("delta") is not None and "strike" in c]
    out.sort(key=lambda c: c["strike"])
    return out


def pick_by_delta(contracts, expiry, opt_type, target_delta):
    """The ``opt_type`` contract at ``expiry`` whose |delta| is closest to target.

    Ties break to the strike closest to the money side is not needed here -- the
    synthetic and real grids are monotone; ties break to the lower strike (stable).
    """
    legs = _legs(contracts, expiry, opt_type)
    if not legs:
        return None
    return min(legs, key=lambda c: (abs(abs(c["delta"]) - target_delta), c["strike"]))


def pick_short_by_delta(contracts, expiry, opt_type, target_delta,
                        retries=_DELTA_RETRIES):
    """Delta-pick a SHORT strike; if the pick is illiquid (low OI), retry adjacent.

    Wave 4B candidate-breadth: the primary pick is the strike whose |delta| is closest
    to ``target_delta``. If that strike's OI is below the liquidity floor, retry at each
    adjacent delta in ``retries`` (e.g. 0.25 / 0.35) and return the first liquid pick.
    When no adjacent DELTA is liquid either, fall back to a nearest-liquid-STRIKE snap
    (``pick_by_delta_liquid``): the delta-retry targets can themselves resolve to 0-OI
    half-dollar strikes (the real GOOG bull_put_spread: 0.30d short put landed on 332.5
    OI 0, and 0.25/0.35 mapped to other thin strikes, while 330 / 335 were deeply liquid).
    Only when NO strike at the expiry is liquid does the illiquid primary come back (the
    liquidity gate then declines it downstream -- breadth is exhausted, not hidden).
    Returns None only when there are no contracts of that type at the expiry.
    """
    primary = pick_by_delta(contracts, expiry, opt_type, target_delta)
    if primary is None:
        return None
    oi = primary.get("oi")
    if oi is not None and oi >= _MIN_OI:
        return primary
    for alt in retries:
        cand = pick_by_delta(contracts, expiry, opt_type, alt)
        if cand is None:
            continue
        c_oi = cand.get("oi")
        if c_oi is not None and c_oi >= _MIN_OI:
            return cand
    return pick_by_delta_liquid(contracts, expiry, opt_type, target_delta)


def pick_by_delta_liquid(contracts, expiry, opt_type, target_delta):
    """Delta-pick a strike, then SNAP to the nearest LIQUID strike by proximity.

    ``pick_by_delta`` can land on a 0-OI half-dollar strike sitting BETWEEN two
    deeply-liquid round strikes (observed: 332.5 OI 0 between 330 / 335, both large
    OI). Retrying adjacent DELTA targets (``pick_short_by_delta``) can't fix that --
    the neighbours BY STRIKE are the liquid ones. So if the primary pick is illiquid,
    snap to the listed strike minimizing ``abs(strike - primary_strike)`` among those
    with ``oi >= _MIN_OI``; tie-break to the closest |delta| to target, then the lower
    strike (stable). "Liquid" here = OI floor only -- the full spread check stays in
    ``leg_liquid`` downstream; this only removes the 0-OI-hole failure.

    Returns None only when there are no contracts of that type at the expiry. When NO
    liquid strike exists at all, returns the primary unchanged (the liquidity gate then
    declines it -- breadth is exhausted, disclosed, never hidden).
    """
    primary = pick_by_delta(contracts, expiry, opt_type, target_delta)
    if primary is None:
        return None
    oi = primary.get("oi")
    if oi is not None and oi >= _MIN_OI:
        return primary
    liquid = [c for c in _legs(contracts, expiry, opt_type)
              if c.get("oi") is not None and c["oi"] >= _MIN_OI]
    if not liquid:
        return primary
    return min(liquid, key=lambda c: (abs(c["strike"] - primary["strike"]),
                                      abs(abs(c["delta"]) - target_delta),
                                      c["strike"]))


def pick_long_put_below(contracts, expiry, short_strike):
    """The long-put wing below ``short_strike`` -- the nearest LIQUID listed strike.

    Targets a 5-10% width of spot, taking the highest listed put strike below the short
    so the wing sits 1-2 strikes out. Prefers the nearest strike with OI >= ``_MIN_OI``,
    skipping 0-OI half-dollar strikes: the real GOOG bull_put_spread had a liquid 335
    short but the wing landed on the next-listed 332.5 (OI 0) and declined, when the liquid
    330 sat one strike further out. Falls back to the nearest listed strike when none below
    is liquid, so the liquidity gate still discloses. Returns None if none below.
    """
    puts = [c for c in contracts
            if c.get("expiration") == expiry and c.get("type") == "put"
            and "strike" in c and c["strike"] < short_strike]
    if not puts:
        return None
    liquid = [c for c in puts if c.get("oi") is not None and c["oi"] >= _MIN_OI]
    return max(liquid or puts, key=lambda c: c["strike"])


def pick_long_call_above(contracts, expiry, short_strike):
    """The bear-call wing above ``short_strike`` -- the nearest LIQUID listed strike.

    Nearest higher listed call strike, preferring OI >= ``_MIN_OI`` (skipping 0-OI
    strikes); falls back to the nearest listed strike when none above is liquid so the
    liquidity gate still discloses. Returns None if none above.
    """
    calls = [c for c in contracts
             if c.get("expiration") == expiry and c.get("type") == "call"
             and "strike" in c and c["strike"] > short_strike]
    if not calls:
        return None
    liquid = [c for c in calls if c.get("oi") is not None and c["oi"] >= _MIN_OI]
    return min(liquid or calls, key=lambda c: c["strike"])


def _strike_at_or_below(contracts, expiry, opt_type, level):
    """Nearest listed ``opt_type`` strike <= ``level`` at ``expiry`` (or None)."""
    legs = [c for c in contracts
            if c.get("expiration") == expiry and c.get("type") == opt_type
            and "strike" in c and c["strike"] <= level]
    if not legs:
        return None
    return max(legs, key=lambda c: c["strike"])


# --------------------------------------------------------------------------- #
# Liquidity gate (per leg).
# --------------------------------------------------------------------------- #

def leg_liquid(leg):
    """(ok, reason). oi >= 100 AND spread <= max(0.10, 0.10*mark).

    Missing bid/ask -> use OI only (+ disclose in the reason string).
    """
    oi = leg.get("oi")
    if oi is None or oi < _MIN_OI:
        return False, f"oi {_fmt(oi)} < {_MIN_OI}"

    bid, ask = leg.get("bid"), leg.get("ask")
    if bid is None or ask is None:
        return True, "oi-only (bid/ask missing -- spread not verified)"

    spread = ask - bid
    mark = _mark(leg) or 0.0
    limit = max(_SPREAD_FLOOR, _SPREAD_PCT * mark)
    if spread > limit + 1e-9:
        return False, (f"spread {_fmt(_clean(spread))} > "
                       f"max(0.10, 0.10*mark) {_fmt(_clean(limit))}")
    return True, "ok"


def _gate_legs(legs):
    """(all_ok, first_reason). None legs fail as 'missing strike'."""
    for leg in legs:
        if leg is None:
            return False, "a required strike is not listed on the chain"
        ok, reason = leg_liquid(leg)
        if not ok:
            return False, f"leg {_fmt(leg.get('strike'))}: {reason}"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Management-rule templates (per structure family).
# --------------------------------------------------------------------------- #

_MGMT_CREDIT = [
    "profit target: close at 50% of max credit",
    "stop: 2x credit received OR short-strike breach",
    "time exit: 21 DTE",
    "adjustment: roll the tested side out/down for a further credit if thesis intact",
]
_MGMT_CONDOR = [
    "profit target: 25-35% of net credit",
    "stop: short-strike breach on either side",
    "time exit: 21 DTE",
    "adjustment: roll the UNTESTED side toward the money to re-center / add credit",
]
_MGMT_DEBIT = [
    "profit target: 100% gain on the debit",
    "stop: -50% of the debit paid",
    "time exit: 21 DTE",
]


# --------------------------------------------------------------------------- #
# Per-structure builders (economics from chain marks).
# --------------------------------------------------------------------------- #

def _credit_spread(short_leg, long_leg, name, side):
    """Generic vertical CREDIT spread economics (bull put / bear call).

    net_credit = short.mark - long.mark ; width = |short.strike - long.strike| ;
    max_profit = net_credit ; max_loss = width - net_credit ;
    breakeven (put spread) = short_strike - credit ; (call spread) = short_strike + credit ;
    PoP ~ 1 - |delta of short strike| (labeled).
    """
    cs, cl = short_leg, long_leg
    credit = _mark(cs) - _mark(cl)
    width = abs(cs["strike"] - cl["strike"])
    max_loss = width - credit
    short_delta = abs(cs.get("delta") or 0.0)
    pop = 1 - short_delta
    if side == "put":
        breakeven = cs["strike"] - credit
    else:
        breakeven = cs["strike"] + credit

    arithmetic = (
        f"credit = short {_fmt(cs['strike'])} mark {_fmt(_clean(_mark(cs)))} "
        f"- long {_fmt(cl['strike'])} mark {_fmt(_clean(_mark(cl)))} = "
        f"{_fmt(_clean(credit))}; width {_fmt(_clean(width))}; "
        f"max_loss = width - credit = {_fmt(_clean(max_loss))}; "
        f"breakeven = {_fmt(cs['strike'])} {'-' if side == 'put' else '+'} credit = "
        f"{_fmt(_clean(breakeven))}; PoP = 1 - |Δ_short {_fmt(_clean(short_delta))}| = "
        f"{_fmt(_clean(pop))}")

    return {
        "name": name,
        "type": "credit_spread",
        "expiry": cs["expiration"],
        "legs": [
            _leg_record(cs, "short"),
            _leg_record(cl, "long"),
        ],
        "net_credit": _clean(credit),
        "max_profit": _clean(credit),
        "max_loss": _clean(max_loss),
        "breakevens": [_clean(breakeven)],
        "pop": _clean(pop),
        "pop_method": "PoP approx = 1 - |delta of short strike| (delta-as-ITM-probability)",
        "arithmetic": arithmetic,
        "management": list(_MGMT_CREDIT),
        "warnings": [],
    }


def build_bull_put_spread(contracts, expiry):
    """Bull put spread: short ~0.30Δ put, long the next lower listed put."""
    short = pick_short_by_delta(contracts, expiry, "put", _SHORT_DELTA)
    if short is None:
        return None
    long = pick_long_put_below(contracts, expiry, short["strike"])
    if long is None:
        return None
    return _credit_spread(short, long, "bull_put_spread", "put")


def build_bear_call_spread(contracts, expiry):
    """Bear call spread: short ~0.30Δ call, long the next higher listed call."""
    short = pick_short_by_delta(contracts, expiry, "call", _SHORT_DELTA)
    if short is None:
        return None
    long = pick_long_call_above(contracts, expiry, short["strike"])
    if long is None:
        return None
    return _credit_spread(short, long, "bear_call_spread", "call")


def build_cash_secured_put(contracts, expiry, short_strike=None,
                           alignment_note=None):
    """Cash-secured put: sell ~0.30Δ put (or a specific ``short_strike`` if aligned).

    max_loss = strike - credit (assignment risk to zero), labeled effective entry.
    breakeven = strike - credit ; PoP ~ 1 - |delta short|.
    """
    if short_strike is not None:
        short = chain_mod.nearest_strike(contracts, short_strike, expiry, "put")
        if short is None or abs(short["strike"] - short_strike) > 1e-9:
            short = None
    else:
        short = pick_short_by_delta(contracts, expiry, "put", _SHORT_DELTA)
    if short is None:
        # fall back to the delta pick if the aligned strike is not listed.
        short = pick_short_by_delta(contracts, expiry, "put", _SHORT_DELTA)
    if short is None:
        return None

    credit = _mark(short)
    strike = short["strike"]
    effective_entry = strike - credit
    short_delta = abs(short.get("delta") or 0.0)
    pop = 1 - short_delta

    arithmetic = (
        f"credit = put {_fmt(strike)} mark {_fmt(_clean(credit))}; "
        f"effective entry = {_fmt(strike)} - {_fmt(_clean(credit))} = "
        f"{_fmt(_clean(effective_entry))}; PoP = 1 - |Δ {_fmt(_clean(short_delta))}| = "
        f"{_fmt(_clean(pop))}")

    st = {
        "name": "cash_secured_put",
        "type": "cash_secured_put",
        "expiry": expiry,
        "legs": [
            _leg_record(short, "short"),
        ],
        "net_credit": _clean(credit),
        "max_profit": _clean(credit),
        "max_loss": _clean(effective_entry),
        "max_loss_note": (f"assignment risk to zero, effective entry "
                          f"{_fmt(_clean(effective_entry))}"),
        "breakevens": [_clean(effective_entry)],
        "pop": _clean(pop),
        "pop_method": "PoP approx = 1 - |delta of short strike| (delta-as-ITM-probability)",
        "arithmetic": arithmetic,
        "management": list(_MGMT_CREDIT),
        "warnings": [],
    }
    if alignment_note:
        st["alignment_note"] = alignment_note
    return st


def _debit_vertical(long_leg, short_leg, name, side):
    """Generic vertical DEBIT spread economics (long call / long put vertical).

    net_debit = long.mark - short.mark ; width = |strikes| ; max_loss = debit ;
    max_profit = width - debit ; breakeven (call) = long_strike + debit ;
    (put) = long_strike - debit ; PoP ~ |delta of long| (rough), labeled.
    """
    ll, sl = long_leg, short_leg
    debit = _mark(ll) - _mark(sl)
    width = abs(ll["strike"] - sl["strike"])
    max_profit = width - debit
    long_delta = abs(ll.get("delta") or 0.0)
    pop = long_delta
    if side == "call":
        breakeven = ll["strike"] + debit
    else:
        breakeven = ll["strike"] - debit

    arithmetic = (
        f"debit = long {_fmt(ll['strike'])} mark {_fmt(_clean(_mark(ll)))} "
        f"- short {_fmt(sl['strike'])} mark {_fmt(_clean(_mark(sl)))} = "
        f"{_fmt(_clean(debit))}; width {_fmt(_clean(width))}; "
        f"max_profit = width - debit = {_fmt(_clean(max_profit))}; "
        f"breakeven = {_fmt(ll['strike'])} {'+' if side == 'call' else '-'} debit = "
        f"{_fmt(_clean(breakeven))}; PoP ~ |Δ_long {_fmt(_clean(long_delta))}| = "
        f"{_fmt(_clean(pop))}")

    return {
        "name": name,
        "type": "debit_spread",
        "expiry": ll["expiration"],
        "legs": [
            _leg_record(ll, "long"),
            _leg_record(sl, "short"),
        ],
        "net_debit": _clean(debit),
        "max_profit": _clean(max_profit),
        "max_loss": _clean(debit),
        "breakevens": [_clean(breakeven)],
        "pop": _clean(pop),
        "pop_method": "PoP approx = |delta of long strike| (rough; delta-as-ITM-probability)",
        "arithmetic": arithmetic,
        "management": list(_MGMT_DEBIT),
        "warnings": [],
    }


def build_long_call_vertical(contracts, expiry):
    """Long call vertical: long ~0.55Δ call, short the ~0.30Δ call above it.

    Both legs snap off 0-OI holes to the nearest liquid strike (``pick_by_delta_liquid``)
    so a debit vertical isn't declined on a half-dollar 0-OI strike wedged between
    liquid rounds. If snapping collapses long and short onto the SAME strike, decline
    (no zero-width vertical).
    """
    long = pick_by_delta_liquid(contracts, expiry, "call", _LONG_CALL_DELTA)
    short = pick_by_delta_liquid(contracts, expiry, "call", _SHORT_DELTA)
    if long is None or short is None or short["strike"] <= long["strike"]:
        return None
    return _debit_vertical(long, short, "long_call_vertical", "call")


def build_long_put_vertical(contracts, expiry):
    """Long put vertical: long ~0.55Δ put, short the ~0.30Δ put below it.

    Both legs snap off 0-OI holes to the nearest liquid strike (``pick_by_delta_liquid``);
    if snapping collapses long and short onto the SAME strike, decline (no zero-width
    vertical).
    """
    long = pick_by_delta_liquid(contracts, expiry, "put", _LONG_PUT_DELTA)
    short = pick_by_delta_liquid(contracts, expiry, "put", _SHORT_DELTA)
    if long is None or short is None or short["strike"] >= long["strike"]:
        return None
    return _debit_vertical(long, short, "long_put_vertical", "put")


def build_iron_condor(contracts, expiry, one_sigma,
                      put_short_delta=_CONDOR_SHORT_DELTA,
                      call_short_delta=_CONDOR_SHORT_DELTA):
    """Iron condor: short put + call by delta, long wings 1-2 strikes further out.

    net credit = (short put + short call) - (long put + long call) marks.
    max_loss = max wing width - credit. PoP ~ 1 - (|Δ short put| + |Δ short call|).
    HONESTY CHECK: if the profit-zone half-width (distance between the short strikes / 2)
    is less than the snapshot 1σ expected move, the full-profit probability is LOW and a
    warning + pop_full_profit_note fire.

    Wave 4B: ``put_short_delta`` / ``call_short_delta`` default to ~0.25 each side, but
    skew routing may set them per-side (sell the rich wing NEARER the money at ~0.30,
    push the CHEAP wing FURTHER out at ~0.20 -- i.e. WIDEN the cheap wing).
    """
    sp = pick_short_by_delta(contracts, expiry, "put", put_short_delta)
    sc = pick_short_by_delta(contracts, expiry, "call", call_short_delta)
    if sp is None or sc is None:
        return None
    lp = pick_long_put_below(contracts, expiry, sp["strike"])
    lc = pick_long_call_above(contracts, expiry, sc["strike"])
    if lp is None or lc is None:
        return None

    credit = (_mark(sp) + _mark(sc)) - (_mark(lp) + _mark(lc))
    put_width = sp["strike"] - lp["strike"]
    call_width = lc["strike"] - sc["strike"]
    max_width = max(put_width, call_width)
    max_loss = max_width - credit

    sp_delta = abs(sp.get("delta") or 0.0)
    sc_delta = abs(sc.get("delta") or 0.0)
    pop = 1 - (sp_delta + sc_delta)

    be_low = sp["strike"] - credit
    be_high = sc["strike"] + credit

    # honesty check: profit-zone half-width vs 1σ expected move.
    zone_half = (sc["strike"] - sp["strike"]) / 2
    warnings = []
    pop_full_profit_note = None
    if one_sigma is not None and zone_half < one_sigma:
        pop_full_profit_note = (
            f"profit-zone half-width {_fmt(_clean(zone_half))} < 1σ expected move "
            f"{_fmt(_clean(one_sigma))}: full-profit probability is LOW")
        warnings.append(
            "profit zone sits inside the 1σ expected move -- probability of full "
            "profit is LOW; this is a bet that realized vol cools")

    arithmetic = (
        f"credit = (short put {_fmt(sp['strike'])} {_fmt(_clean(_mark(sp)))} + "
        f"short call {_fmt(sc['strike'])} {_fmt(_clean(_mark(sc)))}) - "
        f"(long put {_fmt(lp['strike'])} {_fmt(_clean(_mark(lp)))} + "
        f"long call {_fmt(lc['strike'])} {_fmt(_clean(_mark(lc)))}) = "
        f"{_fmt(_clean(credit))}; max wing width {_fmt(_clean(max_width))}; "
        f"max_loss = width - credit = {_fmt(_clean(max_loss))}; "
        f"PoP = 1 - (|Δ_put {_fmt(_clean(sp_delta))}| + |Δ_call {_fmt(_clean(sc_delta))}|) = "
        f"{_fmt(_clean(pop))}")

    st = {
        "name": "iron_condor",
        "type": "iron_condor",
        "expiry": expiry,
        "legs": [
            _leg_record(sp, "short"),
            _leg_record(lp, "long"),
            _leg_record(sc, "short"),
            _leg_record(lc, "long"),
        ],
        "net_credit": _clean(credit),
        "max_profit": _clean(credit),
        "max_loss": _clean(max_loss),
        "breakevens": [_clean(be_low), _clean(be_high)],
        "pop": _clean(pop),
        "pop_method": ("PoP approx = 1 - (|Δ short put| + |Δ short call|) "
                       "(delta-as-ITM-probability)"),
        "arithmetic": arithmetic,
        "management": list(_MGMT_CONDOR),
        "warnings": warnings,
    }
    if pop_full_profit_note is not None:
        st["pop_full_profit_note"] = pop_full_profit_note
    return st


# --------------------------------------------------------------------------- #
# Selection matrix (direction x vol verdict) + liquidity gating.
# --------------------------------------------------------------------------- #

_NEUTRAL_DECLINE = ("no vol edge for neutral premium selling; realized exceeds implied "
                    "-- stand aside or trade direction with defined risk")


def _candidate_specs(direction, verdict, skew_v, one_sigma):
    """Ordered (name, builder) specs for direction x verdict, SKEW-ROUTED (Wave 4B).

    Each builder is a callable ``fn(contracts, expiry) -> structure|None`` so the breadth
    loop can re-run the SAME specs at a fallback expiry. Skew routing reorders/adds:
      - bullish + puts_rich: prefer SELLING puts (bull-put spread / CSP) over buying calls
        even in the cheap regime (harvest the rich put skew);
      - bearish + calls_rich: prefer SELLING calls (bear-call spread) over buying puts;
      - neutral condor: sell the rich wing nearer the money, WIDEN the cheap wing.
    Bearish now always carries a debit-put-vertical fallback so breadth >= 2 (goal 5a).
    """
    puts_rich = skew_v == "puts_rich"
    calls_rich = skew_v == "calls_rich"

    def condor(c, e):
        if puts_rich:      # put wing rich -> sell puts near, widen call wing
            return build_iron_condor(c, e, one_sigma,
                                     put_short_delta=_CONDOR_RICH_DELTA,
                                     call_short_delta=_CONDOR_CHEAP_DELTA)
        if calls_rich:     # call wing rich -> sell calls near, widen put wing
            return build_iron_condor(c, e, one_sigma,
                                     put_short_delta=_CONDOR_CHEAP_DELTA,
                                     call_short_delta=_CONDOR_RICH_DELTA)
        return build_iron_condor(c, e, one_sigma)

    specs = []
    if direction == "bullish":
        if verdict in ("rich_vs_realized", "fair"):
            specs = [("bull_put_spread", build_bull_put_spread),
                     ("cash_secured_put", build_cash_secured_put)]
        else:  # cheap
            if puts_rich:
                # put skew is rich -> sell puts FIRST even in the cheap regime.
                specs = [("bull_put_spread", build_bull_put_spread),
                         ("cash_secured_put", build_cash_secured_put),
                         ("long_call_vertical", build_long_call_vertical)]
            else:
                specs = [("long_call_vertical", build_long_call_vertical),
                         ("bull_put_spread", build_bull_put_spread)]
    elif direction == "bearish":
        if verdict in ("rich_vs_realized", "fair"):
            # goal 5a: bearish/rich also tries a debit-put-vertical fallback (>=2).
            specs = [("bear_call_spread", build_bear_call_spread),
                     ("long_put_vertical", build_long_put_vertical)]
        else:  # cheap
            if calls_rich:
                # call skew is rich -> sell calls FIRST even in the cheap regime.
                specs = [("bear_call_spread", build_bear_call_spread),
                         ("long_put_vertical", build_long_put_vertical)]
            else:
                specs = [("long_put_vertical", build_long_put_vertical),
                         ("bear_call_spread", build_bear_call_spread)]
    elif direction == "neutral":
        if verdict == "rich_vs_realized":
            specs = [("iron_condor", condor)]
        # cheap or fair -> NO premium structure (handled as a fallback decline).
    return specs


def _gate_and_collect(specs, contracts, expiry, cheap, recommended, declined):
    """Run each spec at ``expiry``, liquidity-gate, sort into recommended/declined.

    Returns the number of builder ATTEMPTS made (candidates_tried contribution). Mutates
    ``recommended`` / ``declined`` in place. Cheap-vs-realized tags surviving credit
    structures with the premium-sellers-not-paid honesty warning.
    """
    attempts = 0
    for name, builder in specs:
        attempts += 1
        st = builder(contracts, expiry)
        if st is None:
            declined.append({"name": name,
                             "reason": "a required strike is not listed on the chain"})
            continue
        ok, reason = _gate_legs(_leg_contracts(st))
        if not ok:
            declined.append({"name": st["name"], "reason": f"liquidity: {reason}"})
            continue
        if cheap and st.get("type") in ("credit_spread", "cash_secured_put",
                                        "iron_condor"):
            st.setdefault("warnings", []).append(
                "realized > implied: premium sellers are NOT being paid for delivered vol")
        recommended.append(_attach_strikes(st))
    return attempts


def select_structures(contracts, expiry, direction, verdict, one_sigma,
                      skew_verdict_="balanced", all_expiries=None):
    """(recommended, declined, candidates_tried). Skew-routed matrix, then liquidity.

    A structure whose builder returns None (a required strike not listed) or whose any
    leg fails the liquidity gate is moved to ``declined`` with a reason. Cheap-vs-realized
    tags every CREDIT structure with the premium-sellers-not-paid honesty warning.

    Wave 4B candidate breadth:
      - the direction x verdict matrix is SKEW-ROUTED (``skew_verdict_``) and bearish
        carries a debit-put-vertical fallback so >= 2 candidates are tried;
      - short strikes retry adjacent deltas when the delta pick is illiquid
        (``pick_short_by_delta``);
      - if EVERY candidate at ``expiry`` fails, the SAME specs are tried ONCE at the next
        listed expiry (``all_expiries``), with a disclosure decline;
      - ``candidates_tried`` counts total builder attempts so the breadth is visible.
    """
    cheap = verdict == "cheap_vs_realized"
    specs = _candidate_specs(direction, verdict, skew_verdict_, one_sigma)

    recommended = []
    declined = []
    candidates_tried = 0

    if not specs and direction == "neutral":
        # neutral x cheap/fair -> NO premium structure (asserted up-front).
        declined.append({"name": "neutral_premium", "reason": _NEUTRAL_DECLINE})
        return recommended, declined, candidates_tried

    candidates_tried += _gate_and_collect(
        specs, contracts, expiry, cheap, recommended, declined)

    # Expiry fallback (goal 5b): if every candidate at the primary expiry failed, try
    # the SAME specs ONCE at the next listed expiry after ``expiry``.
    if not recommended and specs and all_expiries:
        later = [e for e in all_expiries if e and e > expiry]
        if later:
            next_expiry = min(later)
            declined.append({
                "name": "primary_expiry_fallback",
                "reason": (f"all candidates at {expiry} failed the liquidity gate -- "
                           f"retrying at the next listed expiry {next_expiry}")})
            candidates_tried += _gate_and_collect(
                specs, contracts, next_expiry, cheap, recommended, declined)

    return recommended, declined, candidates_tried


def _attach_strikes(structure):
    """Add a top-level ``strikes`` list (sorted, from the legs) to a structure.

    The trade-plan pass 2 (--synthesize) folds recommended structures into the
    expression and REQUIRES each to carry a top-level ``strikes`` list (it exits 2
    otherwise). Deriving it from the legs keeps the two modules consistent by
    construction. Returns the same structure (mutated) for chaining.
    """
    structure["strikes"] = sorted(
        {leg["strike"] for leg in structure.get("legs", []) if leg.get("strike") is not None})
    return structure


def _leg_contracts(structure):
    """Minimal leg dicts (strike/oi/bid/ask/mark) for the liquidity gate.

    The structure's leg records carry bid/ask when the raw contract had them, so the
    spread half of the gate is preserved; when absent the gate degrades to oi-only.
    """
    out = []
    for leg in structure.get("legs", []):
        d = {"strike": leg.get("strike"), "oi": leg.get("oi"), "mark": leg.get("mark")}
        if "bid" in leg:
            d["bid"] = leg["bid"]
        if "ask" in leg:
            d["ask"] = leg["ask"]
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Event gates (earnings <=30d, ex-div in tenor) applied to selected structures.
# --------------------------------------------------------------------------- #

def apply_event_gates(recommended, declined, days_to_earnings, ex_div_in_tenor):
    """Apply mechanical event honesty gates in place; returns (recommended, declined).

    - days_to_earnings <= 30 -> EXCLUDE cash_secured_put (undefined-ish risk into the
      event) and tag every surviving structure with the IV-crush / defined-risk warning.
    - ex-div within tenor -> short-call legs carry an early-assignment note.
    """
    event_near = days_to_earnings is not None and 0 <= days_to_earnings <= _EVENT_DAYS

    kept = []
    for st in recommended:
        if event_near and st["name"] == "cash_secured_put":
            declined.append({
                "name": "cash_secured_put",
                "reason": (f"earnings within {_EVENT_DAYS}d (in {days_to_earnings}d): "
                           "CSP excluded -- assignment/undefined risk into the event")})
            continue
        if event_near:
            st.setdefault("warnings", []).append(
                "IV-crush/defined-risk-only into event: expect a post-event vol collapse")
        if ex_div_in_tenor and _has_short_call(st):
            st.setdefault("warnings", []).append(
                "ex-dividend within tenor: short-call legs carry early-assignment risk")
        kept.append(st)

    return kept, declined


def _has_short_call(structure):
    """True if the structure has any short call leg."""
    return any(leg.get("side") == "short" and leg.get("type") == "call"
               for leg in structure.get("legs", []))


# --------------------------------------------------------------------------- #
# Wave 4B: IV-crush simulation (prices the post-earnings vol collapse via bs_price).
# --------------------------------------------------------------------------- #

def _leg_iv(contracts, leg, expiry):
    """The chain IV for a structure leg (matched by expiry/type/strike), or None.

    Structure leg records don't carry iv (they carry strike/delta/mark/oi), so the crush
    sim looks the iv back up on the ORIGINAL contracts. Returns None when the leg's
    contract has no iv -- the caller then cannot price the crush for that structure.
    """
    strike = leg.get("strike")
    opt_type = leg.get("type")
    for c in contracts:
        if (c.get("expiration") == expiry and c.get("type") == opt_type
                and c.get("strike") == strike):
            return c.get("iv")
    return None


def crush_ev_for_structure(structure, contracts, spot, one_sigma, t_post_years, r,
                           factor=IV_CRUSH_FACTOR):
    """Crush-adjusted EV of a structure across ±1σ/±2σ scenarios, or None if unpriceable.

    For each scenario spot ``S = spot + k*one_sigma`` (k in -2..+2), every leg is
    RE-PRICED with ``chain.bs_price(S, K, T_post, r, iv_leg * factor, opt_type)`` --
    i.e. the post-earnings IV is CRUSHED by ``factor`` and only ``t_post_years`` (DTE
    after the print) remains. The scenario PnL is signed by side:
        short leg PnL = entry_mark - repriced_price   (received premium, buy to close)
        long  leg PnL = repriced_price - entry_mark   (paid premium, sell to close)
    summed over legs, then ``crush_ev = Σ scenario_prob * PnL`` over the documented
    symmetric weight set. Returns None when spot/one_sigma/a leg iv/a leg mark is missing
    (the structure then cannot be crush-gated and is disclosed, not silently passed).
    """
    if spot is None or one_sigma is None or t_post_years is None:
        return None
    expiry = structure.get("expiry")
    legs = structure.get("legs", [])
    if not legs:
        return None
    t_post = max(t_post_years, _CRUSH_MIN_T)
    r = r or 0.0

    ev = 0.0
    for sigma, prob in zip(_CRUSH_SCENARIO_SIGMAS, _CRUSH_SCENARIO_PROBS):
        scen_spot = spot + sigma * one_sigma
        pnl = 0.0
        for leg in legs:
            entry_mark = leg.get("mark")
            iv = _leg_iv(contracts, leg, expiry)
            strike = leg.get("strike")
            opt_type = leg.get("type")
            if entry_mark is None or iv is None or strike is None or opt_type is None:
                return None  # cannot price this structure's crush honestly
            priced = chain_mod.bs_price(scen_spot, strike, t_post, r,
                                        iv * factor, opt_type)
            if leg.get("side") == "short":
                pnl += entry_mark - priced
            else:
                pnl += priced - entry_mark
        ev += prob * pnl
    return ev


def apply_crush_gate(recommended, declined, event_in_horizon, contracts, spot,
                     one_sigma, t_post_years, r):
    """Crush-EV gate (Wave 4B): decline event-window structures with crush_ev <= 0.

    Every surviving structure gets ``crush_ev`` + ``survives_crush``:
      - event_in_horizon (earnings falls before the structure's expiry): compute the
        crush-adjusted EV; a structure with ``crush_ev <= 0`` is DECLINED with reason
        "negative crush-adjusted EV" (an event structure that dies on the crush is not
        recommended); ``crush_ev`` unpriceable (missing iv/mark) -> disclosed, not gated.
      - NOT event_in_horizon: the crush gate does not apply (no earnings in horizon);
        ``crush_ev`` = None, ``survives_crush`` = True, with a note.
    Returns (kept, declined).
    """
    kept = []
    for st in recommended:
        if not event_in_horizon:
            st["crush_ev"] = None
            st["survives_crush"] = True
            st["crush_note"] = ("no earnings within the structure horizon -- "
                                "crush-EV gate not applied")
            kept.append(st)
            continue

        ev = crush_ev_for_structure(st, contracts, spot, one_sigma, t_post_years, r)
        if ev is None:
            # unpriceable (missing iv/mark): disclose, do not gate.
            st["crush_ev"] = None
            st["survives_crush"] = True
            st["crush_note"] = ("crush-EV unpriceable (a leg lacks iv/mark) -- "
                                "gate not applied; disclosed")
            kept.append(st)
            continue

        st["crush_ev"] = _clean(ev)
        st["survives_crush"] = ev > 0
        st["crush_note"] = (
            f"crush-adjusted EV = Σ prob×PnL over ±1σ/±2σ with iv×{_fmt(IV_CRUSH_FACTOR)} "
            f"and {_fmt(_clean(t_post_years))}y post-event T = {_fmt(_clean(ev))}")
        if ev > 0:
            kept.append(st)
        else:
            declined.append({
                "name": st["name"],
                "reason": (f"negative crush-adjusted EV (crush_ev {_fmt(_clean(ev))} "
                           f"<= 0 with iv×{_fmt(IV_CRUSH_FACTOR)} post-print)")})
    return kept, declined


# --------------------------------------------------------------------------- #
# CSP alignment to the trade-plan entry_1.
# --------------------------------------------------------------------------- #

def align_csp_to_entry(recommended, contracts, expiry, entry_1):
    """If a listed put strike sits within 2% of entry_1, rebuild the CSP at THAT strike.

    Mutates the recommended list in place (replacing the CSP) and labels it aligned.
    """
    if entry_1 is None:
        return recommended
    put_strikes = sorted({c["strike"] for c in contracts
                          if c.get("expiration") == expiry and c.get("type") == "put"
                          and "strike" in c})
    aligned_strike = None
    for s in put_strikes:
        if s != 0 and abs(s / entry_1 - 1) <= _CSP_ALIGNMENT_PCT:
            if aligned_strike is None or abs(s - entry_1) < abs(aligned_strike - entry_1):
                aligned_strike = s
    if aligned_strike is None:
        return recommended

    out = []
    for st in recommended:
        if st["name"] == "cash_secured_put":
            note = (f"aligned to stock-plan entry_1 {_fmt(_clean(entry_1))} "
                    f"(listed strike {_fmt(aligned_strike)} within 2%)")
            rebuilt = build_cash_secured_put(contracts, expiry,
                                             short_strike=aligned_strike,
                                             alignment_note=note)
            if rebuilt is not None:
                # preserve any warnings already accreted on the original CSP (e.g. the
                # cheap-vs-realized tag) so alignment does not silently drop them.
                rebuilt.setdefault("warnings", []).extend(st.get("warnings", []))
                out.append(_attach_strikes(rebuilt))
                continue
        out.append(st)
    return out


# --------------------------------------------------------------------------- #
# Hedge construction (pipeline; module_tradeplan hedge.required).
# --------------------------------------------------------------------------- #

def build_hedge(contracts, expiry, strikes_from, spot, premium_cap_pct):
    """A protective PUT SPREAD from the trade-plan hedge strikes_from levels.

    long put at the nearest listed strike <= strikes_from[0]; short put at the nearest
    listed strike <= strikes_from[1]. cost = long.mark - short.mark. If cost/spot exceeds
    the premium cap, emit a COLLAR alternative (add a short call ~0.20Δ, recompute net).
    """
    if not strikes_from or len(strikes_from) < 2:
        return None
    long_leg = _strike_at_or_below(contracts, expiry, "put", strikes_from[0])
    short_leg = _strike_at_or_below(contracts, expiry, "put", strikes_from[1])
    if long_leg is None or short_leg is None:
        return None

    cost = _mark(long_leg) - _mark(short_leg)
    arithmetic = (
        f"cost = long put {_fmt(long_leg['strike'])} {_fmt(_clean(_mark(long_leg)))} "
        f"- short put {_fmt(short_leg['strike'])} {_fmt(_clean(_mark(short_leg)))} = "
        f"{_fmt(_clean(cost))}; cost/spot = {_fmt(_clean(cost / spot))} "
        f"vs cap {_fmt(_clean(premium_cap_pct))}")

    hedge = {
        "type": "put_spread",
        "expiry": expiry,
        "legs": [
            {"side": "long", "type": "put", "strike": _clean(long_leg["strike"]),
             "mark": _clean(_mark(long_leg)), "oi": long_leg.get("oi")},
            {"side": "short", "type": "put", "strike": _clean(short_leg["strike"]),
             "mark": _clean(_mark(short_leg)), "oi": short_leg.get("oi")},
        ],
        "cost": _clean(cost),
        "cost_pct_of_spot": _clean(cost / spot) if spot else None,
        "premium_cap_pct": _clean(premium_cap_pct),
        "arithmetic": arithmetic,
        "collar_alternative": None,
    }

    if spot and premium_cap_pct is not None and (cost / spot) > premium_cap_pct + 1e-12:
        short_call = pick_by_delta(contracts, expiry, "call", _COLLAR_CALL_DELTA)
        if short_call is not None:
            call_credit = _mark(short_call)
            net = cost - call_credit
            hedge["collar_alternative"] = {
                "type": "collar",
                "note": (f"put-spread cost {_fmt(_clean(cost))}/spot exceeds cap "
                         f"{_fmt(_clean(premium_cap_pct))} -- financing the protection with "
                         f"a short call caps upside"),
                "short_call": {
                    "strike": _clean(short_call["strike"]),
                    "delta": _clean(short_call.get("delta")),
                    "mark": _clean(call_credit),
                    "oi": short_call.get("oi"),
                },
                "net_cost": _clean(net),
                "arithmetic": (
                    f"net = put-spread cost {_fmt(_clean(cost))} - short call "
                    f"{_fmt(short_call['strike'])} credit {_fmt(_clean(call_credit))} = "
                    f"{_fmt(_clean(net))}"),
            }
    return hedge


# --------------------------------------------------------------------------- #
# Flow block + expected-move passthrough.
# --------------------------------------------------------------------------- #

def build_flow(snapshot):
    """Positioning flow passthrough from snapshot sentiment + options blocks."""
    sentiment = snapshot.get("sentiment") or {}
    options = snapshot.get("options") or {}
    return {
        "pc_oi": sentiment.get("put_call_ratio_full_chain"),
        "pc_volume": sentiment.get("put_call_ratio_full_chain_volume"),
        "pc_realtime": sentiment.get("put_call_ratio_realtime"),
        "max_pain_by_expiry": options.get("max_pain_by_expiry") or [],
        "oi_walls": options.get("oi_walls"),
    }


# --------------------------------------------------------------------------- #
# Direction derivation (pipeline).
# --------------------------------------------------------------------------- #

def direction_from_grade(grade):
    """composite grade -> direction: A|B -> bullish, C -> neutral, D -> bearish."""
    if grade in ("A", "B"):
        return "bullish"
    if grade == "D":
        return "bearish"
    return "neutral"


# --------------------------------------------------------------------------- #
# Module assembly.
# --------------------------------------------------------------------------- #

def _one_sigma_for_expiry(snapshot, expiry):
    """The snapshot 1σ expected move at ``expiry`` (or None)."""
    for em in (snapshot.get("options") or {}).get("expected_moves") or []:
        if em.get("expiry") == expiry:
            return em.get("one_sigma")
    return None


def _ex_div_in_tenor(snapshot, as_of, expiry):
    """True iff events.dividends.ex_date falls within [as_of, expiry]."""
    ex = (((snapshot.get("events") or {}).get("dividends") or {}).get("ex_date"))
    exd = _parse_date(ex)
    a = _parse_date(as_of)
    e = _parse_date(expiry)
    if exd is None or a is None or e is None:
        return False
    return a <= exd <= e


def _days_to_earnings(snapshot, as_of):
    """Calendar days from as_of to events.next_earnings.date (or None)."""
    ne = (snapshot.get("events") or {}).get("next_earnings")
    ed = ne.get("date") if isinstance(ne, dict) else None
    a, e = _parse_date(as_of), _parse_date(ed)
    if a is None or e is None:
        return None
    return (e - a).days


def _dte(as_of, expiry):
    """Calendar days from as_of to the working expiry (or None)."""
    a, e = _parse_date(as_of), _parse_date(expiry)
    if a is None or e is None:
        return None
    return (e - a).days


def _risk_free(snapshot):
    """Risk-free proxy for bs_price: macro.treasury_10y.value / 100, else 0.0."""
    t = (snapshot.get("macro") or {}).get("treasury_10y")
    val = t.get("value") if isinstance(t, dict) else t
    try:
        return float(val) / 100.0 if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def build_module(snapshot, direction, direction_source, mode, tradeplan):
    """Assemble the full module_options.json document from parsed inputs."""
    from scripts import build_snapshot as bs
    meta = snapshot.get("meta") or {}
    as_of = bs._as_of_date(meta.get("as_of_utc"))
    ticker = meta.get("ticker")
    spot = (snapshot.get("price") or {}).get("last")

    options = snapshot.get("options") or {}
    expected_moves = options.get("expected_moves") or []
    atm = options.get("atm_iv_by_expiry") or []
    expiries = [r.get("expiry") for r in atm if r.get("expiry")]
    if not expiries:
        # fall back to expected_moves expiries.
        expiries = [em.get("expiry") for em in expected_moves if em.get("expiry")]

    vol_dashboard = build_vol_dashboard(snapshot)
    verdict = vol_dashboard["verdict"]
    # unknown is treated as fair for selection.
    verdict_for_selection = "fair" if verdict == "unknown" else verdict

    # -- expiry selection --------------------------------------------------
    catalyst_date = None
    days_to_catalyst = None
    entry_1 = None
    hedge_spec = None
    if tradeplan is not None:
        ne = (snapshot.get("events") or {}).get("next_earnings")
        catalyst_date = ne.get("date") if isinstance(ne, dict) else None
        days_to_catalyst = (tradeplan.get("expression") or {}).get("days_to_catalyst")
        entries = (tradeplan.get("stock_plan") or {}).get("entries") or []
        entry_1 = entries[0].get("level") if entries else None
        hedge_spec = (tradeplan.get("stock_plan") or {}).get("hedge")

    expiry = select_expiry(expiries, as_of, catalyst_date, days_to_catalyst)

    one_sigma = _one_sigma_for_expiry(snapshot, expiry) if expiry else None

    # -- skew-informed routing (Wave 4B): 25d risk-reversal at the SELECTED expiry --
    rr_25d = None
    skew_v = "unknown"
    if expiry is not None and spot:
        rr_25d = chain_mod.skew_25d(snapshot["_contracts"], spot, expiry)
        skew_v = skew_verdict(rr_25d)

    # -- structure selection + gates --------------------------------------
    global_warnings = []
    recommended, declined = [], []
    candidates_tried = 0
    if expiry is not None:
        recommended, declined, candidates_tried = select_structures(
            snapshot["_contracts"], expiry, direction, verdict_for_selection, one_sigma,
            skew_verdict_=skew_v, all_expiries=expiries)

        # CSP alignment to the trade-plan entry_1 (pipeline).
        if entry_1 is not None:
            recommended = align_csp_to_entry(recommended, snapshot["_contracts"],
                                             expiry, entry_1)

        # crush-EV gate (Wave 4B): decline event-window structures that die on the
        # post-earnings IV crush. event-in-horizon = earnings falls before the expiry.
        days_to_earn = _days_to_earnings(snapshot, as_of)
        dte = _dte(as_of, expiry)
        event_in_horizon = (days_to_earn is not None and dte is not None
                            and 0 <= days_to_earn <= dte)
        t_post_years = ((dte - days_to_earn) / 365.0
                        if (event_in_horizon and dte is not None) else None)
        r = _risk_free(snapshot)
        recommended, declined = apply_crush_gate(
            recommended, declined, event_in_horizon, snapshot["_contracts"], spot,
            one_sigma, t_post_years, r)

        # event gates.
        exdiv = _ex_div_in_tenor(snapshot, as_of, expiry)
        recommended, declined = apply_event_gates(
            recommended, declined, days_to_earn, exdiv)
    else:
        global_warnings.append(
            "no working expiry could be selected from the chain -- no structures emitted")

    if verdict == "unknown":
        global_warnings.append(vol_dashboard.get("disclosure",
                                                 "IV-vs-realized gate unknown"))
    if verdict == "cheap_vs_realized":
        global_warnings.append(
            "PRIMARY GATE: IV is CHEAP vs realized -- premium selling has no edge; "
            "long-premium/defined-risk directional preferred")
    # The binary event must be visible in the module even when zero structures
    # survive (Gate-3 finding, ETSY: per-structure IV-crush warnings vanish with
    # the structures, leaving a 13-days-to-earnings module with no event trace).
    days_to_earn_global = _days_to_earnings(snapshot, as_of)
    if days_to_earn_global is not None and 0 <= days_to_earn_global <= _EVENT_DAYS:
        global_warnings.append(
            f"BINARY EVENT: earnings in {days_to_earn_global}d -- defined-risk only; "
            "IV-crush risk on long premium held through the print")

    # -- liquidity verdict -------------------------------------------------
    if len(recommended) < 2:
        liquidity_verdict = "thin -- declining to force structures"
    else:
        liquidity_verdict = "adequate"

    # -- hedge -------------------------------------------------------------
    hedge_structure = None
    if (tradeplan is not None and hedge_spec is not None
            and hedge_spec.get("required") and expiry is not None and spot):
        hedge_structure = build_hedge(
            snapshot["_contracts"], expiry,
            hedge_spec.get("strikes_from"), spot,
            hedge_spec.get("premium_cap_pct"))

    return {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "ticker": ticker,
        "as_of": as_of,
        "mode": mode,
        "direction": direction,
        "direction_source": direction_source,
        "selected_expiry": expiry,
        "vol_dashboard": vol_dashboard,
        "term_structure": vol_dashboard["term_structure"],
        "skew_verdict": skew_v,
        "skew_rr_25d": _clean(rr_25d),
        "expected_moves": expected_moves,
        "flow": build_flow(snapshot),
        "recommended_structures": recommended,
        "declined": declined,
        "candidates_tried": candidates_tried,
        "hedge_structure": hedge_structure,
        "liquidity_verdict": liquidity_verdict,
        "warnings_global": global_warnings,
        "signal": None,
    }


# --------------------------------------------------------------------------- #
# Bundle I/O.
# --------------------------------------------------------------------------- #

def _find_snapshot(bundle):
    """Newest ``snapshot_*.json`` in the bundle directory, or None."""
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


def _run(args):
    if not os.path.isdir(args.bundle):
        print(f"ERROR: bundle directory not found: {args.bundle}", file=sys.stderr)
        return 2

    # -- mode-specific inputs ----------------------------------------------
    tradeplan = None
    direction = None
    direction_source = None

    if args.mode == "pipeline":
        composite = _load_json(os.path.join(args.bundle, "module_composite.json"))
        if composite is None:
            print("ERROR: module_composite.json missing -- run composite-score first "
                  "(pipeline mode derives direction from its grade).", file=sys.stderr)
            return 2
        tradeplan = _load_json(os.path.join(args.bundle, "module_tradeplan.json"))
        if tradeplan is None:
            print("ERROR: module_tradeplan.json missing -- run trade-plan first "
                  "(pipeline mode aligns to its entry and hedge spec).", file=sys.stderr)
            return 2
        grade = composite.get("grade")
        direction = direction_from_grade(grade)
        direction_source = f"composite grade {grade}"
    else:  # standalone
        if not args.direction:
            print("ERROR: --direction is required in standalone mode "
                  "(bullish|bearish|neutral).", file=sys.stderr)
            return 2
        direction = args.direction
        direction_source = "flag"

    # -- snapshot ----------------------------------------------------------
    snap_path = _find_snapshot(args.bundle)
    if snap_path is None:
        print(f"ERROR: no snapshot_*.json in {args.bundle}", file=sys.stderr)
        return 2
    try:
        with open(snap_path) as fh:
            snapshot = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read snapshot {snap_path}: {exc}", file=sys.stderr)
        return 2

    # -- chain (loaded ONLY via chain.load_contracts; never into context) --
    chain_file = (snapshot.get("options") or {}).get("chain_file_path")
    if not chain_file:
        # NO-CHAIN DEGRADATION (V3 acceptance finding): a snapshot can pass its
        # QC gate with the options block disclosed-missing. The stated policy is
        # that a failed snapshot gate is the ONLY full stop, so this module must
        # emit a disclosed empty module rather than exit 2 and leave the report
        # un-renderable. Everything downstream already handles zero structures.
        doc = build_no_chain_module(snapshot, direction, direction_source, args.mode)
        out = args.out or os.path.join(args.bundle, "module_options.json")
        emit_json(doc, out)
        print(out)
        return 0
    chain_path = chain_file if os.path.isabs(chain_file) \
        else os.path.join(args.bundle, chain_file)
    try:
        contracts = chain_mod.load_contracts(chain_path)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read options chain {chain_path}: {exc}", file=sys.stderr)
        return 2

    # Stash the contracts on the snapshot dict so build_module can reach them without
    # ever serializing the chain into any output.
    snapshot["_contracts"] = contracts

    doc = build_module(snapshot, direction, direction_source, args.mode, tradeplan)

    out = args.out or os.path.join(args.bundle, "module_options.json")
    emit_json(doc, out)
    print(out)
    return 0


def build_no_chain_module(snapshot, direction, direction_source, mode):
    """Disclosed empty module for a snapshot whose options block is missing.

    Emitted instead of a hard stop so the pipeline's degradation invariant holds
    (only a failed snapshot QC gate stops the run). Zero structures, explicit
    reason, vol verdict 'unknown' — downstream synthesize/renderer already
    disclose unexecutable expressions and empty strategy tables.
    """
    meta = snapshot.get("meta") or {}
    reason = ("no options chain in the snapshot (options block missing/disclosed "
              "in meta.missing) -- options analysis unavailable for this run")
    return {
        "skill": SKILL_NAME,
        "rubric_version": RUBRIC_VERSION,
        "ticker": meta.get("ticker"),
        "as_of": meta.get("as_of_utc"),
        "mode": mode,
        "direction": direction,
        "direction_source": direction_source,
        "selected_expiry": None,
        "vol_dashboard": {"verdict": "unknown", "iv30": None, "rv20": None,
                          "diff": None, "iv_pctile_1yr": None,
                          "atm_iv_by_expiry": [], "term_structure": "flat",
                          "skew_25d_30d": None, "disclosure": reason},
        "term_structure": "flat",
        "skew_verdict": "unknown",
        "skew_rr_25d": None,
        "expected_moves": [],
        "flow": {},
        "recommended_structures": [],
        "declined": [{"name": "all", "reason": reason}],
        "candidates_tried": 0,
        "hedge_structure": None,
        "liquidity_verdict": "no chain -- options analysis unavailable",
        "warnings_global": [reason.upper()[:1] + reason[1:]],
        "signal": None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Options-strategy decision skill (rubric v%s): turn a direction + "
                    "the REAL options chain into defined-risk structures with shown "
                    "arithmetic and honest probabilities. IV-vs-REALIZED is the primary "
                    "gate -- never IV level alone." % RUBRIC_VERSION)
    parser.add_argument("--bundle", required=True, help="bundle directory")
    parser.add_argument("--mode", required=True, choices=("pipeline", "standalone"),
                        help="pipeline: derive direction from composite grade + align "
                             "to trade-plan; standalone: explicit --direction")
    parser.add_argument("--direction", default=None,
                        choices=("bullish", "bearish", "neutral"),
                        help="directional view (REQUIRED in standalone mode)")
    parser.add_argument("--out", default=None,
                        help="output path (default <bundle>/module_options.json)")
    args = parser.parse_args(argv)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
