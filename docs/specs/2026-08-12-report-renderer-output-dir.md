# `report-renderer --output-dir` — the cheap recovery entry point

- **Date:** 2026-08-12
- **Requested by:** kurama / Sasuke (portfolio-OS side) — `/Volumes/OWC-2TB/dev/kurama-data/handoff/trading-desk/2026-08-12-report-renderer-output-dir.md`
- **Target release:** plugin **1.6.0** (from 1.5.1)
- **Size:** small. Path plumbing + one stdout diagnostic. No gate, score, or number moves.

---

## 1. Problem

`report-renderer` is the only L4/L5 entry point that can turn an **already-complete bundle** into a
finished report + docket without re-running the engine. It is also the only one of the three
docket-producing skills that cannot be reached by a programmatic caller, because its Step-1 discovery
is CWD-relative:

```bash
ls -dt ./trading_desk_<TICKER>/detail_reports_* ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

Both globs are relative, and both assume the nested/legacy layout. kurama's bundles are **flat under
an absolute `--output-dir`** (the v1.2.0 layout), and kurama's harness pins CWD to its own repo root,
so a `cd` cannot bridge the gap.

`--output-dir` mentions per SKILL.md at 1.5.1: `full-trade-analysis` 9, `refresh-analysis` 7,
**`report-renderer` 0**.

**Consequence:** any interruption *after* the bundle is complete costs a full engine re-run. On
2026-08-12 a PLTR refresh passed every substantive gate — snapshot QC 14/0, decision gates, context
QC, delta QC — and died during a 73-word `word_cap` trim. A complete, QC-passing bundle was written
off because the only supported way forward was to re-run everything.

**Not in scope:** the interruption itself. It is a host-level fault (Claude Code's session sandbox
intermittently losing its `/Volumes` grant, upstream issue #57540, no kernel denial recorded). No
plugin change would have saved that run. What the plugin owns is the **recovery cost**.

---

## 2. What already works — the ask is narrower than it looks

The **write** path needs nothing:

- `render_report._output_dir` — basename starts with `detail_reports` → write to the bundle's parent.
  Verified correct against the flat workspace on the failed run itself.
- `render_pdf._scale_workspace_root` and `score_composite._resolve_default_config` both **walk up**
  from an absolute `--bundle`, so config/scale discovery already resolves under a redirected root.

Every script in the chain takes an absolute `--bundle` today. **Only Step-1 discovery is CWD-bound.**
§3 is therefore a SKILL.md-prose change with no script edit.

---

## 3. Design

### Unit 1 — `skills/report-renderer/SKILL.md`: workspace-root threading

A new section, placed after the "Output location" paragraph, mirroring `refresh-analysis`'s structure
so all three skills read the same way.

**Resolution (do this FIRST):**

| Flag | `WORKROOT` | `TICKER_WS` |
|---|---|---|
| `--output-dir <ABS_DIR>` given | `<ABS_DIR>` (MUST be absolute) | `<WORKROOT>` — **flat**, v1.2.0 |
| absent | `.` (invoker's CWD) | `./trading_desk_<TICKER>` — unchanged |

**Step-1 discovery, redirected branch** — never consults CWD:

```bash
ls -dt <WORKROOT>/detail_reports_* 2>/dev/null | head -1
# only if that is EMPTY, fall back to a workroot that holds a human-layout tree:
ls -dt <WORKROOT>/trading_desk_<TICKER>/detail_reports_* <WORKROOT>/td_bundle_<TICKER>_* 2>/dev/null | head -1
```

Flat-first, with the nested/legacy fallback ordered *after* it rather than merged into one `ls`, so a
stray legacy sibling can never win on mtime over the flat bundle the caller meant. The absent branch
keeps today's two globs byte-for-byte.

**`--prev-dir <ABS_DIR>` (delta only), tolerant:**

- basename starts with `detail_reports` → that path **is** `<PREV_BUNDLE>`;
- otherwise `<PREV_BUNDLE>` = newest `detail_reports_*` under it (legacy `td_bundle_*` fallback).

Named `--prev-dir`, **not** `--previous`: at script level `--previous` already means a *bundle*
directory, and one token meaning workspace-root at the skill boundary and bundle-dir at the script
boundary is a wrong-path bug waiting to happen. `--prev-dir` matches `refresh-analysis` exactly, so a
caller computes the value once and passes it to any of the three skills unchanged. **One name** — the
alternative spelling is deliberately not accepted.

`--prev-dir` is **READ-ONLY**, stated in the same words `refresh-analysis` uses.

**Fan-out.** Every `python3 scripts/…` invocation in Steps 2 / 4 / 4b / 5(b)(d)(e) and Delta mode
takes absolute `<BUNDLE>`, `<REPORT>`, `<PREV_BUNDLE>`. The literal
`./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>` is replaced by `<BUNDLE>` throughout.

**A gap this exposes.** Step 5's preamble promises a three-PDF docket ("exec, detail, and, when a
prior bundle exists, a delta note"), but step (e) shows only `--doc exec` and `--doc detail`. The
`--doc delta` command lives **only** in `refresh-analysis`. As the recovery entry point,
`report-renderer` must be able to mint the full docket, so Step 5 gains:

- the `--doc delta --previous <PREV_BUNDLE>` command;
- `delta_interpretation` **required** in `pdf_slots.json` when a prior bundle is given (null only
  when there is none);
- `--previous <PREV_BUNDLE>` on the `--pdf-slots` gate, so the Δ values are admitted to the
  provenance set;
- `--previous <PREV_BUNDLE>` on `--doc exec`, giving the What-Changed box.

**Preserved verbatim (the requester's §5 non-negotiables), restated in Important Notes:**

- `report_qc.py` stays blocking — no `--force`, no `--skip-qc`, no `word_cap` override, not behind a flag.
- The `--pdf-slots` provenance gate stays blocking.
- Read-only over the bundle except the two artifacts the skill already authors (`pdf_slots.json`,
  `charts/`), and read-only over `<PREV_BUNDLE>` entirely.
- No implicit re-analysis: a missing module file exits 2 naming it. Never silently invoke an upstream
  skill to fill a gap.

### Unit 2 — `scripts/word_budget.py` (new) + a render-time budget block

**Why not the requested per-slot budgets.** The request assumed the render-time author has meaningful
room to allocate. It does not: five of the prose slots (`brief_<dim>`) are **transcluded verbatim**
from `brief_<dim>.md` and capped upstream at authoring time, so the author controls only `tension`,
`event_playbook`, `catalyst_notes`, `monitoring_notes`. When the skeleton is near cap the author is
close to powerless — which is the trap the 2026-08-12 run fell into. Per-slot allowances would not
have helped; **surfacing the skeleton's own count at render time** does. In-mark budgets would also
change the slot-mark format and disturb `no_empty_slots` plus a large renderer test body, for a
secondary ask.

**Import direction forces the shape.** `report_qc` already imports `render_report`, so
`render_report` cannot import `report_qc` back. A new stdlib-only `scripts/word_budget.py` owns the
primitives:

- `page_sections(report_text)` — split on `## Page` headers;
- `countable_prose(section_text)` — drop pipe-table rows and the `### Data Integrity` section;
- `count_words(report_text)` → `(total, per_page)`;
- `section_contributors(report_text, top_n)` → ranked `###`-section word counts.

