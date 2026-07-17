---
name: report-renderer
description: Render the final 3-page trade decision report (or a delta report) from a completed bundle. `scripts/render_report.py` generates the ENTIRE report skeleton — every table, header, and number — from the bundle's module JSONs; you fill ONLY the marked `<!-- SLOT:... -->` prose slots, citing numbers that already appear in the scripted tables. `scripts/report_qc.py` then verifies the finished document numerically against the bundle (blocking §12 gate). Use when the user says "render report [bundle]", "trade decision report", "delta report", or when full-trade-analysis needs the final output. A report can never ship with a number that is not in the bundle.
---

# Report Renderer (Output Layer — the 3-Page Report)

Turn a completed bundle into the final **3-page trade decision report**. **Every number is script-written** by `scripts/render_report.py` from the module JSONs — you never type a level, a strike, an EV, a score, or a percent into the report. Your only job is to fill the prose slots the script leaves for you, then run the blocking QC gate until it is green.

This is the **L4 output layer**. It consumes the entire bundle (snapshot + `module_{technical,risk,sentiment,fundamental,composite,tradeplan,options}.json`) and emits `<TICKER>_Trade_Report_<date>.md`.

**Output location:** if the bundle directory's basename starts with `detail_reports` (the `trading_desk_<T>/detail_reports_<date>/` layout), the report is written to the bundle's **parent** directory (a sibling of the data folder); otherwise it is written **inside** the bundle (legacy layout). The exact path is always printed to stdout — use that path for QC. `--out` overrides.

**Why this architecture (kills number leakage BY CONSTRUCTION):** the renderer writes the whole skeleton — every table, header, and figure — from the bundle. LLM prose goes ONLY into `<!-- SLOT:... -->` marks. `report_qc.py` then extracts every numeric token from the finished document and checks it against the bundle's numeric values. A number you invent in a slot has no bundle source and **fails the gate**.

Trigger phrases: "render report for MU", "trade decision report AAPL", "delta report vs last week".

---

## Step 1 — Verify bundle completeness

In the invoker's CWD, find the newest bundle for the ticker:

```bash
ls -dt ./trading_desk_<TICKER>/detail_reports_* ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

Newest first across both layouts: the new `./trading_desk_<TICKER>/detail_reports_<date>/` bundles and the legacy `./td_bundle_<TICKER>_<date>/` bundles (fallback for old runs).

A **full report requires all seven module files plus a snapshot**: `module_technical`, `module_risk`, `module_sentiment`, `module_fundamental`, `module_composite`, `module_tradeplan`, `module_options`. If any is missing, the renderer exits 2 naming it — run the missing upstream skill first (composite-score runs the four evidence skills; trade-plan runs composite then options-strategy; then synthesize). Renormalized absences *inside* a module are fine; the **files** must exist.

---

## Step 2 — Render the skeleton

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_report.py --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>
```

The script writes `<TICKER>_Trade_Report_<date>.md` (exact path — bundle or parent per the output-location rule above — printed to stdout). It contains three pages, all tables and numbers already filled from the bundle, with empty `<!-- SLOT:name -->` marks for your prose:

- **Page 1 — Decision:** header block, the call (`grade — action`, composite score), composite table (+ sensitivity), trade-plan table (entries/exits/invalidation/size/hedge/expression), event-playbook skeleton.
- **Page 2 — Evidence:** per dimension a scripted score headline + a mini-table (ladder / subscores / positioning / downside map / EV scenarios).
- **Page 3 — Context & Protocol:** full S/R ladder + downside map, catalyst calendar, scenario & EV table, options expression block (vol verdict, structures, declined, hedge, matrix), monitoring protocol, data-integrity footer, disclaimer.

---

## Step 3 — Fill every slot with prose (cite ONLY printed numbers)

Read the rendered report. Replace each `<!-- SLOT:name -->` with prose. **The slot-fill rule: no new numbers.** Every figure you mention must already be printed in a scripted table on that page — QC will catch any number that is not in the bundle. If a *table* is wrong, that is a module/bundle bug — fix the module and re-render; never edit a scripted number in the report.

Word budgets (kept tight — the whole report has a 2100-word cap):

