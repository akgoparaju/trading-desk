# Spec — Wave 1: Confidence / Provenance Layer (`confidence-v1.0.0`)

**Date:** 2026-07-20 · **Status:** proposed · **Source:** `jutsu-trading-desk/docs/reviews/2026-07-20-development-priorities.md` (Wave 1, B23) + the three user-ratified decisions (per-module badge + composite roll-up; driver = source + depth + staleness; disclosure-first).

**Goal.** Every report — premium or degraded — ships a **per-module confidence badge + a composite roll-up**, computed **deterministically by script** (never LLM judgment) from three axes: **source** (where the data came from), **depth** (rubric maturity), **staleness** (print freshness / reuse). Level = the **weakest link** (`min`) of the three. This makes "no MCP → medium/low" a first-class, versioned artifact and turns rubric maturity into a visible, honest signal ("this dimension is still shallow" until its R-wave lands).

**Verification note (no-guessing).** Every field, insertion point, and QC fact below was read from source at the cited `file:line` (two Explore passes over the scorers, composite, render, QC, snapshot builder, and refresh planner). The one belief-laden input — the initial DEPTH table — is called out explicitly as a **governed, cited belief** (not a measurement) with a one-line rationale, so it is reviewable rather than smuggled in.

---

## Scope decisions (explicit, so they're not silently assumed)

1. **No snapshot schema change.** The layer reads existing fields only; it writes a `confidence` block into the module/composite JSONs. **B22's R1 event-fields (`implied_move`, `days_to_event`, `earnings_move_history`, `news_heat`, `short_campaign`) are DEFERRED to Wave 2**, where R1 actually consumes them — building them now would be unused fields whose exact shape depends on R1's (undesigned) scoring. This is a deliberate deviation from the priorities doc's "one 0.3.0 bump" idea, justified by the no-guessing mandate.
2. **Disclosure, not governor (v1.0.0).** Confidence is displayed; it does **not** change any score, weight, EV, or size. Whether a LOW dimension should also *act* (widen an EV band, cap size) is an open question deferred past v1.0.0.
3. **Roll-up = weakest included evidence dimension.** v1.0.0 uses `min` over the four *evidence* dimensions that carry data provenance (technical/fundamental/sentiment/risk); the thesis-conviction dimension (LLM-set flags, no data provenance) is **excluded** from the roll-up min. The "material-weight cutoff" refinement is deferred until one real degraded run calibrates it.

---

## The model (`confidence-v1.0.0`)

Each module (and the composite) gains a `confidence` block:
```json
"confidence": {
  "level": "HIGH|MEDIUM|LOW",
  "source":    {"level": "...", "why": "<digit-free tag>"},
  "depth":     {"level": "...", "why": "<digit-free tag>"},
  "staleness": {"level": "...", "why": "<digit-free tag>"},
  "rule": "min(source, depth, staleness)",
  "version": "1.0.0"
}
```
`level = min(source.level, depth.level, staleness.level)` with the ordering `LOW < MEDIUM < HIGH`. Every `why` is **word-only (no digits)** — a report_qc `number_provenance` constraint (report_qc.py:70 `_NUM_RE` matches leading-digit tokens; spell counts as words or omit them).

### SOURCE axis (per module)
Verified provenance signals: `meta.data_mode` (run-level), `fundamentals.web_transcribed_fields` (only per-field source list, fundamental-only; build_snapshot.py:616), `technicals.series_source` (stooq flag; build_snapshot.py:404), and the **one by-design web scored input: `short_interest`** (sentiment; COVERS build_snapshot.py:65). Rules:
- **HIGH** — `data_mode == alpha_vantage` AND this module has no web-by-design / web-transcribed scored input AND (technical) `series_source` is not stooq.
- **MEDIUM** — `data_mode == av_free_degraded`, OR the module has a web-by-design scored input (**sentiment always**, via `short_interest`), OR `fundamentals.web_transcribed_fields` is non-empty (fundamental), OR technical `series_source == stooq`.
- **LOW** — `data_mode == web_fallback`, OR the module's core scored inputs are absent/stood-aside.
- Per-module `why` tags (digit-free): technical `"AV premium"`/`"stooq series"`; risk `"AV premium"`; sentiment `"AV premium; web short-interest"` (→ MED); fundamental `"coverage + AV"` / `"web-transcribed fields present"` / `"web fallback"`.

