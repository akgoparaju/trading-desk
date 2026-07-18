---
name: composite-score
description: Roll the four evidence modules (technical, fundamental, sentiment, risk) plus an in-script thesis-conviction dimension into one weighted composite score, letter grade, action, and expected-value block, off an existing market-snapshot bundle. Use when the user says "score it", "composite", "composite score [ticker]", or when a report needs the overall call. Consumes the four module JSONs (runs the missing evidence skills first) — it does NOT re-read the snapshot's scored fields. Weights are FIXED per profile (balanced|trader|long-term). Rubric v1.0.0.
---

# Composite Score (Decision Layer)

Roll the four evidence dimensions plus a fifth **thesis-conviction** dimension into one call for a ticker. **All arithmetic is done by `scripts/score_composite.py`** — you supply judgment (a scenario set and four conviction flags, each with mandatory reasoning), run the script, read its JSON, and write prose. You never compute a composite, a weight, a grade, or an expected value in text.

This is the **L3 decision layer**. It consumes the four evidence module JSONs' final scores (`module_technical.json`, `module_fundamental.json`, `module_sentiment.json`, `module_risk.json`) — it does **not** re-read the snapshot's scored fields, so it never double-counts a fact. It reads the snapshot only for `price.last` (EV reference) and `meta.ticker`/`as_of`.

**Non-negotiables:**
- **Never do arithmetic in prose.** Every number you cite must already appear in `module_composite.json` (its `dimensions`, `thesis_conviction.subscores`, `ev`, `sensitivity`) or an evidence module JSON. A composite, a contribution, a grade, an EV, a break-even entry you would have to compute is a script change, not a prose change.
- **Conviction is asserted, never assumed.** The four conviction flags have **no defaults** — you set all four with honest justifications read off the evidence briefs, and you construct the scenario set with stated probability reasoning. A missing flag, missing justification, or missing scenario set is a hard error (exit 2).
- **Weights are FIXED per profile.** You never hand-tune a weight. Comparability across names beats per-name personalization (spec §9.3). The `--profile` flag selects a fixed weight column; that is the only lever.

Trigger phrases: "score it", "composite for MU", "composite score AAPL", "what's the overall call on NVDA".

---

## Step 1 — Locate the bundle and ensure the four evidence modules exist

In the invoker's CWD, find the newest bundle for the ticker:

```bash
ls -dt ./trading_desk_<TICKER>/detail_reports_* ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

Newest first across both layouts: the new `./trading_desk_<TICKER>/detail_reports_<date>/` bundles and the legacy `./td_bundle_<TICKER>_<date>/` bundles (fallback for old runs).

- **If NO bundle exists**, invoke the `market-snapshot` skill for `<TICKER>` first, then continue with the bundle it produces.
- **Ensure the four evidence module JSONs are present** in the bundle. For each missing one, run its skill first:
  - `module_technical.json` → run the **technical-analysis** skill.
  - `module_sentiment.json` → run the **sentiment-positioning** skill.
  - `module_risk.json` → run the **risk-analytics** skill.
  - `module_fundamental.json` → this is where the run chooses **deep FSI reuse vs the compressed pass**. If invoked from **full-trade-analysis**, honor its Phase-0 FSI decision. If running **standalone** and the FSI plugins are **absent** (no `equity-research:*` skills): check `./trading_desk_config.json` for a recorded `fsi_offer` first (honor it silently); absent → you MUST make the same offer once and RECORD the answer to the config (`"fsi_offer": {"asked": true, "choice": ...}`) — never auto-install, never silently skip the ask on a user-initiated run:
    > "Deep fundamental mode uses the claude-for-financial-services plugins. Install now, or proceed with the built-in compressed fundamental pass?"

If the user chooses install, hand them these EXACT commands (verified marketplace source — do not improvise them; the user runs them in their own prompt, you cannot):
```
/plugin marketplace add anthropics/financial-services
/plugin install equity-research
/plugin install financial-analysis
```
Then tell them: the new plugins load in the NEXT session — this run continues with the compressed pass, and the next analysis will use deep FSI mode automatically.

    Run the fundamental scorer. **The moat flags are context-grounded whenever `module_context.json` exists** (which it always does when this runs from full-trade-analysis Phase 2, and whenever the company-context skill has been run standalone). Read `module_context.json`'s `competitive` block (`position` / `moat_evidence`) and the `findings[]` behind it, then pass `--moat <wide|narrow|none>` with a `--moat-justification` that **cites ≥1 finding ID** (`C\d+`, e.g. `C3`):
    ```bash
    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_fundamental.py \
      --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD> \
      --moat narrow \
      --moat-justification "durable HBM share but commoditizing DRAM caps the moat (C3, C5)"
    ```
    The script **exits 2** if `--moat` is given without a justification, and **exits 2** if the justification does not match the citation regex `C\d+` — so name real IDs from the context registry. `--moat wide` scores 10, `narrow` 6, `none` 2.

    **The compressed-without-context floor is the last resort.** ONLY when no `module_context.json` exists (the FSI-absent floor where the context module ran `web_compressed` but produced no registry to cite, or context was skipped entirely), run the scorer with **`--moat` omitted**:
    ```bash
    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_fundamental.py --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>
    ```
    Omitting `--moat` scores the moat sub-component `0` "n/a (no context assessment)" and does NOT count toward the dimension's evaluable inputs — **disclose that** in your brief. This is the snapshot-only compressed pass (deep FSI initiation/model reuse not applied); the module carries `fundamental_mode: "compressed_snapshot_pass"`.

    Then **write `<bundle>/brief_fundamental.md`** — fundamental has no standalone skill, so the composite step owns its evidence brief. Read `module_fundamental.json` directly and write the brief in the **same format as the other evidence briefs** (technical / sentiment / risk), in order:
      1. **Score headline** — `## Fundamental Score: <score>/100`. Copy `score` verbatim. If `renormalized` is true, add a one-line note quoting `renormalization_note`.
      2. **A single paragraph, ≤120 words.** Cite ONLY numbers present in `module_fundamental.json` (the `subscores[].arithmetic` strings and `inputs`) or the snapshot — zero computed-in-prose numbers. Walk the sub-dimensions (quality, valuation), naming the points each earned and why, using the `arithmetic` strings as your source of truth. **State the mode disclosure**: if `fundamental_mode` is `compressed_snapshot_pass`, say the pass was snapshot-only (deep model reuse not applied).
      3. **One-line signal** — your single-sentence read of the fundamentals (prose, never a number).
      4. **Footer** — `_Rubric v<rubric_version> · as of <as_of>_` using the JSON's fields.

    This keeps the report-renderer's evidence-brief inputs complete: all four evidence dimensions (technical, sentiment, risk, fundamental) now have a `brief_<dimension>.md`.

Each present module JSON only needs its final `score`; the composite reads nothing else from them. A **missing** module is not fatal on its own — the script excludes that dimension, rescales the remaining weights to sum 1, and discloses it. But if **≥ 3 of the 5 dimensions** are missing (i.e. ≥ 2 of the 4 evidence modules absent), the script exits 2 ("insufficient evidence modules") — run the missing evidence skills first.

---

## Step 2 — Construct the scenario set (judgment, with stated reasoning)

Write a scenario JSON `<bundle>/scenarios.json` — a list of `{"name", "prob", "price_target"}`. This is analyst judgment, anchored to **real levels** from the snapshot and module JSONs, never invented:

- **Price targets** reference real anchors: ladder support/resistance levels (`module_technical.json`), consensus PT (from the snapshot / sentiment module), a valuation floor (`module_risk.json` / fundamental valuation). A bull target above resistance, a base near the current structure, a bear at a proven support / valuation floor.
- **Probabilities** are your analyst judgment and **must carry reasoning** (passed to `--scenario-reasoning`). Use a differentiated bull/base/bear split when you have a view.
- **`25/50/25` is a disclosed FALLBACK only** — use it when you have no differentiated view, and say so in the reasoning ("no differentiated view; symmetric 25/50/25 fallback").

Probabilities must sum to 1.0 (±1e-6) or the script exits 2 (validated by `scripts/ev_kelly.scenario_ev`).

Example `scenarios.json`:

```json
[
  {"name": "bull", "prob": 0.30, "price_target": 165.0},
  {"name": "base", "prob": 0.50, "price_target": 120.0},
  {"name": "bear", "prob": 0.20, "price_target": 85.0}
]
```

---

## Step 3 — Set the four conviction flags (read the evidence briefs FIRST)

Read the four evidence briefs, then set all four flags (no defaults — each is REQUIRED with a one-line honest justification):

1. **`--variant strong|some|none`** (+ `--variant-justification`): how differentiated is your read vs consensus? `strong` 20 / `some` 12 / `none` 4. "none" is not a failure — a plain, consensus-aligned read is `none`.
2. **`--catalyst-clarity clear|partial|vague`** (+ `--catalyst-clarity-justification`): is there a dated, identifiable catalyst? `clear` 20 / `partial` 12 / `vague` 4.
3. **`--invalidation both-legs|one-leg|none`** (+ `--invalidation-justification`): do you have BOTH a thesis-invalidation and a trade-stop named? `both-legs` 20 / `one-leg` 10 / `none` 0.
4. **The scenario set + `--scenario-reasoning`** (Step 2) drives the EV-asymmetry sub-score (max 40, mechanical: `ev / hurdle`, banded).

If you cannot justify a flag value from the briefs, pick the honest lower value — do not inflate conviction.

**HARD RULE — conviction justifications and scenario reasoning ground in context finding IDs.** When `module_context.json` exists, the `--variant-justification`, `--catalyst-clarity-justification`, and `--scenario-reasoning` MUST cite the context registry by ID (`(C<n>)`): a `variant` call rests on the argued `module_context.cases`; a `catalyst-clarity` call on the `module_context.live_tape` and its dated findings. A conviction justification (or a scenario-probability rationale) that asserts a differentiated / dated view with **no `(C<n>)` anchor is unanchored** — cite the finding or drop to the honest lower flag. (These justification strings are free-text — the script does not regex them — so this discipline is on you; the moat justification IS regex-gated by `score_fundamental.py`.)

---

## Step 4 — Choose the profile and run the scorer

Ask the user for the profile if interactive; otherwise default `balanced`. The profile selects a **fixed** weight column and the EV **horizon** (trader 0.5y, balanced 1.5y, long-term 4.0y — the hurdle is `0.08 × horizon_years`).

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_composite.py \
  --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD> \
  --scenarios ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>/scenarios.json \
  --scenario-reasoning "HBM demand is the asymmetric driver (C2); base assumes in-line ramp" \
  --variant some --variant-justification "differentiated on gross-margin path vs street (C4)" \
  --catalyst-clarity clear --catalyst-clarity-justification "HBM3E ramp dated to next print (C2)" \
  --invalidation both-legs --invalidation-justification "thesis: GM stalls <35%; trade stop below 200DMA" \
  --profile balanced
```

Optional repeatable `--entry-level <price>` flags add `ev_at_levels` rows — useful for ad-hoc what-if runs and re-scores; the trade-plan skill computes its own EV-at-level from the same scenario set and does NOT re-invoke this script. The script writes `<bundle>/module_composite.json` (path printed to stdout). Any missing flag/justification, a bad scenario file, a probability sum ≠ 1, or ≥ 3 missing dimensions is exit 2 — fix and re-run.

---

## Step 5 — Read the module JSON and write the brief

The module JSON is small — read it directly. Then write `<bundle>/brief_composite.md` with exactly these parts, in order. **Every number comes from `module_composite.json` or an evidence module JSON — zero computed-in-prose numbers.**

1. **The call** — `## <TICKER> — Grade <grade> (<action>), composite <score>/100 (<profile>)`. Copy `grade`, `action`, `score`, `profile` verbatim. If `renormalization_note` is non-null, add a one-line note quoting it.
2. **The one-line tension sentence** — the strongest bull point vs the strongest bear point in a single sentence (e.g. "The setup rewards the HBM ramp (technical 70, clear catalyst) but the below-median valuation floor and a middling risk read cap the conviction — a hold, not an add."). This is the `tension` slot; it stays `null` in the JSON and lives only in prose.
3. **Composite table** — from `dimensions`, one row per dimension:

   | Dimension | Score | Weight | Contribution | Source |
   |-----------|-------|--------|--------------|--------|
   | technical / fundamental / sentiment / risk / thesis_conviction | … | … | … | … |

   Quote `score`, `weight`, `weight_renormalized` (if it differs from `weight`, say weights were renormalized), `contribution`, `source` verbatim.
