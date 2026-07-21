# Spec — Wave 4B: Event-Vol-Aware Options (`options-v1.1.0`, PROVISIONAL)

**Date:** 2026-07-21 · **Status:** proposed (Philosophy A) · **Source:** review R4/B27; priorities Wave 4.

**Verification note.** An Explore pass mapped `options_strategy.py` + `chain.py`. The 95% line: event-vol extraction, ex-earnings RV, skew routing, and candidate breadth are pure arithmetic on existing chain marks / dates. **Crush simulation is the one piece needing new infrastructure — a Black-Scholes pricer (there is NONE in the codebase).** The pricer is ~15 lines of stdlib (`math.erf`) and is **testable against known reference values**, so it stays inside the bar; the `0.62` crush factor is a **cited provisional constant** (review: avg post-print IV crush 38.2% → ×0.62) with a falsifier — not a guess. Expired-expiry drop (goal 6) is **already done** (QF3, confirmed at build_snapshot.py:758).

## The options module doesn't move composite scores
`options_strategy` produces `module_options.json` (the expression layer) → feeds `trade_plan.expression`, not the composite score. So the E2E gate here is "sensible structures + honest gating," not grade movement.

## Goals (each Philosophy-A provisional where a threshold is involved)

### 1. Event-vol extraction (`chain.py` + `build_snapshot`) — 95%
New `chain.event_implied_vol(contracts, spot, earnings_date)`: find the bracketing expiries (last `< earnings_date` = pre, first `>= earnings_date` = post; reuse `future_expiries`/expiry navigation). Variance additivity: `event_var = atm_iv_post² × T_post − atm_iv_pre² × T_pre` (T in years); `event_vol = sqrt(max(0, event_var))` and an `event_implied_move` = the isolated earnings-day 1σ. Pure arithmetic over two `chain.atm_iv` calls. Emit into `options.event_vol = {event_implied_move, iv_pre, iv_post, exp_pre, exp_post}`; null when no earnings date / no bracketing pair. This **replaces the blunt iv30-vs-rv20 for event names** as a richer signal (iv30-vs-rv20 stays as the base gate).

### 2. Ex-earnings RV (`indicators.py` + `build_snapshot`) — 95%
New `indicators.realized_vol_ex_earnings(closes, dates, earnings_dates, n)`: compute the date-aligned log-return series, **mask returns on days within ±1 session of any earnings date** (the print-day jump), stdev over the remaining, annualize by `sqrt(252)`. **Annualization convention (documented Philosophy-A choice):** annualize by `sqrt(252)` unconditionally (treat stripped days as non-events, not as missing time) — stated explicitly in the docstring. Earnings dates come from `events.earnings_move_history[].quarter_end` (Wave 2A). Emit `options.rv20_ex_earnings`; the vol gate can then compare `iv30` vs the cleaner ex-earnings RV (disclosed). Keep the contaminated `rv20` too for continuity.

### 3. Crush simulation (`chain.py` pricer + `options_strategy`) — pricer VERIFIED, factor CITED
- **New `chain.bs_price(S, K, T, r, iv, opt_type)`** — Black-Scholes via `math.erf` (`norm_cdf(x) = 0.5×(1+erf(x/√2))`). **MUST be verified against ≥3 known textbook reference values** (e.g. S=100,K=100,T=1,r=0,iv=0.2 call ≈ 7.966; a put via parity; an ITM/OTM case) in tests — do not ship the pricer without reference-value tests. `r` = the snapshot's risk-free proxy (`macro.treasury_10y` / 100) or 0 if absent.
- **Crush sim** in `options_strategy`: for each candidate structure, at scenario spots (±1σ/±2σ from `expected_move`) re-price each leg at `iv_post = iv_leg × IV_CRUSH_FACTOR` with `T_remaining_post` (DTE after the event). **`IV_CRUSH_FACTOR = 0.62`** — a labeled module constant, disclosed (cited: ~38% avg crush), Philosophy-A provisional. Net the legs → scenario PnL; **structure-level EV** = Σ scenario_prob × PnL. Add `crush_ev` + `survives_crush: bool` to each candidate; **gate event-name structures on `crush_ev > 0`** (an event structure that dies on the crush is declined with reason `"negative crush-adjusted EV"`).
- This is the review's "vega math determines structure survival" — priced, not narrated.

