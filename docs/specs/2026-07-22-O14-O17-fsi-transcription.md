# Spec — O14 + O17: transcribe the FSI model into the scored pipeline

**Date:** 2026-07-22 · **Source:** `outstanding-tasks.md` O14/O17 + the 2026-07-22 FSI finding · **Bar:** no guesses, data-driven, 95%.
**User decisions (2026-07-22):** O14 = **feed the scores**; O17 = **augment** (do NOT regenerate the price fan) **+ govern** the DCF/comps conflict.

**Core principle:** mirror `valuation_anchors.json` — the FSI initiation *authors* a cited, machine-readable transcription of what its model already computed; `coverage_qc` gates coherence; the scorers *consume* it. No new FSI analysis, no invented numbers. Every transcribed value cites a `coverage/*.md` section. Both artifacts are OPTIONAL (absent → today's GAAP/comps behavior, graceful).

---

## O14 — adjusted-financials bridge (feed the scores)

### New coverage artifact `coverage/adjusted_financials.json` (optional; present when the FSI flags material one-time items)
Transcribed from the FSI model (cited). Schema:
```json
{
  "core_eps_fwd": 10.85,           // FY2026E CLEAN operating EPS (model.md §projections_base)
  "consensus_eps_fwd": 14.25,      // the flattered NTM consensus (snapshot eps_ntm) — for GAAP-vs-core display
  "core_roe": 0.318,               // clean ROE, FY2025A NI / equity (pre-Q1'26 gain)
  "gaap_roe_ttm": 0.389,           // the inflated snapshot TTM ROE — for display
  "one_time_items": [{"label":"Q1'26 unrealized equity-securities gain","pre_tax_usd_m":37700,"period":"2026Q1","source":"coverage/research.md §Q1-2026 / model.json consensus_reference"}],
  "as_of":"2026-07-21",
  "citations": {"core_eps_fwd":"coverage/model.md §projections_base FY2026E","core_roe":"coverage/model.md §historical FY2025A + §balance-sheet"}
}
```
**Why clean FORWARD EPS (not core-TTM):** the FY2026 consensus (14.25) is flattered because FY2026 *includes* the actual Q1'26 quarter with the $37.7B gain; the FSI's clean FY2026E is 10.85. Using it is CITED and avoids inventing an after-tax TTM figure (which the FSI does not state — that would be a guess).

### `score_fundamental` — consume via a new `--adjusted <file>` flag (sibling of `--anchors`)
- **Valuation own-history component:** recompute `pe_fwd` from `last / core_eps_fwd` (GOOG: 351.37/10.85 = **32.4x** vs the flattered 24.65x). The own-history ratio `pe_fwd_core / pe_5yr_median` uses this. Disclose GAAP (24.65) **and** core (32.4) in the arithmetic string.
- **Quality ROE component:** use `core_roe` (0.318) in place of the snapshot's inflated `roe` (0.389). Disclose both.
- **Disclosure:** a `fundamental` module block `adjusted_financials_applied` listing GAAP vs core EPS/PE/ROE + the one-time items (for the report).
- **Guard/graceful:** absent `--adjusted` → today's exact behavior (byte-identical). Present → the two components use core, disclosed.
- **GOOG expected (verify):** pe_fwd 24.65→32.4 (own-history ratio 2.27→2.98, still >1.25 → same tier 1.2/8); core_roe 0.318 still ≥0.30 → roe 8/8. **Score-neutral on GOOG (tiers hold) but the disclosed multiples are corrected** — the mechanism moves the score for names where a one-time item crosses a tier.

### `coverage_qc` — coherence gate (when present)
`adjusted_financials.json` validates (required keys numeric+positive where applicable; every `citations`/`source` non-empty). Add as an OPTIONAL artifact (not in `_REQUIRED_ARTIFACTS`; checked only when present), mirroring `anchors_coherent`.

---

## O17 — driver scenarios + reverse-DCF + disagreement-state (augment; govern the conflict)

### New coverage artifact `coverage/scenario_drivers.json` (optional)
Transcribed from `model.md`/`model.json` + `valuation.md` (cited). Schema:
```json
{
  "scenarios": {
    "bear": {"eps_fy28": 10.73, "fcf_fy28_m": -313, "rev_growth_path":[0.115,0.090,0.070], "op_margin": 0.290},
    "base": {"eps_fy28": 14.16, "fcf_fy28_m": 41794, "rev_growth_path":[0.155,0.135,0.120], "op_margin": 0.330},
    "bull": {"eps_fy28": 17.18, "fcf_fy28_m": 80734, "rev_growth_path":[0.185,0.170,0.150], "op_margin": 0.360}
  },
  "dcf_reverse_inputs": {"pv_explicit_fcf_m": 312850, "pv_terminal_base_m": 1270900, "terminal_g_base": 0.03, "wacc": 0.1066, "net_cash_m": 49339, "diluted_shares_m": 12238},
  "citations": {"scenarios":"coverage/model.md §scenarios + §assumptions","dcf_reverse_inputs":"coverage/valuation.md §DCF (valuation bridge + sensitivity)"}
}
```

### Reverse-DCF (new `scripts/valuation_reconcile.py`, pure) — a DISCLOSURE, not a new price target
Solve for the implied terminal growth `g*` that makes the DCF equal the current price, holding the transcribed FCF/WACC fixed:
- `target_equity_m = last × diluted_shares_m` ; `target_ev_m = target_equity_m − net_cash_m`.
- `pv_terminal_needed_m = target_ev_m − pv_explicit_fcf_m`.
- The terminal-value ratio scales as `(1+g)/(wacc−g)`, so solve `pv_terminal_needed / pv_terminal_base = [(1+g*)/(wacc−g*)] / [(1+g_base)/(wacc−g_base)]` for `g*` (closed-form linear solve).
- If `g* ≥ wacc` (no finite solution) → emit `implied_terminal_g = null, note "market prices FCF above the model path (no finite g)"`.
- **GOOG expected (verify): g\* ≈ 0.081 (8.1%) vs base 3.0%** → disclose "the $351 price implies ~8% perpetual FCF growth vs the model's 3% — pricing the AI-capex earning out beyond the 5-yr window."
- Output `{implied_terminal_g, g_base, implied_vs_base, wacc}` for the report.

### Disagreement-state machine + GOVERN (score_composite)
- State from the disagreement already computed in `score_fundamental` (`|dcf_base − comps_mid|/comps_mid`, edge 0.25):
  - `≤ 0.25` → **CONSISTENT**
  - `> 0.25` → **UNRESOLVED_CONFLICT** (a prose reconciliation EXPLAINS but does not RESOLVE the numeric gap; the models still disagree — GOOG 0.86 → UNRESOLVED)
  - DCF/comps value ≤0 or missing → **MODEL_INVALID**
- **GOVERN (score_composite `thesis_conviction`):** on `UNRESOLVED_CONFLICT` — (a) the `variant` sub-score cannot claim the full valuation-split differentiation bonus (cap it at the "some" tier — the split is a *risk*, not pure edge), and (b) **prevent an A grade** (cap the composite grade at B until reconciled). Disclosed + PROVISIONAL + B9 falsifier. **GOOG expected: conviction variant capped, grade already B (no change to grade); disclose the state + the cap.**
- **Augment (NOT regenerate):** the bull/base/bear PRICE targets stay comps-derived; EV, trade plan, sizing are UNCHANGED. O17 adds disclosure (driver scenarios + reverse-DCF) + the conviction govern.

### `coverage_qc` — coherence gate (when present)
`scenario_drivers.json` validates (scenarios have numeric eps/fcf; dcf_reverse_inputs numeric; citations non-empty).

### Report disclosure
The report surfaces: the driver scenarios (a small table), the reverse-DCF line, and the disagreement state. Every number a bundle field → `number_provenance` stays PASS.

---

## Authoring the two GOOG artifacts (for validation)
The FSI *runtime* authoring (company-context / initiating-coverage transcribes them, cited) is a SKILL-doc update. For THIS build's validation, the two GOOG JSONs are authored by hand from the FSI artifacts already read (every value cited to `valuation.md`/`model.json` — no invention), placed in `trading_desk_GOOG/coverage/`.

## Build order
Author GOOG JSONs → **O14** (score_fundamental --adjusted + coverage_qc gate + disclosure), validate on GOOG → **O17** (scenario_drivers + valuation_reconcile.py + score_composite govern + coverage_qc gate + report disclosure), validate → SKILL-doc updates (authoring contract) → memory + outstanding-tasks. O14 and O17 both touch `coverage_qc` → build SEQUENTIALLY.
