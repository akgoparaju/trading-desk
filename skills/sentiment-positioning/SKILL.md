---
name: sentiment-positioning
description: Score a ticker's sentiment and positioning (street view + news heat, revisions momentum, smart money & insiders, options/short positioning incl. DTC/volume-P/C/skew, price momentum vs SPY) against a versioned rubric, off an existing market-snapshot bundle. Use when the user says "sentiment check [ticker]", "positioning", "what's the street saying", or when a report needs the sentiment evidence module. Consumes an existing snapshot bundle only (runs market-snapshot first if none exists). Rubric v1.1.0 (PROVISIONAL).
---

# Sentiment & Positioning (Evidence Module)

Score the sentiment/positioning dimension for one ticker from an already-built snapshot bundle. **All arithmetic is done by `scripts/score_sentiment.py`** — you run the script, read its small JSON, and write prose. You never compute a score, a percentage, a ratio, or a relative-return in text.

**Non-negotiables:**
- **Never do arithmetic in prose.** Every number you cite must already appear in `module_sentiment.json` or the snapshot. A number you would have to compute (buy%, PT-upside, rel-return vs SPY) is a script change, not a prose change.
- **Single-snapshot rule.** This module never fetches market data. A figure missing from the snapshot is a *snapshot extension request*, not a fetch here.
- **The three judgment flags are formed from snapshot text/context ONLY.** You read the snapshot's own news/ratings/insider evidence to set `--rating-actions`, `--inst-flow`, and `--insider-baseline`. You never invent an action, a fund flow, or an insider read the snapshot does not support. Each non-default flag carries a one-line justification.

Trigger phrases: "sentiment check MU", "positioning for AAPL", "what's the street saying on NVDA".

---

## PROVISIONAL — sentiment-v1.1.0 (Wave 3A, unratified)

**This rubric is provisional (Philosophy A).** v1.1.0 re-splits three factors IN PLACE to fold in new snapshot signals — **without changing any top-level weight** (Street 25 · Revisions 20 · Smart-money 20 · Positioning 20 · Momentum 15 are unchanged; only sub-component splits move, so score movement stays small):
- **Positioning (stays 20)** → SI+DTC 6 · OI-P/C 4 · volume-P/C 3 · skew 4 · IV 3 (was SI 8 / OI-P/C 6 / IV 6). New: a **DTC** notch (−1 when `sentiment.dtc` > 10 & `si_trend` rising; +1 when `dtc` < 2), a **volume-P/C** flow band, and a **skew** band (`sentiment.skew_25d_30d` = 25Δ RR = IV(25Δ put) − IV(25Δ call); positive = put bid = fear).
- **Smart-money (stays 20)** → insider (12) gains a **Cohen/Malloy/Pomorski routine-vs-opportunistic** classifier that ACTIVATES ONLY with ≥24 months of per-insider history (`insider_classification.classifier_active`); else it falls back to the **unchanged** v1.0.0 net-90d + baseline logic (graceful — no guessing where the data is thin).
- **Street (stays 25)** → buy% 8 · PT-vs-price 8 · rating-actions 4 · **news_heat 5** (was buy% 10 / PT 10 / rating-actions 5). news_heat scores the **dynamics** of the raw news feed (`sentiment.news_heat.ewma`, half-life 3d, with a `volume_z` attention-spike notch), not the vendor number. A null `news_heat` renormalizes the street dimension over its available sub-components — it is **never** read as bearish.

The bands are **cited defaults; the B9 structural set (2026-07-23) confirmed the TESTED bands (balanced/moderate skew, bull/neutral news-heat, the `dtc<2` notch) fire correctly** (the falsifier survived); predictiveness stays outcome-forward-tracking. **Two branches remain unexercised on real names:** the crowded-short `dtc>10` notch never fired — high-SI names are high-volume, so days-to-cover stays moderate (a name carrying ~30% short interest still read dtc≈6; the SI% band captures the crowding instead) — and the extreme-skew / bearish-news bands did not fire even on a crashing, heavily-shorted name; both flagged for a follow-up positioning calibration.

**Falsifier (pre-registered):** _if across the B9 set the news_heat/skew/DTC sub-signals do not separate names with known positioning stress (rising DTC + put skew + negative news heat, e.g. BE) from calm names, OR the re-split swings a composite grade >1 letter where the top-level sentiment score barely moved, the bands are refuted and re-set._

The module JSON stamps `rubric_version: "1.1.0"` and `module_note: "sentiment-v1.1.0 PROVISIONAL — positioning/news bands unratified pending B9; falsifier pre-registered"`. **Confidence stays honestly MEDIUM** — deeper rubric, but sentiment SOURCE is structurally capped at MEDIUM anyway (short interest is web-transcribed by design), so the badge does not rise. Surface the provisional status; do not overstate it.

