---
name: sentiment-positioning
description: Score a ticker's sentiment and positioning (street view, revisions momentum, smart money & insiders, options/short positioning, price momentum vs SPY) against a versioned rubric, off an existing market-snapshot bundle. Use when the user says "sentiment check [ticker]", "positioning", "what's the street saying", or when a report needs the sentiment evidence module. Consumes an existing snapshot bundle only (runs market-snapshot first if none exists). Rubric v1.0.0.
---

# Sentiment & Positioning (Evidence Module)

Score the sentiment/positioning dimension for one ticker from an already-built snapshot bundle. **All arithmetic is done by `scripts/score_sentiment.py`** — you run the script, read its small JSON, and write prose. You never compute a score, a percentage, a ratio, or a relative-return in text.

**Non-negotiables:**
- **Never do arithmetic in prose.** Every number you cite must already appear in `module_sentiment.json` or the snapshot. A number you would have to compute (buy%, PT-upside, rel-return vs SPY) is a script change, not a prose change.
- **Single-snapshot rule.** This module never fetches market data. A figure missing from the snapshot is a *snapshot extension request*, not a fetch here.
- **The three judgment flags are formed from snapshot text/context ONLY.** You read the snapshot's own news/ratings/insider evidence to set `--rating-actions`, `--inst-flow`, and `--insider-baseline`. You never invent an action, a fund flow, or an insider read the snapshot does not support. Each non-default flag carries a one-line justification.

Trigger phrases: "sentiment check MU", "positioning for AAPL", "what's the street saying on NVDA".

---

## Step 1 — Locate the snapshot bundle

In the invoker's CWD, find the newest bundle for the ticker:

```bash
ls -dt ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

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
  --bundle ./td_bundle_<TICKER>_<YYYY-MM-DD>
```

With judgment flags (each non-default value paired with its justification):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_sentiment.py \
  --bundle ./td_bundle_<TICKER>_<YYYY-MM-DD> \
  --rating-actions positive --rating-actions-justification "3 upgrades post-print" \
  --inst-flow accumulating --inst-flow-justification "13F net buys last quarter" \
  --insider-baseline unusual --insider-baseline-justification "CFO cluster sales at highs"
```

The script loads the newest snapshot, scores the five dimensions, builds the positioning and momentum-vs-SPY tables and the hedging-cost note, and writes `<bundle>/module_sentiment.json` (its path is printed to stdout). A non-default flag without its justification is a hard error (exit 2). Exit 2 also means the bundle/snapshot could not be read — fix that and re-run.

---

## Step 4 — Read the module JSON and write the brief

The module JSON is small — read it directly. Then write `<bundle>/brief_sentiment.md` with exactly these parts, in order:

1. **Score headline** — `## Sentiment Score: <score>/100 — <one-line read>`. Copy `score` verbatim. If `renormalized` is true, add a one-line note quoting `renormalization_note`.
2. **A single paragraph, ≤120 words.** Cite ONLY numbers present in `module_sentiment.json` (the `subscores[].arithmetic` strings and `inputs`) or the snapshot. Zero computed-in-prose numbers. Walk the five dimensions (street view, revisions momentum, smart money & insiders, positioning & derivatives, price momentum), naming the points each earned and why, using the `arithmetic` strings as your source of truth. If the street-view dimension was capped ("PT below price: dimension capped at 10/25"), say so plainly.
3. **Momentum & positioning mini-table** — from `tables.momentum_vs_spy` and `tables.positioning`, quoting the relative returns and positioning figures (never recompute a `rel_*` value):

   | Metric | Value |
   |--------|-------|
   | rel_3m / rel_12m | … |
   | short_interest_pct (trend) | … |
   | put_call_ratio_full_chain | … |
   | iv_pctile_1yr | … |

4. **Hedging-cost note** — if `tables.hedging_cost_note` is non-null, include it as a one-liner (protective structures historically cheap; cross-ref options-strategy). Omit if null.
5. **One-line signal** — your single-sentence read of the sentiment/positioning setup (e.g. "Street is constructive and revisions are turning up, but a call-heavy chain and low short interest into overbought RSI say the froth is already priced — no fresh-money edge here."). This is the ONLY place a signal appears; it is prose, never a number, and the JSON's `signal` field stays `null`.
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
- **Options sentiment scores HERE and nowhere else (single-mapping, spec §2).** The options-derived *sentiment* fields — full-chain put/call ratio, 1-yr IV percentile, skew — score in this module only. Options-derived *levels* (e.g. max-pain, high-OI strikes as support/resistance) score in technical-analysis. That split — sentiment here, levels there — is the single-mapping rule: each snapshot fact scores in exactly one module. Do not narrate an options *level* as a sentiment factor, or the P/C ratio as a technical level.
- **PT-upside is scored HERE, not in risk-analytics.** Consensus analyst-target upside (`pt_vs_price_pct`) is scored in this module's street view. The design spec listed it in both risk-analytics and sentiment; risk-analytics documents that it deliberately does NOT score it (reallocating the points into its asymmetry + dist-from-ATH components) to keep single-mapping. Score PT-upside only here.
- **Complacency guard.** In the positioning dimension, `short_interest_pct < 1.5%` combined with `rsi14 > 70` is scored as **complacency (2 pts), not a bullish signal** — a stock nobody is shorting while it is overbought has priced out the doubters, which is a crowded-long risk, not a squeeze setup. The guard fires *before* the normal short-interest bands, and its thresholds are STRICT: SI must be below 1.5 AND rsi14 above 70 — a 68-70 RSI or a 1.5-2% SI does NOT fire it (it lands in the normal `<2% → 6` band); do not describe near-misses as complacency in the brief. `rsi14` only *conditions* this guard here; it is scored in technical-analysis, never double-counted here (it is a GUARD field, not an INPUT field).
- **13F lag is disclosed.** `--inst-flow` defaults to `unknown` (0 pts, "n/a — 13F not assessed; lag disclosed") precisely because 13F filings lag by up to 45 days. Only override `unknown` when the snapshot carries a real flow read, and say so in the justification.
- **Judgment flags come from snapshot text/context, never invention.** `--rating-actions`, `--inst-flow`, and `--insider-baseline` are read off the snapshot's news/ratings/insider evidence. Each non-default value carries a one-line justification; an unjustifiable flag is a hard error and, more importantly, a fabrication you must not make.
- **Rubric version travels with the numbers.** The rubric version (`1.0.0`) is printed in the module JSON and MUST appear in the brief footer, so any reader can tell which scoring rule produced the score.
- **Snapshot is read-only.** This module never edits `snapshot.json`.
