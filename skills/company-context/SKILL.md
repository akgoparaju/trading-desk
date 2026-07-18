---
name: company-context
description: Build the coverage-first company-context module for a ticker — a business + competitive + cases + risks brief with a live tape of what is moving the stock NOW, every substantive claim traced to a coverage artifact or cited web source via a findings[] citation registry. Sourcing is coverage_distilled (FSI initiation research/model/valuation reused) or web_compressed (FSI-absent floor, cited web research). Use when the user says "company context [ticker]", "build context module", or when full-trade-analysis Phase 2 needs the qualitative grounding. `scripts/report_qc.py --context` gates it: number_provenance over the prose + structural checks over the registry. Context is UNSCORED — it feeds fundamental's moat justification and grounds composite's conviction. v1.0.0.
---

# Company Context (Coverage-First Qualitative Layer)

Build one `module_context.json` for a ticker: the **business** (what they sell, revenue drivers, segments), the **competitive** position (moat evidence, competitors), the **live tape** (3-6 dated events answering *what is moving this stock NOW*), argued **bull/base/bear cases** with falsifiable conditions, and the **risks** — with **every substantive factual claim traced** through a `findings[]` citation registry that the prose references inline as "(C3)".

This is the qualitative grounding the rest of the desk was missing. It is **UNSCORED as a dimension** — it does not add a number to the composite. Its `findings[]` registry is consumed downstream: `score_fundamental`'s `--moat` justification cites finding IDs, and composite's conviction reasoning is grounded in the argued cases. Single-mapping is preserved: this module scores nothing, so it can never double-count a fact an evidence module already scored.

**Non-negotiables:**
- **Findings are the citation registry — write them FIRST.** Author `findings[]` before the prose. Every substantive factual claim in business / competitive / cases / risks must trace to a finding, and load-bearing prose references the finding inline "(C3)". IDs are sequential `C1..Cn`, unique.
- **Never invent a number.** Every number in the narrative prose must already exist in the snapshot or the coverage artifacts — the gate (`report_qc.py --context`) extracts every numeric token from the prose and checks it against the bundle, exactly like the report and pdf-slots gates. A fabricated figure orphans and fails. Fix the PROSE, never the numbers.
- **Never invent evidence.** A claim you cannot anchor to a coverage-artifact section or a cited URL does not go in. `coverage_distilled` cites the FSI artifact section; `web_compressed` cites the web source per the market-snapshot transcription rules.

Trigger phrases: "company context MU", "build context module for AAPL", or full-trade-analysis Phase 2 requesting the qualitative grounding for `<TICKER>`.

---

## Step 1 — Locate the ticker workspace + bundle

In the invoker's CWD, find the newest bundle for the ticker:

```bash
ls -dt ./trading_desk_<TICKER>/detail_reports_* ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

Newest first across both layouts (the new `trading_desk_<TICKER>/detail_reports_<date>/` bundle and the legacy `td_bundle_<TICKER>_<date>/`). **If NO bundle exists**, invoke the `market-snapshot` skill for `<TICKER>` first, then continue with the bundle it produces. The bundle's `snapshot.json` (single source of truth) supplies `meta.ticker` / `meta.as_of_utc` and every number the prose may cite; its `raw/news_sentiment.json` and `snapshot.events` feed the live tape.

---

## Step 2 — SOURCING (by mode)

Pick the mode by what is available, and disclose it. The mode is pinned in the module (`mode` field) and checked by the gate.

### `coverage_distilled` (FSI initiation present)

If an FSI equity-research **initiation** exists for the ticker (the `coverage/` artifacts — initiation research, financial model, valuation docs — under `trading_desk_<TICKER>/coverage/`), reuse them:

1. Read `trading_desk_<TICKER>/coverage/` artifacts (research.md / model.md / valuation.md, or whatever the initiation produced).
2. Distill the business / competitive / cases / risks from the coverage.
3. **Every claim cites the artifact section** — the finding's `source` is the exact section, e.g. `"coverage/research.md §Competition"` or `"coverage/model.md §Pricing"`.
4. Fold the **live tape** from the bundle's `raw/news_sentiment.json` (recent headlines) and `snapshot.events` (dated catalysts).

### `web_compressed` (FSI-absent floor)

If no FSI initiation exists, this is the floor: cited web research, **per the market-snapshot transcription rules** (`skills/market-snapshot/SKILL.md`, Step 2-ALT). Transcribe figures verbatim with units checked — never compute — and record the **source URL** as the finding's `source`. Any number you cite in prose must also exist in the snapshot (the gate checks the prose against the bundle, not against the web), so keep narrative numbers to figures the snapshot already carries; put the web-sourced qualitative facts in the claims and anchor them to the URL.

---

## Step 3 — Author `module_context.json` (findings-first)

Write the module to `<bundle>/module_context.json`. **The contract is pinned EXACTLY** (this shape is enforced structurally by the gate):

```json
{"skill": "company-context", "version": "1.0.0", "ticker": "<T>", "as_of": "<YYYY-MM-DD>",
 "mode": "coverage_distilled" | "web_compressed",
 "business": {"what_they_sell": "<str>", "revenue_drivers": ["<str>"], "segments": ["<str>"]},
 "competitive": {"position": "<str>", "moat_evidence": ["<str>"], "competitors": ["<str>"]},
 "live_tape": [{"date": "YYYY-MM-DD", "event": "<str>", "why_it_matters": "<str>"}],
 "cases": {"bull": {"narrative": "<str>", "conditions": ["<str>"]},
           "base": {"narrative": "<str>", "conditions": ["<str>"]},
           "bear": {"narrative": "<str>", "conditions": ["<str>"]}},
 "risks": [{"risk": "<str>", "why": "<str>", "anchor": "<coverage section | URL>"}],
 "findings": [{"id": "C1", "claim": "<str>", "source": "<coverage artifact section | URL>"}],
 "qc": null}
```

Author in this order (findings-first discipline):

1. **`findings[]` FIRST** — the citation registry. Each is `{"id": "C<n>", "claim": "<one factual assertion>", "source": "<coverage section | URL>"}`. IDs are sequential `C1..Cn`, unique, no gaps. The `claim` and `source` are both non-empty. This is the single artifact `score_fundamental --moat` and composite's conviction cite by ID.
2. **Prose referencing the IDs** — write business / competitive / cases / risks, and where a claim is load-bearing, reference its finding inline as "(C3)". At least one `C<n>` reference must appear in the cases or competitive prose (a registry no prose cites is dead weight — the gate fails it).
3. **Numbers in prose must exist in the snapshot / coverage** — the gate extracts every numeric token from the narrative and checks it against the bundle. Product/model names (`HBM3E`, `A100`) and finding refs `(C3)` are chrome the gate ignores; standalone figures are checked. If a number would orphan, it is not in the bundle — rephrase to a figure the snapshot carries, never invent one.
4. **`live_tape`: 3-6 dated entries** answering *what is moving this stock NOW* (the founding requirement). Each `{"date", "event", "why_it_matters"}`; every date is `YYYY-MM-DD` and **≤ `as_of`** (the gate checks parse + ceiling). Dated off the bundle's news / events.
5. **`cases`: argued narratives with FALSIFIABLE conditions** tied to scenario targets — bull / base / bear, each a real argument (not a hedge) whose `conditions[]` are the things that would confirm or break it. Reference the driving findings.
6. **`risks[]`**: each `{"risk", "why", "anchor"}` where `anchor` is the coverage section or URL grounding the risk (parallel to a finding's `source`).
7. **Sector-regime thesis (record as a normal cited finding).** If the coverage/context work surfaces a **structural break in HOW the sector is valued** — a re-rating regime, a cost-of-equity shift, a consolidation that resets the fair-value band (not just this company's own re-rating) — record it as an ordinary cited `findings[]` entry like any other claim (a `C<n>` with a coverage-section or URL source). This matters downstream: a **sector scale must cite such a finding as its `evidence`** (a scale never rests on an uncited assertion). The scale itself is authored/re-based by the **`scale-review`** skill under its adversarial gate — this module just supplies the cited evidence; it never writes or tunes a scale.
8. Leave **`qc": null`** — the gate stamps it on pass.