`report_qc` keeps `_page_sections` / `_countable_prose` as thin aliases onto the new module, so its
behavior, its messages, and the tests that call those private names are untouched. **One counter,
which is the property that matters — the render-time number and the gate's number can never drift.**

`render_report.main` prints after the path line:

```
WORD BUDGET: skeleton 2041 / cap 2100  (prose+headings; table rows and Data Integrity excluded)
  remaining for open slots: 19   (cap 2100 − margin 40 − skeleton 2041)
  open slots: 4 → ~4 words each
  ⚠ NEAR CAP — largest contributors: brief_fundamental 148, brief_risk 141, brief_sentiment 139
```

- Contributors are ranked `###`-section counts, so the **dominant transcluded brief is named** — the
  author learns where the mass is instead of trimming four small slots to the bone against an overage
  they did not cause.
- The ⚠ fires when `skeleton > cap − margin` (2060), i.e. when the open slots cannot absorb normal
  prose. Cap (2100) and margin (40) are imported from the same constants the gate enforces.
- Open-slot count comes from the `<!-- SLOT:` marks actually left in the rendered text, so a bundle
  whose briefs all transcluded reports 4, and one missing its brief files reports more.

**Known and accepted:** this block prints on **every** render, redirected or not. The requester's
byte-for-byte clause covers artifacts and paths — no file output changes — but stdout does gain the
block. Named here rather than left to be discovered.

