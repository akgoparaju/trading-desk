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
stray legacy sibling can never win on mtime over the flat bundle the caller meant — a merged glob
would make a silent wrong-bundle render possible, the worst available failure on a path whose whole
purpose is recovery. The absent branch keeps today's two globs byte-for-byte.

The fallback is **not** hypothetical tolerance: real pre-v1.2.0 nested workspaces exist on the
requester's disk (`…/ORCL/2026-07-23/trading_desk_ORCL/detail_reports_2026-07-24/`, from the incident
that prompted the 2026-07-24 flatten handoff). Point the recovery path at one of those and flat misses
while the fallback resolves correctly. That directory holds **two** nested bundles (`…_2026-07-23`
and `…_2026-07-24`), so it also exercises newest-wins *inside* the fallback branch.

**Three guardrails on the fallback (G1 is non-negotiable — the fallback does not ship without it):**

- **G1 — the fallback fires ONLY on "no bundle found at all", never on "bundle found but
  incomplete."** Once a path resolves, that IS the bundle: run the completeness check on it, and on
  failure exit 2 naming the missing module exactly as today. Never re-enter discovery and render from
  a different bundle. **Discovery success is terminal; completeness is a separate, later gate.**
- **G2 — announce which branch resolved**, alongside the bundle path: `flat under --output-dir`,
  `legacy nested fallback under --output-dir`, or `CWD`. A fallback that fires silently is
  indistinguishable from one that never fired, and the operator needs to know which bundle they
  actually rendered.
- **G3 — the not-found message must not assert absence.** Under the host fault a denied `readdir`
  returns **empty rather than erroring**, so `glob` swallows it and "no bundle here" is
  indistinguishable from "I cannot read this directory" (the requester has been bitten by exactly
  this — a bogus "no snapshot in bundle" that was really a permission fault). Word it *"no bundle
  found at `<path>`"*, name the globs searched, and when the path is outside the home volume add the
  remedy: verify readability with a **real byte-read**, not `stat` — `stat` succeeds against a dead
  volume.

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

**Conditional, as the preamble already says:** render the delta only when a prior bundle is
resolvable. **No prior ⇒ a two-PDF docket is a PASS**, not a degradation to disclose. (Acceptance
criterion 3 names three PDFs only because the PLTR fixture genuinely has a 2026-08-11 prior.)

**The layer translation, stated once and explicitly** — this is where D1 earns itself: the *skill*
boundary takes `--prev-dir <PREV_WORKROOT>` (a workspace root), the *script* boundary takes
`--previous <previous_bundle>` (a bundle dir). The skill resolves prev-dir → prior bundle **once**,
then passes the **bundle** to `render_pdf.py --doc delta --previous`, to `render_report.py --delta
--previous`, and to every `report_qc.py --previous`. One name per layer, translated at one place.

### Unit 1b — resume an already-rendered report (D-R)

Step 2 today re-renders unconditionally, which on a recovery is wrong twice over: it discards the
authored prose (~1,736 words across 14 slots on this fixture) and it rewrites `module_decision.json`
in the bundle. A recovery path that expensive would not get used, and the alternative people reach
for is hand-driving the pipeline tail — the exact behavior the caller's no-resume rule exists to
prevent. **A recovery path has to be cheap or it is theatre.**

- **Report already exists at the output location ⇒ skip Step 2 and QC it as-is**, announcing loudly:
  `resumed existing report at <path>; skeleton not re-rendered`. **Touch nothing in the bundle on
  this path** — no `module_decision.json` rewrite.
- **Report absent ⇒ render normally.** Unchanged. On a genuine fresh render, writing
  `module_decision.json` is Step 2's job and is not a violation.
- **`--fresh-skeleton`** forces a re-render even when a report exists. Explicit, never automatic:
  discarding ~1,700 words of authored analysis is not a call a machine makes unprompted.
- **Drift is a hard fail with a named remedy.** If `report_qc` fails a numeric/structural check
  (`number_provenance`, `composite_arithmetic`, `ev_consistency`, `strikes_in_chain`, …) on a
  **resumed** report, that is report/bundle drift: fail, and name `--fresh-skeleton` in the error so
  the operator chooses to discard the prose. Never auto-recover.
- **`word_cap` is not drift** — that is the trim-and-retry path, and it stays exactly as it is.
- **Open slots are not drift** — fill them; `no_empty_slots` already gates that. No re-render.

