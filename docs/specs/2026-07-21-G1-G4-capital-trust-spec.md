# Spec — G1–G4 capital-trust fixes (from the GOOG review validation)

**Date:** 2026-07-21 · **Source:** `jutsu-trading-desk/docs/reviews/2026-07-21-goog-review-validation.md` · **Bar:** no guesses, data-driven, 95% confidence.
**Standing gates (project contract):** real-data E2E re-score before commit (read before/after grade, not just green tests); reference-value verification for any new primitive; verify the diff against intent, not the subagent's passing test.

Each task below carries a **data finding** section — what the code/data actually supports — because two of the four differ materially from the review's framing. Those differences are surfaced, not silently absorbed.

---

## G1 — Reconciled issuer market cap (P0, verified score-moving bug) — READY

### Root cause (verified in code)
`build_snapshot.build_price` emits both `mktcap_overview` (AV `MarketCapitalization`, issuer-level) and `mktcap_computed` (`last × SharesOutstanding`). For a multi-class issuer AV's `SharesOutstanding` is **one class only** (GOOG = Class C, 5.4996B), so `mktcap_computed` = **$1.932T** while the correct issuer cap `mktcap_overview` = **$4.288T**. Three consumers read the wrong `mktcap_computed`:
- `build_snapshot.build_valuation` L848/869 → `fcf_yield` = 3.33% (correct: **1.50%**).
- `render_report._page1_header` L220 + `render_pdf` L903 → page-1 market cap shows $1.93T.
- `score_risk` L131/980 + `score_liquidity` net-cash ratio → 2.8% (correct: ~1.27%).

`qc.check_mktcap` **already detects** the divergence (fails at 54.9%); today it requires a manual waiver.

