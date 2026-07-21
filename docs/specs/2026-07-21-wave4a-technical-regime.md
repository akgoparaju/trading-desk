# Spec — Wave 4A: Technical Regime + Institutional Levels (`technical-v1.1.0`, PROVISIONAL)

**Date:** 2026-07-21 · **Status:** proposed (Philosophy A) · **Source:** review R5/B28; priorities Wave 4.

**Verification note.** An Explore pass confirmed the current 4-factor rubric + that the daily `rows` (full OHLCV incl. high/low/volume) reach `build_technicals` but only `adjusted_close`+`volume` are used today. All R5 additions are pure-OHLCV deterministic **except sector-relative RS, which is DATA-BLOCKED** (no sector-ETF series in any manifest) and is therefore **DEFERRED** (honest — needs a new fetch + validation, not a guess).

## Design: enrich in place; regime as GUARD; top-level weights UNCHANGED
Current factors (verified): Trend 30 / Momentum 25 / Structure 25 / Volume-extension 20. **Top-level weights stay** — only sub-splits + guard modulation change (same low-risk principle as Wave 3A). Regime enters as a **guard/modulator** (the review: "regime as GUARD field improves every existing subscore's precision"), not a new scored factor.

## New snapshot fields (schema 0.3.1 → 0.3.2, additive, pure-OHLCV)
- **`technicals.adx14`** — Wilder ADX(14) from `rows` high/low/close. New `indicators.adx(rows, 14)` (True Range → +DM/−DM → smoothed DIs → DX → ADX). Deterministic.
- **`technicals.stage`** — Weinstein stage `{1 basing | 2 advancing | 3 topping | 4 declining}` derived from `price.last` vs `ma50` vs `ma200` + slope signs (all existing fields). Threshold (slope-flat band) = Philosophy-A default, documented.
- **`technicals.ad_line_slope`** — Chaikin A/D line, reported as its 20-day slope sign+magnitude. New `indicators.ad_line(rows)` (MFM = ((close−low)−(high−close))/(high−low); MFV = MFM×vol; cumsum). Deterministic.
- **`technicals.upvol_ratio`** — up-day volume / total volume over trailing ~50d (close>prev_close = up). New `indicators.updown_volume(rows, n)`. Deterministic.
- **`technicals.vwap_52wk_high`** + **`technicals.vwap_earnings`** — anchored VWAP (Σ typical_price×vol / Σ vol) from the anchor date. 52wk-high date = argmax over trailing 252 `rows` (derive inline); earnings anchor = `events.next_earnings.date` or the last reported quarter. Thread `earn_q`/anchor into `build_technicals`. Deterministic once anchor chosen.

## Scoring changes (`technical-v1.1.0`, top-level weights unchanged)
1. **Momentum (25) — regime-conditioned (the R5 core):** ADX + stage GUARD the momentum read. Provisional: when `adx14 < 20` (choppy, momentum unreliable — cited default ADX<20=no-trend) discount the MACD sub-component by half (momentum signals are noise in a rangebound regime); when `stage == 4` (declining) cap the RSI "healthy" band bonus. Band SHAPES unchanged; the guard modulates. Documented provisional thresholds.
2. **Volume factor (20) — enrich with A/D + up/down volume:** re-split (extension 12 / vol-regime 8) → extension 10 / vol-regime 5 / A-D-line 3 / upvol 2. A/D-line slope positive → 3 (accumulation), flat → 2, negative → 0; upvol_ratio > 0.55 → 2, [0.45,0.55] → 1, < 0.45 → 0. Keeps factor at 20.
3. **Structure factor (25) — anchored VWAP as a level:** add the anchored VWAPs to the S/R ladder as a new level type (`vwap_52wk_high`, `vwap_earnings`) so support/resistance proximity can register an institutional cost-basis level. The ladder already scores proximity; this adds level candidates, not new points. (Coordinates with `levels.py` ladder build.)

## Deferred (data-blocked, honest — NOT built)
- **Sector-relative RS** — no sector-ETF series in any manifest; needs a `sector_daily_adjusted` fetch + a sector-map + validation. Note it in the SKILL as the next R5 increment; SPY-relative RS already lives in sentiment (single-mapping bars duplication).

## Disclosure + falsifier + confidence
`rubric_version → "1.1.0"`; module note PROVISIONAL. **Falsifier:** *if across the B9 set the regime guard (ADX/stage) flips a technical grade in a way that contradicts the realized trend continuation, or the A/D + upvol signals don't separate accumulation from distribution names, the guard thresholds are refuted and re-set.* In the SKILL.
**Confidence:** `confidence.py` DEPTH_TABLE — set `technical` `"1.1.0"` = **HIGH** ("regime-conditional depth"). This IS a promotion (technical was MEDIUM "pre-regime"): the review found technical v1.0.0 already beyond FSI, and R5 makes it regime-aware — HIGH is honest here (unlike sentiment, technical's source is AV-premium not web-dependent, so source doesn't cap it). Confirm `compute_module` returns HIGH for a technical module at 1.1.0 on premium data.

## Implementation
1. `indicators.py` — `adx`, `ad_line`, `updown_volume`, anchored `vwap` helpers (pure/stdlib; verify ADX against a known reference series in tests).
2. `build_snapshot.py` — compute the new fields in `build_technicals` (thread anchor date/earn_q); schema → 0.3.2.
3. `levels.py` / ladder — add anchored-VWAP level candidates (if the ladder is built there).
4. `score_technical.py` — regime guard on momentum; volume re-split; VWAP levels into structure scoring; INPUT_FIELDS; rubric 1.1.0 + note.
5. `confidence.py` — technical 1.1.0 → HIGH.
6. `skills/technical-analysis/SKILL.md` — provisional note + falsifier + the deferred sector-RS note.

## Tests
- `indicators`: ADX against a hand/reference series; A/D line on a known fixture; up/down volume; anchored VWAP.
- `build_snapshot`: each new field + null-safety; stage classification on 4 constructed MA-stack fixtures; schema 0.3.2.
- `score_technical`: regime guard (ADX<20 discounts MACD; stage-4 caps RSI bonus); volume re-split sums to 20; VWAP levels appear in the ladder; rubric 1.1.0 + note.
- `confidence`: technical 1.1.0 on premium → depth HIGH, and overall HIGH (source premium, staleness fresh).
- Full suite green; re-pin fixtures.

## E2E gate (standing)
Re-score BE: confirm adx/stage/A-D/upvol/VWAP compute on real data, the regime guard behaves sensibly (BE's downtrend/choppy state), grade movement sensible, **technical confidence badge now reads HIGH on a fresh premium build**, before commit.

## Definition of done
technical-v1.1.0 scores regime-conditioned momentum + A/D + upvol, adds anchored-VWAP levels; top-level weights unchanged; sector-RS deferred+disclosed; confidence promotes to HIGH (honest — AV-premium source); provisional + falsifier; suite green; E2E sensible.