Safe by construction: `number_provenance` re-verifies every figure in a resumed report against the
bundle, so a resumed report that passes QC carries the same numeric guarantee as a freshly rendered
one. The gate already *is* the drift detector — it only needed to fail loudly with a named remedy
instead of being quietly pre-empted by a re-render.

**Preserved verbatim (the requester's §5 non-negotiables), restated in Important Notes:**

- `report_qc.py` stays blocking — no `--force`, no `--skip-qc`, no `word_cap` override, not behind a flag.
- The `--pdf-slots` provenance gate stays blocking.
- Read-only over the bundle except the two artifacts the skill already authors (`pdf_slots.json`,
  `charts/`) — plus `module_decision.json` on a **fresh render only**, never on a resume — and
  read-only over `<PREV_BUNDLE>` entirely.
- No implicit re-analysis: a missing module file exits 2 naming it. Never silently invoke an upstream
  skill to fill a gap.

### Unit 2 — `scripts/word_budget.py` (new) + a render-time budget block

**Measured first, on the real fixture** (an earlier draft of this spec argued from an assumption and
got it backwards; the numbers below are the correction):

| | countable words | open slots |
|---|---|---|
| fresh skeleton, re-rendered from the bundle | **397** / cap 2100 | **14** |
| the authored report, as the run died | **2133** | 0 |

The skeleton is nowhere near the cap. The author had `2100 − 40 − 397 = 1663` words of room and spent
**1736**, against the SKILL's own stated target of ≤1,000 words of authored prose. **Authored prose is
the entire overage**, so the original request's instinct — budgets surfaced *before* authoring — is
right, and the "the skeleton is the mass" reframing was wrong.

Why the earlier draft got it wrong: it assumed the five `brief_<dim>` slots transclude from
`brief_<dim>.md`. **That bundle contains no `brief_*.md` at all**, so nothing transcluded and all 14
slots came back open. (Root cause is caller-side and now gated there: those files are authored by the
module *skills*, and the runs that lack them drove `run_pipeline.py` / the `score_*.py` scripts
directly instead. Not a plugin defect — but the silent degradation is on this side of the line, and
Unit 2 is what makes it visible.)

So the block carries **both** levers. In-mark budgets stay declined: they would change the slot-mark
format and disturb `no_empty_slots` plus a large renderer test body, for a secondary ask, and the
block delivers the same information at the same moment.

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

`render_report.main` prints the block for a **full report** (not the delta — `check_word_cap` does not
run on deltas), immediately **before** the path line, so the path stays the last line of stdout for
any consumer that reads it that way. On the real fixture:

```
WORD BUDGET  skeleton 397 / cap 2100   (prose+headings; table rows and Data Integrity excluded)
  room for authored prose: 1663        (cap 2100 - margin 40 - skeleton 397)
  14 open slots, budgeted 985:
    brief_{technical,fundamental,sentiment,risk,thesis}   150 each = 750
    signal_{technical,fundamental,sentiment,risk,thesis}   15 each =  75
    tension 30 | event_playbook 60 | catalyst_notes 35 | monitoring_notes 35
  NOTE  5 brief slots are OPEN -- brief_*.md absent or missing its delimiters, so
        ~750 words that normally transclude must be authored here.
```

Three notices, each firing on its own condition:

- **over-subscribed** — budgeted total > room. The author cannot win by writing to budget; the fix is
  upstream, not a tighter slot.
- **skeleton near cap** — `skeleton > cap − margin`. The transcluded-bundle case the earlier draft
  assumed; kept as the backstop it should always have been.
- **brief slots open** — any `brief_*` slot unfilled means transclusion did not happen. On this
  fixture that one line makes the whole causal chain visible before a word is authored.

Each warning **names the largest contributors** (ranked `###`-section counts over the *countable*
text, so the excluded Data Integrity footer cannot dominate the ranking — it is 1,192 words on this
fixture and would otherwise top every list). Cap and margin are the same constants the gate enforces.

**Known and accepted:** the block prints on every full render, redirected or not. The byte-for-byte
clause covers artifacts and paths — no file output changes, and the path line stays last — but stdout
does gain the block.

### Unit 3 — tests

- `tests/test_word_budget.py` — parity (the new counter returns exactly `report_qc`'s existing numbers
  on the same fixture), contributor ranking and ordering, the ⚠ threshold and its absence, the
  open-slot count, and a zero-open-slots case.
- `tests/test_output_dir_workspace.py` — doc-contract tests over `skills/report-renderer/SKILL.md`:
  `--output-dir` and `--prev-dir` present; the redirected discovery branch carries no bare
  `./trading_desk_` glob; and the three guardrails are actually written down (G1's
  discovery-is-terminal rule, G2's branch announcement, G3's non-absence wording plus the byte-read
  remedy). Discovery is skill prose rather than code, so the doc IS the implementation — asserting on
  it is the only available regression test, and it doubles as a guard against the stale-doc-string
  class that recurred often enough that 1.5.1 made a sweep for it a standing release-procedure step.
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

**Second acceptance case — the fallback branch (D4/G2).** Copy
`…/ORCL/2026-07-23/` (nested layout, two bundles inside `trading_desk_ORCL/`) to scratch and invoke
with `--output-dir <SCRATCH>/ORCL/2026-07-23`. Flat must miss, the fallback must resolve
`detail_reports_2026-07-24` (newest wins *inside* the fallback), and the run must announce that the
legacy branch resolved. Discovery only — this case is not carried through to a full docket.

**Pass criteria:**

1. Discovery finds the bundle with no `cd` and no CWD-relative glob matching anything.
2. `report_qc` runs and **fails on `word_cap`** — it must still block. Trim, re-run, green. (This
   fixture is valuable *because* it fails: it proves the recovery path cannot smuggle an ungated
   report through.)
3. `PLTR_Trade_Report_2026-08-12.pdf`, `PLTR_Detail_2026-08-12.pdf`, `PLTR_Delta_Note_2026-08-12.pdf`
   land in the scratch workroot, non-empty.
4. Nothing under `detail_reports_2026-08-12/` is mutated except `pdf_slots.json` and `charts/`. On
   the resume path that includes `module_decision.json` — it must **not** be rewritten (amended per
   D-R; on a fresh render it legitimately is).
5. kurama's live workspace is untouched — mtimes unchanged against the baseline captured before any
   copy was taken.
6. The resume path announces itself, and `--fresh-skeleton` re-renders when asked.

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
| D4 | Flat-first discovery, nested/legacy as an ordered fallback | A merged `ls -dt` could let a stray legacy sibling win on mtime; real pre-v1.2.0 nested workspaces exist on the caller's disk. Ships **only** with G1/G2/G3 |
| D5 | stdout budget block, not in-mark budgets | Same information at the same moment without touching the slot-mark contract or the renderer test body |
| D5a | **CORRECTED** — the block carries per-slot budgets, not just a skeleton count | Measured: skeleton 397/2100, 14 open slots, 1,736 authored words against 1,663 of room. Authored prose is the whole overage; the earlier "skeleton is the mass" argument was assumption, not evidence |
| D9 (D-R) | Resume an existing report by default; `--fresh-skeleton` to force | A re-render discards ~1,700 authored words and rewrites a module JSON; an expensive recovery path does not get used, and the fallback behavior is hand-driving the tail |
| D10 | Drift fails loudly naming `--fresh-skeleton`; `word_cap` and open slots are not drift | Discarding authored analysis is an operator decision, not an automatic one; `number_provenance` already guarantees a resumed report numerically |
| D6 | Shared `word_budget.py` rather than a duplicated counter | `report_qc` → `render_report` import direction forbids the reverse; one counter cannot drift |
| D7 | `--doc delta` added to Step 5 | Step 5 already promises a three-PDF docket; only `refresh-analysis` could produce one |
| D8 | Acceptance on a scratch copy, internal disk; live workspace never written | A fresh docket on a stale bundle would read green to a freshness-blind gate; recovery value zero, hazard real |

## 5. Out of scope

- The host `/Volumes` EPERM fault (upstream #57540).
- Finishing kurama's PLTR docket — now or later. If PLTR needs to be current, the answer is a fresh
  run into a new dated workspace.
- Any relaxation of the blocking gates, and any `--bundle`-style direct-path flag on the skill
  (`--output-dir` was chosen for one mental model across the three skills).
- Chasing the caller's missing `brief_*.md` — root-caused to their side (driving `run_pipeline.py` /
  `score_*.py` directly instead of the module skills) and gated there.

## 6. Noted for the backlog, not this release

- **`run_pipeline.py` is referenced by no SKILL**, yet it is discoverable, self-documents via
  `--help`, and produces a bundle that passes the plugin's own QC while missing artifacts the report
  layer expects (`brief_*.md`). That is a sharp edge for any orchestrator. Worth considering whether
  it should refuse to run without a marker, or stamp the bundle with which rail produced it.
- **An output-layer skill writing a module JSON** (`render_report` → `module_decision.json`) is
  surprising. Idempotent today — verified byte-identical across a re-render — but that is the kind of
  property that stops holding quietly.