### Design — one reconciled field, chosen by the check that already exists
Add `price.mktcap` (authoritative issuer cap) + `price.mktcap_basis` in `build_price`. Reconciliation rule (pure function, unit-tested):
1. `mktcap_overview` absent/≤0 → `mktcap = mktcap_computed`, basis `"computed_only"`.
2. Both present and reconcile within `_MKTCAP_TOL` at `last` **or** `prev_close` (single-class name; `check_mktcap` would PASS) → `mktcap = mktcap_computed`, basis `"reconciled_agree"` (keep the fresh, today's-price figure).
3. Both present but diverge beyond tol (multi-class scope error, or reused-stale overview) → `mktcap = mktcap_overview`, basis `"overview_authoritative"`.

Rationale: case 3 is exactly the multi-class signal `check_mktcap` fires on; AV's `MarketCapitalization` is issuer-level (verified: GOOG $4.288T ≈ 12.2B sh × price). `mktcap_computed`/`mktcap_overview` stay in the snapshot for provenance.

### Consumers → read `price.mktcap`
- `build_valuation`: `mktcap = price.get("mktcap") or price.get("mktcap_computed")` (fallback preserves old behavior if field absent). `fcf_yield` denominator becomes reconciled.
- `render_report._page1_header` + `render_pdf`: display `price.get("mktcap")`.
- `score_risk`: `INPUT_FIELDS` `price.mktcap_computed` → `price.mktcap`; `mktcap = price.get("mktcap")`.

### QC change
`check_mktcap`: when reconciliation basis is `overview_authoritative` because BOTH last- and prev-reconciliation fail AND the overview is fresh (≤2d), emit a **non-failing** result `"reconciled to issuer overview (multi-class: AV SharesOutstanding is one class); computed=<x> overview=<y>"` instead of FAIL-needs-waiver. Preserve a hard FAIL only when overview is fresh, computed diverges, **and** the magnitude is implausible for a class split (guard: `0.15 < computed/overview < 1.0` is the multi-class band → reconcile; outside → FAIL as a real anomaly). Keep the existing stale-overview SKIP path.

### Tests (add to `tests/`)
- `reconcile_mktcap` unit table: single-class agree → computed; GOOG-like divergence (1.932e12 vs 4.288e12) → overview; overview absent → computed; implausible ratio (e.g. 0.05) → FAIL retained.
- `build_valuation` fcf_yield uses reconciled cap (GOOG fixture: 64.429e9/4.288e12 ≈ 0.0150).
- `score_risk` net-cash ratio uses reconciled cap.
- render_report / render_pdf show reconciled cap.
- Re-pin any snapshot golden fixtures that carried `mktcap_computed` on page 1.

### Acceptance criteria (measured on the GOOG bundle re-score)
- Page-1 market cap: $1.932T → **$4.288T**.
- `snapshot.valuation.fcf_yield`: 0.03334 → **~0.01503**.
- `module_fundamental` valuation `fcf_yield_points`: 5/7 → **3/7** (anchored `_fcf_yield_anchored`: 0.0150 ∈ [0.015,0.03)); valuation subscore 18.2 → **16.2**; fundamental 66.2 → **~65.2**; composite grade unchanged (B) — a small, correct move.
- `module_risk` `liquidity_solvency` net-cash ratio: 2.8% → ~1.27% (net_ratio_points 5/7 likely unchanged at [0,0.05] band → verify).
- Full suite green; new tests green.

---

## G2 — Adjusted-financials bridge (P1) — RE-SCOPED (data findings change the framing)

### Data findings (verified this session — no-guess constraint)
1. **AV `INCOME_STATEMENT` has no structured one-time line.** `otherNonOperatingIncome` and `investmentIncomeNet` are `null` for every GOOG quarter; the ~$37.7B pre-tax equity-securities mark (Q1'26 NI $62.578B vs ~$34B run-rate) is buried in `netIncome`. **⇒ the adjustment cannot be auto-derived from AV; a run-rate heuristic would be a guess (barred).**
2. **On GOOG the one-time item does not move the score.** Core TTM NI ≈ 160.2B − ~28.5B after-tax ≈ 131.7B; core ROE ≈ **31.7%** vs GAAP 38.9% — **still ≥30%, still `roe_points` 8/8.** Quality 48/50 is unchanged by normalization. The review's "quality is inflated by the one-time item" does not hold at the score level for this name (quality scores on roe/margins/fcf/growth/moat; only roe is exposed and it survives the tier).

### Consequence — what G2 actually is
A **coverage-supplied** adjusted-financials input (like DCF/comps anchors are coverage-supplied), threaded into the fundamental module as **disclosure + a corrected `pe_5yr_median` baseline**, not an auto-normalizer and not (for GOOG) a score mover:
- New optional coverage artifact `coverage/adjusted_financials.json`: `{core_eps_ttm, core_net_income_ttm, one_time_items:[{label, pre_tax, after_tax, source_citation}], as_of}` — every item **must** carry a filing citation (no citation → rejected, mirrors the moat C-ID gate).
- `build_snapshot`: when present, emit `fundamentals.core_eps_ttm`, `valuation.pe_ttm_core`, and rebuild `pe_5yr_median` off `core_eps_ttm` (fixes the inflated-baseline distortion — a real, if small, correction to the own-history component).
- `score_fundamental`: display GAAP **and** core (P/E 26.4x GAAP / ~32x core); score on core ROE where a coverage figure exists; **disclose** when only GAAP is available.
- Degrades gracefully: no coverage artifact → GAAP-only, disclosed (the FSI-absent floor).

### Decision for the user (surfaced, not assumed)
G2 as auto-normalization is **not buildable under no-guess** (data finding 1) and is **score-neutral on GOOG** (finding 2). Options: **(a)** build the coverage-input + disclosure path now (trust/disclosure win, minimal score effect); **(b)** defer G2 until a name where the item is score-moving and pair it with the FSI initiation layer that already reads filings. Recommend **(a)** at low effort for the disclosure + baseline fix, but flag the honest impact.

---

## G3 — Issuer/security master (P1→P2) — STAGED

G1 is the targeted down-payment; G3 generalizes it to a real security master (security-level price/liquidity/options/short-interest vs issuer-level fundamentals/EV; multi-class cap = Σ class price × class shares; total/diluted reconcile to filing). This is a **layer**, not a one-shot subagent task — it touches the snapshot schema (bump), every scorer's denominators, and the EV multiples. **Recommendation:** ship + validate G1, then scope G3 as its own spec with a schema-version bump and a golden-fixture migration. Building it blind in one pass would violate the 95% bar.

---

## G4 — Canonical decision contract + semantic assertions (P1) — READY TO SPEC IN FULL

Highest-leverage trust fix. Two parts:
1. **Decision object** built before render (new `scripts/decision_contract.py`): `{profile, position_state, horizon_months, scenario_horizon_months, annual_return_hurdle, total_return_hurdle, ev_at_current, hurdle_clearing_price, capital_eligible, capital_blockers[], action_unowned, action_owned}` — sourced deterministically from `module_composite` + `module_tradeplan` + snapshot. Page-1 action/labels render from this object; the LLM narrates, never authors capital status.
2. **Semantic assertions in `report_qc.py`** (block on violation): `scenario_horizon == hurdle_horizon`; `BUY|ACCUMULATE ⇒ EV_at_entry ≥ hurdle`; `LOW core confidence ⇒ capital_eligible == false`; `"first positive EV" ⇒ all higher levels EV ≤ 0`; `"hurdle-clearing" ⇒ EV ≥ hurdle`; `"reclaimed X" ⇒ price ≥ X`; `profit_take > executed entry`; version labels == owning module version; `base narrative target == base scenario target`.

Kills the three confirmed GOOG defects (353.32 triple-label, "first positive-EV" slip, invalidation duplication) and would have blocked the page-1 mktcap render. Note G4 depends on `capital_eligible` semantics from G5 (confidence binding) for the LOW-confidence assertion — sequence G4 core now, wire the LOW-confidence blocker when G5 lands.

---

## Build order (this batch)
**G1 (now, full) → validate/E2E → G4 (decision contract + assertions) → G2 (coverage-input + disclosure, pending user's (a)/(b)) → G3 (separate staged spec).**
G1 first because it is the verified score-mover and the down-payment on G3; G4 second because it is the trust forcing-function; G2/G3 gated on the decisions above.
