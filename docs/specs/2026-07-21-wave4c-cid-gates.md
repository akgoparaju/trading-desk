# Spec — Wave 4C: C-ID Referential-Integrity Gate on All Judgment Flags (B29)

**Date:** 2026-07-21 · **Status:** proposed · **Source:** review "Cross-cutting: C-ID gates on all judgment flags"; priorities B29.

**Verification note.** Confirmed at investigation: composite's conviction flags (`variant`/`catalyst_clarity`/`invalidation` justifications) ALREADY require C-IDs at score time (the Wave-3B E2E hit that gate). Fundamental `--moat` also gates at score time (it runs in the composite step, after context). **The remaining ungated judgment flags — technical `divergence`, sentiment's three (`rating_actions`/`inst_flow`/`insider_baseline`), risk `top_risk` — are set by scorers that run in PARALLEL with `module_context` (Phase 2), so they cannot reliably do referential-integrity at score time.** The correct, ordering-safe enforcement point is `report_qc.py` (Phase 5 — context guaranteed present, already a blocking gate, and naturally no-ops on the `web_compressed` floor where no context registry exists).

## The gate
Add a **`judgment_flag_citations`** check to `report_qc.py` (the blocking §12 gate). When `module_context.json` exists in the bundle (the `coverage_distilled` path):
- Collect every judgment-flag justification string across the module JSONs: technical `flags.divergence_justification`; sentiment `flags.{rating_actions,inst_flow,insider_baseline}_justification`; risk `flags.top_risk`; composite `flags.{variant,catalyst_clarity,invalidation}_justification` (already C-ID-gated upstream, but re-verified here for referential integrity against the final registry).
- For each justification that is **non-empty AND corresponds to a non-default flag** (a set judgment, not the neutral default), extract all `C\d+` tokens (regex, same as fundamental `--moat`).
- **Referential integrity:** every extracted `C\d+` must exist as a finding `id` in `module_context.findings[]`. An orphan citation (C-ID not in the registry) → **check FAILS** (fabricated citation).
- **Grounding:** a non-default judgment flag whose justification contains **zero** `C\d+` tokens → **check FAILS** ("ungrounded judgment flag — cite a context finding"). This is the regex-grounding half of the review's "regex + referential integrity."

When `module_context.json` is ABSENT (the `web_compressed` floor) → the check **passes automatically** (no registry to cite; disclosed by the compressed-floor statement). This mirrors fundamental's "omitting --moat is only correct on the compressed floor."

Waivable with a real justification (`--waive "judgment_flag_citations:reason"`, same mechanics as the other gate checks) — disclosed, never to hide a fabricated citation.

## What "non-default flag" means (per module — do NOT fail on a neutral default)
- technical: `divergence != "none"`.
- sentiment: `rating_actions != "neutral"`, `inst_flow != "unknown"`, `insider_baseline != "normal"` (the neutral defaults carry no justification requirement — the scorers already only require a justification when non-default).
- risk: `top_risk` present (non-null) AND `stress_pct` set (the risk stress judgment).
- composite: `variant != "none"`, `catalyst_clarity != "vague"`, `invalidation != "none"` (already enforced upstream; re-checked for registry integrity).

The check reads each module's `flags` block + the flag VALUES (to know default vs non-default) — all present in the module JSONs.

## SKILL guidance (make the requirement legible)
Update the four evidence/orchestration SKILLs (`technical-analysis`, `sentiment-positioning`, `risk-analytics`, `composite-score`) to state: **a non-default judgment flag's justification MUST cite ≥1 context finding ID (C<n>) when a `module_context` registry exists; the report_qc `judgment_flag_citations` check enforces it.** (The composite SKILL already says this; extend to the three evidence SKILLs + risk `top_risk`.) This is the escalation-ladder move: the prose requirement now has a checking artifact.

## Non-negotiable: this MOVES NO SCORE
The gate is a report-time validator. It changes no rubric, band, or weight. It can REJECT a report whose judgment flags cite fabricated/absent findings — that is the point (it forces grounding), but it never alters a number.

## Implementation
1. `scripts/report_qc.py` — new `check_judgment_flag_citations(bundle)`: load module JSONs + `module_context.json` (if present); per the rules above, collect non-default flag justifications, extract `C\d+`, verify grounding (≥1 C-ID) + referential integrity (each exists in findings). Return the standard `{check, passed, detail}` shape; wire into the blocking check list. No-op pass when no context.
2. `skills/{technical-analysis,sentiment-positioning,risk-analytics,composite-score}/SKILL.md` — the C-ID citation requirement + naming the report_qc check.

## Tests (`tests/test_report_qc.py`)
- A bundle with context + a technical divergence flag justification citing a valid C-ID → passes.
- A non-default flag justification with an ORPHAN C-ID (not in findings) → fails (referential integrity).
- A non-default flag justification with NO C-ID → fails (grounding).
- A neutral/default flag (e.g. divergence "none") with no justification → passes (no requirement).
- No `module_context.json` (compressed floor) → passes automatically.
- The `--waive` path works.
- Full suite green; re-pin any report_qc fixture.

## E2E gate (standing, adapted)
On a coverage_distilled bundle (BE has context): confirm the check passes when flags cite real C-IDs, and fails when a flag is fabricated to cite a non-existent C-ID — before commit. (No composite grade change — pure validation.)

## Definition of done
`report_qc.py` blocks a report whose non-default judgment flags are ungrounded or cite absent context findings; passes on grounded citations and on the compressed floor; the four SKILLs state the requirement + name the check; no score moves; suite green; E2E confirms both the pass and the fail path.
