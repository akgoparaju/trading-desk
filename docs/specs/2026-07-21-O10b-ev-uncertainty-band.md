# Spec — O10b: EV-uncertainty band (PROVISIONAL v1.1.0)

**Date:** 2026-07-21 · **Branch:** `feature/o10b-ev-uncertainty-band`
**Backlog:** resolves the RESIDUAL half of O10 — LOW confidence should GOVERN the forecast
distribution, not just DISCLOSE. G4b/G5 already govern the *point* EV vs the hurdle; O10b governs the
*width* of the EV forecast under confidence.
**Bar:** no guesses, data-driven, real-data E2E before commit. **Status of the constants:** PROVISIONAL
— disclosed, versioned, and killable via a pre-registered B9 falsifier (project calibration philosophy:
"ship a cited, versioned, falsifiable default; ratify at B9").

---

## Problem

The decision contract's `EV_BELOW_HURDLE` blocker compares a single point estimate `ev_at_current` to
`total_return_hurdle`. A name whose point EV *just* clears the hurdle can pass the capital gate even when
the underlying scenario forecast is wide and the confidence is LOW — the exact case the point estimate
hides. O10 asked confidence to GOVERN, not merely annotate. O10b makes the forecast's *uncertainty*
part of the capital decision without minting any new input.

## Design

All inputs already exist in the scored bundle (no new data):

- `last` ← `snapshot.price.last`
- `scenarios` ← `module_composite.ev.scenarios` (`[{name, price_target, prob}]`)
- `ev_at_current`, `total_return_hurdle` ← `module_composite.ev.{ev_at_current, hurdle_total}`
- `conf_level` ← `module_composite.confidence.level`

### Band math (`decision_contract.compute_ev_band`)

```
r_i     = price_target_i / last - 1
r_bull  = max(r_i)
r_bear  = min(r_i)
spread  = r_bull - r_bear
k       = _EV_BAND_K[conf_level]           # confidence-keyed; see table
halfwidth = k * spread
ev_band = [ev_at_current - halfwidth, ev_at_current + halfwidth]
```

**Guard:** need ≥2 numeric-target scenarios, `last > 0`, and a numeric `ev_at_current`; `spread ≥ 0`.
Otherwise `ev_band = None`, all band fields `None`, and the new blocker is NOT added (never fabricated).

### The k table (PROVISIONAL constants — module-level, greppable, one-line-ratifiable)

```python
_EV_BAND_K = {"LOW": 0.25, "MEDIUM": 0.15, "HIGH": 0.05}
_EV_BAND_DEFAULT_LEVEL = "LOW"   # absent/unrecognized confidence -> conservative (widest) band
```

These are the review's first numbers, keyed on composite/data confidence as a **v1.1.0 proxy for
forecast uncertainty** (the bundle carries no per-name forecast sigma yet). k scales the bull–bear
scenario spread into a half-width; LOW confidence widens most, HIGH least. Not calibrated.

### Robustness verdict (`decision_contract.ev_robust_vs_hurdle`)

```
ev_robust_vs_hurdle = (ev_low >= hurdle) == (ev_high >= hurdle)
```

True when the hurdle verdict is the SAME at both band ends (the band clears — or fails — the hurdle
robustly); False when the band STRADDLES the hurdle (verdict flips across the interval). `None` when the
band or hurdle is absent.

### New blocker — `EV_NOT_ROBUST_UNDER_UNCERTAINTY`

Added to `capital_blockers` ONLY when:

```
ev_at_current >= total_return_hurdle   (the POINT would PASS the EV gate)
AND ev_low     <  total_return_hurdle   (the conservative band end FAILS it)
```

When the point EV is already below the hurdle, `EV_BELOW_HURDLE` already covers it — the new blocker is
NOT double-added. This blocker is ACTIVE (it contributes to `capital_eligible = len(blockers)==0`) but
is flagged PROVISIONAL via the contract's `provisional_note`. It targets exactly the case the point
estimate hides: a marginal pass whose LOW-confidence band straddles the hurdle.

### Contract fields (added; every existing field kept; `CONTRACT_VERSION` 1.0.0 → 1.1.0)

