---
name: risk-analytics
description: Score a ticker's risk-reward conditions (volatility state, drawdown profile, margin of safety, liquidity & solvency) against a versioned rubric, off an existing market-snapshot bundle, and build a downside map from the shared S/R ladder. Use when the user says "risk profile [ticker]", "downside map", "how risky is this entry", or when a report needs the risk evidence module. Consumes an existing snapshot bundle AND the technical-analysis module (runs technical-analysis first if its module is missing). Higher score = better risk-reward conditions. Rubric v1.0.0.
---

# Risk Analytics (Evidence Module)

Score the risk-reward dimension for one ticker from an already-built snapshot bundle. **All arithmetic is done by `scripts/score_risk.py`** — you run the script, read its small JSON, and write prose. You never compute a score, a percentage, a ratio, or a level in text.

**Higher score = BETTER conditions.** A high risk-analytics score means the *setup* is favorable (low volatility, shallow historical drawdowns, a real discount, tight downside vs. upside, deep liquidity, a strong balance sheet) — it does NOT mean the stock is dangerous. State this convention as a short note in the score HEADLINE line (not inside the ≤120-word paragraph, where it eats the word budget).

**Non-negotiables:**
- **Never do arithmetic in prose.** Every number you cite must already appear in `module_risk.json` or the snapshot. A number you would have to compute is a script change, not a prose change.
- **Single-snapshot rule.** This module never fetches market data. A figure missing from the snapshot is a *snapshot extension request*, not a fetch here.
- **The ladder is the only legal source of levels.** Every level in your downside map comes from `module_technical.json`'s `ladder` (built by `scripts/levels.py`), plus the two script-computed anchors: the valuation floor and the stress row. You never invent a level.
- **Stress scenario is a JUDGMENT input, never a computed one.** You pick the single top risk and the stress percentage; the *script* turns the percentage into a level. Never compute the stress level in prose.

Trigger phrases: "risk profile MU", "downside map for AAPL", "how risky is this entry on NVDA".

---

## Step 1 — Locate the snapshot bundle and require the technical module

In the invoker's CWD, find the newest bundle for the ticker:

```bash
ls -dt ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

- **If a bundle exists**, confirm it holds a `snapshot_<TICKER>_*.json`.
- **If NO bundle exists**, invoke the `market-snapshot` skill for `<TICKER>` first, then continue with the bundle it produces. Do not attempt to fetch data here yourself.
- **Require `module_technical.json`.** This module reads the S/R ladder from the technical module. If `<bundle>/module_technical.json` is absent, invoke the `technical-analysis` skill for `<TICKER>` first (it mints the ladder). The scorer exits 2 with `run technical-analysis first (module_technical.json missing)` if you skip this.

---

## Step 2 — Decide the stress scenario (judgment)

Before running the script, decide whether to add a stress row to the downside map. This is a judgment call, made from the snapshot's catalyst/news evidence — never computed in prose:

1. **Name the single top risk.** Read the snapshot's `events` and `sentiment` (news summary, upcoming catalysts). Pick the ONE risk most likely to drive a sharp drawdown (e.g. an earnings miss, a demand air-pocket, a guidance cut). One risk, named plainly.
2. **Pick a stress percentage anchored to a downside-map level.** Choose a signed fraction (e.g. `-0.30`) that lands near a real ladder level below `last` — so the stress scenario reads against structure the snapshot already contains, not a round guess.

Both are fed to the script as flags; the script computes the stress *level*. If you have no concrete risk to name, run without `--stress-pct` — an unfounded stress number is worse than none.

---

## Step 3 — Run the scorer

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_risk.py \
  --bundle ./td_bundle_<TICKER>_<YYYY-MM-DD>
```

With a stress scenario (both flags required together):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_risk.py \
  --bundle ./td_bundle_<TICKER>_<YYYY-MM-DD> \
  --stress-pct -0.30 \
  --top-risk "HBM demand air-pocket into the next print"
