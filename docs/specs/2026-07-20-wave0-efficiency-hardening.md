# Spec — Wave 0: Efficiency & Correctness Hardening

**Date:** 2026-07-20 · **Status:** proposed · **Owner:** — · **Source:** `jutsu-trading-desk/docs/reviews/2026-07-20-development-priorities.md` (Wave 0). Companion inputs: the efficiency audit (T1/T2/E1) + the quality review's quick fixes (QF1–5).

**Goal.** Ship the cheap, pure-win speed and correctness fixes **first** — no schema change, no rubric-band change, no quality trade-off — so the expensive Wave 1–4 work (which each need many validation runs) iterates on a faster, cleaner machine. Every item here is either a wall-clock win or a correctness/honesty bug.

**Scope guard.** Wave 0 touches NO scoring band, NO composite weight, NO valuation formula. The one additive schema field (`meta.latest_trading_day`, QF2) is backward-compatible and consumed only for display now; the confidence layer (B23, Wave 1) reads it later. If a change would move a score, it is out of Wave 0.

**Verification note (no-guessing).** Root causes below are read from source at the cited `file:line`. Two items were re-scoped after inspection: **QF1** (the specific mislabel is already fixed in 0.12.1; only a residual overclaim remains) and **B19** (marginal; subsumed by B18). Both are documented honestly rather than specced as fresh fixes.

---

## B18 — Collapse the IV-history sampling loop  ·  Impact M (time+cache) · Effort M

### Problem (measured)
The cold snapshot agent runs the IV-history refresh as **~54 serial tool round-trips** — 27 `HISTORICAL_OPTIONS` fetches + 27 per-sample `Bash` one-liners (each computes one ATM IV via `chain.py`, then `rm`s the temp chain). Measured: 105-message / 7.5-min snapshot agent, and those serial turns drive both the wall-clock and the 10.5M cache-read load (a 105-turn conversation re-reads its context every turn). `iv_history_<T>.json` confirmed to hold 27 biweekly samples.

### Root cause (source)
`skills/market-snapshot/SKILL.md` Step 4 instructs the LLM to loop: sample `HISTORICAL_OPTIONS date=…` → run an inline `python3 - … <<PY` one-liner calling `chain.load_contracts` / `chain.expiries` / `chain.atm_iv` → append to cache → `rm`, once per ~26 dates. The per-sample compute is serialized through the LLM because each is a separate `Bash` turn.

### Change
Two independent collapses; **neither touches any scored number** (IV percentile inputs are byte-identical, just computed in bulk):

