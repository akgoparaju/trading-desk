---
name: report-renderer
description: Render the final 3-page trade decision report (or a delta report) from a completed bundle. `scripts/render_report.py` generates the ENTIRE report skeleton — every table, header, and number — from the bundle's module JSONs; you fill ONLY the marked `<!-- SLOT:... -->` prose slots, citing numbers that already appear in the scripted tables. `scripts/report_qc.py` then verifies the finished document numerically against the bundle (blocking §12 gate). Use when the user says "render report [bundle]", "trade decision report", "delta report", or when full-trade-analysis needs the final output. A report can never ship with a number that is not in the bundle.
---

# Report Renderer (Output Layer — the 3-Page Report)

Turn a completed bundle into the final **3-page trade decision report**. **Every number is script-written** by `scripts/render_report.py` from the module JSONs — you never type a level, a strike, an EV, a score, or a percent into the report. Your only job is to fill the prose slots the script leaves for you, then run the blocking QC gate until it is green.

This is the **L4 output layer**. It consumes the entire bundle (snapshot + `module_{technical,risk,sentiment,fundamental,composite,tradeplan,options}.json`) and emits `<TICKER>_Trade_Report_<date>.md`. After the md QC gate passes it OPTIONALLY renders the **docket** — the exec/detail (and, when a prior bundle resolves, delta) PDFs — when the render venv is present; if it is not, the report ships md-only and the degradation is disclosed (Step 5).

**Output location:** if the bundle directory's basename starts with `detail_reports` (the `trading_desk_<T>/detail_reports_<date>/` layout), the report is written to the bundle's **parent** directory (a sibling of the data folder); otherwise it is written **inside** the bundle (legacy layout). The exact path is always printed to stdout — use that path for QC. `--out` overrides. **Do not pass `--out`** — the resume check and the QC `--report` both assume the default location.

**Workspace root (`--output-dir`) + prior workspace (`--prev-dir`).** This skill accepts an optional **`--output-dir <ABS_DIR>`** and, for a delta note, an optional **`--prev-dir <ABS_DIR>`** (e.g. `report-renderer PLTR --output-dir /abs/workspace --prev-dir /abs/prior`). Resolve them FIRST:

- **`--output-dir <ABS_DIR>` given** → `WORKROOT = <ABS_DIR>` (MUST be absolute) and — **FLAT under `--output-dir` (v1.2.0)** — `TICKER_WS = <WORKROOT>`: the ticker workspace IS `<WORKROOT>`, so drop the `trading_desk_<TICKER>/` segment (the caller passes a per-ticker dir). All I/O is rooted here, decoupled from the process CWD.
- **`--output-dir` absent** → `WORKROOT = .` and `TICKER_WS = ./trading_desk_<TICKER>` (the human/CWD layout — byte-for-byte unchanged).
- **`--prev-dir <ABS_DIR>`** (delta only) → the PRIOR **workspace root**; `<PREV_BUNDLE>` = the newest `detail_reports_*` under it. **Tolerant:** if the given path's own basename starts with `detail_reports`, it IS `<PREV_BUNDLE>` — a caller who handed you a bundle instead of a root is right, not wrong. **`--prev-dir` is READ-ONLY**; never write into it.
- **`--prev-dir` absent** → resolve a prior ONLY from bundles sitting BESIDE `<BUNDLE>`: `ls -dt <TICKER_WS>/detail_reports_*`, then **discard `<BUNDLE>` itself** and take the next. **`<BUNDLE>` can never be `<PREV_BUNDLE>`** — unlike refresh-analysis, the bundle already exists when this skill runs, so "newest under the workspace" IS `<BUNDLE>`, and no script rejects `--previous <BUNDLE>`: it renders an all-zero Delta Note that reads as a real one. Nothing beside it ⇒ **no prior resolved**: `delta_interpretation` is `null`, no `--previous` anywhere, two-PDF docket, PASS.
- **`--fresh-skeleton`** (no value) → re-render the skeleton even when a report already exists, **discarding its authored prose**. Never take this branch on your own initiative — see Step 2. **Set in prose too** — "fresh skeleton", "re-render from scratch", "discard the draft and re-render" all mean `--fresh-skeleton`. Nothing you infer from a QC failure ever sets it.

