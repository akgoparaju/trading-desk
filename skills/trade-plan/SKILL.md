---
name: trade-plan
description: Turn a scored composite into an EXECUTABLE trade plan off an existing bundle — a mechanical entry ladder (confluences of proven support and valuation anchors), profit-take and bull targets, a both-leg invalidation (trade stop AND thesis-invalidation metric), Kelly-arithmetic sizing, a hedge trigger, a don't-chase line, and the EXPRESSION decision (stock vs options) via decision table expression-v1.0.0. Use when the user says "trade plan [ticker]", "entry exit plan", "how would I position [ticker]", or a report needs an actionable plan. Consumes module_composite/technical/risk + the newest snapshot; runs the composite-score skill first if absent; invokes options-strategy in pipeline mode, then re-synthesizes. Rubric v1.0.0.
---

# Trade Plan (Decision Layer — Execution)

Turn a scored composite into an **executable** plan for a ticker. **All arithmetic is done by `scripts/trade_plan.py`** — you set two honest judgment groups (the catalyst-in-thesis selector flag and the fundamental-invalidation leg), run the script twice (before and after options-strategy), read its JSON, and write prose. You never compute an entry, an EV-at-level, a Kelly fraction, a required multiple, or a hedge trigger in text.

This is an **L3 execution layer**. It consumes module outputs — the composite's EV block (`module_composite.json`), the technical S/R ladder (`module_technical.json`), the risk downside_map (`module_risk.json`) — and reads the newest snapshot only for plan references (`price.last`, `events.next_earnings.date`, `sentiment.iv_pctile_1yr`, `options.iv_minus_rv20`, `fundamentals.eps_ntm_consensus`). It scores **no** snapshot field directly (`INPUT_FIELDS = set()`), so single-mapping is preserved by construction.

**Non-negotiables:**
- **Never do arithmetic in prose.** Every number you cite must already appear in `module_tradeplan.json` (`stock_plan.entries[].ev_at_level`, `sizing.arithmetic`, `exits.bull_target.required_multiple`, `hedge.strikes_from`, `dont_chase.above`) or an upstream module JSON. A level you would have to compute is a script change, not a prose change.
- **Both invalidation legs are mandatory.** A trade stop (technical leg, minted off the ladder) AND a named thesis-invalidation metric (fundamental leg, your judgment). The fundamental leg has **no default** — name a real thesis-pillar metric or the script exits 2.
- **The catalyst-in-thesis flag is asserted honestly, never assumed.** Does the composite's scenario reasoning actually lean on the upcoming event? If yes, the expression selector tilts every profile toward options; if no, it does not. The flag has no default (exit 2 if missing).
- **No level is invented.** Entries anchor on the ladder's proven supports and the composite's valuation anchors; targets come from the ladder and the scenario set. The script mints them; you narrate.

Trigger phrases: "trade plan for MU", "entry exit plan AAPL", "how would I position NVDA".

---

## Step 1 — Locate the bundle and ensure the composite exists

In the invoker's CWD, find the newest bundle for the ticker:

```bash
ls -dt ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

- **If NO bundle exists**, invoke the `market-snapshot` skill for `<TICKER>` first.
- **Require `module_composite.json`.** If absent, run the **composite-score** skill first (it in turn runs the missing evidence skills). The trade-plan script exits 2 with "run composite-score first" if the composite is missing — the plan has no EV block, no hurdle, no scenario set without it.
- `module_technical.json` (the ladder) and `module_risk.json` (the downside_map) should already be present from the composite run. If either is missing the plan degrades gracefully (fewer entries / no valuation-floor confluence), but a complete plan wants both.

---

## Step 2 — Set the two judgment groups honestly (read the composite FIRST)

Read `module_composite.json` (especially `ev.scenarios` + `ev.scenario_reasoning`) and the fundamental brief, then set:

1. **`--catalyst-in-thesis yes|no`** (+ `--catalyst-in-thesis-justification`): does the thesis in the composite's **scenario reasoning** actually lean on the upcoming earnings/event? If the bull case is "the HBM3E ramp shows up at the next print", that is `yes`. If the thesis is structural and the print is incidental, that is `no`. **Be honest** — this flag is the expression selector; inflating it manufactures an options tilt the thesis does not support.

2. **The fundamental invalidation leg** (all three REQUIRED, no defaults):
   - `--fund-invalidation-metric` — a **real thesis-pillar metric** from the fundamental brief (e.g. `"HBM revenue growth"`, not a vague "growth slows").
   - `--fund-invalidation-threshold` — the falsifying threshold (e.g. `"< 20% for 2 consecutive quarters"`).
   - `--fund-invalidation-justification` — one line on why this metric is load-bearing for the thesis.

   If you cannot name a real thesis-pillar metric, that is a signal the thesis is under-specified — go back to the composite, don't fabricate one.

The technical invalidation leg is minted mechanically off the ladder (first proven support below the deepest entry) — you do not supply it.

---

## Step 3 — Run pass 1 (`--stock-plan`)

Profile defaults to the composite's profile; override with `--profile trader|balanced|long-term` if the user asked for a different lens.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/trade_plan.py --stock-plan \
  --bundle ./td_bundle_<TICKER>_<YYYY-MM-DD> \
  --catalyst-in-thesis yes \
  --catalyst-in-thesis-justification "bull case rests on the HBM3E ramp at the next print" \
  --fund-invalidation-metric "HBM revenue growth" \
  --fund-invalidation-threshold "< 20% for 2 consecutive quarters" \
  --fund-invalidation-justification "HBM is the entire margin-expansion thesis" \
  [--profile balanced]
```

The script writes `<bundle>/module_tradeplan.json` (path printed to stdout) with `stock_plan` (entries, exits, invalidation, sizing, hedge, dont_chase) and a **preliminary** `expression`. Any missing required flag, a missing composite, or a missing snapshot is exit 2 — fix and re-run.

**How the plan is built (all mechanical):**
- **Entries** — valuation anchors = `{ev_breakeven_entry}` ∪ downside_map `valuation_floor` rows. A proven support (swing_low/ma50/ma200/put_wall) within **3%** of an anchor is a **confluence**. `entry_1` = the highest confluence below `last`; **but** if `ev_at_current ≥ hurdle_total`, EV already clears the hurdle and `entry_1` = the current price, **sized down** (half the recommended size). `entry_2`/`entry_3` are the next lower confluences/proven supports, each ≥3% apart. Each entry carries its `ev_at_level` (via `ev_kelly.ev_at`).
- **Exits** — `profit_take` = nearest ladder resistance above `last`; `bull_target` = the max scenario target, with `required_multiple = target / eps_ntm_consensus` ("implies N× fwd EPS", null-safe).
- **Invalidation** — technical leg (weekly close below the first proven support under the deepest entry) + your fundamental leg.
- **Sizing** — `f*` from `ev_kelly.kelly` at `entry_1`, capped by `ev_kelly.size_recommendation` for the profile; **−1 notch (quarter-Kelly + half-cap) on a binary event within 30 days**. The full arithmetic string is in `sizing.arithmetic`.
- **Hedge** — required iff (binary event within 30d AND recommended size ≥ 5%) **OR** (iv_pctile_1yr ≤ 25). Spec names the trigger, structure (put spread or collar), `strikes_from` (first two downside_map levels), expiry rule, and premium cap 1.5%.
- **Don't-chase** — 5% above the top entry.

---

## Step 4 — Invoke options-strategy in pipeline mode, then run pass 2 (`--synthesize`)

The **options-strategy** skill consumes this stock plan (the expression mode, the entries, the hedge requirement) and writes `<bundle>/module_options.json` with `recommended_structures` (names + strikes) and a `hedge` spec. **Invoke it now** in pipeline mode for `<TICKER>`.

Then fold its choices back into the plan:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/trade_plan.py --synthesize \
  --bundle ./td_bundle_<TICKER>_<YYYY-MM-DD>
