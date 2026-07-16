---
name: technical-analysis
description: Score a ticker's technical setup (trend, momentum, structure, volume/extension) against a versioned rubric, off an existing market-snapshot bundle. Use when the user says "technical analysis [ticker]", "chart check", "support and resistance", "is it overbought", or when a report needs the technical evidence module. Consumes an existing snapshot bundle in the CWD (runs market-snapshot first if none exists). Rubric v1.0.0.
---

# Technical Analysis (Evidence Module)

Score the technical dimension for one ticker from an already-built snapshot bundle. **All arithmetic is done by `scripts/score_technical.py`** — you run the script, read its small JSON, and write prose. You never compute a score, a percentage, or a level in text.

**Non-negotiables:**
- **Never do arithmetic in prose.** Every number you cite must already appear in `module_technical.json` or the snapshot. A number you would have to compute is a script change, not a prose change.
- **Single-snapshot rule.** This module never fetches market data. A figure missing from the snapshot is a *snapshot extension request*, not a fetch here.
- **The ladder is the only legal source of levels.** Every support/resistance level in your brief comes from `module_technical.json`'s `ladder` (built by `scripts/levels.py`). You never invent a level.
- **Options SENTIMENT is out of scope.** Only options-derived *levels* enter here, and only via the ladder (max pain, OI walls). Put/call ratios, IV, skew, and flow belong to other modules.

Trigger phrases: "technical analysis MU", "chart check for AAPL", "support and resistance on NVDA", "is TSLA overbought".

---

## Step 1 — Locate the snapshot bundle

In the invoker's CWD, find the newest bundle for the ticker:

```bash
ls -dt ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

- **If a bundle exists**, use it. Confirm it holds a `snapshot_<TICKER>_*.json` (the score script needs it).
- **If NO bundle exists**, invoke the `market-snapshot` skill for `<TICKER>` first, then continue with the bundle it produces. Do not attempt to fetch data here yourself.

---

## Step 2 — Run the scorer

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_technical.py \
  --bundle ./td_bundle_<TICKER>_<YYYY-MM-DD>
```

The script loads the newest snapshot, builds the S/R ladder (reusing the daily rows and, if present, the options chain), scores the four dimensions, and writes `<bundle>/module_technical.json` (its path is printed to stdout). Exit 2 means the bundle/snapshot could not be read — fix the bundle and re-run.

**Divergence flag (use ONLY with explicit chart evidence).** Default is `none`. If — and only if — you can point to concrete evidence of RSI divergence (e.g. price making higher highs while RSI makes lower highs into resistance), pass the flag WITH a required justification:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_technical.py \
  --bundle ./td_bundle_<TICKER>_<YYYY-MM-DD> \
  --divergence bearish \
  --divergence-justification "price higher highs, RSI lower highs into 130 resistance"
```

`--divergence bullish|bearish` without `--divergence-justification` is a hard error (exit 2) — this is intentional. Never assert divergence you cannot cite.

---

## Step 3 — Read the module JSON and write the brief

The module JSON is small — read it directly (unlike the snapshot's options chain, it is safe to load). Then write `<bundle>/brief_technical.md` with exactly these parts, in order:

1. **Score headline** — `## Technical Score: <score>/100 — <trend_claim>`. Copy `score` and `trend_claim` verbatim from the JSON. If `renormalized` is true, add a one-line note quoting `renormalization_note`.
2. **A single paragraph, ≤120 words.** Cite ONLY numbers present in `module_technical.json` (the `subscores[].arithmetic` strings and `inputs`) or the snapshot. Zero computed-in-prose numbers. Walk the four dimensions (trend, momentum, structure, volume/extension), naming the points each earned and why, using the `arithmetic` strings as your source of truth.
3. **S/R ladder mini-table** — the top 3 ladder entries BELOW `price.last` and the top 3 ABOVE, each with its `type` and `basis`:

   | Direction | Level | Type | Basis |
   |-----------|-------|------|-------|
   | Resistance | … | … | … |
   | (support) | … | … | … |

   Take these straight from the `ladder` array (already sorted ascending by level, each carrying `level`, `type`, `basis`, `pct_from_last`). Do not recompute distances — quote `pct_from_last` if you show one.
4. **One-line signal** — your single-sentence read of the setup (e.g. "Constructive uptrend holding MA50 support with room to the swing high; watch RSI into resistance."). This is the ONLY place a signal appears; it is prose, never a number, and the JSON's `signal` field stays `null`.
5. **Footer** — `_Rubric v<rubric_version> · as of <as_of>_` using the JSON's fields.

---

## Step 4 — Output contract

Report to the user (and to any calling skill):
- **Module path** — `<bundle>/module_technical.json`
- **Brief path** — `<bundle>/brief_technical.md`
- **Score** — `<score>/100`
- **Trend claim** — `uptrend | downtrend | sideways`
- **Signal line** — the one-liner from the brief

---

## Important Notes

- **Single-snapshot rule.** No fetching. A missing figure (e.g. `null` RSI) contributes 0 to its component and is named "n/a" in the arithmetic; if an entire dimension is null, the script renormalizes the score over the remaining max and sets `renormalized: true` — disclose that, never hide it.
- **Options sentiment out of scope (single-mapping).** Only options-derived LEVELS enter this module, and only through the ladder. IV, put/call, skew, and flow are other modules' evidence — do not read or narrate them here.
- **Ladder is the only legal source of levels.** No level may appear in the brief that is not in `module_technical.json`'s `ladder`. The LLM selects and narrates levels; `scripts/levels.py` mints them.
- **Rubric version travels with the numbers.** The rubric version (`1.0.0`) is printed in the module JSON and MUST appear in the brief footer, so any reader can tell which scoring rule produced the score.
- **`trend_claim` is mechanical and report-level.** It is computed from the price/MA stack for later cross-module QC; do not override it in prose.
- **Snapshot is read-only.** This module never edits `snapshot.json`.