4. **EV table** — from `ev`: `ev_at_current`, `hurdle_total` (with `horizon_years_convention`), `ev_breakeven_entry`, and any `ev_at_levels` rows. State the `scenario_reasoning`.
5. **Three-profile sensitivity row** — from `sensitivity`, show all three profiles' `score` + `grade`. **When the grades differ, show all three** and say which profile you ran (e.g. "trader lens grades this a B, long-term a C — the call is profile-sensitive").
6. **Conviction subscores** — the four `thesis_conviction.subscores` arithmetic strings, verbatim (EV asymmetry, variant, catalyst clarity, invalidation), so a reader sees how the 5th dimension was built.
7. **Mode disclosures** — if the fundamental module was the compressed pass, note it. Note any renormalization.
8. **Footer** — `_Rubric v<rubric_version> · as of <as_of>_` using the JSON's fields.

---

## Step 6 — Output contract

Report to the user (and to any calling skill):
- **Module path** — `<bundle>/module_composite.json`
- **Brief path** — `<bundle>/brief_composite.md`
- **Call** — `<grade> (<action>), <score>/100 (<profile>)`
- **Sensitivity** — the three profile grades (flag if they differ)
- **Conviction flags** — the four flags used and their justifications
- **Tension line** — the one-liner from the brief

---

## Important Notes

- **Weights are FIXED per profile (spec §9.3).** Comparability across names beats per-name personalization. You never hand-tune a weight; `--profile` is the only lever, and it selects a whole fixed column. The table: balanced (tech .25, fund .25, sent .20, risk .15, conviction .15); trader (.35/.10/.25/.15/.15); long-term (.10/.40/.15/.15/.20).
- **Grade bands are fixed.** A ≥80 → Buy/Add; B 60–79 → Hold/Accumulate-on-weakness; C 45–59 → Hold/Trim; D <45 → Reduce/Avoid. Never re-band.
- **Scenario probabilities MUST carry reasoning.** `--scenario-reasoning` is mandatory. `25/50/25` is a disclosed fallback for "no differentiated view", never a lazy default you leave silent.
- **The EV hurdle is profile-scoped, but sensitivity re-bands it.** The chosen profile's thesis-conviction EV asymmetry uses that profile's hurdle (`0.08 × horizon_years`). The `sensitivity` block recomputes the FULL composite — including the EV asymmetry re-banded per each profile's own hurdle — for all three profiles, which is why the same name can grade B under one lens and C under another.
- **Context-grounded fundamentals + conviction (coverage-first).** When `module_context.json` exists, the fundamental step ALWAYS passes `--moat <wide|narrow|none> --moat-justification "<cites C-IDs>"` derived from `module_context.competitive` (the script exits 2 on a missing or non-citing justification), and the conviction/scenario justifications cite the context registry by ID. Compressed-without-context (no registry to cite) is the **last-resort floor**: `--moat` is omitted (moat sub-component `0` "n/a"), disclosed. The context module is UNSCORED — it grounds fundamental's moat and conviction's reasoning; it never adds a composite dimension, so single-mapping holds.
- **Single-mapping preserved by construction.** This module scores NO snapshot field directly — it consumes module scores and reads `price.last` only as an EV reference. Its `INPUT_FIELDS` is empty, so it can never collide with an evidence module.
- **Calibration is provisional.** The composite rubric's bands and weights are provisional until 5–10 names have been scored and the grade distribution reviewed — say so if a reader treats a single grade as gospel.
- **Rubric version travels with the numbers.** The rubric version (`1.0.0`) is printed in the module JSON and MUST appear in the brief footer, so any reader can tell which scoring rule produced the call.
- **Snapshot is read-only.** This module never edits `snapshot.json` or any evidence module JSON.