```

The script loads the newest snapshot, reads the ladder from `module_technical.json`, scores the four dimensions, builds the downside map (ladder entries below `last` + a valuation-floor row when computable + the stress row when flagged) and the volatility-profile context block, and writes `<bundle>/module_risk.json` (its path is printed to stdout). `--stress-pct` without `--top-risk` is a hard error (exit 2). Exit 2 also means the bundle/snapshot could not be read or the technical module is missing — fix that and re-run.

---

## Step 4 — Read the module JSON and write the brief

The module JSON is small — read it directly. Then write `<bundle>/brief_risk.md` with exactly these parts, in order:

1. **Score headline** — `## Risk Score: <score>/100 — <one-line conditions read>`. Copy `score` verbatim. If `renormalized` is true, add a one-line note quoting `renormalization_note`. State once, plainly, that higher = better conditions.
2. **A single paragraph, ≤120 words.** Cite ONLY numbers present in `module_risk.json` (the `subscores[].arithmetic` strings and `inputs`) or the snapshot. Zero computed-in-prose numbers. Walk the four dimensions (volatility state, drawdown profile, margin of safety, liquidity & solvency), naming the points each earned and why, using the `arithmetic` strings as your source of truth.
3. **Downside-map mini-table** — the FIRST 5 rows of `tables.downside_map` — the map is emitted nearest-first, so rows 1-5 are the nearest anchors below `last` in the order price would fall through them, each with its `level`, `type`, and `basis`; include `pct_from_last` if you show a distance (quote it, never recompute), and the `risk` text on the stress row:

   | Level | Type | Basis | % from last |
   |-------|------|-------|-------------|
   | … | … | … | … |

4. **Correlation note vs SPY** — one line from `tables.vol_profile`: quote `beta`, `corr`, and `beta_n_days`. Correlation is context (it is NOT scored) — say so, so no reader treats it as a scored factor.
5. **One-line signal** — your single-sentence read of the risk-reward setup (e.g. "Volatility is calm and the balance sheet is net-cash, but the entry sits near the highs with thin downside asymmetry — size small until it pulls back to structure."). This is the ONLY place a signal appears; it is prose, never a number, and the JSON's `signal` field stays `null`.
6. **Footer** — `_Rubric v<rubric_version> · as of <as_of>_` using the JSON's fields.

---

## Step 5 — Output contract

Report to the user (and to any calling skill):
- **Module path** — `<bundle>/module_risk.json`
- **Brief path** — `<bundle>/brief_risk.md`
- **Score** — `<score>/100` (higher = better conditions)
- **Top downside anchor** — the nearest downside-map level below `last` and its type
- **Signal line** — the one-liner from the brief

---

## Important Notes

- **Higher score means better conditions.** This module scores risk-*reward*, not danger. A near-100 score is a calm, discounted, asymmetric, liquid, cash-rich setup. State the convention in the brief so it can never be read backwards.
- **Single-snapshot rule.** No fetching. A missing figure (e.g. `null` beta) contributes 0 to its component and is named "n/a" in the arithmetic; if an entire dimension is null, the script renormalizes the score over the remaining max and sets `renormalized: true` — disclose that, never hide it.
- **PT-upside is scored elsewhere (single-mapping).** Consensus analyst-target upside is scored in the sentiment-positioning module's street view, NOT here — even though the design spec (§5.3) listed it in both. Listing one fact in two modules violates the spec's own single-mapping rule ("each snapshot fact scores in exactly one module"). We resolve it by scoring PT-upside only in sentiment; the ~10 points it would have carried here are reallocated into the asymmetry component (18) and distance-from-ATH (12), which already express margin of safety without double-counting the analyst target. Do not narrate consensus-target upside as a risk-analytics factor.
- **Ladder is the only legal source of levels.** The downside map's structural rows all come from `module_technical.json`'s `ladder`. The valuation floor (`pe_5yr_median × eps_ntm`) and the stress row (`last × (1 + stress_pct)`) are the only two script-computed anchors, and both carry explicit `type`/`basis` provenance. No other level may appear in the brief.
- **Correlation is context, not a score.** `corr` (and `beta_n_days`) sit in `vol_profile` for the SPY correlation note; only `beta` feeds the volatility-state score. Do not treat correlation as a scored factor.
- **Rubric version travels with the numbers.** The rubric version (`1.0.0`) is printed in the module JSON and MUST appear in the brief footer, so any reader can tell which scoring rule produced the score.
- **Snapshot is read-only.** This module never edits `snapshot.json`.