### DEPTH axis (per module) — **GOVERNED BELIEF, cited, reviewable**
Depth = "has this module received its institutional-depth pass?" Keyed on `(module, rubric_version[, mode])`. Initial table (rationale: the quality review's Part 1 depth-asymmetry finding — fundamental v1.2 anchored is two versions ahead; technical/sentiment/risk are still mechanical snapshot-band scorers awaiting R5/R3/R1):

| Module | Current | Depth | Promotes to HIGH when |
|---|---|---|---|
| fundamental | `coverage_anchored_pass` (v1.2 anchored) | **HIGH** | (already) |
| fundamental | `compressed_snapshot_pass` | **MEDIUM** | coverage present (anchored) |
| technical | rubric 1.0.0 | **MEDIUM** | rubric 1.1.0 (R5/B28: regime-conditional) |
| sentiment | rubric 1.0.0 | **MEDIUM** | rubric 1.1.0 (R3/B25: positioning dynamics) |
| risk | rubric 1.0.0 | **MEDIUM** | rubric 1.1.0 (R1/B24: event-aware) |

Depth never returns LOW (a scored module is ≥ MEDIUM). The table lives in `confidence.py` as an explicit, commented, versioned map citing the quality review; each R-wave bumps one row (a one-line, disclosed change). `why` tags (digit-free): `"anchored coverage"`, `"snapshot pass"`, `"pre-event-aware"` (risk), `"pre-regime"` (technical), `"pre-positioning-dynamics"` (sentiment). **This is the one reviewable judgment in the spec — flagged for user sign-off; a different initial assignment is a one-line table edit.**

### STALENESS axis (run-level, applied to price-sensitive modules)
Inputs: `meta.latest_trading_day` (QF2; build_snapshot.py:1156, null when absent), `meta.as_of_utc` (build_snapshot.py:1154), and — on a refresh — `refresh_plan.json` (`groups[g].action/age_days`, `iv_history.action`, `events.judgment_review_required`; refresh_plan.py:253-279/466/499). Rules:
- **HIGH** — `latest_trading_day` present AND `== as_of` date AND (no refresh, or no in-window reuse of a price group).
- **MEDIUM** — weekend/stale print (`latest_trading_day != as_of` date), OR a refresh reused a price group within its window.
- **LOW** — `latest_trading_day` is null (freshness unverifiable), OR a reused group is at/over its staleness window.
- Applied to the price-sensitive modules (technical, risk, sentiment). **Fundamental staleness = coverage freshness** (current vs `model-update`d — already gated upstream); v1.0.0 treats fundamental staleness as HIGH when coverage is current, else MEDIUM. `why` tags (digit-free): `"fresh print"`, `"weekend print"`, `"reused in-window"`, `"freshness unverifiable"`, `"coverage current"`.

### Roll-up (composite)
`composite.confidence.level = min` over the four evidence dimensions' `confidence.level` that are **present and not renormalized-away**; `why` names the weakest dimension(s), e.g. `"MEDIUM — risk pre-event-aware; sentiment web short-interest"`. A dimension the composite excluded (renormalized) → its confidence is `n/a` and is skipped in the min (renormalization contract). Thesis-conviction is excluded from the min.

---

## Implementation

### 1. NEW `scripts/confidence.py` — the rubric (single source of truth)
- `CONFIDENCE_VERSION = "1.0.0"`.
- `DEPTH_TABLE` (the governed-belief map above, commented + citing the quality review) and a `_SOURCE_BY_DESIGN_WEB = {"sentiment": ["short_interest"]}` map.
- `compute_module(module_doc, snapshot, bundle_dir=None) -> confidence_dict`: pure function. Reads `module_doc.get("skill"/"rubric_version"/"fundamental_mode"/"renormalized")`, `snapshot.meta.*`, `snapshot.fundamentals.web_transcribed_fields`, `snapshot.technicals.series_source`; optionally loads `refresh_plan.json` from `bundle_dir` for the staleness reuse signal (absent on fresh runs → staleness from print freshness only). Returns the block above. Deterministic; **zero arithmetic beyond the ordinal `min`**.
- `rollup(dimension_confidences: list[dict|None]) -> confidence_dict`: `min` over non-None evidence dimensions, names the driver.
- Ordinal helper `_LEVELS = {"LOW":0,"MEDIUM":1,"HIGH":2}` and `_min_level(*levels)`.
- All `why` strings digit-free (assert in tests).

### 2. Evidence scorers — add `confidence` to each module doc (single-mapping: each module owns its own)
Each scorer, at its doc-construction site, calls `confidence.compute_module(doc, snapshot, bundle_dir)` and adds `"confidence": <block>` to the emitted doc:
- `scripts/score_technical.py` — doc at lines 519-537.
- `scripts/score_risk.py` — doc at lines 725-747.
- `scripts/score_sentiment.py` — doc at lines 696-730.
- `scripts/score_fundamental.py` — doc at lines 963-996 (pass `fundamental_mode` so depth reads anchored vs compressed).
Each scorer already has `snapshot` + its `doc` + the bundle path in scope.

### 3. `scripts/score_composite.py` — read per-module confidence + roll-up
- At line 335 (where it reads `module_scores[name].get("score")`), also read `.get("confidence")`; carry it into each `dimensions` row (rows built at lines 352-360, which already hold `name/score/weight/weight_renormalized/contribution/source`).
- Compute `confidence.rollup(...)` over the four evidence dimensions and attach a top-level `confidence` to the composite doc (assembled ~line 530-558, after `grade`). A renormalized-away dimension contributes `None` to the roll-up.

### 4. `scripts/render_report.py` — badges (word-only, QC-safe)
- `_score_headline(label, module, slot_suffix)` (line 348): append the per-dimension badge from `module.get("confidence")`, e.g. `### Technical — 72/100 (rubric v1.0.0) · ● HIGH (AV premium)`. Glyphs `● HIGH` / `◐ MEDIUM` / `○ LOW`. One edit covers all four dimensions.
- `build_the_call(composite)` (line 205): append the roll-up badge from `composite.get("confidence")`, e.g. `**B — Buy/Add** (composite 68/100, balanced profile) · Confidence: ◐ MEDIUM (risk pre-event-aware)`.
- **Footer/methodology:** add `confidence-v1.0.0` to the rubric-version footer the renderer already emits (rubric-version-travels).

### 5. `scripts/render_pdf.py` — docket badges (no QC interaction, keep consistent)
- `_draw_evidence_section` (line 1271) / the `section_head` at line 1285: append the per-dimension badge. The composite header on the exec/detail page: append the roll-up badge. (report_qc's number_provenance does not scan PDF content; still keep tags digit-free for consistency.)
- The METHODOLOGY appendix (fully scripted) gains a one-line `confidence-v1.0.0: min(source, depth, staleness)` convention note + the depth table, rendered from `confidence.py` constants so it cannot drift.

### 6. QC
- **No `report_qc.py` change required** (verified: HIGH/MED/LOW + word-only tags pass number_provenance/word_cap/no_empty_slots/footer_integrity). 
- **Add a regression test** asserting a rendered report carrying badges passes `report_qc.py` (guards against a future digit-bearing tag). Optionally (deferred) a `no_missing_confidence` check that every dimension headline carries a badge — not required for v1.0.0.

---

## Tests (`tests/test_confidence.py` + integration)
- **Unit (`test_confidence.py`):** each SOURCE rule (premium→HIGH; degraded→MED; web_fallback→LOW; sentiment→MED via short-interest; fundamental web-transcribed→MED; stooq series→MED); each DEPTH row (table lookups incl. fundamental anchored vs compressed); each STALENESS rule (fresh/weekend/null/reuse); `min` combiner (LOW dominates); rollup (min over evidence dims, renormalized dim skipped, thesis-conviction excluded); **every `why` string is digit-free** (regex assert).
- **Scorer integration:** each `module_*.json` now carries a well-formed `confidence` block (extend the existing per-scorer tests).
- **Composite:** `module_composite.json` carries a roll-up `confidence`; a run with one renormalized evidence module rolls up over the remaining three.
- **Render/QC:** a rendered report with badges passes `report_qc.py` exit 0 (number_provenance clean); the badge text appears on each dimension headline and the call line.
- **Suite:** full suite green (re-pin any test asserting exact module/composite doc key sets).

---

## Constraints honored
- **Script-is-rubric / zero LLM arithmetic** — `confidence.py` is the versioned rubric; the only operation is an ordinal `min`.
- **Single-mapping** — each module computes and owns its own `confidence`; the composite only reads + rolls up.
- **Single-snapshot** — derived from existing snapshot fields; no fetch, no schema change.
- **Rubric-version-travels** — `confidence-v1.0.0` in the footer + methodology page.
- **Renormalization contract** — an excluded dimension's confidence is `n/a` and skipped in the roll-up.
- **Governed belief** — the DEPTH table is explicit, cited, versioned, and reviewable; each R-wave bumps one row with disclosure.

## Sequencing (execution is coupled — mostly serial)
1. **`confidence.py` + `test_confidence.py`** first (nothing imports it yet) — the foundational rubric. **Opus** (the axis logic + the governed-belief table warrant care).
2. **Wire into the 4 scorers + composite** (they import `confidence.py`) — **Sonnet**.
3. **Render badges (md + pdf) + footer/methodology + render/QC tests** — **Sonnet**.
Steps 2 and 3 depend on 1; 3 depends on 2 (composite must emit the roll-up before render shows it). Run as a short chain, full suite green at the end.

## Definition of done (Wave 1)
Every `module_*.json` and `module_composite.json` carries a `confidence` block; the md report and docket show a per-dimension badge + a composite roll-up; risk/technical/sentiment read MEDIUM-depth (honest, pre-R-wave) while fundamental-anchored reads HIGH; a degraded (`web_fallback`) run rolls up to LOW; `report_qc` passes with badges present; `confidence-v1.0.0` travels in the footer + methodology; full suite green; the DEPTH-table initial values confirmed with the user (the one reviewable belief).
