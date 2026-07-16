---
name: full-trade-analysis
description: Run the end-to-end trade decision pipeline for a ticker — snapshot → evidence (parallel subagents) → composite score → executable trade plan + options expression → blocking report → thesis registration + monitoring. Orchestrates the 8 other trade-decision skills through phase gates. Use when the user says "full trade analysis [ticker]", "trade decision report [ticker]", "score [ticker] end to end", or wants the complete call, not one dimension. Every number comes from the bundle; the orchestrator does zero arithmetic in prose.
---

# Full Trade Analysis (L5 Orchestrator)

Coordinate the eight trade-decision skills into one phase-gated pipeline for a ticker: build the verified snapshot, score the evidence in parallel, roll the composite, mint the executable plan and options expression, render the blocking report, then register the thesis and offer a re-score. **You are a conductor, not a calculator** — every figure lives in the bundle's module JSONs; you never compute a score, a level, an EV, or a percent in prose. Your job is to invoke the right skill at the right gate, stop the line when a gate fails, and keep the conversation lean (paths + summaries, never file dumps).

**Non-negotiables:**
- **Single-snapshot rule (restated for the whole pipeline).** `market-snapshot` builds the one `snapshot.json` that is the single source of truth. No downstream skill — and no subagent — fetches market data. A figure missing from the snapshot is a *snapshot extension request*, never a downstream fetch.
- **Zero LLM arithmetic.** Every number in every brief, the report, and the thesis entry already appears in a bundle module JSON. A number you would have to compute is a script/module change, not a prose change.
- **Module JSONs are the inter-skill contract.** Skills talk to each other through `module_*.json` files in the bundle, not through the conversation. The chain file is never read by anyone but `scripts/chain.py`.
- **Two blocking gates stop the line.** The snapshot QC gate (`qc_gate.py` exit 0) and the report QC gate (`report_qc.py` exit 0). A FAILED snapshot gate is the ONLY full stop; everything else degrades and discloses.

`${CLAUDE_PLUGIN_ROOT}` is the plugin install dir (where `scripts/` lives). All bundle outputs stay under the invoker's CWD.

Trigger phrases: "full trade analysis MU", "trade decision report AAPL", "score NVDA end to end".

---

## Phase 0 — Scope

State the run parameters back to the user in **one line** before starting. Ask if interactive; if unattended, infer with the stated defaults and say which you assumed:

- **Profile** — `trader | balanced | long-term` (default **balanced**). This selects the fixed weight column and EV horizon downstream (composite-score, trade-plan).
- **Horizon note** — a one-line intent (e.g. "swing into the print" vs "multi-year hold"), for the thesis entry.
- **Position context** — record ONLY if the user volunteers it. **Never solicit holdings.** v1 sizes a fresh position; existing-position deltas are out of scope (note it if offered).
- **Depth** — if an FSI equity-research **initiation** already exists for the ticker, reuse its coverage for the fundamental read; else the compressed snapshot pass is used. This check is **best-effort** — absence is fine and disclosed, never a blocker.

One-line echo, e.g.: `Running full-trade-analysis MU · profile=balanced (assumed) · horizon: swing into next print · no position context · fundamental: compressed pass (no FSI initiation found).`

---

## Phase 1 — Snapshot

Invoke the **market-snapshot** skill for `<TICKER>`. It builds `./td_bundle_<TICKER>_<YYYY-MM-DD>/`, fetches raw Alpha Vantage data, lets in-repo Python compute every number, fills qualitative text slots, and runs its own blocking gate.

**GATE — snapshot QC (`qc_gate.py` exit 0).** The snapshot skill runs the gate itself. A check may be waived ONLY with a real, written justification (`--waive "check:reason"`). Print the attestation paragraph.

**On gate failure after fixes: STOP and report.** Root-cause it (bad raw file / script bug / genuinely inconsistent data) inside the snapshot skill; if it still cannot pass honestly, **do not proceed** — an unverified snapshot poisons every downstream number. This is the pipeline's only full stop.

---

## Phase 2 — Evidence (PARALLEL subagents)

Dispatch evidence scoring to **subagents via the Agent tool**, one per module, so the independent modules run concurrently. Read `superpowers:dispatching-parallel-agents` conventions if unsure.

**Dependency: technical-analysis must COMPLETE before risk-analytics starts** — risk-analytics reads the S/R ladder that technical-analysis mints (`module_technical.json`). So:

- **Wave 1 (parallel):** `{ technical-analysis, sentiment-positioning }` — sentiment has no cross-module dependency; technical mints the ladder.
- **Wave 2 (after wave 1 completes):** `{ risk-analytics }` — reads the ladder. (The **fundamental** compressed pass is NOT dispatched here — the **composite-score** skill runs `score_fundamental.py` itself in Phase 3 if `module_fundamental.json` is absent. Note this so you don't double-run it.)

**Every subagent prompt MUST contain, verbatim in spirit:**
1. **The bundle path** — `./td_bundle_<TICKER>_<YYYY-MM-DD>` (absolute is safest).
2. **READ AND FOLLOW its SKILL.md**, naming the exact path:
   - technical → `${CLAUDE_PLUGIN_ROOT}/skills/technical-analysis/SKILL.md`
   - sentiment → `${CLAUDE_PLUGIN_ROOT}/skills/sentiment-positioning/SKILL.md`
   - risk → `${CLAUDE_PLUGIN_ROOT}/skills/risk-analytics/SKILL.md`
3. **The judgment-flag protocol** — set only honestly-supported flags off the snapshot's own text/context, each with a one-line written justification; an unjustifiable flag is a fabrication, not just a script error. (Sentiment: `--rating-actions` / `--inst-flow` / `--insider-baseline`. Technical: `--divergence` only with `--divergence-justification` citing chart evidence. Risk: the stress scenario `--stress-pct` + `--top-risk`, both together or neither.)
4. **Single-snapshot + no-arithmetic-in-prose** — the subagent fetches nothing and computes nothing in text; every cited number already sits in the module JSON or snapshot.
5. **Return contract:** the **score + the module JSON path + a ≤5-line summary** only. Briefs (`brief_<dim>.md`) live in the bundle, NOT in the conversation — never paste a brief or a file dump back.
6. **Model guidance:** run at a **sonnet-or-opus class** model — a capable scorer, **never a frontier orchestrator model**. These are bounded, script-driven tasks; a heavyweight model wastes budget.

**GATE — evidence complete.** Confirm `module_technical.json`, `module_sentiment.json`, and `module_risk.json` all exist in the bundle, and that each subagent cited the snapshot only (no fetches). A module that internally renormalized around a null dimension is fine and disclosed — the **file** must exist.

---

## Phase 3 — Score

Invoke the **composite-score** skill for `<TICKER>` at the chosen `--profile`. It: ensures the four evidence modules exist (running the **fundamental compressed pass** — `score_fundamental.py` — itself if `module_fundamental.json` is absent, in `compressed_snapshot_pass` mode); constructs the scenario set with **stated probability reasoning** (real anchors, `25/50/25` only as a disclosed fallback); sets the four conviction flags with honest justifications read off the evidence briefs; runs `score_composite.py`; and writes `brief_composite.md`.

**GATE — composite exists.** `module_composite.json` is present; scenario probabilities summed to 1.0 (the script enforces this — exit 2 otherwise); if ≥3 of 5 dimensions were missing the script would have exited 2. Capture the call (grade / action / score) and the three-profile sensitivity row.

---

## Phase 4 — Plan

Invoke the **trade-plan** skill for `<TICKER>`. It runs in two passes with options-strategy in between:
1. **Pass 1 (`--stock-plan`)** — mints entries (ladder/valuation confluences), exits, both-leg invalidation, Kelly sizing, hedge trigger, don't-chase line, and a preliminary expression. Requires the honest `--catalyst-in-thesis` flag and the fundamental-invalidation leg (metric + threshold + justification), both with no defaults.
2. **options-strategy (pipeline mode)** — trade-plan invokes it; it derives direction from the composite grade, aligns to `entry_1`, gates on **IV-vs-realized** (never IV level alone), and writes `module_options.json` with real strikes.
3. **Pass 2 (`--synthesize`)** — folds the chosen structures + hedge spec back into `expression`.

**GATE — plan complete.** `module_tradeplan.json` carries **both invalidation legs** (technical stop + fundamental metric/threshold); sizing ≤ the profile cap (5/8/10% trader/balanced/long-term, −1 notch on a binary event ≤30d); options strikes exist in the chain (the synthesize pass exits 2 if a recommended structure's strikes are absent); and the expression is synthesized (`synthesized: true`) — an executable structure, or a disclosed-unexecutable "stand aside" if the chain was thin / had no vol edge.

---

## Phase 5 — Report

Invoke the **report-renderer** skill for the bundle: `render_report.py` writes the full 3-page skeleton (every table/number script-owned); you fill only the `<!-- SLOT:... -->` prose slots citing numbers already printed on the page; then `report_qc.py` runs the **blocking §12 gate** (`--report <path>`), which must exit 0.

**GATE — report QC (`report_qc.py` exit 0, BLOCKING).** Fix the **prose**, never the numbers: `no_empty_slots` → fill the slot; `number_provenance` orphan → remove/rephrase to a printed figure (never invent a number); a table-driven check failing (composite_arithmetic / ev_consistency / sizing / strikes / pop_method) is an upstream module bug — fix the module and re-render. A genuinely justified failure may be `--waive "check:reason"` (disclosed, never to hide a fabricated number). Re-run until exit 0.

**Deliver:** the **report path** + the **composite line** (`grade — action, score/100, profile`) + the **expression line** (recommended structure/size for the profile) + the **QC attestation** (gate verdict).

---

## Phase 6 — Register & monitor

**(a) Thesis entry.** Write `<bundle>/thesis_entry.md` from the embedded template below, **filled from the module JSONs only** (no invented fields). If the **FSI equity-research `thesis-tracker` skill** is installed, ALSO register the thesis there (soft dependency). If it is absent, say so in one line and rely on the local file — do not fabricate a tracker path.

**(b) Re-score offer (OFFER, never auto-create).** Identify the next binary event from `snapshot.events` (next earnings / a dated catalyst). Offer, do not schedule unprompted:
> "Re-run full-trade-analysis `<TICKER>` the day after `{next binary event, YYYY-MM-DD}` and render a delta report vs this bundle."

If the user accepts AND a scheduling facility is available (the `schedule` skill or `CronCreate`), create it — the scheduled action is exactly the re-run + `render_report.py --delta --previous <this_bundle>`. If no facility is available (or the user declines), hand them the one-line manual command instead. Never auto-create a schedule the user did not accept.

**(c) Completeness statement (MANDATORY).** Emit the embedded completeness block: which of the five dimensions ran, which renormalized or were missing, whether FSI initiation coverage was reused or the compressed pass ran, and the snapshot's `meta.api_tier_notes`. The report always ships with this statement even under degradation.

---

## Embedded thesis-entry template

Fill every field from the module JSONs / snapshot — nothing computed in prose. Write to `<bundle>/thesis_entry.md`:

```markdown
# Thesis — <TICKER> (<YYYY-MM-DD>)

- **Grade / composite:** <grade> (<action>) · <composite score>/100 · profile <profile>
- **Thesis (2-3 lines):** <distilled from module_composite.json ev.scenario_reasoning + the tension line — the bull driver vs the cap on conviction>
- **Pillars (top evidence signals):** <the strongest signal from each of technical / fundamental / sentiment / risk, one clause each, taken from the module signal/subscore text>
- **Invalidation (BOTH legs, verbatim from module_tradeplan.json):**
  - Technical: weekly close below <technical_leg.level>
  - Fundamental: <fundamental_leg.metric> <fundamental_leg.threshold>
- **Conviction:** <grade> (= the composite grade)
- **Catalysts (with dates):** <events.catalysts / next_earnings entries, each with its YYYY-MM-DD>
- **Expression + size:** <expression.recommended_for_profile structure(s)> · <sizing.recommended_pct>
- **Next review:** <the day after the next binary event, YYYY-MM-DD>

_Sourced from bundle module JSONs · rubric versions in the report footer._
```

## Embedded completeness statement

```markdown
### Run completeness — <TICKER> <YYYY-MM-DD>
- **Dimensions run:** technical / fundamental / sentiment / risk / thesis-conviction — <ran | renormalized | missing> each.
- **Renormalized / missing:** <list any dimension the composite excluded or rescaled, or "none">.
- **Fundamental depth:** <FSI initiation reused | compressed_snapshot_pass>.
- **API tier notes:** <snapshot meta.api_tier_notes, verbatim>.
- **Gates:** snapshot QC <PASS/WAIVED:…> · report QC <PASS/WAIVED:…>.
```

---

## Degradation policy

- **Any single module failure** → that dimension is `n/a`, the composite **renormalizes** the remaining weights to sum 1 and **discloses** it (the scripts already do this). Do not stop the pipeline.
- **≥2 of 4 evidence modules missing** → composite-score exits 2 ("insufficient evidence modules"); re-run the missing evidence skills before proceeding.
- **A FAILED snapshot QC gate is the ONLY full stop.** Everything downstream degrades gracefully (fewer entries, no valuation floor, a "stand aside" expression) and discloses.
- **The report always ships with the completeness statement** — even a degraded run produces an honest, QC-passing report that names what was reduced.

---

## Important Notes

- **Single-snapshot rule (pipeline-wide).** One `snapshot.json` feeds every module and every subagent. Nobody re-fetches market data; a missing figure is a snapshot extension request, not a fetch.
- **Subagent prompts forbid arithmetic-in-prose.** Every subagent must be told: cite only numbers already in the module JSON / snapshot; a number you would compute is a script change, not a prose change. This is the same rule the modules encode — the orchestrator enforces it at dispatch.
- **Token hygiene.** Subagents return **paths + a ≤5-line summary**, never briefs or file dumps. Briefs live in the bundle; the report-renderer condenses them. **The options chain file is never read by anyone** but `scripts/chain.py`.
- **Model discipline.** Evidence subagents run on a sonnet-or-opus-class scorer, never a frontier orchestrator model — the work is bounded and script-driven.
- **Rubric versions travel with the numbers.** All nine skills' rubric/rule versions (each `module_*.json`'s `rubric_version`, the expression `rule_version`, the snapshot schema, the plugin version) appear in the report footer **automatically** — `render_report.py` reads them from the bundle; you never type a version.
- **Typical wall-clock.** Snapshot ~10–15 min (incl. IV history sampling), evidence ~5 min (parallel), decision + report ~10 min. Report the estimate if the user asks; do not pad it.
- **Provisional by design.** Composite weights/bands and the expression decision table (`expression-v1.0.0`) are provisional until enough names are scored — say so if a reader treats a single call as settled.
- **Educational only.** This is analysis, not investment advice. Verify every figure independently before acting.
