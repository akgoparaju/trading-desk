---
name: full-trade-analysis
description: Run the end-to-end trade decision pipeline for a ticker — snapshot → evidence (parallel subagents) → composite score → executable trade plan + options expression → blocking report → thesis registration + monitoring. Orchestrates the 8 other trading-desk skills through phase gates. Use when the user says "full trade analysis [ticker]", "trade decision report [ticker]", "score [ticker] end to end", or wants the complete call, not one dimension. Every number comes from the bundle; the orchestrator does zero arithmetic in prose.
---

# Full Trade Analysis (L5 Orchestrator)

Coordinate the eight trading-desk skills into one phase-gated pipeline for a ticker: build the verified snapshot, score the evidence in parallel, roll the composite, mint the executable plan and options expression, render the blocking report, then register the thesis and offer a re-score. **You are a conductor, not a calculator** — every figure lives in the bundle's module JSONs; you never compute a score, a level, an EV, or a percent in prose. Your job is to invoke the right skill at the right gate, stop the line when a gate fails, and keep the conversation lean (paths + summaries, never file dumps).

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

**FSI runtime offer (MANDATORY ask-once, RECORDED — never auto-install).** This is not optional prose; it is a gate with a required artifact:
1. If the `equity-research:*` skills are available → skip the offer, reuse per the Depth bullet.
2. Else read `./trading_desk_config.json` → if it has `"fsi_offer": {"asked": true, ...}` → honor the recorded choice silently.
3. Else you MUST ask the user now (do not self-classify the run as unattended when a user prompt started it):
> "Deep fundamental mode uses the claude-for-financial-services plugins. Install now, or proceed with the built-in compressed fundamental pass?"

If the user chooses install, hand them these EXACT commands (verified marketplace source — do not improvise them; the user runs them in their own prompt, you cannot):
```
/plugin marketplace add anthropics/financial-services
/plugin install equity-research
/plugin install financial-analysis
```
Then tell them: the new plugins load in the NEXT session — this run continues with the compressed pass, and the next analysis will use deep FSI mode automatically.
4. WRITE the answer to `./trading_desk_config.json`: `"fsi_offer": {"asked": true, "choice": "install"|"compressed", "date": "<YYYY-MM-DD>"}` — the recorded artifact is what makes this ask-once instead of ask-never or ask-always. Re-open only when the user says "set up FSI" / "change fundamental mode".
Genuinely unattended (scheduled/cron re-runs) → compressed pass + disclose + record `"choice": "compressed", "unattended": true`. Never auto-install.

The source + data-mode preflight (Phase 1) runs inside market-snapshot — it reads `./trading_desk_config.json` (ask-once source selection) and detects the AV tier; fold both outcomes into the scope echo once known.

One-line echo, e.g.: `Running full-trade-analysis MU · profile=balanced (assumed) · horizon: swing into next print · no position context · fundamental: coverage (deep, current) · data_source: alphavantage · data_mode: alpha_vantage.`

---

## Phase 0.5 — Coverage (deep is the DEFAULT; compressed is the FSI-absent floor)

**The design in one line:** deep coverage is the default read; the compressed pass is the floor you fall to ONLY when FSI is absent and declined; `module_context` (Phase 2) feeds scoring either way. Check for existing coverage, and if absent, **always initiate** when FSI is installed — coverage is permanent and later runs are cheap, so the first run pays for every run after it.

Look for `./trading_desk_<TICKER>/coverage/` (the FSI initiation artifacts — research / model / valuation) and branch:

**(a) Coverage EXISTS.** Run a **freshness check**: compare the latest reported quarter in `snapshot.events` / `snapshot.fundamentals` against the quarter the coverage model was built on (the model artifact's own as-of / last-modeled quarter). If a reported quarter **postdates** the model artifacts, the model is stale — **run FSI `equity-research:model-update` on the `coverage/` artifacts** (mandatory: stale coverage that scores as if current is a lie) before scoring, updating them in place. Announce plainly: `coverage current` (no newer quarter) or `coverage updated — model-update run for <quarter>` (refreshed). Coverage now feeds Phase 2 in `coverage_distilled` mode.

**(b) Coverage ABSENT + FSI installed** (`equity-research:*` skills available). **ANNOUNCE plainly, then INITIATE — no ask** (always-initiate is the recorded default):
> "No coverage for <TICKER> — running initiation now (FSI `initiating-coverage` Tasks 1-3: company research, financial model, valuation; typically 30-60+ min, token-heavy; coverage is permanent and later runs are cheap)."

Then **INVOKE the `equity-research:initiating-coverage` skill scoped to Tasks 1-3 ONLY** — company research, the financial model, and the valuation. That skill requires **task-by-task execution with verified prerequisites** (it is its own gated workflow) — FOLLOW its SKILL, running Task 1 → Task 2 → Task 3 in order. **SKIP its Tasks 4-5** (chart generation, final report assembly): our docket renders charts and our report-renderer assembles the report, so those FSI stages are redundant. Direct the three artifacts into `./trading_desk_<TICKER>/coverage/` (the `research.md` / `model.md` / `valuation.md` layout the company-context skill reads). Coverage then feeds Phase 2 in `coverage_distilled` mode. (A user may say **"skip initiation"** to override for this one run → fall through to compressed as in (c); record nothing — the default stays always-initiate.)

**(c) Coverage ABSENT + FSI absent** (no `equity-research:*` skills). This is the **recorded `fsi_offer` flow** (unchanged from Phase 0's FSI runtime offer — the ask-once, RECORDED gate above): honor a recorded choice silently, else ask once and record. If the user **installs**, the plugins load next session and *this* run continues compressed. If the user **declines** (or a recorded `"compressed"` choice), the run drops to the **COMPRESSED FLOOR, LOUDLY DISCLOSED**:
> "Running compressed — no coverage, no FSI; the context module runs `web_compressed` and the fundamental moat is scored from cited web research, not a distilled model."

Announce the coverage outcome in one line, e.g.: `Coverage: initiated this session (FSI Tasks 1-3) — coverage_distilled.` / `Coverage: current — coverage_distilled.` / `Coverage: none (FSI declined) — compressed floor, context runs web_compressed.` Carry the **coverage mode** (`coverage_distilled | web_compressed`) and **whether initiation ran this session** forward to Phases 2, 5, and 6.

---

## Phase 1 — Snapshot

Invoke the **market-snapshot** skill for `<TICKER>`. It runs the **source + data-mode preflight** (Step 0 — settles the `data_source` via `./trading_desk_config.json` ask-once, then announces the AV tier `alpha_vantage | av_free_degraded | web_fallback` and, if interactive, asks before proceeding on a degraded mode), builds `./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>/` under the ticker parent, fetches raw data from the chosen source (Alpha Vantage, a foreign MCP via `docs/CANONICAL_CONTRACT.md` adapters, or cited web sources), lets in-repo Python compute every number, fills qualitative text slots, and runs its own blocking gate. **Carry the reported `data_source` and `data_mode` forward** — they feed the Phase 6 completeness statement.

**GATE — snapshot QC (`qc_gate.py` exit 0).** The snapshot skill runs the gate itself. A check may be waived ONLY with a real, written justification (`--waive "check:reason"`). Print the attestation paragraph.

**On gate failure after fixes: STOP and report.** Root-cause it (bad raw file / script bug / genuinely inconsistent data) inside the snapshot skill; if it still cannot pass honestly, **do not proceed** — an unverified snapshot poisons every downstream number. This is the pipeline's only full stop.

---

## Phase 2 — Evidence (PARALLEL subagents)

Dispatch evidence scoring to **subagents via the Agent tool**, one per module, so the independent modules run concurrently. Read `superpowers:dispatching-parallel-agents` conventions if unsure.

**Dependency: technical-analysis must COMPLETE before risk-analytics starts** — risk-analytics reads the S/R ladder that technical-analysis mints (`module_technical.json`). So:

- **Wave 1 (parallel):** `{ technical-analysis, sentiment-positioning, company-context }` — sentiment and context have no cross-module dependency; technical mints the ladder.
- **Wave 2 (after wave 1 completes):** `{ risk-analytics }` — reads the ladder. (The **fundamental** compressed pass is NOT dispatched here — the **composite-score** skill runs `score_fundamental.py` itself in Phase 3 if `module_fundamental.json` is absent. Note this so you don't double-run it.)

**The company-context module (Phase-0.5-scoped).** Invoke the **company-context** skill for `<TICKER>` (`${CLAUDE_PLUGIN_ROOT}/skills/company-context/SKILL.md`) in the **mode Phase 0.5 settled**: `coverage_distilled` when coverage exists/was initiated, `web_compressed` on the compressed floor. It runs parallel with technical/sentiment (no market fetch, no cross-module read — it consumes the snapshot + coverage/web), but it **MUST COMPLETE before Phase 3** — its `module_context.json` `findings[]` registry is the citation source the composite step's fundamental moat flag and conviction reasoning ground in. It is **UNSCORED** — it adds no dimension to the composite; it grounds the ones that are scored. The context skill runs its own blocking gate (`report_qc.py --context`); confirm `module_context.json` exists (with `qc.qc_passed: true`) before the composite.

**Every subagent prompt MUST contain, verbatim in spirit:**
1. **The bundle path** — `./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>` (absolute is safest; legacy `./td_bundle_<TICKER>_<date>` bundles also resolve via the discovery glob).
2. **READ AND FOLLOW its SKILL.md**, naming the exact path:
   - technical → `${CLAUDE_PLUGIN_ROOT}/skills/technical-analysis/SKILL.md`
   - sentiment → `${CLAUDE_PLUGIN_ROOT}/skills/sentiment-positioning/SKILL.md`
   - risk → `${CLAUDE_PLUGIN_ROOT}/skills/risk-analytics/SKILL.md`
3. **The judgment-flag protocol** — set only honestly-supported flags off the snapshot's own text/context, each with a one-line written justification; an unjustifiable flag is a fabrication, not just a script error. (Sentiment: `--rating-actions` / `--inst-flow` / `--insider-baseline`. Technical: `--divergence` only with `--divergence-justification` citing chart evidence. Risk: the stress scenario `--stress-pct` + `--top-risk`, both together or neither.)
4. **Single-snapshot + no-arithmetic-in-prose** — the subagent fetches nothing and computes nothing in text; every cited number already sits in the module JSON or snapshot.
5. **Return contract:** the **score + the module JSON path + a ≤5-line summary** only. Briefs (`brief_<dim>.md`) live in the bundle, NOT in the conversation — never paste a brief or a file dump back.
6. **Model guidance:** run at a **sonnet-or-opus class** model — a capable scorer, **never a frontier orchestrator model**. These are bounded, script-driven tasks; a heavyweight model wastes budget. (The company-context skill runs the same class — it is authoring a cited registry off the snapshot + coverage, not orchestrating.)

**GATE — evidence complete.** Confirm `module_technical.json`, `module_sentiment.json`, `module_risk.json`, and `module_context.json` all exist in the bundle (context's `qc.qc_passed` is `true`), and that each subagent cited the snapshot only (no fetches). A module that internally renormalized around a null dimension is fine and disclosed — the **file** must exist. The context module MUST be present before Phase 3 (the composite step cites its `findings[]` IDs).

---

## Phase 3 — Score

Invoke the **composite-score** skill for `<TICKER>` at the chosen `--profile`. It: ensures the four evidence modules exist (running the **fundamental pass** — `score_fundamental.py` — itself if `module_fundamental.json` is absent); constructs the scenario set with **stated probability reasoning** (real anchors, `25/50/25` only as a disclosed fallback); sets the four conviction flags with honest justifications read off the evidence briefs; runs `score_composite.py`; and writes `brief_composite.md`.

**HARD RULE — judgments ground in context finding IDs.** `module_context.json` is present (Phase 2 gate). The composite step MUST use it:

- **The fundamental step passes the moat flags.** When `module_context.json` exists, `score_fundamental.py` is run with `--moat <wide|narrow|none> --moat-justification "<text citing ≥1 context finding ID, e.g. C3>"`, the level and citations derived from `module_context.competitive` (its `moat_evidence` / `position` and the `findings[]` behind them). This is not optional: `score_fundamental.py` **exits 2** if `--moat` is given without a justification, and **exits 2** if the justification does not match the citation regex `C\d+` — so the justification MUST name real finding IDs from the context registry. (Omitting `--moat` scores the moat sub-component `0` "n/a" — only correct on the compressed floor where no context registry exists to cite.)
- **The four conviction flags and the scenario probabilities MUST cite context finding IDs** in their justifications. `--variant-justification` and `--catalyst-clarity-justification` (and the scenario `--scenario-reasoning`) ground their claims in `module_context` findings by ID — a variant call rests on the argued cases (`module_context.cases`), a catalyst-clarity call on the live tape (`module_context.live_tape`) and its findings. State this as a hard rule to the composite step: **a conviction justification or a scenario-probability rationale that asserts a differentiated view without a `(C<n>)` anchor is unanchored** — cite the finding or lower the flag.

**GATE — composite exists.** `module_composite.json` is present; scenario probabilities summed to 1.0 (the script enforces this — exit 2 otherwise); if ≥3 of 5 dimensions were missing the script would have exited 2; and `module_fundamental.json`'s `flags` carry the moat level + a C-ID-citing justification (unless the compressed floor omitted `--moat`). Capture the call (grade / action / score) and the three-profile sensitivity row.

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

**Deliver:** the **report path** — `render_report.py` writes it to the **ticker parent** `./trading_desk_<TICKER>/<TICKER>_Trade_Report_<date>.md` (a sibling of the `detail_reports_<date>/` data folder; legacy bundles keep it inside), printed to stdout — plus the **composite line** (`grade — action, score/100, profile`) + the **expression line** (recommended structure/size for the profile) + the **coverage line** (`coverage_distilled` vs `web_compressed`, and "initiation run this session" if Phase 0.5 (b) fired) + the **QC attestation** (gate verdict).

**Then the docket (report-renderer Step 5).** After the md gate is green, report-renderer renders the **docket** — the `exec` (2pp) and `detail` (~10-15pp) PDFs — into the ticker parent (`<TICKER>_Trade_Report_<date>.pdf` / `<TICKER>_Detail_<date>.pdf`), gated by the `pdf_slots.json` provenance stamp. This requires the matplotlib+reportlab render venv: if `render_env.py --check` exits 3 the report ships **md-only** and the docket is skipped (disclosed, with the one-line bootstrap) — it never blocks the run. **Deliver the two PDF paths (or the md-only note).**

---

## Phase 6 — Register & monitor

**(a) Thesis entry.** Write `<bundle>/thesis_entry.md` from the embedded template below, **filled from the module JSONs only** (no invented fields). If the **FSI equity-research `thesis-tracker` skill** is installed, ALSO register the thesis there (soft dependency). If it is absent, say so in one line and rely on the local file — do not fabricate a tracker path.

**(b) Re-score offer (OFFER, never auto-create).** Identify the next binary event from `snapshot.events` (next earnings / a dated catalyst). Offer, do not schedule unprompted:
> "Re-run full-trade-analysis `<TICKER>` the day after `{next binary event, YYYY-MM-DD}` and render a delta report vs this bundle."

If the user accepts AND a scheduling facility is available (the `schedule` skill or `CronCreate`), create it — the scheduled action is exactly the re-run + `render_report.py --delta --previous <this_bundle>`. If no facility is available (or the user declines), hand them the one-line manual command instead. Never auto-create a schedule the user did not accept.

**(c) Completeness statement (MANDATORY).** Emit the embedded completeness block: which of the five dimensions ran, which renormalized or were missing, the **coverage mode** (`coverage_distilled` vs `web_compressed`) and **whether an initiation was run this session** (Phase 0.5 (b)), whether the coverage model was current or `model-update`d this run, the snapshot's `meta.data_mode`, its `meta.api_tier_notes`, and **whether the docket rendered (exec + detail PDFs) or degraded to md-only** (render venv not built). **When `data_mode` is not `alpha_vantage`, name it explicitly and list `fundamentals.web_transcribed_fields`** (the fields sourced from cited web transcription) so a reader sees the reduced-provenance surface. The report always ships with this statement even under degradation.

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
- **Dimensions run:** technical / fundamental / sentiment / risk / thesis-conviction — <ran | renormalized | missing> each. Context module (unscored): <ran, coverage_distilled | ran, web_compressed | missing>.
- **Renormalized / missing:** <list any dimension the composite excluded or rescaled, or "none">.
- **Coverage:** <coverage_distilled (current) | coverage_distilled (model-update run this session for <quarter>) | coverage_distilled (initiated this session, FSI Tasks 1-3) | web_compressed (compressed floor — no coverage, FSI absent/declined)>.
- **Data mode:** <meta.data_mode; when not alpha_vantage, add: web-transcribed fields = <fundamentals.web_transcribed_fields, or "none">, options = stand-aside>.
- **API tier notes:** <snapshot meta.api_tier_notes, verbatim>.
- **Gates:** snapshot QC <PASS/WAIVED:…> · report QC <PASS/WAIVED:…>.
- **Docket:** <rendered (exec + detail PDFs) · pdf-slots gate PASS | md-only — render venv not built (bootstrap: `python3 scripts/render_env.py`)>.
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
- **Coverage-first: deep is the default, compressed is the floor.** Phase 0.5 initiates coverage the first time a ticker is seen (FSI installed) — the always-initiate default — because coverage is permanent and every later run reuses it cheaply. Stale coverage is `model-update`d, never scored as current. The compressed `web_compressed` floor is reached ONLY when FSI is absent and declined, and it is loudly disclosed. Either way the context module grounds scoring; the difference is `coverage_distilled` (distilled from the FSI model) vs `web_compressed` (cited web research).
- **Data mode gates the depth, not the pipeline.** The market-snapshot preflight sets `alpha_vantage | av_free_degraded | web_fallback`. A degraded/fallback run still produces an honest, QC-passing report — options stand aside, fundamentals may be web-transcribed — but the completeness statement must name the mode. Only a FAILED snapshot QC gate stops the line.
- **Subagent prompts forbid arithmetic-in-prose.** Every subagent must be told: cite only numbers already in the module JSON / snapshot; a number you would compute is a script change, not a prose change. This is the same rule the modules encode — the orchestrator enforces it at dispatch.
- **Token hygiene.** Subagents return **paths + a ≤5-line summary**, never briefs or file dumps. Briefs live in the bundle; the report-renderer condenses them. **The options chain file is never read by anyone** but `scripts/chain.py`.
- **Model discipline.** Evidence subagents run on a sonnet-or-opus-class scorer, never a frontier orchestrator model — the work is bounded and script-driven.
- **Rubric versions travel with the numbers.** All nine skills' rubric/rule versions (each `module_*.json`'s `rubric_version`, the expression `rule_version`, the snapshot schema, the plugin version) appear in the report footer **automatically** — `render_report.py` reads them from the bundle; you never type a version.
- **Typical wall-clock.** Snapshot ~10–15 min (incl. IV history sampling), evidence ~5 min (parallel), decision + report ~10 min. Report the estimate if the user asks; do not pad it.
- **Provisional by design.** Composite weights/bands and the expression decision table (`expression-v1.0.0`) are provisional until enough names are scored — say so if a reader treats a single call as settled.
- **Educational only.** This is analysis, not investment advice. Verify every figure independently before acting.