**One name per layer, translated exactly once.** The SKILL boundary takes `--prev-dir <workspace root>`; the SCRIPT boundary takes `--previous <bundle dir>`. Resolve `--prev-dir` → `<PREV_BUNDLE>` here, once, then pass the **bundle** to every `render_report.py --previous`, `report_qc.py --previous`, and `render_pdf.py --previous` below.

**Fan it out.** Give every `python3 scripts/…` path argument (`--bundle`, `--report`, `--previous`, `--pdf-slots`) as an **absolute path** built from `<BUNDLE>` / `<TICKER_WS>`. The scripts then never fall back to the CWD: `render_report.py` writes the report to the bundle's parent per the output-location rule above, and config/scale discovery walks up from the absolute bundle path.

**Why this architecture (kills number leakage BY CONSTRUCTION):** the renderer writes the whole skeleton — every table, header, and figure — from the bundle. LLM prose goes ONLY into `<!-- SLOT:... -->` marks. `report_qc.py` then extracts every numeric token from the finished document and checks it against the bundle's numeric values. A number you invent in a slot has no bundle source and **fails the gate**.

Trigger phrases: "render report for MU", "trade decision report AAPL", "delta report vs last week".

---

## Step 1 — Locate the bundle, then verify completeness

**Discovery is ORDERED, and its result is TERMINAL.** Take the first branch that matches, then stop looking:

**(a) `--output-dir` given — flat (v1.2.0), the programmatic caller's layout:**
```bash
ls -dt <WORKROOT>/detail_reports_* 2>/dev/null | head -1
```

**(b) ONLY if (a) matched nothing — a pre-v1.2.0 nested workspace, or a legacy bundle, sitting under the given root:**
```bash
ls -dt <WORKROOT>/trading_desk_<TICKER>/detail_reports_* <WORKROOT>/td_bundle_<TICKER>_* 2>/dev/null | head -1
```
Merging the nested and legacy globs here is fine — unlike (a) vs (b), these two are both pre-v1.2.0 layouts with no authority relationship between them, so newest-wins is correct.

**(c) `--output-dir` absent — the invoker's CWD (unchanged):**
```bash
ls -dt ./trading_desk_<TICKER>/detail_reports_* ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

The path the winning branch printed is `<BUNDLE>` for the rest of this document.

Keep (a) and (b) as SEPARATE commands in that order — **never merge them into one `ls -dt`**. A merged glob sorts by mtime across layouts, so a stale legacy sibling can outrank the flat bundle the caller meant and you would silently render the wrong bundle. Flat is authoritative; (b) is last resort.

**ANNOUNCE which branch resolved**, with the path: `bundle: <BUNDLE> (discovery: flat under --output-dir | legacy nested fallback under --output-dir | CWD)` — pick ONE parenthetical, do not print the menu. A fallback that fires silently is indistinguishable from one that never fired, and on a recovery path the operator needs to know which bundle was actually rendered.

**If nothing matched, do NOT assert absence.** Name only the globs THIS run actually searched. With `--output-dir`: `no bundle found at <WORKROOT> (searched detail_reports_*, then trading_desk_<TICKER>/detail_reports_* and td_bundle_<TICKER>_*)`. Without it (CWD mode): `no bundle found in CWD (searched trading_desk_<TICKER>/detail_reports_* and td_bundle_<TICKER>_*)`. A denied `readdir` returns EMPTY rather than erroring, so `ls` cannot tell "nothing here" from "I cannot read this directory". When `<WORKROOT>` is outside the home volume, add: verify readability with a **real byte-read** (e.g. `head -c 1 <WORKROOT>/trading_desk_config.json`) — **`stat` succeeds against a dead volume** and proves nothing — if that file is legitimately absent, byte-read any regular file under the root (`find <WORKROOT> -maxdepth 2 -type f | head -1`). Distinguish `No such file` (root readable, really empty) from `Input/output error` / `Permission denied` (root unreadable — absence unproven).

**Completeness is a SEPARATE, LATER gate — never a reason to resume discovery.** Once a bundle resolves, that IS the bundle. A **full report requires all seven module files plus a snapshot**: `module_technical`, `module_risk`, `module_sentiment`, `module_fundamental`, `module_composite`, `module_tradeplan`, `module_options`. If any is missing, the renderer exits 2 naming it — report that and **STOP**. **Never fall back to a different bundle because this one is incomplete**; run the missing upstream skill first (composite-score runs the four evidence skills; trade-plan runs composite then options-strategy; then synthesize). Renormalized absences *inside* a module are fine; the **files** must exist.

---

## Step 2 — Render the skeleton (SKIPPED when a report already exists)

**Check for an existing report FIRST. Bind its date from the BUNDLE, never from today.** `<DATE>` = the `<date>` in the resolved `<BUNDLE>`'s `detail_reports_<date>` basename; for a legacy `td_bundle_*` bundle, `<DATE>` = the bundle's `snapshot.meta.as_of_utc[:10]`. `render_report.py` names the report from that same field, so **any other date is a miss against a file that exists**.

`<REPORT> = <REPORT_DIR>/<TICKER>_Trade_Report_<DATE>.md`, where `<REPORT_DIR>` = `<TICKER_WS>` when `<BUNDLE>`'s basename starts with `detail_reports`, else `<BUNDLE>` itself (the legacy layout puts the report inside the bundle).

**Confirm by listing, not by constructing:** `ls -1 <REPORT_DIR>/<TICKER>_Trade_Report_*.md 2>/dev/null`. The one matching `<DATE>` is yours to resume. A report at any **other** date belongs to a different bundle — never resume it, never overwrite it.

- **`<REPORT>` exists → SKIP the render.** Announce it loudly: `resumed existing report at <REPORT>; skeleton not re-rendered.` Go straight to Step 3 (fill any open slots) and Step 4 (QC). **Do not write into the bundle at this step** — in particular `module_decision.json` stays exactly as the original render left it. (Step 5 still writes `pdf_slots.json` and `charts/` normally; the prohibition is on re-rendering, not on the docket.) This is the recovery entry point — a bundle can be complete and QC-passing while the report is a trim away from shipping, and re-rendering would discard every authored word and rewrite `module_decision.json` for nothing.

  Print the budget for the report you resumed:
  ```bash
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/word_budget.py --report <REPORT>
  ```
  On a resumed report the block's `skeleton` figure is the **full current** word count, so `room` is literally the trim you owe (negative = over cap). Read it before you touch a word.
- **`<REPORT>` absent → render it:**

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_report.py --bundle <BUNDLE>
```

