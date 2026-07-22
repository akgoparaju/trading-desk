# Spec — O15: issuer/security master (full layer + schema bump)

**Date:** 2026-07-22 · **User decisions:** FULL layer + **schema bump**; **keep AV's EV** (do NOT self-compute EV).
**Bar:** no guesses, data-driven, 95%. **No live defect exists** (G1 fixed the live market-cap path) — this is correctness/completeness + formalization, so the hard rule is: **every scored number stays byte-identical to what G1/0.16.0 produces.** O15 adds a formal block + disclosure + the schema bump; it must not move a score.

## What it is
Formalize the security-level vs issuer-level split the snapshot currently leaves implicit. G1 routed market-cap ratios through the reconciled `price.mktcap`; O15 makes the whole split a first-class, QC-checkable `security_master` block and reconciles the issuer share count.

## 1. Schema bump — `SCHEMA_VERSION "0.3.3" → "0.4.0"`
`build_snapshot.SCHEMA_VERSION`. Update the ~3 tests that pin `"0.3.3"` + any golden snapshot fixture asserting `schema_version`. Note the bump in the methodology/CHANGELOG.

## 2. `security_master` block (new; snapshot top-level, sibling of `price`)
Built in `build_snapshot` from data already present (+ optional coverage). Pure. Shape:
```json
{
  "ticker": "GOOG",
  "share_class": "C",                       // parsed from overview Name ("... Class C") or null
  "class_shares_m": 5499.638,               // AV SharesOutstanding = ONE class
  "issuer_total_shares_m": 12202.0,         // see derivation below
  "issuer_diluted_shares_m": 12202.0,       // == total unless coverage gives diluted
  "issuer_mktcap": 4287617827000.0,         // == price.mktcap (the G1 reconciled cap)
  "mktcap_basis": "overview_authoritative", // == price.mktcap_basis
  "shares_source": "derived: issuer mktcap / class price",   // or "av_class_shares" / "coverage:valuation.md §DCF"
  "reconciled_to_filing": false,            // true only when a filing/coverage diluted-shares figure was used
  "other_listed_classes": ["GOOGL"]         // optional, disclosed; [] when unknown
}
```
### `issuer_total_shares_m` derivation (no guessing; disclosed source)
- **single-class** (`mktcap_basis == "reconciled_agree"` or `computed_only`): issuer_total = `class_shares_m` (this class IS the issuer); `shares_source="av_class_shares"`, `reconciled_to_filing` = (overview present).
- **multi-class** (`mktcap_basis == "overview_authoritative"`): if the bundle's `coverage/valuation_anchors.json` (or a new optional `coverage/*` shares field) carries a filing diluted-shares figure, use it (`shares_source="coverage:…"`, `reconciled_to_filing=true`); ELSE derive `issuer_total ≈ mktcap_overview / last` (`shares_source="derived: issuer mktcap / class price"`, `reconciled_to_filing=false`). GOOG derived ≈ 12,202M (vs coverage 12,238M — 0.3% apart; disclosed as derived).
- `other_listed_classes`: parse siblings from a small static class map (GOOG↔GOOGL, etc.) ONLY when known; else `[]` (never guess a ticker).

## 3. Rewire consumers to the canonical block (scored numbers UNCHANGED)
- The mktcap ratios already read `price.mktcap` (G1) = `security_master.issuer_mktcap` — keep them working; `price.mktcap`/`mktcap_basis` STAY (provenance + back-compat). No score change.
- Any **per-share ISSUER** metric that currently divides by `price.shares_diluted_m` (a one-class count) → use `security_master.issuer_diluted_shares_m`. Audit `shares_diluted_m` consumers (`grep`); most ratios use mktcap (already correct), so this is a small, targeted rewire — and for single-class names the two are equal (byte-identical). Disclose any that change.
- **EV stays AV** (`overview.EVToEBITDA`/`EVToRevenue`) — do NOT self-compute EV.

## 4. QC — `check_security_master` (in `qc.py`, part of the snapshot gate)
- The block validates (ticker present; `issuer_mktcap == price.mktcap`; `class_shares_m ≤ issuer_total_shares_m`; `shares_source`/`reconciled_to_filing` present).
- Coherence: `issuer_mktcap ≈ issuer_total_shares_m × representative_price` within a tolerance (representative price = `last`; for a derived multi-class total this is ~exact by construction — assert the round-trip).
- Never FAILS on a legitimately-derived (unreconciled) block — it discloses `reconciled_to_filing=false`, it doesn't block.

## 5. Golden-fixture migration
Bump the version pins to `0.4.0`; add `security_master` to golden snapshot fixtures / `build_snapshot` output tests; ensure existing snapshot-consuming tests tolerate the new block (additive). **Never weaken an assertion** — where a fixture now carries the block, pin its expected values. Re-run the FULL suite; a scored number that changed is a BUG (the rewire must be byte-identical for single-class names + for GOOG).

## Acceptance (real GOOG)
- snapshot `schema_version == "0.4.0"`; `security_master` present: share_class "C", class_shares_m 5499.638, issuer_total_shares_m ≈ 12202 (derived) [or 12238 if coverage wired], issuer_mktcap == price.mktcap (4.288e12), other_listed_classes ["GOOGL"].
- **Every evidence/composite score UNCHANGED vs 0.16.0** (fundamental/risk/composite identical) — O15 moves no number.
- `check_security_master` PASS; full suite green.

## Build
One Opus subagent (schema migration is judgment-heavy + fixture-risky). Sequential, not parallel. Validate by diffing the GOOG module scores before/after (must be identical) + the new block + the version bump.