```

This re-reads `module_tradeplan.json` + `module_options.json` (exit 2 "run options-strategy first" if the latter is missing) and updates `expression` with `synthesized: true`, `structures_selected` (the options module's chosen structures matching the expression mode, with strikes), and `hedge_structure` (the options module's hedge spec, if the plan's hedge is required). If a recommended structure's strikes are absent from `module_options.json`, the script exits 2 (consistency) — the options module must carry real strikes.

---

## Step 5 — Write the brief

Read `module_tradeplan.json` (small — read it directly). Write `<bundle>/brief_tradeplan.md`. **Every number comes from the JSON — zero computed-in-prose numbers.**

**Page-1 trade-plan table** (one row each, verbatim from the JSON):

| Row | Value |
|-----|-------|
| Don't-chase | `dont_chase.above` (5% above top entry) |
| Entry 1 / 2 / 3 | `entries[].level` + `condition` + **EV-at-level** (`ev_at_level`); flag `sized_down` if set |
| Profit-take | `exits.profit_take.level` (`type`) |
| Bull target | `exits.bull_target.level` + the required-multiple note ("implies N× fwd EPS") |
| Invalidation | **both legs**: technical (`technical_leg.level`, weekly close below) AND fundamental (`fundamental_leg.metric` `threshold`) |
| Size | `sizing.recommended_pct` + the **Kelly arithmetic footnote** (`sizing.arithmetic`, verbatim) |
| Hedge | `hedge.required` + `trigger` + `structure` + `strikes_from` (or "not required") |
| **Expression** | `expression.recommended_for_profile` (+ `synthesized` structures if pass 2 ran) |

**Expression matrix** — from `expression.mode_per_profile`, show all three profiles' lines and **which selector fired** (`selector_fired`: `catalyst` or `profile-default`), plus any `modulators`. State the `catalyst_in_thesis` flag and its justification.

**Event playbook box** (prose, the `event_playbook` slot — stays `null` in JSON, lives only in the brief): beat / inline / miss → the pre-committed action for each, **vs the options-implied move** (cite `sentiment.implied_move_next_earnings_pct` or the options module's expected move — numbers from the module/snapshot only, none invented). E.g. "Beat: add the second tranche at entry_2. Inline: hold; the implied ±X% is already priced. Miss: the weekly-close invalidation and the hedge do the work — no discretionary averaging down."

**Footer** — `_Rubric v<rubric_version> · expression <expression.rule_version> · as of <as_of>_`.

---

## Step 6 — Output contract

Report to the user (and to any calling skill):
- **Module path** — `<bundle>/module_tradeplan.json`
- **Brief path** — `<bundle>/brief_tradeplan.md`
- **Entries** — the entry levels with EV-at-level (and whether entry_1 was sized down)
- **Size** — `recommended_pct` for the profile
- **Invalidation** — both legs (trade stop level + thesis-invalidation metric/threshold)
- **Hedge** — required or not, and the trigger
- **Expression** — the recommended line for the profile + which selector fired

---

## Important Notes

- **Catalyst-proximity-first expression.** The selector formalizes the lived rule: **a catalyst in sight selects options for leverage; long-term quality selects stock — the profile only implements.** RULE 1 (days-to-catalyst ≤ 60 AND catalyst-in-thesis=yes) tilts every profile toward defined-risk options tenored past the event; RULE 2 is the per-profile default. The horizon profile decides *how much* stock-vs-options, never *whether* the catalyst matters. This is why long-term still gets a small options **kicker** under RULE 1 — the catalyst earns leverage even for a stock-dominant sleeve.
- **Falsifier.** If catalyst-tilted expressions underperform the profile-default expression across the decision journal (~20+ decisions), the selector order gets revisited. The decision table is provisional (`expression-v1.0.0`), not gospel — say so if a reader treats a single call as settled.
- **Sizing caps are 5 / 8 / 10% by profile (trader / balanced / long-term), −1 notch on a binary event.** These live in `ev_kelly.size_recommendation`; the notch is quarter-Kelly + half-cap when a binary event is within 30 days. Never re-cap in prose.
- **position_ctx deltas are v2.** This plan sizes a fresh position from Kelly + caps. Adjusting for an existing position (already long, averaging in, tax lots) is out of scope for v1 — note it if the user has a position.
- **Both invalidation legs, always.** A plan with only a trade stop and no thesis-invalidation metric (or vice-versa) is incomplete. The technical leg is mechanical; the fundamental leg is your judgment and is REQUIRED.
- **Rubric + rule versions travel with the numbers.** `rubric_version` (`1.0.0`) and `expression.rule_version` (`expression-v1.0.0`) print in the JSON and MUST appear in the brief footer.
- **Snapshot and upstream modules are read-only.** This skill writes only `module_tradeplan.json` and `brief_tradeplan.md`; it never edits the snapshot or any evidence/composite/options module JSON (pass 2 rewrites only `module_tradeplan.json`).
