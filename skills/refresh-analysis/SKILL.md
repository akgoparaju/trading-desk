---
name: refresh-analysis
description: Re-run an existing ticker workspace CHEAPLY — selective FETCHING (fast-moving market surface + anything an earnings/dividend event revised), never selective SCORING (all modules re-emit against one new snapshot). Carries judgments forward disclosed unless an event forces re-affirmation. Renders the full report AND a delta vs the previous bundle, then appends a dated thesis entry with the invalidation checks. Use when the user says "refresh [ticker]", "update the analysis", "re-score [ticker]", or "update the score". Consumes the previous bundle; plans via scripts/refresh_plan.py; re-runs the full module chain. Append-only — never edits the previous bundle.
---

# Refresh Analysis (cheap re-run of an existing workspace)

A refresh is a full-trade-analysis run that FETCHES selectively. `scripts/refresh_plan.py` decides — deterministically — which manifest groups to refetch (the fast-moving market surface always; anything an earnings/dividend event between the two runs revised) and which to REUSE verbatim from the previous bundle. Everything else is identical to a fresh run: **one new snapshot, ALL modules re-emit against it** (the single-snapshot rule). You are a conductor, not a calculator — every number lives in a bundle module JSON; you compute nothing in prose.

**Non-negotiables:**
- **Selective FETCHING, never selective SCORING.** The plan only controls which raw files are refetched vs copied. Every module (technical → risk; sentiment; fundamental; composite; trade-plan; options; synthesize) re-runs against the single new snapshot — no module is skipped because "nothing changed there."
- **Append-only workspace.** A refresh NEVER edits, overwrites, or deletes the previous bundle. It writes a new `detail_reports_<as_of>/` sibling. Reused raw files are COPIED (the originals stay put).
- **Honest provenance on reuse.** A copied raw file keeps its ORIGINAL `retrieved_utc` in the new manifest — the plan only authorizes a reuse when the file is within its staleness window, so the copy passes the QC gate legally without back-dating anything.
- **Zero LLM arithmetic.** Same rule as the full pipeline: a number you would compute is a script/module change, not a prose change.

`${CLAUDE_PLUGIN_ROOT}` is the plugin install dir (where `scripts/` lives). All outputs stay under the resolved workspace root (`WORKROOT`, see below).

Trigger phrases: "refresh MU", "update the analysis for AAPL", "re-score NVDA", "update the score".

---

## Workspace root (`--output-dir`) + prior workspace (`--prev-dir`)

This orchestrator accepts an optional **`--output-dir <ABS_DIR>`** and, for a redirected refresh, an optional **`--prev-dir <ABS_DIR>`** (e.g. `refresh-analysis GOOG --output-dir /abs/new --prev-dir /abs/prior`). Resolve them FIRST:

- **`--output-dir <ABS_DIR>` given** → `WORKROOT = <ABS_DIR>` (MUST be absolute; `mkdir -p` if missing) — all NEW output lands here, decoupled from the process CWD. **Absent** → `WORKROOT = .` (the invoker's CWD — byte-for-byte unchanged).
- **Ticker workspace — FLAT under `--output-dir` (v1.2.0):** given → `TICKER_WS = <WORKROOT>` (the ticker workspace IS `<WORKROOT>`; drop the `trading_desk_<TICKER>/` segment — the caller passes a per-ticker dir). Absent → nested `TICKER_WS = ./trading_desk_<TICKER>` (human layout, unchanged). **Config + scales/adapters stay at `<WORKROOT>/trading_desk_config[.json]`** regardless of layout.

**The prior bundle — `<previous_bundle>`:**
- **`--prev-dir <PREV_DIR>` given** (a fresh, empty `--output-dir` refresh — the new run gets its own dated dir): the prior lives in a SEPARATE workspace. `PREV_WS = <PREV_DIR>` = the directory whose immediate children are `detail_reports_*` (+ `coverage/`, `iv_history_<TICKER>.json` when present). `<previous_bundle>` = the newest `detail_reports_*` under `<PREV_DIR>`. **`--prev-dir` is READ-ONLY** — the append-only rule holds; NEVER write into `<PREV_DIR>`.
- **`--prev-dir` absent** (v1.1.0 behavior): `PREV_WS = TICKER_WS` — `<previous_bundle>` = the newest `detail_reports_*` under `<TICKER_WS>` (a persistent per-ticker workspace keeps prior bundles as siblings of the new one).

Fan it out: everywhere below that reads/writes `./trading_desk_<TICKER>/…` (the NEW bundle, `coverage/`, the report) → use `<TICKER_WS>/…`. Pass **`--output-dir <WORKROOT>`** into each sub-skill (`market-snapshot`, `company-context`) when you re-run the module chain (exactly as `full-trade-analysis` Phases 2-4); pass **`--prev-dir <PREV_DIR>`** to `refresh_plan.py`; resolve `<previous_bundle>` under `<PREV_WS>` for the reuse-`cp`, carry-forward judgments, the delta report, and every `--previous <previous_bundle>` QC call; give every `python3 scripts/…` path argument absolute from `<TICKER_WS>`/`<WORKROOT>`. One root governs the NEW workspace; `--prev-dir` is the only read of the prior.

---

## Step 1 — Locate the workspace + data mode

Locate the workspaces: NEW output goes to `<TICKER_WS>/`; the PRIOR bundle is `<previous_bundle>`, the newest `detail_reports_*` under `<PREV_WS>` (= `--prev-dir` if given, else `<TICKER_WS>`). Legacy `td_bundle_<TICKER>_<date>` bundles are accepted — output still migrates to the new `detail_reports_<as_of>/` layout.

**Source + data-mode context.** Also read `<WORKROOT>/trading_desk_config.json` (if present) and the previous manifest's `data_source` — reuse the recorded source (e.g. `alphavantage | mcp:polygon | stooq+web`) without re-asking; refetches use the same source's fetch pass (and its persisted `trading_desk_config/adapters/` transforms for bulk groups). Reuse the previous manifest's `data_mode` as the default and ANNOUNCE both (`alpha_vantage | av_free_degraded | web_fallback`). Run the full market-snapshot **source + tier preflight (Step 0)** ONLY if the workspace records no context (no `data_source`/`data_mode` in the previous manifest and no config file) — keep it light; do not re-probe a workspace that already declared its source and tier.

---

## Step 2 — Plan the refresh (deterministic) and PRESENT it

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/refresh_plan.py \
  --ticker-dir <TICKER_WS> [--prev-dir <PREV_DIR>] [--as-of <YYYY-MM-DD>]
```

Pass **`--prev-dir <PREV_DIR>`** whenever the caller gave one (a fresh `--output-dir` refresh) — `refresh_plan.py` reads the PRIOR bundle from `--prev-dir` (else from `--ticker-dir`), and roots the new `refresh_plan.json` + scale/proposal discovery at `--ticker-dir` (= `<TICKER_WS>`, walked up to find `trading_desk_config/scales` under either the flat or nested layout). It writes `<TICKER_WS>/refresh_plan.json` (path printed to stdout). Exit 2 = no previous bundle ("nothing to refresh — run a full analysis first") → tell the user to run `full-trade-analysis` first and stop.

**Coverage freshness (coverage-first).** If `<TICKER_WS>/coverage/` exists, compare the latest reported quarter (the fresh `earnings_calendar` / `snapshot.events`, `snapshot.fundamentals`) against the quarter the coverage model was built on. If a **new quarter reported since the coverage model** was last built, the model is stale for this refresh — **run FSI `equity-research:model-update` on the `coverage/` artifacts before rescoring** (updating them in place), exactly as full-trade-analysis Phase 0.5 (a) does. A model-update is **append-only to the coverage**: it revises the existing artifacts and **appends a new `{"skill": "equity-research:model-update", ...}` entry to `coverage/coverage_manifest.json`** (and refreshes `generated_utc`) — it does NOT re-initiate. **Note it in the plan presentation** ("coverage current" or "coverage stale — model-update for <quarter> before rescore"). If no `coverage/` exists, the refresh carries the previous run's coverage mode (`web_compressed` floor) forward — nothing to update.

**The full-depth gate is an INITIATION contract, re-checked on refresh in the RECORDED mode.** The full FSI-depth `coverage_qc.py` gate (full-trade-analysis Phase 0.5) governs the INITIAL coverage build — a refresh never re-initiates, so it does not re-decide depth. But after any model-update (or before rescoring on carried-forward coverage), **re-run `coverage_qc.py` in the mode the manifest records** so the updated coverage still meets the depth floor it was built to:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/coverage_qc.py \
  --coverage <TICKER_WS>/coverage \
  --mode <the manifest's depth_mode: full | shallow>
```

Read `coverage/coverage_manifest.json` `depth_mode` and pass the matching `--mode` (`full` for `"full"`, `shallow` for `"shallow (user-requested)"`) — the gate's mode-vs-manifest agreement check fails a mismatch. A FAIL means the model-update left the coverage below its own floor — complete it before rescoring, exactly as an initiation would.

**PRESENT the plan to the user in 2-3 lines before executing:** what refetches (count from `estimated_refetch_calls`; note IV refresh adds ~26 separately if `iv_history.action == "refresh"`), what's reused, the **coverage freshness verdict** (current / model-update needed / web_compressed floor), and whether `events.judgment_review_required` is true (an earnings or dividend event fell between the runs → judgments get re-affirmed, not carried). On a free tier, remind the user a no-event refresh is only ~6-8 calls and fits the anonymous quota.

**Scale falsifier monitoring (read, disclose — NEVER redesign).** The plan carries `scales[]` (per-scale falsifier results with `any_tripped` + a pre-registered `action_required`), `scale_review_required`, and `pending_proposals[]`. Read them here and act by contract, never by improvisation:

- **`scale_review_required: true` (a falsifier tripped).** Apply **ONLY** the pre-registered `on_trip` consequence — it is already in the scale's `action_required` string (a `flag+disclose`-class consequence; nothing that silently changes a parameter). **Disclose the trip in the delta** (which scale, which falsifier, the consequence applied). **Recommend invoking the `scale-review` skill** for a deliberate re-examination. **NEVER redesign a scale parameter inline** — a re-base is the scale-review skill's adversarial-gated job, not a refresh's.
- **`pending_proposals[]` non-empty.** Surface each pending proposal **verbatim** to the user (its filename under `<WORKROOT>/trading_desk_config/scales/proposals/`), with the one-word ratification path: typing **`ratify <name>@<version>`** files it into the active scales (see the ratification flow below). An unratified proposal is never silently skipped.
- **`scale_review_required: false` and no proposals.** Nothing to surface; the active scales still govern fundamental scoring unchanged.

This monitoring is disclosure-only within the refresh — the refresh **signals**; the scale-review skill **decides** any re-base.

---

## Step 3 — Assemble the new bundle (refetch some, copy the rest)

Create `<TICKER_WS>/detail_reports_<as_of>/raw/` (= `<BUNDLE>`) and start a fresh `manifest.json` (same skeleton as market-snapshot Step 0.5, with the previous `data_mode`). Every `raw/…` path below is under `<BUNDLE>/`.

For each group in `refresh_plan.groups`:
- **`action: "reuse"`** → `cp` the raw file from `<previous_bundle>`'s `raw/` (under `<PREV_WS>` — READ-only) into the new bundle's `raw/`, AND copy its manifest entry **VERBATIM** into the new manifest — keeping the ORIGINAL `retrieved_utc` (honest provenance; the plan only reused it because it is in-window, so QC passes).
- **`action: "refetch"`** → fetch it per the **market-snapshot SKILL's conventions** — the SAME endpoints, manifest keys, `return_full_data=true` + `datatype=json` rules, and the web gap-fill steps for web groups (`web_spot_check`, `short_interest`, and the `earnings_calendar` web fallback). Record a NEW `retrieved_utc`. `options_chain` with reason `absent last run` is a gap-fill — fetch it if the tier allows; if the tier blocks it, leave it absent and disclose (options stand aside, exactly as in a fresh degraded run).

For `iv_history`:
- **`reuse`** → the cache carries forward. **When `--prev-dir` was given** (a fresh, empty `<TICKER_WS>` with no cache of its own), **`cp` the prior cache `<PREV_DIR>/iv_history_<TICKER>.json` → `<TICKER_WS>/iv_history_<TICKER>.json`** (append-only: read prior, write new, never touch the prior) — **GUARDED on the prior file existing** (on a degraded/governed tier the prior run skipped IV sampling, so there is no cache to copy and the refresh proceeds with `iv_pctile_1yr` null, exactly as today). Without `--prev-dir` (v1.1.0), the prior cache is already the sibling in `<TICKER_WS>` — leave it untouched.
- **`refresh`** → run the market-snapshot **Step 4** batched biweekly sampling to (re)build `<TICKER_WS>/iv_history_<TICKER>.json` — the SAME two-phase collapse (parallel `HISTORICAL_OPTIONS` fetches → `<BUNDLE>/raw/iv_samples.json` manifest → ONE `scripts/build_iv_history.py --samples <BUNDLE>/raw/iv_samples.json --daily <BUNDLE>/raw/daily_adjusted.json --out <TICKER_WS>/iv_history_<TICKER>.json` call that deletes the consumed chains) — never the retired per-sample fetch→one-liner→`rm` loop (~26 API calls, a few turns; skip on any degraded/fallback tier).

---

## Step 4 — Build the snapshot + carry the text slots

Run the builder against the new bundle:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/build_snapshot.py \
  --bundle <BUNDLE> --ticker <TICKER>
```

Then fill the qualitative TEXT slots: **carry forward the previous snapshot's text slots** (`inst_flow_notes`, and any prose events context), but **UPDATE `sentiment.news_sentiment_summary` and `events.catalysts`** from the FRESH `news_sentiment` / `earnings_calendar` fetches — they were always refetched, so their summaries must reflect the new data, not the old. Never edit a numeric field by hand.

**GATE — snapshot QC (BLOCKING, exit 0):**
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/qc_gate.py <new_bundle>/snapshot_<TICKER>_<as_of>.json
```
(The snapshot path is POSITIONAL — there is no `--bundle` flag on this CLI.) Waivers per usual (`--waive "check:reason"`, real justification only). Reused in-window files pass staleness by construction. Print the attestation.

---

## Step 5 — Re-run ALL modules (single-snapshot rule) with JUDGMENT CARRY-FORWARD

Re-run the full module chain against the new bundle, in dependency order — **technical → risk; sentiment; company-context; fundamental (via the composite step); composite; trade-plan pass 1; options pipeline; synthesize** — exactly as `full-trade-analysis` Phases 2-4 do (parallel evidence subagents where independent; technical before risk; context alongside them, completing before composite). No module is skipped.

**Model directive (EXPLICIT).** When you spawn the re-run evidence subagents (technical / sentiment / risk) and the company-context subagent, **set the Agent tool's `model` parameter to `sonnet`** on each dispatch — do not rely on inheritance; a bounded, script-driven scorer must not run on the orchestrator's tier. These are the same bounded scorers full-trade-analysis Phase 2 runs; a frontier orchestrator model wastes budget. (A refresh never re-initiates coverage, so there is no `opus` coverage-init dispatch here — a model-update, if triggered in Step 2, follows the FSI skill's own model.)

**CONTEXT REFRESH RULE (coverage-first).** The company-context module is re-authored with a **partial carry-forward** — the live tape is always fresh; the durable narrative carries unless an event re-opens it:
- **`live_tape` is ALWAYS re-authored** from the fresh `news_sentiment` / `snapshot.events` — drop stale events, add new dated entries (each `≤` the new `as_of`). The live tape answers *what is moving the stock NOW*, so it can never be carried stale.
- **`business` / `competitive` / `cases` are CARRIED FORWARD** from the previous `module_context.json`, tagged `"[carried forward from <previous_as_of>]"` on each carried block — UNLESS `events.judgment_review_required == true` (an earnings/dividend event fell between runs), in which case **re-affirm them against the fresh evidence** and state what changed (a beat/miss can reshape the competitive read or a case's falsifiable conditions).
- **Finding IDs stay stable** across the refresh (downstream `--moat` and conviction justifications cite them by ID — a renumber is a breaking change). Coverage mode carries forward (`coverage_distilled` if `coverage/` present and current after any model-update; `web_compressed` on the floor).
- **Re-run the `--context` gate** on the refreshed module, folding `--previous <old_bundle>` so carried-forward figures stay in the allowed set:
  ```bash
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py --bundle <new_bundle> \
    --context <new_bundle>/module_context.json --previous <previous_bundle>
  ```

**JUDGMENT CARRY-FORWARD (the honest part).** Each module JSON's `flags` block carries the judgment flags + their justifications used last run. Read the PREVIOUS bundle's module JSONs first:
- **`events.judgment_review_required == false`** (no event between runs) → pass the SAME flag values + justifications to each module, appending `" [carried forward from <previous_as_of>]"` to each justification. The evidence hasn't been re-opened by an event; the judgment stands, disclosed as carried.
- **`events.judgment_review_required == true`** (earnings or dividend event between runs) → **re-derive each judgment honestly from the fresh evidence** and STATE what changed. Never blind-copy a flag across an event that could have moved it (a beat/miss reshapes the rating-actions read, the insider baseline, the fundamental-invalidation leg).

**Scenarios:** same rule. No event → carry the previous scenario set forward. Event → re-judge the scenarios on the fresh evidence. **Update scenario price targets ONLY by re-judgment, never silently** — a carried scenario keeps its old targets; a re-judged one states the new anchor.

---

## Step 6 — Render BOTH reports (blocking)

```bash
# Full report → written to <TICKER_WS>/ (the TICKER folder, one level
# above the detail_reports_<as_of>/ bundle) as <TICKER>_Trade_Report_<as_of>.md
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_report.py \
  --bundle <BUNDLE>
# Delta report vs the previous bundle
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_report.py \
  --bundle <BUNDLE> \
  --delta --previous <previous_bundle>
```

Fill only the `<!-- SLOT:... -->` prose slots (citing numbers already printed on the page); the delta report's `SLOT:delta_interpretation` explains what drove the composite/EV/level moves.

**GATE — report QC (`report_qc.py` exit 0, BLOCKING) on BOTH:**
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py --bundle <new_bundle> --report <full report path>
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py --bundle <new_bundle> --report <delta report path> --previous <previous_bundle>
```
(The delta report is auto-detected by its `Delta_Report` filename and runs the delta check subset; `--previous` folds the old bundle's values into the allowed provenance set.) Fix the PROSE, never the numbers; a table-driven failure is an upstream module bug. Waivers disclosed only.

**Docket (PDFs) — AFTER both md gates pass.** A refresh renders the full docket, including the **delta note** vs the previous bundle. As in report-renderer Step 5, check the venv first — `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_env.py --check` (exit 3 → announce md-only + the one-line bootstrap `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_env.py`, and SKIP the PDF steps; never block). When READY, capture the printed `<venv-python>` and:

```bash
# 1. Deterministic chart pack
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_charts.py --bundle <new_bundle> --set all
# 2. Author <new_bundle>/pdf_slots.json (shape per render_pdf.py: thesis_bullets[3]
#    "Lead — rest", desk_read{setup,edge,trigger,risk},
#    positioning{entry_discipline,sizing_kelly,path_dependency,monitoring}).
#    On a refresh delta_interpretation is REQUIRED — 1-2 sentences on what drove the
#    composite/EV/level moves, citing ONLY numbers in the delta report / module JSONs
#    (the Δ columns are legitimate: the slots gate is run with --previous below).
# 3. BLOCKING slots provenance gate (stamps qc_passed; --previous admits the Δ values)
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py --bundle <new_bundle> \
  --pdf-slots <new_bundle>/pdf_slots.json --previous <previous_bundle>
# 4. Render the three docs (delta REQUIRES --previous; exec gets it too → What-Changed box)
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py --bundle <new_bundle> --doc delta --previous <previous_bundle>
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py --bundle <new_bundle> --doc exec  --previous <previous_bundle>
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py --bundle <new_bundle> --doc detail
```

The PDFs (`<TICKER>_Trade_Report_<as_of>.pdf`, `<TICKER>_Detail_<as_of>.pdf`, `<TICKER>_Delta_Note_<as_of>.pdf`) land in the ticker parent. Fix slot PROSE, never numbers.

---

## Step 7 — Append the thesis entry (dated, append-only)

Append a dated section to `<TICKER_WS>/thesis_entry.md` (create if absent — never overwrite prior entries). Fill from the module JSONs / delta report ONLY:

```markdown
## Refresh — <TICKER> (<as_of>)
- **Grade:** <old grade> → <new grade> · **composite:** <old>/100 → <new>/100 (profile <profile>)
- **What drove the delta:** <2-3 lines from the delta report's composite/EV/level deltas — cite the delta report>
- **Judgments:** <carried forward from <previous_as_of> | re-affirmed on <earnings|dividend> event — what changed>
- **Invalidation check:** technical leg <level> — <did last/price cross it? state plainly: triggered / intact>; fundamental leg <metric> <threshold> — <triggered / intact>.
- **Next review:** <day after the next binary event from snapshot.events, YYYY-MM-DD>

_Sourced from bundle module JSONs · delta vs <previous_bundle>._
```

For the invalidation check: compare the NEW snapshot's price against the previous plan's invalidation levels (`module_tradeplan.json` invalidation legs from the previous bundle). If a leg was breached, **say so plainly** — a triggered invalidation is the single most important thing a refresh can surface.

---

## Ratification flow (`ratify <name>@<version>`)

When the user types **`ratify <name>@<version>`** (in response to a surfaced pending proposal), move the proposal into the active scales — **forward-only, verify first, never recompute the past**:

1. **Verify the proposal exists** — `<WORKROOT>/trading_desk_config/scales/proposals/<name>_<version>.json` is present. Absent → tell the user (naming what proposals ARE pending) and stop.
2. **Verify it is ratifiable** — the proposal's `status` is `pending_ratification` AND its `votes[]` show **≥2 non-refutations** (`NOT_REFUTED`, the scale-review adversarial gate's survival bar). If either check fails, refuse and say why — a proposal that did not survive the gate is not ratifiable.
3. **Archive the current scale (if any)** — if `<WORKROOT>/trading_desk_config/scales/<name>.json` already exists, move it to `<WORKROOT>/trading_desk_config/scales/history/<name>_<old_version>.json` (keyed by its OWN `version`). History is retained, never deleted.
4. **Promote the proposal** — move `<WORKROOT>/trading_desk_config/scales/proposals/<name>_<version>.json` → `<WORKROOT>/trading_desk_config/scales/<name>.json`, **removing the `status` field** and setting `prior` to the old version (the Bayesian anchor the new parameters moved from). The proposal file leaves `proposals/`.
5. **Confirm to the user** — `Ratified <name>@<version>` plus a one-line summary of **what changed** (the parameter/band delta vs the prior), so the tuning is never invisible.

**Forward-only, always:** NEVER edit a history file, NEVER re-run or re-score a past bundle against the newly-ratified scale. The new scale governs the NEXT fundamental score; prior reports stand as rendered under the scale that was active when they ran (their footers name it). Ratification is the ONLY path from proposal to active — a refresh never auto-applies a re-base.

---

## Output contract

Report to the user:
- **Refresh plan summary** — refetched vs reused counts, judgment-review status, estimated calls (+ IV note if refreshed).
- **Both report paths** — `<TICKER>_Trade_Report_<as_of>.md` and `<TICKER>_Delta_Report_<as_of>.md`, plus (when the render venv is present) the **docket PDFs** `<TICKER>_Trade_Report_<as_of>.pdf` / `<TICKER>_Detail_<as_of>.pdf` / `<TICKER>_Delta_Note_<as_of>.pdf`; if the venv is absent, state the docket was skipped (md-only) with the one-line bootstrap.
- **QC attestations** — the gate verdicts (snapshot QC + both report QCs + the pdf-slots gate when the docket rendered).
- **One-line "what changed" verdict** — grade old→new + the single biggest driver (and any triggered invalidation leg, called out first).

---

## Important Notes

- **Single-snapshot rule.** All modules re-emit against ONE new snapshot; the plan controls fetching only, never scoring. A group marked `reuse` still feeds a fully re-run module.
- **Carried-forward judgments are disclosed; an event forces re-affirmation.** No event → flags carry with a `[carried forward from <date>]` tag. Earnings/dividend between runs → re-derive honestly, state what changed. Never blind-copy across an event.
- **Coverage freshness + context refresh (coverage-first).** A new reported quarter since the coverage model → run FSI `model-update` on `coverage/` before rescoring (noted in the plan), appending a `model-update` entry to `coverage_manifest.json`; then **re-run `coverage_qc.py` in the manifest's recorded `depth_mode`** (`--mode full|shallow`) so the updated coverage still clears its depth floor. A refresh never re-initiates and never re-decides depth — the full-depth gate is an initiation contract; the refresh only re-verifies the recorded mode. The context module's `live_tape` is ALWAYS re-authored from fresh news; `business` / `competitive` / `cases` carry forward tagged `[carried forward from <date>]` unless an event forces re-affirmation; finding IDs stay stable; the `--context` gate re-runs with `--previous`.
- **Reused files keep their original retrieval timestamps.** Provenance is honest; the staleness windows (shared with the QC gate) make an in-window reuse legal. The planner never authorizes a reuse the gate would reject.
- **Append-only.** A refresh writes a new dated bundle and appends to `thesis_entry.md`; it NEVER edits the previous bundle. The delta report is your audit trail between runs.
- **Free-tier friendly.** A no-event refresh is ~6-8 calls (the fast-moving market surface only) — it fits the anonymous ~25-call/day quota, so a same-day refresh is affordable where a full run might not be. An event-driven refresh adds the statement set (~8 more); an IV refresh adds ~26 (skip on any degraded tier).
- **No previous bundle → stop.** `refresh_plan.py` exits 2 with "nothing to refresh — run a full analysis first." A refresh presupposes a prior full run; don't fabricate one.
- **Educational only.** This is analysis, not investment advice. Verify every figure independently before acting.