- **`--fresh-skeleton` given → render even though `<REPORT>` exists**, discarding the authored prose. State plainly that you are doing so and what is being discarded. **Never choose this branch on your own initiative**: throwing away authored analysis is an operator's call.

A fresh render also (re)writes `<BUNDLE>/module_decision.json` — that is Step 2's job and is expected here. It must not happen on the resume path, which is exactly why the resume path does not render.

The script writes `<TICKER>_Trade_Report_<date>.md` (exact path — bundle or parent per the output-location rule above — printed to stdout as the LAST line) and, before it, a **WORD BUDGET** block: the skeleton's own word count, the room left under the cap, and a per-slot budget for every open slot. **Read it before you author** — it is the only place the budget appears before the prose is written. `!! OVER-SUBSCRIBED` means writing to budget still fails the cap and the fix is upstream, not a tighter slot. `NOTE n brief slot(s) OPEN` means the `brief_<dim>.md` transclusion did not happen, so ~150 words per open brief that normally arrive for free must be authored here.

The report contains three pages, all tables and numbers already filled from the bundle, with empty `<!-- SLOT:name -->` marks for your prose:

- **Page 1 — Decision:** header block, the call (`grade — action`, composite score), composite table (+ sensitivity), trade-plan table (entries/exits/invalidation/size/hedge/expression), event-playbook skeleton.
- **Page 2 — Evidence:** per dimension a scripted score headline + a mini-table (ladder / subscores / positioning / downside map / EV scenarios).
- **Page 3 — Context & Protocol:** full S/R ladder + downside map, catalyst calendar, scenario & EV table, options expression block (vol verdict, structures, declined, hedge, matrix), monitoring protocol, data-integrity footer, disclaimer.

---

## Step 3 — Fill every slot with prose (cite ONLY printed numbers)

Read the rendered report. Replace each `<!-- SLOT:name -->` with prose. **The slot-fill rule: no new numbers.** Every figure you mention must already be printed in a scripted table on that page — QC will catch any number that is not in the bundle. If a *table* is wrong, that is a module/bundle bug — fix the module and re-render; never edit a scripted number in the report.