---

## Step 4 — Run the BLOCKING provenance + structure gate

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py \
  --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD> \
  --context ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>/module_context.json
```

Exit 0 is REQUIRED. The gate runs two checks and, on pass, stamps `qc: {"qc_passed": true, "checked_utc": ...}` INTO the file:

- **`number_provenance`** — every numeric token in the narrative prose (business / competitive / live_tape / cases / risks — NOT the findings registry or citation anchors) traces to the bundle, using the SAME allowed-set machinery as the report / pdf-slots gates. An orphan number FAILS.
- **`context_structure`** — findings IDs are `C<n>`, unique, sequential `C1..Cn`; every finding has a non-empty claim + source; at least one `C<n>` is referenced from cases / competitive prose; every `live_tape` date parses and is ≤ `as_of`; `mode` is one of the two legal values.

**Fix PROSE, never numbers.** A `number_provenance` orphan means you cited a figure not in the snapshot — rephrase to a printed figure or drop it (never invent). A `context_structure` failure is a registry / live_tape / mode defect — renumber the findings, add the missing source, cite a finding in the prose, correct a bad date, or fix the mode value. Re-run until exit 0. On a refresh, `--previous <old_bundle>` folds the prior bundle's values into the allowed set (a carried-forward figure stays in-bundle).

---

## Step 5 — Write `brief_context.md`

Write `<bundle>/brief_context.md`, **≤200 words**: the situation in plain language (the business in a sentence, the competitive position, and what the live tape says is moving the stock now), then the **top 3 findings** (their claims + sources). This is the human-readable distillation; it cites only claims already in the module.

---

## Step 6 — Output contract

Report to the user (and to any calling skill — full-trade-analysis Phase 2):
- **Module path** — `<bundle>/module_context.json`
- **Brief path** — `<bundle>/brief_context.md`
- **Mode** — `coverage_distilled | web_compressed`, with the one-line sourcing disclosure
- **Findings count** — `C1..Cn` (the registry size downstream consumers cite)
- **Live tape** — the count (3-6) and the single most-moving event
- **Gate verdict** — `CONTEXT QC: PASS` (both checks) with the stamp written

---

## Important Notes

- **Findings are the citation registry consumed downstream.** `score_fundamental`'s `--moat` justification and composite's conviction grounding cite these by ID ("C<n>"). A finding renumbering after those consumers have cited it is a breaking change — keep IDs stable across a refresh.
- **Context is UNSCORED (single-mapping preserved).** This module adds no dimension to the composite. It feeds the fundamental moat justification and grounds conviction only — a single mapping, so it never double-counts a fact an evidence module already scored.
- **Findings-first is not optional.** Write `findings[]`, then the prose that references them. Prose that makes a substantive factual claim with no finding behind it is unanchored — the discipline (and the gate's reference check) exists to prevent exactly that.
- **The gate is blocking.** A module that fails `report_qc.py --context` does not ship. Exit 0 is the ship criterion; the `qc.qc_passed` stamp is the attestation a downstream consumer reads.
- **Numbers live in the snapshot, evidence lives in the coverage/web.** The gate checks prose numbers against the bundle and requires findings to carry a source — the two provenance surfaces. A narrative number with no snapshot source, or a claim with no coverage/URL source, does not belong in the module.
- **Live tape is the founding requirement.** 3-6 dated entries answering *what is moving this stock NOW*, every date ≤ `as_of`. This is the "why now" the qualitative layer exists to answer — not a static company description.
- **Carried-forward on refresh with event re-affirmation.** On a re-run, carry the module forward and re-affirm the live tape (drop stale events, add new dated ones ≤ the new `as_of`), keeping finding IDs stable; re-run the gate with `--previous <old_bundle>`.
- **Read-only over the bundle except the two authored artifacts.** This skill writes `module_context.json` and `brief_context.md`; it never edits the snapshot or any evidence module JSON.