1. **Batch the fetches.** Rewrite Step 4 to issue the ~26 `HISTORICAL_OPTIONS` calls as **parallel tool-use blocks** (multiple MCP calls per assistant turn) rather than one-per-turn. As each offloads to a temp file, append `{"date": "<sample_date>", "chain_file": "<offloaded_path>"}` to an in-bundle manifest `raw/iv_samples.json` (a Bash write, never into context).
2. **One batch script** — new `scripts/build_iv_history.py`:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/build_iv_history.py \
     --samples ./trading_desk_<T>/detail_reports_<date>/raw/iv_samples.json \
     --daily   ./trading_desk_<T>/detail_reports_<date>/raw/daily_adjusted.json \
     --out     ./trading_desk_<T>/iv_history_<T>.json
   ```
   For each `{date, chain_file}` in the manifest: `contracts = chain.load_contracts(chain_file)`; pick the expiry closest to `date + 30d` (`min(chain.expiries(contracts), key=…)`, the same rule the one-liner uses); `atm_iv = chain.atm_iv(contracts, spot, expiry)`; append `{date, atm_iv}`. **Spot per historical date = the RAW (unadjusted) close on that date** — read the daily raw file's `"4. close"` for `date` (NOT `"5. adjusted close"`: historical option strikes are nominal, so ATM selection must use the nominal price). Merge with any existing cache (dedupe by date), sort, write. Skip (with a recorded reason) any sample whose chain is empty/holiday; the LLM handles the step-back-a-day retry before calling the script, exactly as today. The script deletes each consumed `chain_file` on success (the current `rm`, moved into the script).

### Files
- **New:** `scripts/build_iv_history.py` (stdlib + `from scripts import chain`; reuse `build_snapshot.parse_daily_rows` / `_parse_date` for the daily lookup, or inline a minimal reader).
- **Edit:** `skills/market-snapshot/SKILL.md` Step 4 (batched fetch + manifest + single script call; drop the per-sample one-liner + `rm`).
- **Edit:** `skills/refresh-analysis/SKILL.md` line ~71 (`iv_history` refresh path uses the same batch script).

### Determinism gain (verified, worth stating)
The current Step-4 one-liner takes spot as an **LLM-supplied argument with no specified source** (`float(sys.argv[2])`, "the spot at that date" — the SKILL never says where to get it). So today's IV history is **non-deterministic**: two runs could pick different spots and, near a strike boundary, different ATM strikes and IVs (`chain.atm_iv` → `nearest_strike` = strike closest to spot). The batch script fixes this by deriving spot deterministically from the daily raw close. Verified safe: IV history runs **only** in premium `alpha_vantage` mode (Step 4 skips it on degraded/web tiers), where the daily file is AV JSON and `parse_daily_rows` retains `"4. close"` (raw). So B18 is a **correctness improvement**, not only a speed one — but note a one-time cache rebuild may shift historical samples for **split** stocks (where raw ≠ adjusted); BE and other non-split names are unaffected.

### Tests
- `tests/test_build_iv_history.py`: given a fixtures manifest of 3 tiny chain files + a daily fixture, assert the emitted `atm_iv` per sample equals a **hand-computed expected value at the raw-close spot** (correctness, not identity — there is no deterministic legacy value to match, per the finding above); assert nominal `"4. close"` spot is used, not `"5. adjusted close"`; assert holiday/empty chain → skipped with a recorded reason; assert cache-merge dedupes by date.
- Re-pin any market-snapshot doc/smoke test that references the Step-4 one-liner.

### Acceptance
The IV-refresh costs **≲5 serial turns** (batched fetches + 1 script call) vs the current **~54** (27 fetches + 27 one-liners); `iv_history_<T>.json` shape unchanged; sampling now deterministic given the same chains + daily file.

---

## B19 — Offload `EARNINGS` + `EARNINGS_ESTIMATES` out of snapshot context  ·  Impact LOW · Effort S · **RE-SCOPED**

### Finding (measured, honest)
Only these two fetch results land inline (`offload_markers=0`, ~30KB + ~23KB); every larger payload (statements, chain, treasury, daily, news) is harness-offloaded. The offload threshold sits *between* their size (~30KB) and the statements (~45–70KB), and is **not controllable from SKILL prose**. Crucially, the snapshot runs as a **subagent** — these payloads never reach the main session; they die when the subagent returns its path. Their only cost is cache-read amplification **within** the snapshot agent's own turns, which **B18 already removes** (fewer turns → less re-reading).

### Decision
**Do NOT build a standalone offload mechanism.** After B18 lands, re-measure the snapshot agent's cache-read total; if the earnings/estimates residual is still material, the only clean lever is a **narrower request** (fetch fewer estimate horizons if the AV endpoint supports a scope param) — investigate then, don't pre-build. Tracked as "verify-after-B18," not a coding task.

---

## B20 — Pin the evidence-scorer model tier  ·  Impact S (insurance) · Effort S

### Problem
`skills/full-trade-analysis/SKILL.md:149` gives **prose** model guidance ("run at a sonnet-or-opus class model — never a frontier orchestrator model"). Measured runs already comply (scorers ran Sonnet), so this is insurance, not a live waste: if the orchestrator session is Opus and a subagent inherits, a bounded scorer would silently run ~10× heavier.

### Change
Strengthen the dispatch instruction from a class hint to an **explicit model directive**: when spawning the evidence / company-context / market-snapshot subagents, pass the Agent/Task tool's `model` parameter = **`sonnet`** explicitly; reserve `opus` for the coverage-init dispatch (Phase 0.5), which is genuinely deep. Mirror the same directive in `skills/refresh-analysis/SKILL.md` Step 5. Keep the existing rationale line; add: *"set `model: sonnet` on the dispatch — do not rely on inheritance; a bounded, script-driven scorer must not run on the orchestrator's tier."*

### Files
- **Edit:** `skills/full-trade-analysis/SKILL.md` (Phase 2 dispatch bullet + the "Model discipline" note); `skills/refresh-analysis/SKILL.md` (Step 5 dispatch).

### Tests / Acceptance
Prose-only change (rung-1 of the escalation ladder — enforceable only as far as the Task tool exposes `model` and the orchestrator complies; honest caveat stated in the SKILL). Acceptance: the SKILLs name `model: sonnet` explicitly at each evidence dispatch and `model: opus` at coverage-init. No code/test change.

---

## B21 — Quick-fix bundle (QF1–QF5)

Each is small and localized. QF2 adds one backward-compatible snapshot field (re-pin snapshot tests); the rest are display/label/guard fixes touching no score.

### QF1 — `fundamental_mode` overclaim  ·  **CONFIRMED ALREADY FIXED — verify-and-close, do NOT re-implement**
**Verified against git + CHANGELOG (not inferred):** commit **`7f95a2f`** ("fix: anchored runs disclose coverage_anchored_pass, not the compressed-mode note (0.12.1)") is the ONLY commit that introduced both the `FUNDAMENTAL_MODE_ANCHORED` constant and the `fundamental_mode` ternary at `score_fundamental.py:966`. CHANGELOG `0.12.1 — 2026-07-19` describes exactly this fix and notes that `mode_note` now "state[s] the actual split (valuation from anchors … moat via cited flag)." Independently, `score_fundamental.py:960` derives `anchored` from the **same predicate** `score()` uses (line ~876) to select `score_valuation_anchored`, so `fundamental_mode` and `valuation_mode` cannot diverge in current code. The BE artifact behind the review's QF1 was plugin **0.12.0** — it predates the fix.
**Wave-0 action: VERIFY ONLY.** Confirm the 0.12.1 regression test asserts an anchored run emits `coverage_anchored_pass` + the split note, and that the split note names the floored components (quality on snapshot TTM, own-history n/a, no scale) so it does not overclaim. If both hold — the CHANGELOG wording indicates they do — **QF1 is CLOSED; drop it from Wave 0.** Only if the split note is found to overclaim: add one line enumerating the floored components (no change to the machine `fundamental_mode` key — keep it stable for downstream). No fix is pre-authorized here on the strength of the review alone.

### QF2 — `as_of` vs latest-trading-day honesty (weekend/stale prints)
**Problem:** a bundle built on a Friday close but stamped `as_of` Sunday reads as fresh; nothing surfaces the gap.
**Change:** in `build_snapshot.py` `build_price` (reads `gq` at line ~296), capture `gq.get("07. latest trading day")` into **`meta.latest_trading_day`** (new additive field). In `qc_gate.py`, add a non-blocking check/note: when `meta.latest_trading_day` ≠ the `as_of` date, emit `"as_of 2026-07-19 vs latest trading day 2026-07-17 (weekend/stale print)"` into the attestation. This field is the **input B23 (Wave 1) reads for the confidence staleness axis** — landing it now is deliberate.
**Files:** `scripts/build_snapshot.py`, `scripts/qc_gate.py`, snapshot schema note (0.2.1 → 0.2.2, additive). **Tests:** re-pin snapshot fixtures for the new `meta` field; add a qc_gate case (mismatched dates → note present, gate still exit 0).

### QF3 — Expired-expiry rows in `expected_moves`
**Problem:** an already-expired expiry (e.g. 2026-07-17 in a 2026-07-19 bundle) appears in `expected_moves` with a nonsensical ATM IV, because `_nearest_expiry(exps, as_of, 0)` can select a past expiry (`build_snapshot.py:719`).
**Root cause (source, corrected after verification):** the expired expiry is **minted by `build_snapshot`**, not by options_strategy. `build_snapshot.py:704` `exps = chain.expiries(contracts)` includes expiries `< as_of`, feeding both `expected_moves` (via `_nearest_expiry`, line ~719) and `atm_iv_by_expiry` (`chain.atm_iv_by_expiry(contracts, spot)`, line ~751). `options_strategy.py` **consumes** those snapshot fields; its own `select_expiry` (line 260) already restricts to a `[30,90]`-DTE window and `term_structure` (line ~191) already drops past expiries (the Gate-3 MU fix). So the guard belongs at the **single minting site** — no separate options_strategy change needed.
**Change:** add `chain.future_expiries(contracts, as_of)` → `[e for e in expiries(contracts) if e >= as_of]` (keeps `expiries()` pure). Use it in `build_snapshot` where `exps` is built (line ~704) and where `atm_iv_by_expiry` is minted (line ~751), so no expired expiry reaches `expected_moves` or `atm_iv_by_expiry`.
**Files:** `scripts/chain.py` (+ helper), `scripts/build_snapshot.py`. **Tests:** `chain` fixture with a past + future expiry → `future_expiries` drops the past one; a `build_snapshot` run → `expected_moves` and `atm_iv_by_expiry` contain no expiry `< as_of`.

### QF4 — Catalyst-calendar past-print labels + empty rows
**Root cause (source):** `render_report.py:505` `build_catalyst_calendar` emits `""` notes (next-earnings with no consensus; catalysts with no `note`) and never distinguishes a past date from an upcoming one.
**Change:** in `build_catalyst_calendar`, compute the `as_of` date from `snapshot.meta`; for each row whose date `< as_of`, append `" (past)"` to the Note (or a dedicated status column); replace empty notes with `"—"`. Purely presentational; no number.
**Files:** `scripts/render_report.py`. **Tests:** a snapshot fixture with one past + one future catalyst → past row labeled, no empty Note cell.

### QF5 — `revisions_90d` null trace + loud disclosure  ·  **INVESTIGATION + disclosure**
**Root cause candidate (source):** `build_snapshot.py:498-511` builds `revisions` only from the nearest **future** fiscal-year estimates row's `eps_estimate_revision_up/down_trailing_30_days` (+ pct); it nulls when no future-FY row exists or those fields are absent. `score_sentiment.py:207` then renormalizes the 20-pt revisions dimension away.
**Change:** (1) **Trace** — instrument `build_fundamentals` to record WHY `revisions_90d` is null (no future-FY row vs fields absent in the row) into `meta.missing` / a `revisions_null_reason`, so a null is explained not silent. (2) **Disclose loudly** — `score_sentiment` must surface the renormalization prominently when revisions null **within ~14 days of `next_earnings`** (exactly when the signal matters most), not bury it. The upstream data fix (if the AV estimates payload actually carries the fields under a different key/horizon) is decided **after** the trace — do not pre-write a parser change on a guessed cause.
**Files:** `scripts/build_snapshot.py` (null-reason), `scripts/score_sentiment.py` (pre-earnings disclosure). **Tests:** estimates fixture missing the revision fields → `revisions_null_reason` populated; sentiment brief/flags carry the loud disclosure when null and `days_to_earnings ≤ 14`.

---

## Also in Wave 0 (already backlogged — reference, not re-specced here)
- **B5** — Render-venv resolution + auto re-exec (silent-degradation fix; same rot family as QF1). Pin the venv path deterministically; `render_charts`/`render_pdf` re-exec through the venv python.
- **B6** — Grade-history append on re-scores (= QF6). Append a dated appendix entry whenever a bundle is re-scored under a new rubric.

---

## Cross-cutting constraints (all Wave 0 items honor)
- **No score moves.** IV values (B18), labels (QF1/QF4), guards (QF3), and disclosures (QF5) change presentation/plumbing, never a band or weight. The only additive field (QF2 `meta.latest_trading_day`) is display-only until B23.
- **Single-snapshot / zero-LLM-arithmetic.** `build_iv_history.py` computes from files on disk; no fetch, no in-prose math.
- **Script-is-rubric.** Every derived value stays scripted; SKILL edits (B18/B20) are orchestration, not arithmetic.
- **Rubric/schema versions travel.** QF2 bumps the snapshot schema 0.2.1 → 0.2.2 (additive) and re-pins fixtures; note it in the footer/methodology plumbing that already reads the version.
- **Tests re-pinned per change.** The suite (~1,114 tests) must stay green; each item adds its own regression test above.

## Sequencing within Wave 0
0. **QF1** — verify-and-close first (confirm the 0.12.1 test + split note; likely drop, no code).
1. **B20, QF4** — same-day, prose/label/display, zero risk.
2. **QF2, QF3** — small script + one additive field + fixtures re-pin.
3. **QF5** — trace first, then decide the disclosure/parse change.
4. **B18** — the substantive build; land it before the Wave-1 calibration-run campaign so those runs are cheap.
5. **B5, B6** — as capacity allows (already backlogged).
6. **B19** — verify-after-B18 only; likely dropped.

## Definition of done (Wave 0)
The IV-refresh costs ≲5 serial turns vs ~54 (B18) and is now deterministic; QF1 confirmed already-closed (0.12.1); no bundle ships an expired expected-move / `atm_iv_by_expiry` row, an unlabeled past catalyst, or a silent `revisions_90d` null; every evidence dispatch names `model: sonnet` explicitly; `meta.latest_trading_day` is present for B23 to consume; suite green.