---

## Step 1 — Locate the snapshot bundle

In the invoker's CWD, find the newest bundle for the ticker:

```bash
ls -dt ./trading_desk_<TICKER>/detail_reports_* ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

Newest first across both layouts: the new `./trading_desk_<TICKER>/detail_reports_<date>/` bundles and the legacy `./td_bundle_<TICKER>_<date>/` bundles (fallback for old runs).

- **If a bundle exists**, confirm it holds a `snapshot_<TICKER>_*.json`.
- **If NO bundle exists**, invoke the `market-snapshot` skill for `<TICKER>` first, then continue with the bundle it produces. Do not attempt to fetch data here yourself.

This module has **no cross-module dependency** — it scores the snapshot directly and needs neither `module_technical.json` nor the S/R ladder (it scores no price levels).

---

## Step 2 — Form the three judgment flags (from snapshot text/context ONLY)

Before running the script, read the snapshot and set three flags. Each is a judgment call anchored in the snapshot's own evidence — never a computed value, never invented:

1. **`--rating-actions positive|neutral|negative`** (default `neutral`). Read the snapshot's `sentiment.news_sentiment_summary` and any recent-rating-actions / news context. If the recent balance of analyst actions is clearly upgrades → `positive`; clearly downgrades → `negative`; otherwise leave `neutral`. Any non-neutral value **requires** `--rating-actions-justification "…"` (one line naming the actions).
2. **`--inst-flow accumulating|neutral|distributing|unknown`** (default `unknown`). Read `sentiment.inst_flow_notes`. Only set `accumulating`/`neutral`/`distributing` if the snapshot actually carries a 13F read; otherwise **leave `unknown`** — a 13F filing lags by up to 45 days and an unfounded flow call is worse than none. Non-`unknown` values **require** `--inst-flow-justification "…"`.
3. **`--insider-baseline normal|unusual`** (default `normal`). This only matters when `sentiment.insider_net_90d_usd` is **≤ 0** (net selling). Default `normal` reads net selling as routine (diversification, 10b5-1 plans) → 8 pts. Set `unusual` only when the snapshot's insider evidence and the name's own history point to an off-pattern cluster (e.g. concentrated C-suite sales at the highs) → 2 pts; `unusual` **requires** `--insider-baseline-justification "…"`.

Each justification is one line. If you cannot justify a non-default flag from the snapshot, use the default.

---

## Step 3 — Run the scorer

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_sentiment.py \
  --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>
```

With judgment flags (each non-default value paired with its justification):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_sentiment.py \
  --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD> \
  --rating-actions positive --rating-actions-justification "3 upgrades post-print" \
  --inst-flow accumulating --inst-flow-justification "13F net buys last quarter" \
  --insider-baseline unusual --insider-baseline-justification "CFO cluster sales at highs"
```

The script loads the newest snapshot, scores the five dimensions, builds the positioning and momentum-vs-SPY tables and the hedging-cost note, and writes `<bundle>/module_sentiment.json` (its path is printed to stdout). A non-default flag without its justification is a hard error (exit 2). Exit 2 also means the bundle/snapshot could not be read — fix that and re-run.

---

## Step 4 — Read the module JSON and write the brief

The module JSON is small — read it directly. Then write `<bundle>/brief_sentiment.md` with exactly these parts, in order:

1. **Score headline** — `## Sentiment Score: <score>/100 — <one-line read>`. Copy `score` verbatim. If `renormalized` is true, add a one-line note quoting `renormalization_note`.
2. **A single paragraph, ≤120 words.** Wrap the paragraph in `<!-- BRIEF:START -->` … `<!-- BRIEF:END -->` delimiters (one delimiter per line, the paragraph text between them). Cite ONLY numbers present in `module_sentiment.json` (the `subscores[].arithmetic` strings and `inputs`) or the snapshot. Zero computed-in-prose numbers. Walk the five dimensions (street view, revisions momentum, smart money & insiders, positioning & derivatives, price momentum), naming the points each earned and why, using the `arithmetic` strings as your source of truth. If the street-view dimension was capped ("PT below price: dimension capped at 10/25"), say so plainly.
3. **Momentum & positioning mini-table** — from `tables.momentum_vs_spy` and `tables.positioning`, quoting the relative returns and positioning figures (never recompute a `rel_*` value):

   | Metric | Value |
   |--------|-------|
   | rel_3m / rel_12m | … |
   | short_interest_pct (trend) | … |
   | put_call_ratio_full_chain | … |
   | iv_pctile_1yr | … |