### 4. Skew-informed structure choice (`options_strategy`) — 95% routing + provisional threshold
New `skew_verdict(rr_25d, threshold=0.04)` (**0.04 = provisional default**, equity RR typically 0.01–0.10): `rr > +0.04` → puts rich (downside skew/fear) → prefer SELLING puts (CSP/bull-put) over buying calls; `rr < −0.04` → calls rich → prefer selling calls. Route in `select_structures` (the candidate matrix) + tune condor per-side short delta (widen the cheap wing). Use the skew at the SELECTED working expiry (`chain.skew_25d(contracts, spot, selected_expiry)`), not just the fixed 30d.

### 5. Candidate breadth (`options_strategy`) — 95% pure Python
Before declaring `recommended_structures: []`: (a) expand the matrix so bearish/rich also tries a debit vertical fallback (not only the one credit spread); (b) if all candidates at the primary expiry fail liquidity, try the next listed expiry once; (c) if `pick_by_delta` returns a low-OI strike, retry at an adjacent delta (0.25/0.35). Emit `candidates_tried` count so the breadth is visible. The review's "declined ONE candidate then stood aside" becomes "tried N, all declined for <reasons>."

### 6. Drop expired expiries — ALREADY DONE (QF3). Confirm, no change.

## Disclosure + falsifier
`rubric_version → "1.1.0"`; module note PROVISIONAL. **Falsifier:** *if across the B9 set the crush-EV gate declines structures that would have been profitable (crush factor too aggressive) or passes structures that lose on realized crush (too lax), the 0.62 factor is refuted and re-set — ideally calibrated from bracketing IV-history samples once enough earnings are captured; if the 0.04 skew threshold routes to worse-performing structures than the base matrix, it is refuted.* In the SKILL. **Confidence:** options has no confidence DEPTH row (it's the expression layer, not a scored evidence dim) — no confidence change.

## Implementation
1. `chain.py` — `event_implied_vol`, `bs_price` (verified). 
2. `indicators.py` — `realized_vol_ex_earnings`.
3. `build_snapshot.py` — emit `options.event_vol`, `options.rv20_ex_earnings` (thread earnings dates); schema 0.3.2 → 0.3.3 (additive).
4. `options_strategy.py` — `skew_verdict` + routing; candidate breadth (matrix expand + expiry/delta fallback + `candidates_tried`); `IV_CRUSH_FACTOR` + crush sim + `crush_ev`/`survives_crush` + the event-name gate; rubric 1.1.0 + note.
5. `skills/options-strategy/SKILL.md` — provisional note + falsifier + the event-vol / crush / skew / breadth disclosures.

## Tests
- `chain`: `bs_price` vs ≥3 reference values (VERIFICATION); `event_implied_vol` on a two-expiry fixture (hand-computed variance additivity); null paths.
- `indicators`: `realized_vol_ex_earnings` strips the right days (a fixture with a known print-day jump → ex-earnings RV < contaminated RV).
- `options_strategy`: skew routing (puts-rich → sells puts); candidate breadth (bearish/rich now tries ≥2, `candidates_tried` reported, expiry/delta fallback); crush sim (a short-vol event structure with negative crush EV → declined; a positive one → survives); rubric 1.1.0.
- `build_snapshot`: `event_vol` + `rv20_ex_earnings` present/null; schema 0.3.3.
- Full suite green; re-pin options/report fixtures.

## E2E gate (standing, adapted)
Rebuild BE fresh → run options-strategy pipeline: confirm `event_vol.event_implied_move` computes (BE has a 9-day binary), the crush sim runs on a candidate, skew routing sees BE's put-skew (0.22 → puts rich → prefers selling puts), candidate breadth reports `candidates_tried > 1`, before commit. (No composite grade change expected — options is the expression layer.)

## Definition of done
Event-vol extraction + ex-earnings RV + skew routing + candidate breadth ship on present data; crush sim runs on a VERIFIED BS pricer with a cited/falsifiable 0.62 factor and gates event structures on crush-adjusted EV; rubric 1.1.0 PROVISIONAL + falsifier; suite green; E2E sensible.