Word budgets (the whole report has a 2100-word cap, **prose and headings only** — pipe-table rows and the `### Data Integrity` section are excluded by `report_qc`, so the budget is entirely yours to spend on prose. **Target ≤1,000 words of authored prose across all slots; a small overage is acceptable — the gate's margin handles it.**): **Step 2 printed the real numbers for THIS bundle** (on a resume, the `word_budget.py --report` block above) — the per-slot budgets, the room actually left, and any warning that the budgets over-subscribe it. Write to those, not to the abstract targets below.

| Slot | Budget | Content |
|------|--------|---------|
| `tension` | 1 sentence | the one real tension in the call (e.g. "constructive score, but the print is a coin-flip and IV is cheap") |
| `event_playbook` | 3 bullets | beat / inline / miss → the pre-committed action for each, vs the printed implied move |
| `brief_<dim>` (×5) | ≤150 words each (aim ~120) | **transcluded verbatim** by `render_report.py` from `<BUNDLE>/brief_<dim>.md` (the `<!-- BRIEF:START -->`…`<!-- BRIEF:END -->` span) — you do NOT re-condense; the word cap is enforced upstream at module-brief authoring time. If the file or markers are absent the slot mark is left open; fill it manually as before. |
| `signal_<dim>` (×5) | 1 line each | **transcluded verbatim** by `render_report.py` from the `<!-- SIGNAL:START -->`…`<!-- SIGNAL:END -->` span in `brief_<dim>.md`. If absent, fill manually. |
| `catalyst_notes` | 1-2 lines | context on the scheduled catalysts |
| `monitoring_notes` | 1-2 lines | what would change the call between now and the next review |

The `brief_<dim>` and `signal_<dim>` slots are now deterministically transcluded from the bundle's `brief_<dim>.md` files when those files carry the required delimiters (`<!-- BRIEF:START -->`/`<!-- BRIEF:END -->` and `<!-- SIGNAL:START -->`/`<!-- SIGNAL:END -->`). The render-time LLM no longer re-condenses them — cite in-bundle numbers only when filling any remaining open slots.

---

## Step 4 — Run the blocking §12 QC gate

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py --bundle <BUNDLE> \
  --report <REPORT>
```

The gate prints a check table and exits 0 (pass) or 1 (fail). The checks: **number_provenance** (every report number traces to the bundle), composite_arithmetic, ev_consistency, invalidation_both_legs, sizing_within_cap, exit_ordering (QC6: profit_take must never sit at/above bull_target), strikes_in_chain, pop_method_labeled, expression_consistency, footer_integrity, **footer_completeness** (every `snapshot.meta.api_tier_notes` entry appears in the footer), word_cap (≤2100, prose + headings only), **no_empty_slots**.

**Fix loop — fix PROSE, never numbers:**
- `no_empty_slots` fail → you left a slot unfilled. Fill it.
- `number_provenance` fail (orphan number) → you typed a number a bundle table does not carry. Remove it or rephrase to the printed figure. **Never** invent a number to satisfy prose.
- `word_cap` fail → a slot is too long. Tighten (the count is prose + headings only — no table row or the Data Integrity section is inflating it, so an overage is yours to fix).
- `footer_completeness` fail → you (or a prior edit) dropped an `api_tier_notes` entry from the footer. **RESTORE** the deleted note(s) verbatim. **Never** condense or paraphrase the `### Data Integrity` section — it is mandated disclosure, and since it no longer counts against the word cap there is no reason to touch it.
- A **table**-driven check fails (composite_arithmetic / ev_consistency / sizing / strikes / pop_method) → this is a bundle/module bug, not a prose bug. Fix the module and **re-render** (Step 2), then re-fill and re-run.
- **On a RESUMED report, a numeric/structural failure means DRIFT.** If `number_provenance`, `composite_arithmetic`, `ev_consistency`, `invalidation_both_legs`, `sizing_within_cap`, `exit_ordering`, `strikes_in_chain`, `pop_method_labeled` or `expression_consistency` fails on a report you **resumed** rather than rendered, the report no longer matches the bundle. **STOP and report it, naming the remedy:** re-run with `--fresh-skeleton` to discard the prose and render a current skeleton. **Never auto-re-render** — that silently destroys authored analysis, and the choice belongs to the operator.
- **`word_cap` and `no_empty_slots` are NOT drift.** An over-cap resumed report gets trimmed; one with open slots gets them filled. Neither is a reason to re-render.

Re-run until exit 0. Then print the QC verdict and the report path to the user.

---

## Step 4b — Decision-contract gates (BLOCKING)

The consolidated `module_decision.json` (contract **v2.0.0**) that `render_report.py` wrote is the machine-consumable capital instruction a downstream Portfolio-OS binds to, so it is gated separately — every number must trace to the bundle and the shape must be schema-valid:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py \
  --bundle <BUNDLE> --decision-gates
```

Three blocking checks: **schema_version_present** (every scorer + decision module carries a top-level `schema_version`), **decision_subset_of_bundle** (every non-derived numeric leaf in `module_decision.json` equals a bundle value — nothing fabricated), **decision_schema_valid** (validates against `docs/decision.schema.json`). Exit 0 to proceed. A failure is an upstream module/emitter bug — a missing `schema_version` stamp, or a decision leaf with no bundle source — **fix the module and re-render; never hand-edit `module_decision.json`**.

**On a RESUMED report this is DRIFT, not an emitter bug.** A `decision_subset_of_bundle` or `schema_version_present` failure against a `module_decision.json` the original render wrote means the bundle moved underneath it. **STOP and name the remedy (`--fresh-skeleton`)** — never re-render to refresh the contract, for the same reason Step 4 forbids it.

---

**When `<PREV_BUNDLE>` resolved, render and QC the md delta report (Delta mode, below) BEFORE Step 5** — `delta_interpretation` then cites figures the delta report actually prints. With no prior, skip that section entirely.

## Step 5 — Docket (PDF) rendering (AFTER the md QC gate passes)

Once the **md report QC gate is green**, render the institutional **docket** — three deterministic PDFs (`exec` 2pp, `detail` ~10-15pp, and, when a prior bundle exists, a `delta` note). The md report remains the source of truth; the docket is a bank-note-styled render of the SAME QC'd bundle. **Every number on the page is script-minted** (from the module JSONs, the deterministic chart pack, or the What-Changed diff); the only LLM content is the prose in `pdf_slots.json`, and that is provenance-gated before it reaches the renderer.

**(a) Check the render venv (never blocks).**
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_env.py --check
```
Exit 0 prints `READY <venv-python>` — capture that interpreter path for steps (b) and (e). **Exit 3** = the matplotlib+reportlab venv is not built: announce **md-only** ("docket skipped — render venv not built"), give the one-line bootstrap instruction `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_env.py` (one-time ~30s build), and **SKIP the rest of Step 5**. Degradation is disclosed, never a hard stop.

**(b) Render the deterministic chart pack** (use the venv python from step (a)):
```bash
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_charts.py \
  --bundle <BUNDLE> --set all
```
Writes the PNGs + `charts/charts_manifest.json`. A chart with a missing input is SKIPPED (recorded with a reason) — the renderer simply omits it; you fabricate nothing.

**(c) Author `<BUNDLE>/pdf_slots.json`** — the ONLY LLM content in the docket. Shape (per `render_pdf.py`):
```json
{
  "thesis_bullets": ["Lead — rest", "Lead — rest", "Lead — rest"],
  "desk_read": {"setup": "…", "edge": "…", "trigger": "…", "risk": "…"},
  "positioning": {"entry_discipline": "…", "sizing_kelly": "…",
                  "path_dependency": "…", "monitoring": "…"},
  "delta_interpretation": null
}
```
- `thesis_bullets` — exactly **3**, each in the **"Lead — rest"** bold-lead form (em-dash separator).
- `desk_read` — the four keys `setup / edge / trigger / risk`.
- `positioning` — the four keys `entry_discipline / sizing_kelly / path_dependency / monitoring`.
- `delta_interpretation` — **REQUIRED (1-2 sentences) whenever a prior bundle resolved** (`--prev-dir` given, or a prior sits beside the bundle): what drove the composite / EV / level moves, citing ONLY numbers in the delta report or the module JSONs. **`null` when there is no prior** — then the docket is exec + detail only, which is a normal two-PDF pass, not a degradation to disclose.
- **Prose rules:** cite ONLY numbers that already appear in the gated md report or the module JSONs; **≤2 sentences per field**. This is the same number-provenance discipline as the md slots — a number with no bundle source fails the slots gate.

**(d) Run the BLOCKING slots provenance gate** (stamps `qc_passed=true` INTO the file on pass; `render_pdf` refuses exec/detail without that stamp):
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py \
  --bundle <BUNDLE> --pdf-slots <BUNDLE>/pdf_slots.json [--previous <PREV_BUNDLE>]
```
Exit 0 = pass (stamp written). On fail (orphan number), **fix the PROSE, never the numbers** — rephrase to a figure the bundle carries, exactly as with the md gate.

Pass **`--previous <PREV_BUNDLE>`** whenever a prior bundle resolved — it admits the Δ values `delta_interpretation` cites into the allowed provenance set. Without it, legitimate delta numbers orphan.

**(e) Render the PDFs** (venv python from step (a)). When a prior bundle resolved, render `delta` FIRST — it REQUIRES `--previous <PREV_BUNDLE>`; then `exec`, which also takes `--previous <PREV_BUNDLE>` to show the **What-Changed** box; then `detail`:
```bash
# delta FIRST when a prior resolved -- it REQUIRES --previous
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py \
  --bundle <BUNDLE> --doc delta --previous <PREV_BUNDLE>
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py \
  --bundle <BUNDLE> --doc exec [--previous <PREV_BUNDLE>]
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py \
  --bundle <BUNDLE> --doc detail
```
The PDFs land in the **ticker parent** (same location rule as the md report): `<TICKER>_Trade_Report_<date>.pdf` (exec), `<TICKER>_Detail_<date>.pdf` (detail), and `<TICKER>_Delta_Note_<date>.pdf` (delta), path printed to stdout. **The delta note is conditional on a resolvable prior bundle** — with no prior, a two-PDF docket is a complete pass. `render_pdf` exits 3 with a bootstrap line if the venv disappeared between steps — treat that exactly like the (a) exit-3 md-only fallback.

**METHODOLOGY appendix (fully scripted — nothing to author).** Every `--doc detail` PDF ends with a `METHODOLOGY` appendix page: rubric versions, the composite weight table actually used (with the standard column shown for comparison when a CUSTOM weight set applies), the fundamental valuation formula set (anchored vs snapshot component maxima + the DCF-vs-comps disagreement rule + the display-only PEG line), the active sector scale (name/version/basis/formula/parameters/evidence/falsifiers/prior — or "No sector scale active"), the EV-hurdle/grade-band/horizon/judgment-flag conventions, and the governance rules. It is rendered **100% from the module JSONs + the active scale JSON** — the convention constants are imported from the scorers, so it can never drift. **There is NO slot for it and nothing for you to write** — do not attempt to author or edit the methodology page; `pdf_slots.json` authoring is **unchanged** (the four prose blocks above), because the methodology page is out of slots scope by construction.

**Stamps & banners (also fully scripted).** The footer of every docket page carries the weight-set stamp (`Weights: standard v1` or `Weights: CUSTOM <set>@<ver>`) and, when an anchored valuation used a sector scale, the scale stamp (`Scale: <name>@<version>`); a CUSTOM run also tags the grade box. On a refresh, if the bundle's `refresh_plan.json` reports `scale_review_required` (a falsifier tripped) a one-line accent banner appears on Detail p1 and the Delta note pointing to the methodology page, and any `pending_proposals` surface a neutral banner. The Delta note's What-Changed table gains a weight-set / sector-scale transition row when either stamp changed between runs. All of this is sourced from the module/plan JSONs — **you author none of it.**

---

## Delta mode

When the user wants a change-report vs a prior bundle. **Unlike Step 2, there is no resume check here** — the delta report is always re-rendered, never resumed: it carries exactly one authored slot (`delta_interpretation`), so re-authoring it is cheap, and re-rendering guarantees the Δ figures match the current bundle instead of risking the same staleness Step 2 exists to prevent for the much larger full report.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_report.py --bundle <BUNDLE> --delta --previous <PREV_BUNDLE>
```

Both bundles need `module_composite`. Output: `<TICKER>_Delta_Report_<date>.md` — written to the **same location rule as the full report** (the bundle's parent under the `detail_reports_<date>/` layout, inside the bundle for legacy) and printed to stdout. It carries a composite delta table (old/new/Δ, grade change bolded), EV delta, level changes, structures added/removed, and a `delta_interpretation` slot. Fill that one slot, then QC the delta (auto-detected by filename — it runs checks number_provenance / footer_integrity / footer_completeness / no_empty_slots only; the delta renders the same Data-Integrity footer, so the completeness check applies unchanged). **Pass `--previous` to the QC too** so the Δ columns (which are script-computed differences, not bundle leaves) and the old-value columns are recognized as in-bundle:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py --bundle <BUNDLE> \
  --report <path printed by render_report.py --delta> --previous <PREV_BUNDLE>
```

A module absent in either bundle → that section reads "n/a (module absent in {which})".

---

## Step 6 — Optional docx conversion

If the **financial-analysis docx skill** is available, offer to convert the passed `.md` to `.docx` (Times New Roman, initiation-report conventions). If it is not available, note that the report is markdown-only and disclose that in one line — do not fabricate a docx path.

---

## Important Notes

- **Slot-fill rule (no new numbers).** Every number in the report is script-owned. Prose cites only numbers already printed in the scripted tables. QC's number_provenance check will catch any orphan — including a number that is "obviously right" but never made it into the bundle.
- **The §12 gate is blocking.** A report that fails report_qc does not ship. Exit 0 is the ship criterion.
- **Fix tables in the module, not the report.** If a scripted figure is wrong, the fix is upstream (re-run the module, re-render) — editing a number in the `.md` would pass a wrong figure past the gate on the next run and defeats the whole architecture.
- **Waivers are disclosed, not silent.** A genuinely justified failure can be waived (`--waive "check:reason"`, same mechanics as the snapshot gate) — the report table then shows WAIVED with the reason. Use this only for a real, disclosed exception, never to hide a fabricated number.
- **Word cap ~2100, prose and headings only.** The three pages together must stay under the cap, but `report_qc` counts prose and headings only — pipe-table rows and the `### Data Integrity` section are excluded, so the whole budget is the author's to spend on prose. **Target ≤1,000 words of authored prose across all slots; a small overage is acceptable — the gate's margin handles it.** The `brief_<dim>` word cap (≤150 words each, aim ~120) is enforced upstream at module-brief authoring time — each module SKILL instructs the LLM to stay within that budget when it writes `brief_<dim>.md`. The render-time LLM does not re-condense transcluded briefs; the lever is authoring discipline in the upstream module step.
- **The docket is a render of the SAME bundle; md stays the source of truth.** The PDFs (exec/detail, + delta when a prior bundle resolves) carry only script-minted numbers + gated `pdf_slots.json` prose. If the render venv is not built, `render_env.py --check` exits 3 → announce md-only with the one-line bootstrap and skip the PDF steps; **the docket never blocks the md report.**
- **The slots gate is blocking and cannot be bypassed.** `render_pdf` refuses exec/detail unless `report_qc.py --pdf-slots` stamped `qc_passed=true`. Fix slot PROSE, never numbers — the same discipline as the md gate.
- **The METHODOLOGY appendix, footer stamps, and scale banners are fully scripted — nothing to author.** The methodology page keeps every detail transparent (rubric versions, weights, valuation formulas, active scale, conventions, governance) rendered purely from the module + scale JSONs; it is **out of `pdf_slots.json` scope** — do not try to write or edit it. Slot authoring is unchanged (the four prose blocks). Weight-set / scale footer stamps and the scale-review / pending-proposal banners likewise come from the module and refresh-plan JSONs, never from prose.
- **Read-only over the bundle (except the authored artifacts).** This skill writes the report `.md` and — for the docket — `pdf_slots.json` + the `charts/` PNGs + the PDFs. It **never** edits the snapshot or any evidence module JSON. `module_decision.json` is written by `render_report.py` on a **fresh render only** (Step 2); the **resume** path never rewrites it, the snapshot, or any evidence module JSON. The docket's own authored artifacts (`<BUNDLE>/pdf_slots.json`, `<BUNDLE>/charts/`) are written on the resume path exactly as on a fresh one. `<PREV_BUNDLE>` is read-only in every mode.
- **No implicit re-analysis.** A missing module file exits 2 naming it. Never invoke an upstream skill to fill the gap — that turns a cheap recovery into an unaudited partial re-run.
