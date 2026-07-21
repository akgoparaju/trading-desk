# Spec — Wave 3B: Composite / Trade-Plan Honesty (`composite-v1.1.0` + `tradeplan-v1.1.0`, PROVISIONAL)

**Date:** 2026-07-21 · **Status:** proposed (Philosophy A) · **Source:** review R2/B26; priorities Wave 3.

**Verification note.** An Explore pass located every R2 goal at `file:line`. Three findings shaped the spec: (1) base-rate anchoring is **unblocked** — `events.earnings_move_history` (Wave 2A) is in the snapshot but `score_composite` doesn't read `events.*` yet; (2) Kelly f* is **already computed at the entry, not spot** (`build_sizing` called with `entry_1_level`, trade_plan.py:564) — the fix is surfacing only, no arithmetic change; (3) the bull target is `max(scenario price_targets)` (trade_plan.py:290), NOT a direct consensus-PT lookup — triangulation is a discipline on the LLM's scenario input via coverage anchors.

## Scope: five goals, each provisional per Philosophy A
No evidence-module scores change here — this is the orchestration layer. `composite` and `tradeplan` rubrics → 1.1.0. Composite confidence roll-up unchanged (still `min` over evidence dims; composite has no DEPTH row).

### A — Base-rate-anchored scenario probabilities + deviation flag (`score_composite.py`)
`score_composite` already loads the snapshot (for `price.last`). **Also read `events.earnings_move_history`** (list of `{quarter_end, move_pct}`). Compute the empirical base rate:
- Classify each historical move: `move_pct > +0.05` → bull, `< −0.05` → bear, else base. **±5% "material move" is the cited provisional threshold.**
- Base-rate prob = empirical frequency per class (need `N ≥ 4` history, else skip the check — disclosed).
- After the existing prob-sum validation gate, compare each LLM `scenario.prob` to its base-rate analog; **`flags.base_rate_check`** = `{base_rates: {bull,base,bear}, deviations: {...}, flagged: bool, n_history, threshold_pp: 25}`. `flagged = true` when any `|LLM_prob − base_rate| > 0.25` (**25pp, the review's number, provisional**). Soft flag (disclosed in the report), not a hard gate — the LLM may hold a differentiated view, but it must be visible.

### B — Bull-target triangulation from coverage anchors (`trade_plan.py`)
`_run_stock_plan` conditionally loads `valuation_anchors.json` (same optional-existence pattern as `score_fundamental --anchors`), passes `dcf_bull` + `comps_high` into `build_exits`. Triangulate:
- `bull_target.level` = **`min(max_scenario_PT, comps_high)` when `comps_high` present** (the review's concern: raw bull 283 exceeds the desk's own coverage). Keep `dcf_bull` as a displayed reference. Preserve the raw `max_scenario_PT` in a new field `bull_target.scenario_raw` for transparency. When no anchors → unchanged (`max_scenario_PT`), disclosed. **`min` (conservative) is the provisional formula.**

### C — Auto-tension gate (`score_composite.py`)
`composite.tension` is a null LLM slot (score_composite.py:580). Auto-populate: in `build_module`, over the evidence-dimension scores in `composite["dimensions"]` (exclude thesis_conviction), compute `spread = max − min`. When **`spread > 25` (provisional threshold, ~quarter-scale)**, set `tension` to a scripted string naming the high/low dims + spread (e.g. `"sentiment 58.8 vs fundamental 30.8 — 28-pt evidence spread"`). Below threshold → stays null. This changes the documented "tension stays null" contract → note it in the composite SKILL: tension is now auto-populated when the spread fires, LLM prose optional on top.

### D — Kelly headline (surfacing) + expression-leads-executable (`trade_plan.py`)
- **Kelly:** f* is already entry-conditioned. Surface `sizing.f_star` beside `sizing.recommended_pct` and `sizing.cap_pct` in the module (add a `sizing.headline` field: `"f* {f_star} at entry {entry}; capped to {recommended_pct} ({cap_pct} cap)"`) so a reader never sees a bare 36.7% next to a 4% cap without the entry context. Brief/SKILL format change; **no arithmetic change** (the number is already correct).
- **Expression:** in `synthesize()` (trade_plan.py:635), when `not structures_selected` (options gated out), also set `expression.recommended_for_profile` to the **stock-plan fallback** (lead with the executable leg), with a note `"(options gated — implement in stock)"`, instead of leaving the options-tilted text. One-line addition beside the existing `executable=False` / `executability_note` logic.

## Disclosure + falsifiers (Philosophy A)
`composite` + `tradeplan` rubric_version → "1.1.0"; module notes stamp PROVISIONAL. **Falsifiers** (in the respective SKILLs):
- **composite:** *if across the B9 set the base-rate deviation flag fires on names where the LLM view is later validated (i.e. flags good judgment as anomalous) more often than it catches genuine overconfidence, the 25pp threshold / ±5% bins are refuted and re-set; if auto-tension fires on >half the book or never fires, the 25-pt spread threshold is refuted.*
- **tradeplan:** *if `min(PT, comps_high)` triangulation systematically clips bull targets below realized bull outcomes across the B9 set, the formula is refuted (revisit to a median or weighted blend).*

## Implementation
1. `score_composite.py` — read `events.earnings_move_history`; base-rate compute + `flags.base_rate_check`; auto-tension in `build_module`; rubric 1.1.0 + note.
2. `trade_plan.py` — conditional `valuation_anchors.json` load; `build_exits` triangulation + `scenario_raw`; `sizing.headline`; `synthesize` expression-leads-executable; rubric 1.1.0 + note.
3. `skills/composite-score/SKILL.md` + `skills/trade-plan/SKILL.md` — provisional notes, falsifiers, the tension-now-auto and base-rate-flag disclosures, the Kelly-headline + expression-fallback guidance.
4. (Optional) `render_report.py` — surface `base_rate_check.flagged` + the auto `tension` if the renderer doesn't already pick up the fields; keep digit-free where it's a tag, numbers only from the bundle.

## Tests
- `score_composite`: base-rate from an `earnings_move_history` fixture (known moves → known freqs); deviation flag fires >25pp / clears ≤25pp / skips at N<4; auto-tension fires >25 spread with the right dims, null ≤25; rubric 1.1.0.
- `trade_plan`: bull triangulation = min(scenario, comps_high) with anchors, raw preserved, unchanged without anchors; `sizing.headline` carries f* + entry + cap; expression fallback to stock when structures empty; rubric 1.1.0.
- Full suite green; re-pin composite/tradeplan/report fixtures.

## E2E gate (standing)
Re-score BE: confirm base_rate_check computes off BE's real earnings_move_history (+22.7/−2.9/+23.2/−2.4/−11.1/+2.6/+20.1/+16.1 → ~50/12/38 bull/base/bear), the auto-tension fires on BE's wide spread (fundamental 30.8 vs sentiment ~59), bull triangulates against comps_high, grade movement sensible, before commit.

## Definition of done
Base-rate deviation flag + auto-tension populate deterministically; bull target triangulates from coverage anchors (raw preserved); Kelly headline entry-conditioned + expression leads with the executable leg; both rubrics 1.1.0 PROVISIONAL + falsifiers; suite green; E2E sensible.