`ev_band` (`[low, high]` or None) · `ev_uncertainty_halfwidth` · `ev_uncertainty_k` ·
`ev_uncertainty_confidence_level` (the level whose k was used — may be the conservative LOW fallback) ·
`ev_robust_vs_hurdle` · `provisional_note`.

### Disclosure (`render_report.build_capital_status`)

When `ev_band` is present, one line is appended to the page-1 capital-status block (every number is a
contract field, percent-formatted with the existing `_pct` helper):

```
- **EV band ({conf}-confidence, provisional):** [{low%}, {high%}] around EV {ev%} · robust vs hurdle: {yes|no}
```

Omitted when `ev_band` is None. No other line changes.

### Number-provenance (`report_qc.check_number_provenance`)

The band endpoints and halfwidth are DERIVED (`ev_at_current ± k·spread`), so they are not bundle leaves.
`check_number_provenance` re-builds the same deterministic contract and folds those derived values into
its allowed set (`_contract_rendered_numbers`), so the rendered band traces to the contract rather than
orphaning. Everything else in provenance is unchanged.

---

## Falsifier (pre-registered, B9)

`decision-contract-v1.1.0 PROVISIONAL`. The k table and the `EV_NOT_ROBUST_UNDER_UNCERTAINTY` gate are
**falsified** if, across the calibration set:

1. realized forward returns for LOW-confidence names land within their disclosed `ev_band` at a rate
   inconsistent with a ~1-sigma interval — provisionally **<40% or >90% coverage ⇒ k is mis-scaled**; or
2. names blocked ONLY by `EV_NOT_ROBUST_UNDER_UNCERTAINTY` do NOT realize worse risk-adjusted outcomes
   than names that passed (the gate adds no discriminating information).

At B9 the k table is either re-fit to observed coverage or the gate is retired. **Not calibrated.**

---

## Real-GOOG validation

From the `2026-07-21` GOOG bundle (`last=351.37`; scenarios bull 436 / base 365 / bear 294;
`ev_at_current=0.059`; `hurdle_total=0.12`; `confidence.level=LOW`):

- `r_bull = 436/351.37 − 1 ≈ 0.24086`, `r_bear = 294/351.37 − 1 ≈ −0.16328`, `spread ≈ 0.40414`
- `k = 0.25` (LOW), `halfwidth ≈ 0.10103`
- `ev_band ≈ [−0.04203, 0.16003]`, `ev_robust_vs_hurdle = False`
- Point EV 0.059 < hurdle 0.12 → `EV_BELOW_HURDLE` fires and covers it → `EV_NOT_ROBUST_UNDER_UNCERTAINTY`
  is NOT added → `capital_blockers` unchanged at 4.
- Rendered line: `- **EV band (LOW-confidence, provisional):** [-4.2%, 16.0%] around EV 5.9% · robust vs hurdle: no`
- `report_qc` `number_provenance` PASS.

## Tests

- Band math + k selection + `ev_robust_vs_hurdle` unit tables; blocker trigger boundary (point ≥ hurdle
  & low < hurdle → blocker; point < hurdle → no blocker; robust pass → no blocker; no band → no blocker).
- GOOG fixture asserts the exact band endpoints/halfwidth/k/robust and that `EV_NOT_ROBUST` is absent.
- A marginal fixture (`ev_at_current=0.13 > hurdle`, LOW conf, band low < hurdle) proves the blocker fires
  and `capital_eligible=False` while `EV_BELOW_HURDLE` is absent.
- `build_capital_status` renders the band line for an ineligible GOOG-like contract; omits it when
  `ev_band` is None.

**Fixture note:** the `_composite_doc` render fixture now carries a HIGH-confidence rollup. Its wide
150/80 scenarios (70% spread) would straddle the hurdle at conservative LOW k and trip the new gate; a
HIGH-confidence name (k=0.05 → band [14%, 21%]) clears the hurdle robustly and stays eligible — i.e. an
eligible name IS robustly-confident under O10b. LOW-confidence / ineligible tests override this
explicitly. This is a behavioral consequence of the gate, not a weakened assertion.
