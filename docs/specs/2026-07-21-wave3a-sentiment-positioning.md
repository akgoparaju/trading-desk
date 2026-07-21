# Spec — Wave 3A: Sentiment Positioning Dynamics (`sentiment-v1.1.0`, PROVISIONAL)

**Date:** 2026-07-21 · **Status:** proposed (Philosophy A: provisional versioned default + falsifier, ratify after B9) · **Source:** review R3/B25; priorities Wave 3.

**Verification note (no-guessing).** An Explore pass mapped `score_sentiment.py`'s 5 factors + bands and confirmed each raw field at `file:line`. The 95% line: skew/volume-P/C/DTC/news_heat data **exists now**; insider routine-vs-opportunistic is **data-depth-blocked** (raw span 63 days vs 36 months needed) and is therefore built to **degrade gracefully**, not guessed. All new bands are cited provisional defaults with a pre-registered falsifier.

## Design principle: enrich sub-components IN PLACE — top-level weights UNCHANGED
Current factors (verified): Street 25 (buy% 10 + PT 10 + rating-actions 5) · Revisions 20 · Smart-money 20 (inst-flow 8 + insider 12) · Positioning 20 (SI 8 + OI-P/C 6 + IV 6) · Momentum 15. **All five top-level weights stay 25/20/20/20/15.** Only sub-component splits change → smaller, cleaner score movement. This is deliberate risk control on a provisional wave.

## New/enriched scored signals

### 1. Positioning factor (stays 20) — re-split to add skew, volume-P/C, DTC
New split: **SI/DTC 6 · OI-P/C 4 · volume-P/C 3 · skew 4 · IV-pctile 3 = 20** (was SI 8 / OI-P/C 6 / IV 6). Bands (provisional, cited):
- **SI+DTC (6)** — keep the complacency guard (si<1.5 & rsi>70→2). Else combine SI% band with DTC: DTC = `(short_interest_pct/100 × shares_diluted_m×1e6) / (adv_dollar_3m / price.last)`. High DTC (>10) + rising SI → squeeze/crowded-short risk. Provisional: base on SI band, notch −1 when DTC>10 & si_trend rising, notch +1 when DTC<2. New snapshot field `sentiment.dtc` (deterministic; disclose float-vs-shares-outstanding caveat).
- **volume-P/C (3)** — from existing `sentiment.put_call_ratio_full_chain_volume`. Flow signal (vs OI = structural). [0.7,1.3]→3, extreme (<0.5 call-froth or >2.0 hedged)→1, else 2.
- **skew (4)** — promote `options.skew_25d_30d` into `sentiment` (or read from options). 25Δ RR = IV(25Δput)−IV(25Δcall). Provisional: |skew|<0.03→4 (balanced), 0.03–0.08→2 (moderate hedging demand), >0.08→1 (extreme put bid = fear, or negative = call chase). Sign documented.
- **IV-pctile (3)** — same band shape as today, scaled 6→3.

### 2. Smart-money factor (stays 20) — insider CMP with graceful degrade
Insider sub-component (12 pts) gains **Cohen/Malloy/Pomorski routine-vs-opportunistic** classification **when ≥24 months of per-insider history is present**; else **falls back to today's `insider_net_90d_usd` + `insider_baseline` logic UNCHANGED**. New snapshot: parse per-insider rows into `sentiment.insider_classification` = `{opportunistic_cluster: bool, opportunistic_net_usd, routine_net_usd, history_months, n_insiders}` (routine = an insider transacting the same calendar month in ≥2 prior years; opportunistic = else; cluster = ≥2 opportunistic insiders same-side in 30d). Scoring when active: opportunistic net-selling cluster → 2/12 (the review's "insider cluster at the highs" signal); opportunistic net-buying → 12; routine-only → 8 (neutral). **Fetch widening:** `market-snapshot` SKILL Step 2 — change `INSIDER_TRANSACTIONS from_date` from as-of−90d to **as-of−36 months** so future runs carry the history (disclosed; graceful when the vendor returns less). **This is the safe path around the data blocker — the classifier scores only when the data justifies it.**