### Unit 3 — tests

- `tests/test_word_budget.py` — parity (the new counter returns exactly `report_qc`'s existing numbers
  on the same fixture), contributor ranking and ordering, the ⚠ threshold and its absence, the
  open-slot count, and a zero-open-slots case.
- `tests/test_output_dir_workspace.py` — a doc-contract test over `skills/report-renderer/SKILL.md`:
  asserts `--output-dir` and `--prev-dir` are present and that the redirected discovery branch carries
  no bare `./trading_desk_` glob. Cheap guard against the stale-doc-string class that recurred often
  enough that 1.5.1 made a sweep for it a standing release-procedure step.
- Full suite green: baseline **2483 passed / 9 skipped**.

### Unit 4 — acceptance

Run against a **copy** of the real PLTR fixture, on the **internal disk**. The requester's original
§4 invited a run against the live workspace and then corrected it: a docket rendered onto that
workspace now would make a stale bundle (it scores the 2026-08-11 session) read green to a
freshness-blind downstream gate — the exact "unreachable success" that gate exists to prevent.

- `cp -R` both `PLTR/2026-08-12` (the bundle) and `PLTR/2026-08-11` (the `--prev-dir`, named in
  `refresh_plan.json`) to internal scratch, dereferencing the `coverage →` symlink so no read touches
  `/Volumes` during the run.
- Invoke from the plugin repo root — a CWD that is not the workspace.
- Record live-workspace mtimes before and after.

**Pass criteria:**

1. Discovery finds the bundle with no `cd` and no CWD-relative glob matching anything.
2. `report_qc` runs and **fails on `word_cap`** — it must still block. Trim, re-run, green. (This
   fixture is valuable *because* it fails: it proves the recovery path cannot smuggle an ungated
   report through.)
3. `PLTR_Trade_Report_2026-08-12.pdf`, `PLTR_Detail_2026-08-12.pdf`, `PLTR_Delta_Note_2026-08-12.pdf`
   land in the scratch workroot, non-empty.
4. Nothing under `detail_reports_2026-08-12/` is mutated except `pdf_slots.json` and `charts/`.
5. kurama's live workspace is untouched — mtimes unchanged.

### Unit 5 — release

Branch `feature/report-renderer-output-dir` → version **1.6.0** → CHANGELOG → the stale-version-string
sweep (a release-procedure step since 1.5.1) → merge/tag/push → deploy and verify markers in the
resolved plugin cache tree → fill the handoff status block in place.

---

## 4. Decisions on the record

| # | Decision | Rationale |
|---|---|---|
| D1 | `--prev-dir`, not `--previous` | `--previous` collides with the script-level bundle argument; `--prev-dir` matches `refresh-analysis` |
| D2 | Tolerant `--prev-dir` (accepts a bundle path too) | Costs nothing, removes a class of caller error; the contract is still "workspace root" |
| D3 | One spelling only | Two names for one concept is documentation debt that outlives its author |
| D4 | Flat-first discovery, nested/legacy as an ordered fallback | A merged `ls -dt` could let a stray legacy sibling win on mtime |
| D5 | stdout budget block, not in-mark budgets | The author controls 4 small slots; skeleton bulk is the real lever, and the mark format stays untouched |
| D6 | Shared `word_budget.py` rather than a duplicated counter | `report_qc` → `render_report` import direction forbids the reverse; one counter cannot drift |
| D7 | `--doc delta` added to Step 5 | Step 5 already promises a three-PDF docket; only `refresh-analysis` could produce one |
| D8 | Acceptance on a scratch copy, internal disk; live workspace never written | A fresh docket on a stale bundle would read green to a freshness-blind gate; recovery value zero, hazard real |

## 5. Out of scope

- The host `/Volumes` EPERM fault (upstream #57540).
- Finishing kurama's PLTR docket — now or later. If PLTR needs to be current, the answer is a fresh
  run into a new dated workspace.
- Any relaxation of the blocking gates, and any `--bundle`-style direct-path flag on the skill
  (`--output-dir` was chosen for one mental model across the three skills).