4. **Hedging-cost note** — if `tables.hedging_cost_note` is non-null, include it as a one-liner (protective structures historically cheap; cross-ref options-strategy). Omit if null.
5. **One-line signal** — Wrap the signal in `<!-- SIGNAL:START -->` … `<!-- SIGNAL:END -->` delimiters (one delimiter per line, the signal text between them). Your single-sentence read of the sentiment/positioning setup (e.g. "Street is constructive and revisions are turning up, but a call-heavy chain and low short interest into overbought RSI say the froth is already priced — no fresh-money edge here."). This is the ONLY place a signal appears; it is prose, never a number, and the JSON's `signal` field stays `null`.
6. **Footer** — `_Rubric v<rubric_version> · as of <as_of>_` using the JSON's fields.

---

## Step 5 — Output contract

Report to the user (and to any calling skill):
- **Module path** — `<bundle>/module_sentiment.json`
- **Brief path** — `<bundle>/brief_sentiment.md`
- **Score** — `<score>/100`
- **Judgment flags** — the three flags used and their one-line justifications
- **Signal line** — the one-liner from the brief

---

## Important Notes

- **Single-snapshot rule.** No fetching. A missing figure (e.g. `null` short interest) contributes 0 to its component and is named "n/a" in the arithmetic; if an entire dimension is null, the script renormalizes the score over the remaining max and sets `renormalized: true` — disclose that, never hide it.
- **Options sentiment scores HERE and nowhere else (the single-mapping rule).** The options-derived *sentiment* fields — full-chain put/call ratio, 1-yr IV percentile, skew — score in this module only. Options-derived *levels* (e.g. max-pain, high-OI strikes as support/resistance) score in technical-analysis. That split — sentiment here, levels there — is the single-mapping rule: each snapshot fact scores in exactly one module. Do not narrate an options *level* as a sentiment factor, or the P/C ratio as a technical level.
- **PT-upside is scored HERE, not in risk-analytics.** Consensus analyst-target upside (`pt_vs_price_pct`) is scored in this module's street view. The design spec listed it in both risk-analytics and sentiment; risk-analytics documents that it deliberately does NOT score it (reallocating the points into its asymmetry + dist-from-ATH components) to keep single-mapping. Score PT-upside only here.
- **Complacency guard.** In the positioning dimension, `short_interest_pct < 1.5%` combined with `rsi14 > 70` is scored as **complacency (2 pts), not a bullish signal** — a stock nobody is shorting while it is overbought has priced out the doubters, which is a crowded-long risk, not a squeeze setup. The guard fires *before* the normal short-interest bands, and its thresholds are STRICT: SI must be below 1.5 AND rsi14 above 70 — a 68-70 RSI or a 1.5-2% SI does NOT fire it (it lands in the normal `<2% → 6` band); do not describe near-misses as complacency in the brief. `rsi14` only *conditions* this guard here; it is scored in technical-analysis, never double-counted here (it is a GUARD field, not an INPUT field).
- **13F lag is disclosed.** `--inst-flow` defaults to `unknown` (0 pts, "n/a — 13F not assessed; lag disclosed") precisely because 13F filings lag by up to 45 days. Only override `unknown` when the snapshot carries a real flow read, and say so in the justification.
- **Judgment flags come from snapshot text/context, never invention.** `--rating-actions`, `--inst-flow`, and `--insider-baseline` are read off the snapshot's news/ratings/insider evidence. Each non-default value carries a one-line justification; an unjustifiable flag is a hard error and, more importantly, a fabrication you must not make.
- **Rubric version travels with the numbers.** The rubric version (`1.1.0`, PROVISIONAL) is printed in the module JSON and MUST appear in the brief footer, so any reader can tell which scoring rule produced the score. When `module_note` is present (it is on v1.1.0), surface its provisional wording in the brief so a reader knows the positioning/news bands are unratified.
- **Snapshot is read-only.** This module never edits `snapshot.json`.
- **C-ID citation requirement (judgment flags).** When `module_context.json` exists in the bundle, every non-default judgment-flag justification MUST cite ≥1 context finding ID (`C<n>`, e.g. `C3`) from `module_context.findings[]`. This applies to `--rating-actions-justification` (when `rating_actions != "neutral"`), `--inst-flow-justification` (when `inst_flow != "unknown"`), and `--insider-baseline-justification` (when `insider_baseline != "normal"`). A justification that makes a non-default assertion with no `C<n>` anchor is ungrounded. The `report_qc` gate (`judgment_flag_citations` check) enforces both grounding (≥1 C-ID present) and referential integrity (every cited C-ID exists in the registry) — a failure blocks the report. On the compressed floor (no `module_context.json`), this requirement does not apply and the check auto-passes.