| Slot | Budget | Content |
|------|--------|---------|
| `tension` | 1 sentence | the one real tension in the call (e.g. "constructive score, but the print is a coin-flip and IV is cheap") |
| `event_playbook` | 3 bullets | beat / inline / miss → the pre-committed action for each, vs the printed implied move |
| `brief_<dim>` (×5) | ≤120 words each | condense the bundle's existing `brief_<dim>.md` for technical/fundamental/sentiment/risk/thesis — reuse that content, do not re-derive |
| `signal_<dim>` (×5) | 1 line each | the single takeaway signal for the dimension |
| `catalyst_notes` | 1-2 lines | context on the scheduled catalysts |
| `monitoring_notes` | 1-2 lines | what would change the call between now and the next review |

Reuse the bundle's `brief_<dim>.md` files (they already cite only in-bundle numbers) — condense, don't rewrite from scratch.

---

## Step 4 — Run the blocking §12 QC gate

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD> \
  --report <path printed by render_report.py, e.g. ./<TICKER>_Trade_Report_<date>.md>
```

The gate prints a check table and exits 0 (pass) or 1 (fail). The checks: **number_provenance** (every report number traces to the bundle), composite_arithmetic, ev_consistency, invalidation_both_legs, sizing_within_cap, strikes_in_chain, pop_method_labeled, expression_consistency, footer_integrity, word_cap (≤2100), **no_empty_slots**.

**Fix loop — fix PROSE, never numbers:**
- `no_empty_slots` fail → you left a slot unfilled. Fill it.
- `number_provenance` fail (orphan number) → you typed a number a bundle table does not carry. Remove it or rephrase to the printed figure. **Never** invent a number to satisfy prose.
- `word_cap` fail → a slot is too long. Tighten.
- A **table**-driven check fails (composite_arithmetic / ev_consistency / sizing / strikes / pop_method) → this is a bundle/module bug, not a prose bug. Fix the module and **re-render** (Step 2), then re-fill and re-run.

Re-run until exit 0. Then print the QC verdict and the report path to the user.

---

## Delta mode

When the user wants a change-report vs a prior bundle:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_report.py --bundle ./<new_bundle> --delta --previous ./<old_bundle>
```

Both bundles need `module_composite`. Output: `<TICKER>_Delta_Report_<date>.md` — written to the **same location rule as the full report** (the bundle's parent under the `detail_reports_<date>/` layout, inside the bundle for legacy) and printed to stdout. It carries a composite delta table (old/new/Δ, grade change bolded), EV delta, level changes, structures added/removed, and a `delta_interpretation` slot. Fill that one slot, then QC the delta (auto-detected by filename — it runs checks number_provenance / footer_integrity / no_empty_slots only). **Pass `--previous` to the QC too** so the Δ columns (which are script-computed differences, not bundle leaves) and the old-value columns are recognized as in-bundle:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py --bundle ./<new_bundle> \
  --report <path printed by render_report.py --delta> --previous ./<old_bundle>
```

A module absent in either bundle → that section reads "n/a (module absent in {which})".

---

## Step 5 — Optional docx conversion

If the **financial-analysis docx skill** is available, offer to convert the passed `.md` to `.docx` (Times New Roman, initiation-report conventions). If it is not available, note that the report is markdown-only and disclose that in one line — do not fabricate a docx path.

---

## Important Notes

- **Slot-fill rule (no new numbers).** Every number in the report is script-owned. Prose cites only numbers already printed in the scripted tables. QC's number_provenance check will catch any orphan — including a number that is "obviously right" but never made it into the bundle.
- **The §12 gate is blocking.** A report that fails report_qc does not ship. Exit 0 is the ship criterion.
- **Fix tables in the module, not the report.** If a scripted figure is wrong, the fix is upstream (re-run the module, re-render) — editing a number in the `.md` would pass a wrong figure past the gate on the next run and defeats the whole architecture.
- **Waivers are disclosed, not silent.** A genuinely justified failure can be waived (`--waive "check:reason"`, same mechanics as the snapshot gate) — the report table then shows WAIVED with the reason. Use this only for a real, disclosed exception, never to hide a fabricated number.
- **Word cap ~2100.** The three pages together must stay under the cap; the briefs are the main lever — condense the bundle briefs rather than expanding them.
- **Read-only over the bundle.** This skill writes only the report `.md`; it never edits the snapshot or any module JSON.