### 3. Street-view factor (stays 25) — fold in news_heat
Re-split: **buy% 8 · PT-vs-price 8 · rating-actions 4 · news_heat 5 = 25** (rating-actions LLM judgment trimmed 5→4; 5 pts of algorithmic news signal added). New snapshot `sentiment.news_heat` = EWMA of `ticker_sentiment_score × relevance_score` over the raw news feed with **half-life 3 days (cited default: RavenPack/MSCI 2–5d decay; Philosophy-A provisional)** + an article-volume z-spike. Band: EWMA > +0.15 → 5 (bullish heat), [−0.15,0.15] → 3, < −0.15 → 1 (bearish heat, e.g. BE's Hunterbrook cluster), with a −1 notch on a volume z-spike (heightened attention). This is the review's B17-revisited: **score the news DYNAMICS, not the vendor number.** `build_sentiment` currently ignores the feed entirely — this wires it.

## Disclosure + falsifier (Philosophy A)
`RUBRIC_VERSION → "1.1.0"`; module note `"sentiment-v1.1.0 PROVISIONAL — positioning/news bands unratified pending B9; falsifier pre-registered"`. **Falsifier:** *if across the B9 set the news_heat/skew/DTC sub-signals do not separate names with known positioning stress (rising DTC + put skew + negative news heat, e.g. BE) from calm names, OR the re-split swings a composite grade >1 letter where the top-level sentiment score barely moved, the bands are refuted and re-set.* Recorded in the SKILL.
**Confidence:** sentiment DEPTH row in `confidence.py` → set `sentiment` `1.1.0` = MEDIUM ("provisional positioning-aware"); **note that sentiment SOURCE is structurally capped at MEDIUM anyway** (short_interest is web-by-design), so the badge stays MEDIUM — honest: deeper rubric, but still web-dependent data. Do not promote to HIGH.

## Snapshot fields (schema 0.3.0 → 0.3.1, additive)
`sentiment.news_heat {ewma, volume_z, half_life_days, n_articles}`, `sentiment.dtc`, `sentiment.skew_25d_30d` (promoted/copied from options), `sentiment.insider_classification {…}` + retained per-insider rows for the classifier. All null-safe (degraded/web-fallback → null → renormalize).

## Implementation
1. **`build_snapshot.py`** — `_news_heat(news, as_of)` EWMA; `sentiment.dtc` compute; promote `skew_25d_30d` into sentiment; `build_insider_classification(insider_rows, as_of)` (CMP, needs per-row retention). Schema → 0.3.1.
2. **`indicators.py`** — EWMA helper + z-score helper if not present.
3. **`score_sentiment.py`** — re-split factors 1/3/4 per above; add new paths to `INPUT_FIELDS`; keep band SHAPES documented; graceful-degrade insider; rubric → 1.1.0 + provisional note.
4. **`confidence.py`** — sentiment DEPTH `1.1.0` = MEDIUM (+ source-cap comment).
5. **`skills/market-snapshot/SKILL.md`** — widen INSIDER_TRANSACTIONS from_date to 36mo (disclosed). **`skills/sentiment-positioning/SKILL.md`** — falsifier + provisional note + the new judgment context.

## Tests
- `indicators`: EWMA half-life (known-decay fixture), volume z.
- `build_snapshot`: news_heat (fixture feed → hand-computed EWMA + null when feed absent); dtc formula; skew promotion; insider_classification (a ≥24mo fixture → CMP tags; a 63-day fixture → history_months<24 → classifier inactive/graceful).
- `score_sentiment`: each new band; the graceful-degrade path (short history → today's insider logic, score unchanged); factor sums (1=25, 3=20, 4=20); rubric 1.1.0 + note; a BE-like case (rising DTC + put skew + negative news heat → positioning factor low).
- `confidence`: sentiment 1.1.0 → depth MEDIUM, overall MEDIUM (source cap).
- Full suite green; re-pin composite/report fixtures that hard-code a sentiment score.

## E2E gate (standing, per user)
After build: re-score the BE bundle under sentiment-v1.1.0, compare composite grade before/after, confirm sensible movement + honest MEDIUM confidence, before commit.

## Definition of done
sentiment-v1.1.0 scores skew/volume-P/C/DTC/news_heat from present data; insider CMP active only with sufficient history (else graceful, score-unchanged); top-level weights unchanged; provisional + falsifier disclosed; confidence honestly MEDIUM; suite green; E2E grade check sensible.
