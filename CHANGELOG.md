# Changelog

## Unreleased — 2026-07-21 · Wave 4B: event-vol-aware options (`options-v1.1.0`, PROVISIONAL)

R4 — the options module goes event-vol-aware (Philosophy A). The options module is the expression
layer (feeds `trade_plan.expression`), so no composite score moves. Snapshot schema **0.3.2 → 0.3.3**.
Suite: 1446 passed.

- **Black-Scholes pricer** (`chain.bs_price`, via `math.erf`, no scipy) — **verified against reference
  values** (S=K=100,T=1,r=0,iv=0.2 → call 7.9656; put-call parity 7e-15; symmetry exact). This is new
  infrastructure the codebase had none of.
- **Event-vol extraction** (`chain.event_implied_vol`) — desk variance-additivity across the bracketing
  expiries, both horizons measured from `as_of` (`iv_post²·T_post − iv_pre²·T_pre`), isolating the
  earnings-day implied move. Real-data BE: 32.5% event move over the 7/24→7/31 print window.
- **Ex-earnings RV** (`indicators.realized_vol_ex_earnings`) — strips print-day returns from the RV
  lookback (annualized by √252, documented), so the vol gate can compare IV to a clean realized vol.
- **IV-crush simulation** — re-prices each candidate leg at `iv_post = iv_leg × 0.62` (labeled
  provisional constant, cited ~38% avg crush) at ±1σ/±2σ scenario spots; structure-level
  `crush_ev = Σ prob×PnL`; **event-window structures with crush_ev ≤ 0 are declined** ("negative
  crush-adjusted EV"). Vega math now determines structure survival, priced not narrated.
- **Skew-informed routing** — `skew_verdict` (±0.04 provisional) at the selected working expiry routes
  to selling the rich wing; condor widens the cheap wing. Real-data BE: rr 0.224 → puts_rich →
  prefers selling puts.
- **Candidate breadth** — matrix expansion + next-expiry fallback + adjacent-delta retry; emits
  `candidates_tried`. The review's "declined ONE candidate then stood aside" becomes "tried N, all
  declined for <reasons>." Real-data BE: candidates_tried 2, expiry-fallback fired, then honest
  stand-aside on a genuinely illiquid chain.
- Expired-expiry drop was already shipped (QF3). rubric → 1.1.0 PROVISIONAL; falsifier in the SKILL.

## Unreleased — 2026-07-21 · Wave 4A: technical regime + institutional levels (`technical-v1.1.0`, PROVISIONAL)

R5 — regime-conditional technicals (Philosophy A). **Top-level weights UNCHANGED** (Trend 30 /
Momentum 25 / Structure 25 / Volume 20); regime enters as a GUARD, not a new factor. Snapshot schema
**0.3.1 → 0.3.2**. **Technical confidence DEPTH promotes MEDIUM → HIGH** (source is AV-premium, not
web-capped — the honest promotion). Suite: 1409 passed.

- **New pure-OHLCV snapshot fields:** `technicals.adx14` (Wilder ADX — verified vs reference:
  trending→100, choppy→3.7), `technicals.stage` (Weinstein 1/2/3/4 from the MA stack + slopes),
  `technicals.ad_line_slope` (Chaikin accumulation/distribution), `technicals.upvol_ratio`, and
  anchored VWAPs (`vwap_52wk_high`, `vwap_earnings`) added to the S/R ladder as institutional
  cost-basis levels.
- **Regime-conditioned momentum** — ADX < 20 (choppy) halves the MACD sub-component; stage 4
  (declining) caps the RSI healthy-band bonus. Guard modulates points; band shapes unchanged; null
  guard → no modulation (a pre-4A snapshot scores identically to v1.0.0).
- **Volume factor enriched** — re-split to extension 10 / vol-regime 5 / A-D-line 3 / upvol 2
  (within-factor renormalization when a sub-signal nulls; factor never zeroed).
- **Deferred (data-blocked, disclosed):** sector-relative RS needs a sector-ETF fetch not in any
  manifest — noted in the SKILL as the next R5 increment. SPY-relative RS already lives in sentiment.
- rubric → 1.1.0 PROVISIONAL; falsifier in the SKILL. Real-data BE: stage 3 (topping — price < ma50,
  ma200 still rising), adx 21, A/D slope −0.14 (distribution), upvol 0.44 — all correctly bearish;
  technical 54.19 → 51.19; depth badge HIGH, overall MEDIUM (staleness axis honest on the 7/17 print).

## Unreleased — 2026-07-21 · Wave 3B: composite/trade-plan honesty (`composite-v1.1.0` + `tradeplan-v1.1.0`, PROVISIONAL)

R2 — the orchestration-layer honesty fixes (Philosophy A). **No evidence-module score changes**;
these are disclosure flags + presentation. Both rubrics → 1.1.0. Suite: 1356 passed.

- **Base-rate-anchored scenario probabilities** (`score_composite`) — reads `events.earnings_move_history`
  (Wave 2A), computes the empirical bull/base/bear frequencies (±5% material-move bins; N≥4 or skip),
  and flags any LLM scenario prob deviating >25pp from its base rate (`flags.base_rate_check`, soft/
  disclosed). Real-data BE: base rates 0.50/0.375/0.125 from its 8 actual earnings moves vs the LLM's
  0.30/0.45/0.25 — within tolerance, not flagged (Superforecasting discipline, review R2).
- **Auto-tension gate** (`score_composite`) — `composite.tension` was a null LLM slot that never fired
  (review #14). Now auto-populated when the evidence-dimension spread > 25pts. Real-data BE:
  "sentiment 58.75 vs fundamental 30.75 — 28-pt evidence spread" (previously shipped blank).
- **Bull-target triangulation** (`trade_plan`) — conditionally loads `coverage/valuation_anchors.json`
  and sets `bull_target.level = min(scenario_PT, comps_high)` (raw preserved in `scenario_raw`,
  `dcf_bull` shown as reference), so the bull target no longer exceeds the desk's own coverage.
- **Kelly headline** — `sizing.headline` surfaces f* WITH its entry + cap context (f* is already
  entry-conditioned in code; this stops a bare 36.7% appearing beside a 4% cap). **Expression** now
  leads with the executable stock leg when options are gated out (options-tilted text preserved).
- Both rubrics → 1.1.0 PROVISIONAL; falsifiers pre-registered in the composite + trade-plan SKILLs
  (base-rate/tension thresholds; the min-triangulation formula). Provisional defaults: ±5% bins,
  25pp deviation, N≥4, 25-pt tension spread, min(PT, comps_high).

## Unreleased — 2026-07-21 · Wave 3A: sentiment positioning dynamics (`sentiment-v1.1.0`, PROVISIONAL)

R3 — the positioning/flow/news signals a desk reads, scored (Philosophy A: provisional versioned
defaults + pre-registered falsifier, ratify after B9). **Top-level weights UNCHANGED** (Street 25 /
Revisions 20 / Smart-money 20 / Positioning 20 / Momentum 15) — only sub-components re-split, so
score movement is small on a provisional wave. Snapshot schema **0.3.0 → 0.3.1**. Suite: 1322 passed.

- **Positioning factor re-split** (SI 8/OI-P/C 6/IV 6 → SI+DTC 6 / OI-P/C 4 / volume-P/C 3 / skew 4 /
  IV 3): scores the 25Δ risk-reversal skew (`sentiment.skew_25d_30d`, promoted from options),
  volume-vs-OI P/C flow (`put_call_ratio_full_chain_volume`), and days-to-cover
  (`sentiment.dtc = si% × shares / (adv/price)`; float-caveat disclosed).
- **Smart-money factor** — insider sub-component gains Cohen/Malloy/Pomorski routine-vs-opportunistic
  classification (`sentiment.insider_classification`) that activates **only with ≥24mo of per-insider
  history** and otherwise **degrades gracefully to the unchanged v1.0.0 net-90d logic**. The
  `INSIDER_TRANSACTIONS` fetch window is widened 90d → 36mo (SKILL) so future runs carry the history.
- **Street-view factor** — folds in `news_heat`: an EWMA of relevance-weighted `ticker_sentiment_score`
  over the news feed, **half-life 3d** (cited default; the feed was previously loaded and ignored).
  This is the review's B17 "score the news DYNAMICS, not the vendor number." Real-data BE: news_heat
  ewma **−0.62** — the Hunterbrook short cluster the review said was "collected then discarded" now
  scores the lowest band.
- **Disclosure + falsifier:** rubric → 1.1.0; module note stamps PROVISIONAL; falsifier recorded in the
  SKILL. **Confidence stays MEDIUM** — sentiment SOURCE is structurally web-dependent (short_interest),
  so a deeper rubric does not (and should not) promote the badge to HIGH. Honest by construction.
- Real-data BE: sentiment 55 → 58.75, composite 45.28 → 45.88, **grade C held**; skew 0.22 (fear),
  dtc 1.96 (liquid, not crowded), insider classifier gracefully inactive (63-day AV window).

## Unreleased — 2026-07-20 · Wave 2 Part A: event-aware risk DATA + disclosure (R1, scoring gated)

The deterministic half of R1. The event/tail signals a desk actually uses are now COMPUTED and
SURFACED in the risk module — closing the "collected then discarded" gap for the deterministic
signals — while the SCORING re-weight stays gated on a calibration decision (Part B, below).
**No risk score moves** (byte-identical-score regression pinned); risk rubric_version stays 1.0.0.
Snapshot schema **0.2.2 → 0.3.0**. Suite: 1220 passed, 26 skipped.

- **Snapshot 0.3.0 event/tail fields** (deterministic, additive, null-safe):
  - `events.days_to_event` — days to next earnings.
  - `events.implied_move` — the front-expiry earnings straddle move (surfaced from the existing
    `sentiment.implied_move_next_earnings_pct`).
  - `events.earnings_move_history` — the ticker's own last-8-quarter earnings-day reactions
    (from `quarterlyEarnings[].reportedDate` × daily closes; report-spanning close-to-close
    convention, robust to BMO/AMC).
  - `events.implied_move_vs_own_history_pctile` — where the current implied move sits vs the name's
    own reaction history (BE validated: 27.2% implied = 100th pctile — bigger than any of its last 8).
  - `technicals.overnight_gap` — `{mean_abs, p95_abs, max_abs, excess_kurtosis, jump_count_2sigma, n}`
    from adjustment-consistent overnight gaps (the raw open is scaled by the day's
    `adjusted_close/close` factor so a split can't manufacture a spurious gap — a general-case
    correctness guard; BE, which never split, is unaffected and its extreme tails are real).
- **Risk module surfaces the data UNSCORED**: `tables.event_context` + `tables.tail_context`, read
  verbatim from the snapshot (zero arithmetic in the module); new paths flagged CONTEXT-ONLY for
  single-mapping; module note `"event-context v1 (unscored) — scoring gated on calibration"`.
- **Presentation fix**: the long-horizon `dcf_bear` / suspect valuation floor is relabeled
  `"long-horizon anchor (not a swing level)"` in the downside map (the review's "−97.7% anchor in a
  swing map"). Numeric level unchanged.
- **Governance doctrine** added to the risk SKILL: "Risk is a gate/governor, never a reward input —
  conviction never loosens a risk parameter"; `top_risk` directed to name an event ≤30d out and cite
  the event_context figures.
- **GATED (Part B, needs a user decision — not built):** the scored re-weight (`risk-v1.1.0` — a
  calibration decision, B8/B9), `sentiment.news_heat` (EWMA parameters), and `sentiment.short_campaign`
  (short-seller entity list + detection heuristic — a risk gate acting on a false positive needs
  sign-off). Confidence DEPTH for risk stays MEDIUM until the scored re-weight lands.

## Unreleased — 2026-07-20 · Wave 1: confidence / provenance layer (`confidence-v1.0.0`)

Every report now ships a **per-module confidence badge + a composite roll-up**, computed
DETERMINISTICALLY (never LLM judgment) as `min(source, depth, staleness)` — the weakest link.
This makes "no MCP → medium/low" a first-class, versioned artifact and turns rubric maturity
into a visible honesty signal. **Disclosure only — no score, weight, EV, or size changes.**
No snapshot schema change (reads existing fields incl. QF2's `meta.latest_trading_day`); R1's
event fields deferred to Wave 2 with R1. Suite: 1187 passed, 26 skipped.

- **New `scripts/confidence.py`** (`CONFIDENCE_VERSION = "1.0.0"`): `compute_module()` + `rollup()`,
  pure/ordinal (the only arithmetic is a level `min`). Three axes:
  - **SOURCE** — AV-premium/script → HIGH; degraded, a web-by-design scored input (sentiment's
    `short_interest`), fundamental web-transcribed fields, or stooq series → MEDIUM; `web_fallback`
    or absent core inputs → LOW.
  - **DEPTH** (a governed, cited belief — the reviewable one) — fundamental anchored = HIGH,
    compressed = MEDIUM; technical/sentiment/risk at rubric 1.0.0 = MEDIUM (promote to HIGH at 1.1.0
    when their R-wave lands: R5 technical / R3 sentiment / R1 risk). Cites the 2026-07-19 quality review.
  - **STALENESS** — `as_of == latest_trading_day` → HIGH; weekend/stale print or in-window refresh
    reuse → MEDIUM; freshness unverifiable (null `latest_trading_day`) or over-window reuse → LOW.
- **Wired into all four evidence scorers + the composite.** Each `module_*.json` now carries a
  `confidence` block; `score_composite` reads them, carries them into the dimension rows, and rolls
  up `min` over the four evidence dimensions (thesis-conviction excluded; a renormalized-away
  dimension skipped). 51-test `tests/test_confidence.py` + extended scorer/composite tests.
- **Rendered as badges** (`● HIGH` / `◐ MEDIUM` / `○ LOW`, digit-free tags to stay QC-clean):
  per-dimension in `render_report._score_headline` + the roll-up on the call line; same in the
  docket (`render_pdf`) with a scripted `confidence-v1.0.0` note + depth table in the METHODOLOGY
  appendix. `confidence-v1.0.0` travels in the footer. `report_qc` unchanged (verified badges pass
  number_provenance); a regression test pins that.

## Unreleased — 2026-07-20 · Wave 0: efficiency & correctness hardening

Version TBD (release gated with the user). The efficiency audit + quality review's quick
fixes, executed as Wave 0 — cheap, pure-win speed and honesty fixes that move NO score,
band, or weight, landed first so the expensive Wave 1–4 work iterates on a faster machine.
Suite: 1121 passed, 26 skipped.

- **IV-history sampling collapsed from ~54 serial tool round-trips to a batch (B18).** New
  `scripts/build_iv_history.py` (self-contained: `scripts.chain` + stdlib): reads a
  `raw/iv_samples.json` manifest of `{date, chain_file}`, derives spot from the daily file's
  **nominal `"4. close"`** (never adjusted — historical option strikes are nominal), picks the
  ~30-DTE expiry, computes ATM IV via `chain.atm_iv`, merges/dedupes the cache, and deletes
  consumed chains on success. `market-snapshot` Step 4 now issues the ~26 `HISTORICAL_OPTIONS`
  fetches as parallel tool-use + one script call instead of 26 fetch→one-liner→`rm` turns;
  `refresh-analysis` points at the same script. **Also a correctness gain:** spot was previously
  an unspecified LLM-supplied arg (non-deterministic near strike boundaries); it is now
  deterministic. Exit 2 (not a silent adjusted-close fallback) if `"4. close"` is absent.
  `tests/test_build_iv_history.py` (8 tests) asserts correctness at the raw spot.
- **Evidence-scorer model tier pinned explicitly (B20).** `full-trade-analysis` and
  `refresh-analysis` now direct the orchestrator to set `model: sonnet` on the
  evidence / company-context / market-snapshot dispatches (and `model: opus` at coverage-init),
  not rely on inheritance — a bounded, script-driven scorer must never run on the orchestrator's tier.
- **`meta.latest_trading_day` + weekend/stale-print honesty (QF2).** `build_snapshot` captures
  the quote's `"07. latest trading day"` into `meta.latest_trading_day` (null when the source
  lacks it); `qc.py` attestation appends a **non-blocking** note when it differs from `as_of`.
  Snapshot schema **0.2.1 → 0.2.2** (additive). This field is the input the Wave-1 confidence
  layer reads for its staleness axis.
- **Expired expiries no longer leak into `expected_moves` / `atm_iv_by_expiry` (QF3).** New
  `chain.future_expiries(contracts, as_of)` (keeps `expiries()` pure); `build_snapshot` filters
  to future expiries at the single minting site. `options-strategy` already dropped past expiries
  downstream, so no change there.
- **Catalyst-calendar honesty (QF4).** `render_report.build_catalyst_calendar` labels past-dated
  rows `(past)` and replaces empty Note cells with `—`. Presentational only.
- **`revisions_90d` null now explained, and loud pre-earnings (QF5).** `build_snapshot` records a
  `revisions_null_reason` (`no_future_fy_row` vs named absent fields); `score_sentiment` surfaces a
  prominent warning when revisions null within 14 days of earnings instead of silently
  renormalizing. **Traced root cause:** the AV parser keys are correct — the null is an upstream AV
  data gap (JSON `null` on future-FY rows), not a parsing bug, so no parser change was made.
- **QF1 verified already-closed (no change).** The `fundamental_mode` mislabel was fixed in 0.12.1
  (commit 7f95a2f); the flagged BE artifact was 0.12.0. Confirmed the predicate cannot diverge and
  the regression test `test_anchored_run_discloses_anchored_mode` covers it — dropped from Wave 0.

## 0.13.0 — 2026-07-19 · Full-FSI-depth coverage contract

Depth becomes the DEFAULT, CHECKABLE, and PROVENANCE-RECORDED. The user demanded
FULL FSI initiation depth three times; the shipped "proportionate depth" override in
full-trade-analysis Phase 0.5 was implementation drift the user never chose. Project
law — "an instruction without a required artifact is a suggestion" — so this release
gives depth its required artifact: a script gate + a provenance manifest. Shallow
coverage now survives ONLY as an explicit per-run user request, disclosed everywhere.

- **Coverage QC gate + provenance manifest (B1/B2).** New `scripts/coverage_qc.py`:
  `--coverage <dir> [--mode full|shallow] [--waive check:reason ...]` runs eight
  house-style checks (result dicts `{check, passed, detail}`, `--waive`, PASS/FAIL/
  WAIVED table, exit 0 all pass-or-waived / 1 otherwise — all mirrored from
  `report_qc.py`):
  - `artifacts_present` — research.md, model.md, valuation.md, valuation_anchors.json,
    coverage_manifest.json all exist.
  - `manifest_shape` — the manifest carries `depth_mode` (`full` | `shallow
    (user-requested)`), `skills_invoked` (list of `{skill, args_summary}`),
    `data_endpoints`, `artifacts`, `generated_utc`; the `--mode` flag and the recorded
    `depth_mode` must AGREE (a mismatch is a provenance lie → fail).
  - `fsi_invoked` — the equity-research `initiating-coverage` skill was invoked.
  - `subskills_invoked` — (full only; auto-pass shallow) ≥2 distinct
    `financial-analysis:*` sub-skills (`3-statement-model` / `dcf-model` /
    `comps-analysis`).
  - `research_depth` — all NINE real FSI Task-1 sections present (Company Overview,
    Company History, Management Team, Products & Services, Customers & Go-to-Market,
    Industry Overview, Competitive Landscape, Market Opportunity, Risk Assessment),
    each ≥150 words, total ≥2500 (full) / ≥800 (shallow).
  - `model_depth` — 3-statement structure (income / balance / cash-flow) + ≥3 forward
    fiscal years (full) / ≥1 (shallow); statements required in both modes.
  - `valuation_depth` — DCF naming wacc/discount-rate AND terminal growth, a comps
    table with ≥4 ticker rows (full) / ≥2 (shallow), and bull/base/bear (or
    low/mid/high) scenario values.
  - `anchors_coherent` — `valuation_anchors.json` validates (same required keys +
    positivity as `score_fundamental.validate_anchors`, duplicated locally) AND
    dcf_base/bear/bull each transcribed into valuation.md (±0.5% or exact string) —
    anchors are transcriptions, not inventions.
  The section lists and sub-skill names are the REAL FSI structure read from the
  installed cache, not an approximation. Thresholds are FLOORS below FSI's own
  templates — they exist to make silent shrinkage fail loudly, not to cap depth.
  - Files: `scripts/coverage_qc.py`, `tests/test_coverage_qc.py` (35 new tests:
    minimal passing fixtures + one-mutation breakers per check).

- **Phase 0.5 rewrite: full depth is the contract, not a suggestion.** Deleted the
  "proportionate depth" override and the anchors-not-a-10-tab-workbook framing from
  `full-trade-analysis`. The orchestrator now announces the cost and runs FSI
  `initiating-coverage` Tasks 1-3 at the FSI SKILL's own FULL deliverable depth,
  invoking `financial-analysis:3-statement-model` / `:dcf-model` / `:comps-analysis`
  where the workflow prescribes them (Tasks 4-5 still skipped — our docket/renderer
  own charts + assembly). It writes `coverage/coverage_manifest.json` as the work
  happens, transcribes `valuation_anchors.json` (rules unchanged), and runs
  `coverage_qc.py --mode full` — a FAIL means the coverage is not done; complete it
  or surface to the user, never waive a self-depth failure silently. Shallow mode is
  an explicit per-run user request only (`depth_mode "shallow (user-requested)"`,
  gate `--mode shallow`), disclosed in the coverage line, the report, and the Phase-6
  completeness block. `company-context` distills from FULL, gate-passed coverage;
  `refresh-analysis` appends a `model-update` manifest entry and re-runs the gate in
  the recorded mode (the full-depth gate is an initiation contract, not re-decided on
  refresh).
  - Files: `skills/full-trade-analysis/SKILL.md`, `skills/company-context/SKILL.md`,
    `skills/refresh-analysis/SKILL.md`.

## 0.12.1 — 2026-07-19

- **Fix: anchored runs no longer carry the compressed-mode disclosure (ORCL live
  finding).** `score_fundamental`'s top-level `fundamental_mode`/`mode_note` were
  static, so a coverage-anchored run shipped "snapshot-only fundamental pass;
  deep FSI initiation/model reuse not applied" — a false disclosure on exactly
  the runs where initiation HAD run. Anchored runs now stamp
  `coverage_anchored_pass` with a note stating the actual split (valuation from
  coverage anchors; quality from the snapshot per single-mapping; coverage also
  enters via the cited moat flag). Snapshot-mode disclosure unchanged.
  - Files: `scripts/score_fundamental.py`, `tests/test_score_fundamental.py`.

## 0.12.0 — 2026-07-18 · Sector scales, anchored valuation, weights config, methodology

Coverage anchors now SCORE (not just narrate): fundamental valuation v1.2.0 banding
against the coverage DCF and comps, the downside floor moving to the DCF bear case,
and a sector-agnostic **scales registry** that lets a ratified, versioned, falsifiable
regime thesis (e.g. the HBM structural re-rating of memory) legally move the justified
band — semi-deterministic (byte-identical given scale@version), governed (falsifier
monitoring each refresh, adversarial proposal review, user ratification, forward-only
history). Versioned custom composite weight-sets with standard-comparison
transparency, and a fully script-generated METHODOLOGY appendix in every Detail PDF.
Validated end-to-end on MU: fundamental 68 → 55.75, composite 61.1 → 58.0 (Hold/Trim),
the long-term profile de-rating from #2 to last once valuation stopped over-crediting.

- **Provenance gate admits governance stamps (V5 live finding).** Scale/weight-set
  versions are not X.Y.Z-shaped (`2026.1`), so citing the active scale in gated prose
  orphaned its digits — yet the full `name@version` stamp is exactly what disciplined
  prose should cite. Bundle-carried stamps (`sector_scale`, `weight_set`) are now
  scrubbed exact-match before the token scans; a fabricated stamp or a bare version
  tail still orphans.
  - Files: `scripts/report_qc.py`, `tests/test_report_renderer.py`.

- **Sector-scales library + fundamental valuation v1.2.0 (anchored mode, PEG
  display-only) (Task V1).** New `scripts/sector_scales.py`: a versioned,
  validated JSON contract per sector that computes a fair-value BAND from
  first-principles fundamentals — `justified_pb` (Gordon residual income,
  `mid = (roe_normalized - g)/(r - g)`), `justified_pe`
  (`mid = (1 - g/roe_normalized)/(r - g)`), or `nav_based` (pass-through appraised
  NAV multiples) — each enveloped at `±band_spread` (default 0.30), plus falsifier
  evaluation over dotted snapshot metrics (`consecutive_quarters` is passed through
  as caller metadata; unresolvable metrics report `tripped: None`). `validate_scale`
  names every issue (required fields, formula ∈ FORMULAS, formula-specific params,
  `r > g`, C-ID evidence, falsifier shape); `load_scale` raises on invalid.
  Band math is unit-pinned (roe .35 / r .12 / g .04 → mid 3.875, low 2.7125,
  high 5.0375).
  - `scripts/score_fundamental.py` → rubric **v1.2.0**. Quality/moat UNCHANGED.
    Valuation 50 gains an ANCHORED MODE (via `--anchors valuation_anchors.json`):
    DCF-band position (17) + comps-range position (13) + own-history multiple (8,
    the v1.1 pe_fwd/pe_5yr_median band rescaled, sanity band kept) + FCF yield (7,
    rescaled) + justified sector-band position (5, via `--scale`) = 50. Maxima
    (17/13/8/7/5) sized under a 35%-of-50 design cap. DCF disagreement rule: when
    `|dcf_base - comps_mid| / mid > 0.25` the band widens to
    `[min(dcf_bear,comps_low), max(dcf_bull,comps_high)]` and the DCF max takes a
    0.75 confidence haircut (17→12.75), both disclosed in the arithmetic. PEG is
    REMOVED from anchored scoring and re-emitted top-level as `peg_display`
    (display-only). The active mode is disclosed on the valuation subscore
    (`valuation_mode`: `anchored_v1.2` | `snapshot_v1.1`); the sector scale is
    recorded as `sector_scale` (`name@version`). Absent `--anchors`, snapshot mode
    (v1.1 floor: pe 20 / peg 15 / fcf 15) is byte-preserved with PEG still scored.
    Malformed anchors/scale → exit 2 naming the issue.
  - Files: `scripts/sector_scales.py` (new), `scripts/score_fundamental.py`,
    `tests/test_sector_scales.py` (new), `tests/test_score_fundamental.py`.

- **Versioned weights config (`score_composite.py`) (Task V2).** New optional
  `--weights-config <trading_desk_config.json>` (default `./trading_desk_config.json`
  when it exists) supplies per-profile weight columns under `weights.profiles`. Each
  provided profile's weights must sum to `1.0 ±1e-6` (exit 2 names the profile + the
  observed sum) and may carry only the five known dimension keys (unknown key → exit 2);
  a profile ABSENT from the config falls back to the standard fixed table **per-profile**.
  The module JSON records `weight_set` (`"standard v1"` | `"CUSTOM <set_name>@<version>"`),
  the `dimensions[]` rows carry the weights actually used, and the `sensitivity` block
  gains a `weight_set` label plus — when a profile is custom — a
  `standard_comparison: {score, grade}` recomputed under the standard weights (visible
  tuning transparency). Renormalization on a missing dimension works identically under
  custom weights.
  - Files: `scripts/score_composite.py`, `tests/test_score_composite.py`.

- **Scale falsifier monitoring (`refresh_plan.py`) (Task V2).** A refresh now scans
  `trading_desk_config/scales/*.json` (primary: CWD; legacy: the ticker-dir parent) and,
  against the PREVIOUS bundle's snapshot, runs `sector_scales.evaluate_falsifiers` (lazy
  import, degrading gracefully to a skip note when the module is unavailable — no hard
  dependency). The plan gains a `scales[]` block (`scale`, `falsifiers[]`, `any_tripped`,
  `action_required` naming the pre-registered `on_trip` consequence, default
  `flag+disclose`), a top-level `scale_review_required` bool (any tripped), and
  `pending_proposals[]` (filenames under `scales/proposals/`). `judgment_review_required`
  logic is UNCHANGED — scale review is a parallel signal; a `tripped: None` (unresolvable)
  falsifier does not trip review.
  - Files: `scripts/refresh_plan.py`, `tests/test_refresh_plan.py`.

- **`scale-review` skill — adversarial proposal gate (Task V2).** New
  `skills/scale-review/SKILL.md`: enumerate active scales + their consumers → gather
  fresh evidence → verdict per scale (`valid | erosion_suspected | rebasing_proposed`)
  → for a re-base, DRAFT the complete forward-versioned replacement (parameters with
  derivation, cited evidence, falsifiers with pre-registered `on_trip`, `prior` = current
  scale) → an ADVERSARIAL GATE dispatches 3 independent refutation passes, surviving only
  with ≥2 non-refutations (votes recorded) → file to `scales/proposals/<name>_<version>.json`
  as `pending_ratification`. NEVER applies a scale; ratification is the user's one-word
  `ratify <name>@<version>`. Auto-apply reserved for pre-registered `on_trip` consequences;
  refutation survival (not self-reported confidence) is the gate; forward-only versioning;
  every report footer shows the active scale.
  - Files: `skills/scale-review/SKILL.md`.

- **dcf_bear downside floor (`score_risk.py`) (Task V3).** New `--anchors
  valuation_anchors.json`: in anchored mode the downside map's valuation floor becomes
  the coverage DCF bear case (`basis: "dcf_bear (coverage anchors)"`), replacing the
  `pe_5yr_median × eps_ntm` floor and its suspect-flag machinery entirely — this kills
  the pe-median degeneracy that produced a $134 floor on an $853 stock. Snapshot mode
  (no anchors) is byte-identical. The module records
  `downside_floor_mode: "dcf_bear" | "pe_median"`; malformed anchors → exit 2 naming
  the issue (same validation contract as `score_fundamental`).
  - Files: `scripts/score_risk.py`, `tests/test_score_risk.py`.

- **Skill wiring for anchors / scales / weights / ratification (Task V3).**
  full-trade-analysis: the coverage phase transcribes valuation anchors into
  `coverage/valuation_anchors.json` (every number cited to its coverage-artifact
  section; validated by the scorers' exit-2 backstop); scoring steps pass
  `--anchors` / `--scale` / `--weights-config` conditionally (a sector scale governs a
  ticker ONLY via a cited context finding — single-mapping discipline).
  refresh-analysis: reads the plan's `scales[]` / `scale_review_required` /
  `pending_proposals[]`, applies ONLY pre-registered `on_trip` consequences, surfaces
  pending proposals, and documents the ratification flow (`ratify <name>@<version>` →
  current scale archived to `scales/history/`, proposal promoted with `prior` set —
  forward-only, history never recalculated). company-context: sector-regime theses are
  recorded as cited findings that scales must reference as evidence. risk-analytics:
  documents the anchored `dcf_bear` floor invocation.
  - Files: `skills/full-trade-analysis/SKILL.md`, `skills/composite-score/SKILL.md`,
    `skills/refresh-analysis/SKILL.md`, `skills/company-context/SKILL.md`,
    `skills/risk-analytics/SKILL.md`.

- **METHODOLOGY appendix in every Detail PDF (Task V4).** The detail docket gains a
  final, 100% script-generated METHODOLOGY section (zero LLM content; every string is
  a pinned constant or read from module/scale JSONs): rubric-versions table; the
  composite weight table actually used (dual custom-vs-standard table when a CUSTOM
  weight set is active); the fundamental valuation formula set (mode, component maxima
  17/13/8/7/5 anchored or 20/15/15 snapshot, the >25% DCF-vs-comps band-widen + 0.75
  haircut rule, PEG display-only line); the active sector scale (name@version,
  effective, basis, formula, parameters, computed band, evidence C-IDs, falsifiers,
  prior) or "No sector scale active — standard bands"; scoring conventions (EV hurdle,
  grade bands, horizon years, judgment-flag rule — imported from the scorers'
  constants, never retyped); and the governance rules (forward-only versioning,
  pre-registered falsifier consequences, adversarial review + user ratification,
  append-only history). Height-aware pagination (`METHODOLOGY (continued)`).
  - Renderer stamps & banners: footer gains `Weights: standard v1 | CUSTOM <set>@<ver>`
    and `Scale: <name>@<version>`; a CUSTOM tag renders near the grade box; the delta
    note's What-Changed detects weight-set and sector-scale transitions; Detail p1 and
    the delta carry an accent banner when `scale_review_required` and a neutral banner
    for pending proposals; anchored-mode PEG renders as display-only with the
    exclusion note.
  - Files: `scripts/render_pdf.py`, `tests/test_render_pdf.py`,
    `skills/report-renderer/SKILL.md`.

## 0.11.0 — 2026-07-17 · Coverage-first analysis

Deep coverage becomes the default read: the pipeline always initiates (or refreshes)
company coverage, distills it into a cited `company-context` module whose findings[]
registry grounds fundamental moat scoring and composite conviction, argues that case
through the detail docket, and demotes the old web-compressed pass to an FSI-absent
floor. Consolidates Tasks **C1** (fundamental rubric v1.1.0 — moat/positioning enters
scoring), **C2** (`company-context` skill + `report_qc.py --context` provenance +
structure gate), **C3** (coverage-first pipeline wiring across the SKILLs), and **C4**
(detail report argues the case).

- **Layout fix: findings-block pagination + live_tape title wrapping (context page
  overflow).** The COMPANY CONTEXT page's FINDINGS footnote block is now height-aware:
  each finding's wrapped height is measured before it is drawn, and when the footnote
  band is exhausted the block continues on a `FINDINGS (continued)` page (mirroring the
  measured two-up dimension packing) instead of overrunning the page footer and
  truncating findings mid-sentence. `live_tape` entry titles wrap to 2 lines on word
  boundaries rather than hard mid-word truncation (`…ami` → full title). A measurement
  pass (`_measure_context_pages`) keeps the `p N/M` footer exact when findings spill.
  Regression: 20 long-finding fixture renders extra page with no exception; pure
  `_finding_lines` wrapped-height unit test.
  - Files: `scripts/render_pdf.py`, `tests/test_render_pdf.py`.

- **Review fix-round (post-C4): unit-suffix provenance, C-ID referential integrity.**
  Context number-provenance no longer swallows financial shorthand — `42B` / `9999M`
  / `30x` / `45pct` / `200bps` / `3nm` now have their number scanned against the
  bundle (only the unit suffix is stripped, mirroring the report gate's `$42B`), while
  product names (HBM3E, A100, GB300) and finding refs stay scrubbed. `score_fundamental`
  (moat) and `score_composite` (conviction) now verify each cited `C\d+` resolves to a
  real `module_context.json` findings[] id (unresolved → exit 2), not just that a C-ID
  is present. SKILL prose softened off unresolvable `spec §` references.
  - Files: `scripts/report_qc.py`, `scripts/score_fundamental.py`,
    `scripts/score_composite.py`, `skills/*/SKILL.md`, `tests/`.

- **Detail report argues the case (Task C4): why-this-call, context narrative,
  evidence notes, full options render.** The detail docket's pages 3-6 no longer
  dump arithmetic — they argue the thesis, rendering the captured judgment the
  pipeline already wrote.
  - `render_pdf.py`: new **WHY THIS CALL** page (after the exec repeat) —
    pure module-JSON rendering of `ev.scenario_reasoning`, each conviction subscore
    with its flag value + justification (variant / catalyst_clarity / invalidation
    from `module_composite.flags`), the trade-plan judgment flags
    (catalyst-in-thesis + fundamental-invalidation from `module_tradeplan.flags`),
    the fundamental moat flag (from `module_fundamental.flags`, may cite C-IDs), and
    the sentiment judgment flags (rating_actions / inst_flow / insider_baseline).
    New **COMPANY CONTEXT** sections (THE BUSINESS / WHAT'S MOVING THE STOCK / THE
    CASES / RISKS (ARGUED) + a FINDINGS footnote block) rendered ONLY from a
    `module_context.json` carrying `qc.qc_passed`; absent/unstamped → one disclosure
    line (which rides the WHY page, not a near-empty page of its own).
  - Per-dimension EVIDENCE sections now render the ~200-word `evidence_notes` slot
    as the BODY and demote the arithmetic to a small-type **SCORING TRAIL** exhibit;
    bundles without `evidence_notes` keep the brief/arithmetic fallback.
  - Mechanical fixes: chart-to-section mapping corrected (drawdown_history +
    vol_regime → Risk; vol_term_structure / skew / expected_move_cone / oi_walls →
    Options); the **Options** section renders the FULL module (vol dashboard
    mini-table, recommended + declined tables with reasons, per-structure management
    rules, warnings_global, hedge_structure); the downside map is de-duplicated to
    chart + a compact 5-row NEAREST DOWNSIDE ANCHORS table; a scripted deep-entry
    commentary line prints under the trade plan when any entry sits >25% below last.
  - `report_qc.py`: the `--pdf-slots` provenance scan documents + covers the new
    `evidence_notes` map (a fabricated number in a note orphans like any slot prose).
  - Tests: WHY-THIS-CALL justification rendering, context appear/omit (stamped vs
    unstamped), evidence_notes orphan scan, chart-mapping assertions, options
    full-render, downside dedup, deep-entry trigger. 852 tests (was 818), all green.
  - Files: `scripts/render_pdf.py`, `scripts/report_qc.py`, `tests/test_render_pdf.py`.

- **Coverage-first pipeline wiring (Task C3, SKILL prose): always-initiate,
  context-grounded judgments, compressed demoted to floor.** Deep coverage is now
  the DEFAULT read; the compressed pass is the FSI-absent floor only; the context
  module feeds scoring.
  - `full-trade-analysis`: new **Phase 0.5 — Coverage** (after scope, before
    snapshot). Coverage EXISTS → freshness check, run FSI `model-update` if a quarter
    postdates the model. ABSENT + FSI installed → ANNOUNCE + always-initiate (invoke
    `equity-research:initiating-coverage` Tasks 1-3 only — research/model/valuation;
    skip its Tasks 4-5, our docket owns charts/report — artifacts into `coverage/`;
    "skip initiation" overrides per-run, records nothing). ABSENT + FSI absent →
    recorded `fsi_offer` flow; declined → COMPRESSED FLOOR, loudly disclosed. Phase 2
    gains the `company-context` invocation (mode per 0.5; parallel with
    technical/sentiment; completes before composite; evidence gate now requires
    `module_context.json`). Phase 3: HARD RULE — fundamental passes
    `--moat <wide|narrow|none> --moat-justification "<cites C-IDs>"` from
    `module_context.competitive` (`score_fundamental.py` exits 2 without a
    C-ID-citing justification), and conviction flags + scenario probabilities ground
    their justifications in context finding IDs. Phase 5/6 deliverables +
    completeness report coverage mode (coverage_distilled vs web_compressed) and
    initiation-run-this-session.
  - `composite-score`: fundamental step ALWAYS passes the moat flags citing context
    finding IDs when `module_context.json` exists; C-ID grounding rule for conviction
    justifications; compressed-without-context (moat omitted → 0 n/a) demoted to the
    last-resort floor, disclosed.
  - `refresh-analysis`: coverage freshness in the plan step (new quarter since
    coverage model → FSI `model-update` before rescoring, noted in plan); context
    refresh rule — `live_tape` ALWAYS re-authored from fresh news, business/
    competitive/cases carried forward `[carried forward from <date>]` unless
    `judgment_review_required` → re-affirm, finding IDs stable, `--context` gate
    re-run with `--previous`.
  - README: new "Coverage-first analysis" section; FSI section reframed as
    load-bearing for full depth; `company-context` added to the skills table + module
    tree.
- **Files:** `skills/full-trade-analysis/SKILL.md`,
  `skills/composite-score/SKILL.md`, `skills/refresh-analysis/SKILL.md`, `README.md`.
  Prose-only (no scripts); suite unchanged at 818 tests green.
- **Fundamental rubric v1.0.0 -> v1.1.0 (coverage-first, Task C1): moat/positioning
  enters scoring via cited context.** The Quality dimension (still max 50) is
  rebalanced from five mechanical components to six: the mechanical bands shrink
  (rev growth 15->12, gm 8->7, om 7->5, roe 10->8, fcf margin 10->8; sum 40) to make
  room for a new **moat/positioning judgment flag (max 10)**. `score_fundamental.py`
  gains `--moat wide|narrow|none` (wide->10, narrow->6, none->2) with a REQUIRED
  `--moat-justification`; per coverage-first the justification must cite at least one
  context finding ID (`C\d+`, e.g. C3) or the CLI exits 2 ("moat justification must
  cite context finding IDs (e.g. C3)"). Omitting `--moat` entirely scores 0
  ("moat: n/a (no context assessment)") and, mirroring sentiment's inst_flow
  "unknown", does NOT count toward the dimension's evaluable inputs; a present flag
  is always evaluable. Flag + justification are recorded in the module `flags`
  (previously `{}`) mirroring `score_sentiment` conventions. Valuation (50) and the
  renormalization semantics are unchanged. Every quality band test is re-pinned to
  the new maxima (each carrying an `old -> new` comment); +42 tests.
- **Files:** `scripts/score_fundamental.py`, `tests/test_score_fundamental.py`.
  Suite: 818 tests green (12 skips).

## 0.10.2 — 2026-07-17 · Display precision, grade-box labels, valuation-floor breakdown suppression

Final display-integrity round on the PDF docket, verified visually against a full
MU-shaped bundle (exec/detail/delta rendered, every page inspected).

- **Display-precision discipline (the "amateur tell").** Three new pure formatters in
  `render_pdf.py` — `fmt_price(v)` (2dp + thousands separators, e.g. `$853.20`,
  `$1,254.81`), `fmt_ratio(v)` (2dp bare multiples, e.g. `18.39`, `0.14`, `1.66`), and
  `fmt_pct_int(v)` (0dp percentiles, e.g. `92`) — now route EVERY script-minted
  displayed number in the exec/detail/delta chrome: header price, key-stats sidebar
  (beta/P-E/PEG/IV percentile/52wk), trade-plan levels, grade box, What-Changed money,
  options strikes/net/hedge cost, downside-map levels, invalidation stops, footer money.
  `fmt_money_delta` now formats its magnitude to 2dp + separators (`$5.00`, not `$5`).
  Slot prose is left untouched (user text). All unit-tested. The slots provenance gate
  is unaffected (it checks the md/slots, and its rounding-expansion already tolerates
  0-2dp) — confirmed PASS on the live bundle.
- **Catalyst-timeline truncation.** `_short_event` now truncates at WORD boundaries
  (whole tokens + ellipsis), never inside a number token (`"...-5.65%"` is no longer
  cut to `"904.…"`); a single overlong token still hard-slices.
- **Grade-box label truncation.** A new `action_short` map ({`Buy/Add`→`BUY / ADD`,
  `Hold/Accumulate-on-weakness`→`HOLD / ACCUMULATE`, `Hold/Trim`→`HOLD / TRIM`,
  `Reduce/Avoid`→`REDUCE / AVOID`}, fallback: uppercase + stringWidth fit) so the grade
  line reads `B · HOLD / ACCUMULATE` untruncated. Unit-tested.
- **Valuation-floor method-breakdown suppression (end-to-end).** `score_risk.valuation_floor`
  now flags the row `suspect: true, suspect_reason: "approx_current_eps method breakdown"`
  (instead of dropping it) when `floor/last < 0.25` OR `pe_fwd/pe_5yr_median` falls outside
  `[0.2, 5.0]` (mirrors `score_fundamental`'s scoring sanity band, documented in code).
  `build_downside_map` carries the flag. DISPLAY consumers skip suspect rows:
  `render_charts` (football-field anchors + downside-ladder exclude them), `render_pdf`
  (downside-map table shows the row GRAYED with its reason; anchors omit). The real-MU
  ~$134 floor on an ~$850 stock is now suppressed from the anchors chart / ladder and
  grayed in the detail table — verified visually.
- **Files:** `scripts/render_pdf.py`, `scripts/render_charts.py`, `scripts/score_risk.py`,
  `tests/test_render_pdf.py`, `tests/test_render_charts.py`, `tests/test_score_risk.py`.
  Suite: 776 tests green (12 skips base / 2 skips render-venv); +19 new tests.

## 0.10.1 — 2026-07-17 · Docket polish + R5 live validation

Review-round polish on the PDF docket, verified against a real MU refresh bundle (R5).

- **Money-delta sign placement.** A new pure `fmt_money_delta(v, plus=)` puts the minus
  OUTSIDE the dollar sign (`-$182.44`, not `$-182.44`); both What-Changed formatters
  (exec box + delta-note table) route money through it. Unit-tested directly.
- **Score-delta bars no longer overprint row labels.** `_draw_score_delta_bars` reserves a
  measured label column and clamps the bar+value zone to the right of it, so a full-magnitude
  down bar stops clear of the dimension names.
- **Downside ladder labels.** `draw_downside_ladder` flips value labels to the left of the dot
  for rungs hugging the current-price line (no more crossing the dashed line), lifts labels off
  the connector, and widens the bottom margin so x-ticks clear the caption (reuses the
  bottom-margin pattern from the earlier chart-fix round).
- **Detail-page density.** Per-dimension charts are placed two-up (side by side) instead of
  stacked full-width, ~halving section height; dimension sections now pack two/three per page
  (measurement pass keeps the p N/M footer exact). MU detail: 9pp → 7pp.
- **`slots_gate_ok` comment.** Documented that the `qc_passed` stamp is trust-on-write (an
  accidental-bypass guard, not forgery-resistant).
- **R5 validation.** Real MU bundle: charts re-rendered, honest `pdf_slots.json` authored,
  slots gate PASS (first pass, no prose weakening), exec/detail/delta rendered and every page
  visually verified — no collisions/clipping, money format and delta bars confirmed clean.

## 0.10.0 — 2026-07-17 · The PDF docket

The report layer gains an institutional **PDF docket** — the bank-note-styled render of
an already-QC'd bundle — alongside the markdown report (which stays the source of truth).

- **Three documents:** `exec` (2pp trade sheet), `detail` (~10-15pp dossier), and `delta`
  (1pp What-Changed note on a refresh vs the prior bundle). All land in the ticker parent
  next to the `.md` report.
- **Zero LLM arithmetic.** Every number is script-minted — from the module JSONs, the
  deterministic chart pack, or the What-Changed diff. The only authored content is the
  prose in `pdf_slots.json`.
- **Deterministic chart pack** (`render_charts.py`, exec 8 + detail 8): each chart is a
  pure extract (unit-pinned against fixtures) + a matplotlib draw; a chart with a missing
  input is skipped with a recorded reason, never fabricated.
- **Slots provenance gate.** `report_qc.py --pdf-slots` runs number_provenance over the
  docket prose and stamps `qc_passed=true` INTO the file on pass; `render_pdf` refuses to
  render exec/detail without that stamp — the gate cannot be bypassed.
- **Graceful degradation.** matplotlib + reportlab live in a one-time ~30s venv
  (`render_env.py`, kept out of the stdlib-only core). `render_env.py --check` exits 3 when
  absent → the skills ship the report md-only and disclose it; the docket never blocks.
- **Chart-collision fixes** (review findings): staggered ladder-shelf labels, clamped event
  callouts, football-field anchor redesign (nearest two supports, not min..max) + now-line
  label offset, single captions, visible score-bar weight ticks.
- **pe_5yr_median sanity gate** (scoring-integrity fix): the fundamental valuation
  component `pe_fwd / pe_5yr_median` uses the `approx_current_eps` median, which back-projects
  today's EPS across the price history. For a name whose EPS regime shifted (real MU:
  pe_5yr_median 1.82) the baseline is garbage; a ratio outside the sanity band [0.2, 5.0]
  now scores the component 0 and treats it as n/a (like a null input) so the dimension
  renormalizes over the remaining components instead of banding on a bogus multiple.
- **Skill wiring:** report-renderer gains a Docket (PDF) rendering step after the md QC gate;
  refresh-analysis renders the delta note (+ exec/detail); full-trade-analysis's Phase 5
  delivers the exec/detail PDFs and Phase 6 reports whether the docket rendered or degraded.

**Modified:** `scripts/score_fundamental.py` (+ `tests/test_score_fundamental.py`),
`skills/{report-renderer,refresh-analysis,full-trade-analysis}/SKILL.md`, `README.md`,
`CHANGELOG.md`, `.claude-plugin/plugin.json` (→ 0.10.0). (R1-R3 landed the renderers:
`tdstyle.py`, `render_env.py`, `render_charts.py`, `render_pdf.py`, `report_qc.py --pdf-slots`.)

## 0.9.1 — 2026-07-17 · Verified FSI marketplace reference shipped in-package

Real-user finding #5: the FSI offer worked (recorded ask fired) but the agent could
not hand over install commands — the marketplace source wasn't in the package, and it
correctly refused to fabricate one. The verified source (`anthropics/financial-services`,
read from a live registry, not guessed) is now embedded in the offer text (both skills),
the session-start notice, and the README.

## 0.9.0 — 2026-07-17 · Post-install FSI notice (SessionStart hook)

Real-user finding #4: install is silent (no post-install hook exists in the plugin
system) and the in-skill FSI offer only fires when an analysis runs — so a fresh
install surfaced nothing. The plugin now ships a SessionStart hook (harness-executed
script, not prose): shows a one-time notice right after install when FSI is absent
and no fsi_offer is recorded (marker in the plugin data dir), injects a reminder for
the model to make the recorded offer if an analysis starts, and stays silent forever
after — and always silent when FSI is installed or a choice is recorded.

## 0.8.1 — 2026-07-17 · FSI offer hardened

Real-user finding: the FSI install offer never surfaced. It was advisory prose an agent
could skim past ("unattended → proceed"). Now a recorded ask-once, same mechanism as
source selection: check config → absent + user-initiated run → MUST ask → write
`fsi_offer` to `trading_desk_config.json`. The required artifact makes skipping visible.

## 0.8.0 — 2026-07-17 · No bundled MCP servers

Removed the bundled `.mcp.json` (real-user finding: the auto-registered Alpha Vantage
server on a keyless machine errors instead of being absent, and the agent retries a
"present" source instead of falling back to web). The plugin now installs skills +
scripts ONLY — zero MCP servers, zero auto-dependencies. Data sources are user-added
(one-liner in the README); the built-in `stooq+web` mode needs no key at all. New
preflight anti-loop rule: a source failing twice is treated as UNAVAILABLE (announce,
print the fix, fall back) — never retried in a loop.

## 0.7.0 — 2026-07-17 · Bring-your-own data source · Bring-your-own-MCP source abstraction (Feature A)

Market-data source abstraction so the pipeline is no longer Alpha-Vantage-only. Fetching
stays the client agent's job; the builder accepts a fixed, source-neutral set of raw file
shapes. A source is chosen once, persisted, and re-used; foreign bulk artifacts are adapted
by client-generated structural transforms persisted in the user workspace. Adds 1 test
(suite 662 → 663 green).

### Added
- **`meta.data_source` passthrough (`scripts/build_snapshot.py`).** Manifest top-level key
  `data_source` (free-form primary-source name, e.g. `alphavantage`, `mcp:polygon`,
  `stooq+web`) → `meta.data_source`, defaulting to `alphavantage` when absent. Mirrors the
  existing `data_mode` passthrough. Test: `TestDataSource` + a default-value assertion.
- **`docs/CANONICAL_CONTRACT.md`.** The source-neutral interface: the exact raw file shapes
  the builder / `scripts/chain.py` accept per manifest key (envelope handling, the two daily
  shapes, quote/overview/statement/estimates/options/web_fundamentals/pc/calendar/treasury/
  short-interest/insider), plus THE ADAPTER RULE for foreign sources (scalar → cited
  transcription; bulk → structural transform persisted at
  `trading_desk_config/adapters/<source>_<group>.py`, re-run verbatim).

### Changed
- **`skills/market-snapshot/SKILL.md` — Step 0 is now SOURCE + tier preflight.** Step 0a
  reads/writes `./trading_desk_config.json` (`{"primary_source", "fallbacks", "asked": true}`),
  discovers market-data MCP servers via a `ToolSearch` keyword sweep, asks once (unattended
  default: alphavantage if connected else stooq+web), and records `data_source` in the
  manifest. Step 0b is the existing AV tier probe (alphavantage source only). New **Step
  2-MCP** foreign-MCP fetch pass routes scalar groups to cited transcription and bulk groups
  to persisted structural adapters, with per-group fallthrough to stooq+web.
- **`skills/refresh-analysis/SKILL.md` Step 1** also reads `trading_desk_config.json` /
  previous `data_source` for source context alongside `data_mode`.
- **`skills/full-trade-analysis/SKILL.md` Phase 0/1** mention the source preflight + config
  once; scope echo carries `data_source`.
- **README** — new "Bring your own data source" section (config file, ask-once, adapters in
  the user workspace, contract-doc link); provenance note mentions `meta.data_source`.

## 0.6.0 — 2026-07-17 · Refresh mode

Live-validated same day on real MU data: no-event refresh reused 10 groups, refetched 7
(7 AV calls, ~13 min), carried judgments forward tagged, and caught a real −5.65% session
(composite 66.95→62.85, B→B; both invalidation legs verified intact; previous bundle
byte-identical). Review + live-run fixes: strict-inside reuse boundary with a gate
cross-check test over every staleness window; ISO-timestamp tokens verified whole by
their date (time digits no longer orphan); reuse-aware mktcap skip (stale in-window
vendor cap + moved price is unevaluable, not wrong); corrected qc_gate/report_qc
invocations in the skill. — Refresh mode (Feature B)

Event-aware selective-refetch refresh so an existing ticker workspace can be re-run
cheaply. Selective FETCHING, never selective SCORING — one new snapshot per refresh,
all modules re-emit. Adds 34 tests (suite 623 → 657 green).

### Added
- **`scripts/refresh_plan.py` — deterministic refresh planner.** CLI
  `--ticker-dir <path> [--as-of YYYY-MM-DD] [--out <path>]`. Locates the newest
  previous bundle (`detail_reports_*` by name; legacy `td_bundle_*` / bare-bundle
  fallback), reads the previous manifest + snapshot, and emits `refresh_plan.json`
  deciding per manifest group **refetch vs reuse**:
  - **Always-refetch** the fast-moving surface: `global_quote`, `daily_adjusted`,
    `spy_daily_adjusted`, `news_sentiment`, `pc_ratio_realtime`, `web_spot_check`
    (+ `options_chain` when present last run; `absent last run` → still refetch to
    fill the gap).
  - **Window-based** for the rest, REUSING `scripts.qc._STALENESS_WINDOWS` (bound by
    identity, not copied) so an authorized reuse provably passes the QC staleness
    check with the reused file's ORIGINAL `retrieved_utc`.
  - **Event override:** an earnings date in `(previous_as_of, as_of]` forces the
    statement set (income/balance/cash-flow/earnings/estimates/overview/calendar/
    insider) + `judgment_review_required`; a dividend ex-date in-window forces
    overview + earnings_calendar only.
  - `iv_history`: reuse if the newest sample ≤14d old, else refresh; and an
    `estimated_refetch_calls` count.
  - Exit 2 with "nothing to refresh — run a full analysis first" when no previous
    bundle is found.
- **`skills/refresh-analysis/SKILL.md`.** Triggers "refresh [ticker]", "update the
  analysis", "re-score [ticker]", "update the score". Presents the plan, assembles
  a new append-only `detail_reports_<as_of>/` bundle (copy reused raw files +
  manifest entries verbatim, refetch the rest per market-snapshot conventions),
  builds + QC-gates the snapshot, **re-runs ALL modules** with judgment
  carry-forward (disclosed `[carried forward from <date>]` unless an event forces
  honest re-affirmation), renders the full report AND a delta vs the previous
  bundle (both QC-gated), and appends a dated `thesis_entry.md` section with the
  invalidation-leg check. Never edits the previous bundle.
- **`tests/test_refresh_plan.py`** (34 tests): always-refetch set; window
  reuse/refetch boundaries (insider 89d reuse / 91d refetch, short-interest 14/15,
  treasury 7/8); earnings between-runs vs before/after/boundary; dividend override;
  options-chain absent-last-run; iv_history 10d/14d/20d; legacy layout; no-bundle
  exit 2; call arithmetic; determinism; plan-file write + stdout path; a contract
  test that the planner's window table IS `qc._STALENESS_WINDOWS`.

## 0.5.0 — 2026-07-17 · First real-world feedback batch — Real-world feedback: data-mode preflight, web fallback, trading_desk layout

Docs + skills layer for the v6 real-world feedback batch (script layer landed at `61d31fa`).
Prose-only changes; the 623-test suite stays green.

### Added
- **Data-mode preflight (market-snapshot Step 0).** Explicit key/tier detection up front:
  env-var check + one `GLOBAL_QUOTE` probe classify the run as `alpha_vantage` (premium),
  `av_free_degraded` (free key OR **no key exported** — the bundled `.mcp.json` still answers at
  Alpha Vantage's anonymous ~25-call/day quota), or `web_fallback` (no AV MCP). Interactive runs
  are ASKED before proceeding on a degraded mode; unattended runs proceed and disclose. Recorded as
  the top-level manifest `data_mode` key → `meta.data_mode`.
- **Web-fallback fetch pass (market-snapshot Step 2-ALT).** FSI-style cited web research through the
  same QC'd pipeline: stooq CSV OHLCV (ticker + SPY, `series_source: stooq_csv_close_as_adjusted`),
  transcribed `web_fundamentals` (statement files win; gaps disclosed in
  `fundamentals.web_transcribed_fields`) and `overview` substitute, options standing aside. Verbatim
  transcription rule; the QC arithmetic cross-checks are the transcription audit.
- **FSI runtime offer** in `full-trade-analysis` Phase 0 and standalone `composite-score`: ask once
  (interactive) to install the claude-for-financial-services plugins before the compressed fundamental
  pass; never auto-install.
- **Free-tier budget guidance** in market-snapshot Important Notes: ~25 calls/day, one run/day,
  never IV-history sampling; resume-next-day / switch-to-web-fallback on mid-run quota exhaustion.

### Changed
- **Bundle layout → `trading_desk_<TICKER>/`.** Parent dir (no date) holds the persistent
  `iv_history_<TICKER>.json` and each dated `detail_reports_<YYYY-MM-DD>/` bundle. The report lands in
  the parent as `<TICKER>_Trade_Report_<date>.md` (per `render_report.py`'s parent-output rule).
- **Bundle discovery glob** in all evidence/decision/render skills now lists the new
  `trading_desk_<TICKER>/detail_reports_*` layout first with legacy `td_bundle_<TICKER>_*` as a
  labeled fallback for old bundles.
- **Completeness statement** (full-trade-analysis Phase 6) now names `meta.data_mode` and lists
  `web_transcribed_fields` when the mode is not `alpha_vantage`.
- **README** gains a Data modes section (premium / free-or-no-key / web-fallback) and an Output layout
  map; FSI section notes the runtime offer.

## 0.4.1 — 2026-07-16 · Rename

Plugin, marketplace, and repo renamed `trade-decision` → **`trading-desk`** before first
publication (skills now namespace as `trading-desk:*`; install via
`/plugin marketplace add akgoparaju/trading-desk` → `/plugin install trading-desk`).
Historical entries below retain the old working name.

## 0.4.0 — 2026-07-16 · Phase 4: Assembly & Acceptance

Acceptance results (V1–V5 PASS; V6 deferred to a clean-environment run, blocks only
the 1.0.0 tag): V1 AAPL 3-page report shipped with ZERO unwaived report-QC failures
(the reference report it replaces contained four internal contradictions) at 2,099
words; V2 trader-profile run reproduced the balanced run's sensitivity prediction
byte-for-byte; V3 degradation (no chain/SI/P-C) renders a fully disclosed report —
fixed mid-acceptance so the snapshot QC gate is the ONLY full stop; V4 delta report
mechanics pass under the hardened QC; V5 FSI structural parity 5/6 (the one PARTIAL
was brief-format uniformity, fixed). The 12 superseded in-house trade-* analysis
skills are retired (reversible) in the author environment.

Known limitation: number-provenance is a numeric-membership check with rounding/percent
tolerance — bare small integers near real bundle values can pass; fabricated dates,
versions, headers, and prose-only figures are deterministically caught. Revisit
tolerance width post-1.0.: Assembly

### Fixed
- **Report-QC number provenance hardened** (`scripts/report_qc.py`, `tests/test_report_renderer.py`):
  (1) string-leaf numeric scanning is no longer global — it is now restricted to a WHITELIST of
  bundle string paths the renderer echoes / the LLM cites (snapshot `meta.qc` + `meta.api_tier_notes`
  + `sentiment.insider_method`; tradeplan sizing/hedge/executability/invalidation strings; options
  structure `arithmetic`/`pop_method`/`declined[].reason`/`warnings_global`/`liquidity_verdict`/
  vol `disclosure`; composite `renormalization_note` + thesis subscore arithmetic; each evidence
  module's `renormalization_note` **and** `subscores[].arithmetic`). Numeric-leaf scanning stays
  global. Closes the channel where prose could cite a number that only appeared inside an unrelated
  arithmetic string. (2) Dates and versions are now EXACT-MATCH-allowed against the bundle's own
  dates/versions instead of being blindly shape-scrubbed — a fabricated date (`2031-01-01`) or bogus
  version (`v9.99.99`) in prose now orphans. (3) Only the three exact page headers render_report
  emits are treated as chrome; digits in any other `## Page N` line (e.g. `## Page 777`) now orphan.
  (4) `is_allowed` returns False (not True) for an unparseable token. The evidence-module
  `subscores[].arithmetic` whitelist path was forced by re-running the fixed QC against the real V1
  AAPL report (it legitimately cites `19.9%`, `28.6%`, `1.67x`, `59.6%` from those strings); no
  genuine fabrication was found. 12 new regression tests; V1 report still exits 0.

### Changed
- **composite-score SKILL** (`skills/composite-score/SKILL.md`): after running `score_fundamental.py`,
  the step now also writes `<bundle>/brief_fundamental.md` (same ≤120-word evidence-brief format as
  technical/sentiment/risk, mode disclosure included). Fundamental has no standalone skill, so the
  composite step owns its brief — this completes the report-renderer's evidence-brief inputs.

### Added
- **Full-trade-analysis orchestrator** (`skills/full-trade-analysis/SKILL.md`, no scripts): the
  **L5 orchestrator** — a phase-gated prompt that coordinates the other eight skills end to end.
  Phase 0 scope (profile/horizon/position-context-only-if-offered/FSI-depth, one-line echo);
  Phase 1 snapshot + BLOCKING `qc_gate.py` (only full stop); Phase 2 evidence via **parallel
  Agent subagents** (wave 1 = {technical, sentiment}, wave 2 = {risk} after the ladder exists;
  fundamental compressed pass left to composite-score) with per-subagent prompts carrying
  bundle path + exact SKILL.md path + judgment-flag protocol + single-snapshot/no-arithmetic
  rules + score/path/≤5-line-summary return contract + sonnet-or-opus (never frontier) model
  guidance; Phase 3 composite; Phase 4 trade-plan (pass 1 → options-strategy pipeline → pass 2
  synthesize); Phase 5 report-renderer + BLOCKING `report_qc.py`; Phase 6 register + monitor
  (embedded thesis-entry template filled from module JSONs, soft `thesis-tracker` registration,
  OFFER-only re-score via `schedule`/`CronCreate`, mandatory completeness statement). Degradation
  policy: any module failure → n/a + renormalize + disclose; a failed snapshot gate is the only
  full stop; the report always ships with the completeness statement. Contract-cross-checked
  against all eight skill SKILL.md files and the `trade_plan.py` / `render_report.py` /
  `report_qc.py` / `score_fundamental.py` CLIs before writing.
- README: all nine skills marked available; `full trade analysis NVDA` usage example added;
  Status bumped (Phases 1–4 shipped, acceptance V1–V6 in progress).

- **Report renderer + blocking report QC** (report-renderer skill): `scripts/render_report.py`
  + `scripts/report_qc.py` + `skills/report-renderer/SKILL.md` — the **L4 output layer**, the
  3-page trade decision report. Architecture kills LLM-number leakage **by construction**:
  `render_report.py` generates the ENTIRE report skeleton (every table, header, and number)
  from the bundle's module JSONs; LLM prose fills ONLY the marked `<!-- SLOT:... -->` slots.
  `report_qc.py` then verifies the FINAL document numerically against the bundle (blocking
  §12 gate) so a report can never ship with a number that is not in the bundle.
  - **`render_report.py`** (FULL mode requires snapshot + all seven module JSONs; a missing
    file → exit 2 naming it): **Page 1 — Decision** (header block; the call `grade — action`
    + composite score + tension slot; composite table with scripted band-reads
    strong/constructive/mixed/weak + sensitivity row bolded when profile grades differ;
    trade-plan table entries/exits/both-leg invalidation/size/hedge/expression; event-playbook
    skeleton with implied move + slot). **Page 2 — Evidence** (per dimension: scripted score
    headline + brief slot + scripted mini-table [ladder top-3-below/above, subscores,
    positioning subset, top-5 downside map, EV scenarios] + signal slot). **Page 3 — Context &
    Protocol** (full S/R ladder + downside map with options-basis; catalyst calendar + slot;
    scenario & EV table; options expression block [vol verdict, structures, declined, hedge,
    3-profile matrix]; monitoring protocol + slot; data-integrity footer [as_of, per-source
    retrieved stamps, QC attestation, api tier notes, missing disclosures, every module
    rubric_version + expression rule version + snapshot schema + plugin version read from
    `../.claude-plugin/plugin.json`]; disclaimer). **Delta mode** (`--delta --previous
    <old_bundle>`, both need module_composite): composite delta table (old/new/Δ, grade change
    bolded), EV delta, level changes, structures added/removed, interpretation slot; a module
    absent in either bundle → "n/a (module absent in {which})".
  - **`report_qc.py`** (§12, BLOCKING; waiver mechanics mirror `qc_gate.py`): 11 checks —
    **number_provenance** (every numeric token traces to a snapshot/module numeric leaf,
    including numbers embedded in bundle STRINGS like the QC attestation and api notes, with
    rounding + %-form + ±0.01 tolerances; orphans capped at 20), composite_arithmetic,
    ev_consistency, invalidation_both_legs, sizing_within_cap, strikes_in_chain (SKIP if no
    structures), pop_method_labeled, expression_consistency, footer_integrity, word_cap (≤2100),
    no_empty_slots. Delta reports auto-run checks {1, 9, 11} only; `--previous` folds the old
    bundle's leaves + the script-computed Δ columns into the allowed set.
- **Tests**: `tests/test_report_renderer.py` (30 tests) — realistic minimal bundle via
  `_mk_bundle()`; render exit 0 + all SLOT markers; 6+ scripted values trace to module JSONs;
  missing module → exit 2 naming it; delta old/new/Δ + structures added/removed + clean delta
  QC; unfilled skeleton FAILS no_empty_slots; clean fill PASSES all checks; rogue `$123.45`
  FAILS number_provenance; corrupt composite FAILS composite_arithmetic; stripped fundamental
  leg FAILS invalidation; 2200-word slot FAILS word_cap; waiver flips a failure; determinism.

### Notes
- number_provenance number-extraction: raw tokens are captured verbatim (so an orphan reports
  exactly as it appears, e.g. `$95.00`, `8.5%`); ISO dates, `vX.Y.Z` version strings, the
  `## Page N` section headers, and the `52-Week`/`52wk` column label are scrubbed before
  extraction so their digits never register as orphans; `100`/`1.0`/`1`/`0` are treated as
  report-format constants. No §12 check was weakened to be implementable.

## 0.3.0 — 2026-07-16 · Phase 3: Decision Layer

Gate 3 (full decision chain on the three validated bundles): 3/3 PASS — all
composite/EV/sizing arithmetic reproduced by independent hand-recomputation; the
MU standalone options run reproduced a hand-verified prototype's economics on the
same 2026-07-15 chain (CSP mark exact, bull-put PoP 0.70 vs 0.68). Gate-3 fixes:
term-structure tenor window (0-DTE stubs and LEAPs excluded), monthly-first expiry
selection (a closer-but-illiquid weekly was silently killing every structure),
always-global binary-event warning, expression executability disclosure when all
structures are declined. Known deviation: vertical widths follow "1-2 strikes
below" literally, narrower than the 5-10%-of-spot target on dense chains (v0.4
polish).

### Added
- **Options-strategy decision skill** (rubric v1.0.0): `scripts/options_strategy.py` +
  `skills/options-strategy/SKILL.md` — the **L3 structure-selection layer**. It turns a
  DIRECTION + the REAL options chain into concrete, defined-risk option STRUCTURES —
  real strikes only, economics minted from chain marks, probabilities shown as LABELED
  delta approximations, and mechanical honesty gates. It reads the newest snapshot's
  options/sentiment/events blocks + the on-disk chain (loaded ONLY via
  `chain.load_contracts`, NEVER into LLM context) and scores NO snapshot field directly
  (`INPUT_FIELDS = set()`), so single-mapping is preserved by construction (added to
  `tests/test_single_mapping.py` SKILLS like composite/trade-plan). **The central lesson
  it encodes:** IV LEVEL alone never selects a strategy — **IV-vs-REALIZED is the
  PRIMARY GATE**. `vol_verdict(options.iv_minus_rv20)`: `≤ −0.03` → `cheap_vs_realized`
  (no premium-selling edge; long premium viable), `≥ +0.03` → `rich_vs_realized`
  (premium selling favored), between → `fair`, null → `unknown` (treated as fair +
  disclosed). (The MU prototype: a 96% IV that LOOKED rich but sat ~14 pts BELOW
  ~110–116% realized was CHEAP, not rich — a naive "sell premium" call would have been
  wrong.) **Vol dashboard** also carries iv30, rv20, iv_pctile, **term structure**
  (front-vs-back ATM IV: backwardation/contango/flat), 25d skew. **Expiry selection** —
  monthlies preferred (3rd-Friday heuristic `is_monthlyish`); pipeline with a catalyst
  ≤ 60 DTE → first monthlyish expiry AFTER the catalyst, else nearest 45 DTE within
  [30,90]. **Strikes by delta off the real chain** — short put/call ≈ 0.30Δ, long call
  ≈ 0.55Δ, wings 1–2 strikes out, condor shorts ≈ 0.25Δ; pipeline CSP aligns to the
  stock plan's `entry_1` when within 2% of a listed put strike. **Selection matrix
  (direction × vol verdict)** — bullish×rich/fair → bull_put_spread + cash_secured_put;
  bullish×cheap → long_call_vertical (+ bull_put_spread w/ warning); bearish×rich/fair →
  bear_call_spread; bearish×cheap → long_put_vertical (+ bear_call_spread w/ warning);
  neutral×rich → iron_condor; **neutral×cheap/fair → NO premium structure** (a `declined`
  "stand aside" entry). **Economics from chain marks** — net credit/debit, max
  profit/loss, breakevens, PoP with a named `pop_method` (`1 − |Δ short|` credit /
  `|Δ long|` debit), all round-tripped in an `arithmetic` string. **Iron-condor honesty
  check** — profit-zone half-width inside the snapshot 1σ expected move → warning +
  `pop_full_profit_note` (full-profit probability is LOW). **Liquidity gate** (per leg)
  — `oi ≥ 100` AND `spread ≤ max(0.10, 0.10×mark)`; failing leg → structure `declined`;
  < 2 viable → `liquidity_verdict: "thin — declining to force structures"`. **Honesty
  gates** — cheap-vs-realized tags every credit structure ("premium sellers are NOT
  being paid for delivered vol"); earnings ≤ 30d excludes the CSP + tags all structures
  ("IV-crush/defined-risk-only into event"); ex-div within tenor tags short-call legs
  (early-assignment). **Management rules** per family (credit 50%/2×/21 DTE; condor
  25–35%/roll untested; debit 100%/−50%/21 DTE). **Hedge** (pipeline, if the stock
  plan's hedge is required) — a put spread from the hedge `strikes_from`; cost/spot over
  the premium cap → a **collar alternative** (short call ≈ 0.20Δ) emitted + disclosed.
  **Two modes:** `pipeline` derives direction from the composite grade (A|B → bullish,
  C → neutral, D → bearish) and requires both `module_composite.json` and
  `module_tradeplan.json` (exit 2 if either missing), aligning to the stock plan and
  feeding recommended structures (each carrying top-level `strikes`) back to trade-plan's
  `--synthesize`; `standalone` requires an explicit `--direction` (exit 2 if absent).
  The chain file at `snapshot.options.chain_file_path` is resolved relative to the
  bundle (exit 2 if unreadable). Writes `<bundle>/module_options.json` (deterministic,
  `sort_keys`). Tests: `tests/test_options_strategy.py` (65 tests) — delta-targeted
  strike picks, exact credit/debit economics, CSP entry alignment, condor
  inside-1σ warning, all six direction×verdict branches, liquidity declines + thin
  verdict, event gates, cheap-vs-realized warnings, hedge + collar breach, term
  structure, pipeline direction-from-grade, standalone `--direction` requirement,
  missing-chain exit 2, determinism. Full suite: 545 tests green.
- **Trade-plan decision skill** (rubric v1.0.0; expression decision table
  `expression-v1.0.0`): `scripts/trade_plan.py` + `skills/trade-plan/SKILL.md` — the
  **L3 execution layer**. It turns the composite into an EXECUTABLE plan: it consumes
  module outputs (`module_composite.json`'s EV block, `module_technical.json`'s S/R
  ladder, `module_risk.json`'s downside_map) and reads the newest snapshot only for
  plan references (`price.last`, `events.next_earnings.date`,
  `sentiment.iv_pctile_1yr`, `options.iv_minus_rv20`, `fundamentals.eps_ntm_consensus`)
  — it scores NO snapshot field directly (`INPUT_FIELDS = set()`), so single-mapping
  is preserved by construction (added to `tests/test_single_mapping.py` SKILLS like
  composite). **ALL sizing/EV/required-multiple math is delegated to
  `scripts/ev_kelly.py`** (`ev_at`, `kelly`, `size_recommendation`). **Two passes.**
  **Pass 1 (`--stock-plan`)** mints: an **entry ladder** — valuation anchors =
  `{composite.ev.ev_breakeven_entry}` ∪ downside_map `valuation_floor` rows; a proven
  support (swing_low/ma50/ma200/put_wall) within 3% of an anchor is a **confluence**;
  `entry_1` = highest confluence below `last`, **unless** `ev_at_current ≥ hurdle_total`
  → `entry_1` = current price **sized down** (half recommended); `entry_2`/`entry_3` =
  next lower confluences/proven supports, distinct and ≥3% apart (max 3); each carries
  its `ev_at_level`. **Exits** — `profit_take` = nearest ladder resistance above `last`;
  `bull_target` = max scenario target with `required_multiple = target / eps_ntm`
  ("implies N× fwd EPS", null-safe). **Invalidation (BOTH legs mandatory)** —
  technical leg (weekly close below the first proven support under the deepest entry,
  minted off the ladder) + a REQUIRED fundamental leg (`--fund-invalidation-metric /
  -threshold / -justification`, no defaults → exit 2). **Sizing** — full Kelly at
  `entry_1` capped by profile (5/8/10% trader/balanced/long-term), −1 notch
  (quarter-Kelly + half-cap) on a binary event within 30d; the full arithmetic string
  is emitted. **Hedge** — required iff (binary30d AND recommended ≥ 5%) OR (iv_pctile
  ≤ 25); each clause fires independently; spec names trigger, structure, `strikes_from`
  (first two downside_map levels), expiry rule, premium cap 1.5%. **Don't-chase** — 5%
  above the top entry. **Expression decision table (`expression-v1.0.0`)** — a decision
  of record formalizing the lived rule *a catalyst in sight selects options for
  leverage; the profile only implements*: RULE 1 (selector) days-to-catalyst ≤ 60 AND
  `--catalyst-in-thesis yes` → options-tilted for ALL profiles (long-term still gets a
  small defined-risk options **kicker**); RULE 2 → per-profile default; MODULATORS
  appended in order (iv_minus_rv ≥ +0.05 premium-selling; ≤ −0.05 long-premium viable;
  days ≤ 30 defined-risk-only). The `--catalyst-in-thesis yes|no` selector flag is
  REQUIRED (no default → exit 2). **Pass 2 (`--synthesize`)** re-reads the plan +
  `module_options.json` (exit 2 "run options-strategy first" if missing) and folds the
  options module's chosen structures (names + strikes) and hedge spec into
  `expression` (`synthesized: true`, `structures_selected`, `hedge_structure`); a
  recommended structure missing strikes → exit 2 (consistency). Writes
  `<bundle>/module_tradeplan.json` (`stock_plan`, preliminary/synthesized `expression`,
  `flags`, `event_playbook: null` + `signal: null` LLM prose slots). A missing
  `module_composite.json` → exit 2 ("run composite-score first"). Test coverage:
  `tests/test_trade_plan.py` (62 tests — days-to-catalyst/binary-event helpers,
  confluence + entry spacing + EV-at-level + ev≥hurdle sized-down branch, exits +
  required-multiple, both-leg invalidation, Kelly sizing recomputed against ev_kelly,
  hedge firing on each clause independently + null-safety, the full expression decision
  table incl. selector/default/modulator order, and CLI end-to-end for both passes incl.
  every exit-2 gate + determinism). Files: `scripts/trade_plan.py`,
  `skills/trade-plan/SKILL.md`, `tests/test_trade_plan.py`,
  `tests/test_single_mapping.py`.
- **Composite-score decision skill** (composite rubric v1.0.0): `scripts/score_composite.py`
  + `skills/composite-score/SKILL.md` — the **L3 decision layer**. It CONSUMES the
  four evidence module JSONs' final scores (`module_technical.json`,
  `module_fundamental.json`, `module_sentiment.json`, `module_risk.json`) — it does
  NOT re-read the snapshot's scored fields — adds a fifth **thesis-conviction**
  dimension it computes in-script, applies **FIXED per-profile weights** (spec §9.3,
  never hand-tuned), and produces the composite (0-100), a letter grade, an action,
  and an expected-value block. **Thesis conviction** (0-100): EV asymmetry (max 40,
  mechanical — `ev / hurdle` banded, where `ev = ev_kelly.ev_at(scenarios, last)` and
  `hurdle_total = 0.08 × horizon_years` with horizon convention trader 0.5 / balanced
  1.5 / long-term 4.0) + variant perception (`strong|some|none` 20/12/4) + catalyst
  clarity (`clear|partial|vague` 20/12/4) + invalidation quality
  (`both-legs|one-leg|none` 20/10/0). All four judgment flags are REQUIRED with no
  defaults and each carries a mandatory justification — conviction is asserted, never
  assumed; the scenario set (with mandatory `--scenario-reasoning`) is REQUIRED too
  (a missing scenario file, a probability sum ≠ 1 via `ev_kelly.scenario_ev`, a
  missing flag, or a missing justification is exit 2). **Weights** (renormalized over
  PRESENT dimensions, disclosed): balanced .25/.25/.20/.15/.15,
  trader .35/.10/.25/.15/.15, long-term .10/.40/.15/.15/.20 across
  technical/fundamental/sentiment/risk/thesis_conviction. A missing evidence module
  excludes that dimension and rescales the remaining weights to sum 1; ≥ 3 of 5
  dimensions missing → exit 2 ("insufficient evidence modules"). **Grades** (fixed):
  A ≥80 Buy/Add; B 60-79 Hold/Accumulate-on-weakness; C 45-59 Hold/Trim; D <45
  Reduce/Avoid. **EV block**: `ev_at_current`, `hurdle_total`,
  `horizon_years_convention`, `ev_breakeven_entry = Σ(p·target)/(1+hurdle_total)` (the
  entry at which EV exactly clears the hurdle — derivation in code), and repeatable
  `--entry-level` → `ev_at_levels`. **Sensitivity** recomputes the FULL composite —
  including EV asymmetry re-banded per each profile's own hurdle — for all three
  profiles, so the same name can grade B under one lens and C under another. All EV
  math is delegated to `scripts/ev_kelly.py` (`ev_at`, `scenario_ev`); the module
  scores NO snapshot field directly (`INPUT_FIELDS = set()`, reads `price.last` only
  as an EV reference), so single-mapping is preserved by construction and it is added
  to the `tests/test_single_mapping.py` SKILLS dict (governance checks stay green
  trivially). Writes `<bundle>/module_composite.json` with per-dimension rows (score,
  weight, weight_renormalized, contribution, source), the thesis-conviction subscore
  arithmetic strings, the EV block, the three-profile sensitivity, all judgment
  flags, `renormalization_note`, and `tension: null`/`signal: null` (LLM prose slots
  — the one-line tension sentence and any signal live only in the brief, never as
  numbers in the JSON). CLI: `python3 scripts/score_composite.py --bundle <dir>
  --scenarios <path> --scenario-reasoning "…" --variant X --variant-justification "…"
  --catalyst-clarity X --catalyst-clarity-justification "…" --invalidation X
  --invalidation-justification "…" [--profile P] [--entry-level N]... [--out <path>]`.
  Test coverage: `tests/test_score_composite.py` (39 tests — thesis-conviction bands
  per profile, fixed weighting + renormalization, fixed grade bands, EV block +
  break-even + entry-level EV, three-profile sensitivity, and CLI end-to-end incl.
  every exit-2 gate + determinism). Files: `scripts/score_composite.py`,
  `skills/composite-score/SKILL.md`, `tests/test_score_composite.py`,
  `tests/test_single_mapping.py`.
- **Compressed-pass fundamental scorer** (fundamental rubric v1.0.0,
  `compressed_snapshot_pass`): `scripts/score_fundamental.py` is the
  ALWAYS-AVAILABLE fundamental path (design spec §8.1 "FSI absent" branch) — when
  the deep FSI initiation / model reuse is not applied, the composite still gets a
  disclosed, snapshot-only fundamental score instead of a blank dimension. Scores
  two dimensions off an existing snapshot bundle: **Quality** (50 — revenue growth
  15, gross+operating margins 8+7, returns-on-capital/roe 10 with a
  **percent-vs-fraction normalization** where a `roe` value >3 is read as a percent
  and divided by 100 with that normalization labeled in the arithmetic, and FCF
  margin = `fcf_ttm / rev_ttm` 10) and **Valuation** (50 — fwd P/E vs the ticker's
  own 5-yr median 20, PEG 15, FCF yield 15). The pe-vs-history component carries the
  snapshot's `valuation.pe_median_method` label (`approx_current_eps`) into its
  arithmetic string so the median's approximation is disclosed wherever it scores.
  Consumes the **snapshot only** (no dependency on other module JSON or the ladder,
  scores no price levels). Writes `<bundle>/module_fundamental.json` with a
  top-level `fundamental_mode: "compressed_snapshot_pass"` + `mode_note` so a reader
  always knows this was the snapshot-only pass (not the deep model), per-subscore
  arithmetic strings (the actual numbers), verbatim `quality` and `valuation`
  tables, empty `flags` (this pass is fully mechanical — no judgment flags), and
  `signal: null` (the LLM writes the one-line signal in the brief, never numbers).
  Whole-dimension null inputs renormalize the 0-100 score over the remaining max and
  flag `renormalized: true`. CLI: `python3 scripts/score_fundamental.py --bundle
  <dir> [--out <path>]`. SINGLE-MAPPING SPLIT (spec §2): balance-sheet SOLVENCY
  (`fundamentals.net_cash_defined.net`) stays OWNED by risk-analytics and
  EPS-REVISIONS (`fundamentals.revisions_90d`) stay OWNED by sentiment-positioning —
  neither is scored here; `valuation.pe_5yr_median` is scored HERE (risk uses it
  only as an unscored downside-map level, no collision). Files:
  `scripts/score_fundamental.py`, `tests/test_score_fundamental.py`;
  `tests/test_single_mapping.py` now runs its two governance checks 4-way across
  technical/risk/sentiment/fundamental (INPUT_FIELDS verified pairwise disjoint).

## 0.2.0 — 2026-07-16 · Phase 2: Evidence Skills

Gate 2 (validation on the three Gate-1 bundles, fresh agents executing the
SKILL.mds): 3/3 PASS — 9/9 independently recomputed subscores matched module
arithmetic exactly; ma_ordering QC check went live (SKIP→PASS) via AAPL's
trend_claim; ETSY fired the vertical-rally penalty (+14.5%/15d) and the >15%
SI band; MU (−14%/15d) correctly did not. Gate-2 fix: downside map now emits
NEAREST-FIRST (descending) — ascending order made "top rows" read as the
deepest anchors instead of the first supports price would fall through.

### Added
- **`sentiment-positioning` evidence skill** (rubric v1.0.0):
  `scripts/score_sentiment.py` scores five dimensions off an existing snapshot
  bundle — street view (25, analyst buy% + PT-vs-price + a `--rating-actions`
  judgment flag; **spec §5.2: a below-price consensus target caps the WHOLE
  dimension at 10/25**), revisions momentum (20, 90-day EPS-revision band + an
  up/down-30d count adjustment capped/floored to the band), smart money & insiders
  (20, a `--inst-flow` 13F judgment flag defaulting to `unknown`/0 with the 45-day
  lag disclosed + insider net-90d, where non-positive net reads 8 pts "routine"
  under the default `--insider-baseline normal` or 2 pts under `unusual`),
  positioning & derivatives (20, short interest with a **complacency guard** —
  `si <1.5%` AND `rsi14 >70` scores 2 not bullish — evaluated before the normal SI
  bands, + full-chain put/call, + 1-yr IV percentile with a hedges-cheap note when
  <25), and price momentum (15, 12m + 3m relative-to-SPY + 6m absolute). Consumes
  the **snapshot only** — no dependency on `module_technical.json` or the ladder
  (it scores no price levels). Writes `<bundle>/module_sentiment.json` with
  per-subscore arithmetic strings (the actual numbers), a verbatim `positioning`
  table (realtime P/C + iv30 + implied move are unscored context), a
  `momentum_vs_spy` table (rel_3m/rel_12m computed in-script), a `hedging_cost_note`
  (set when IV percentile <25), the three judgment flags, and `signal: null` (the
  LLM writes the one-line signal in the brief, never numbers). Whole-dimension null
  inputs renormalize the 0-100 score over the remaining max and flag
  `renormalized: true`. Each non-default judgment flag (`--rating-actions` ≠
  neutral, `--inst-flow` ≠ unknown, `--insider-baseline` = unusual) requires a
  justification or the CLI exits 2. SINGLE-MAPPING SPLIT (spec §2): options
  *sentiment* fields (P/C, IV percentile, skew) score HERE; options-derived *levels*
  score in technical-analysis. PT-upside scores HERE (street view), not in
  risk-analytics (which documents that reallocation). `INPUT_FIELDS` declares the
  twelve scored snapshot fields; `GUARD_FIELDS` = `{technicals.rsi14}` (it
  gates/caps the complacency guard here but is scored only in technical-analysis —
  guard fields may gate/cap here but score elsewhere); `price.last` and the ladder
  are shared reference infrastructure, deliberately excluded.
  `skills/sentiment-positioning/SKILL.md` forms the three judgment flags from
  snapshot text/context only, runs the script, and writes prose only (score
  headline → ≤120-word paragraph → momentum/positioning mini-table → hedging-cost
  note → one-line signal → rubric-version footer).
- **`tests/test_single_mapping.py`** (governance): imports the three scorer modules
  and asserts (a) their `INPUT_FIELDS` are pairwise disjoint — no snapshot fact is
  scored in two modules — and (b) no scorer scores its own `GUARD_FIELDS`. Pins the
  spec's single-mapping rule ("each snapshot fact scores in exactly one module")
  mechanically. No overlaps found across technical / risk / sentiment.
- **88 unit tests** (`tests/test_score_sentiment.py` + `tests/test_single_mapping.py`):
  every scoring branch pinned to a hand-computed value (buy_pct bands; PT bands +
  the PT-below-price dimension cap; revisions bands + up/down adjustment cap/floor;
  inst-flow incl. unknown→0; insider normal/unusual/positive/null; the complacency
  guard firing at si 1.2/rsi 74→2 vs not at si 1.2/rsi 55→6; SI percent-unit bands
  incl. 26.23→3; P/C + IV-percentile bands + the hedging note; momentum rel bands;
  renormalization; determinism), plus the three justification-required CLI exits and
  an end-to-end CLI run against a real fabricated bundle.
- **`risk-analytics` evidence skill** (rubric v1.0.0): `scripts/score_risk.py`
  scores four dimensions off an existing snapshot bundle — volatility state (25,
  rv30-vs-10yr percentile + benchmark beta), drawdown profile (25, max 10-yr
  drawdown + 30% episode count + a 20%-vs-30% episode-spread severity proxy),
  margin of safety (30, distance below the all-time high + ladder asymmetry of
  proven-support-vs-resistance distance), and liquidity & solvency (20, 3-month
  average dollar volume + net-cash ratio). **Higher score = better risk-reward
  conditions** (calm, discounted, asymmetric, liquid, cash-rich = near 100), the
  opposite polarity from a danger meter. CONSUMES `<bundle>/module_technical.json`
  for the shared S/R ladder (asymmetry reads `levels.nearest_support` /
  `nearest_resistance` off it); exits 2 asking for technical-analysis first when
  the module is absent. Writes `<bundle>/module_risk.json` with per-subscore
  arithmetic strings (the actual numbers), a `downside_map` table (ladder entries
  below `last` + a script-computed valuation-floor row `pe_5yr_median × eps_ntm`
  inserted in sorted position + an optional stress row `last × (1 + stress_pct)`),
  a verbatim `vol_profile` context block (correlation is context, unscored), flags,
  and `signal: null` (the LLM writes the one-line signal in the brief, never
  numbers). Whole-dimension null inputs renormalize the 0-100 score over the
  remaining max and flag `renormalized: true`. The `--stress-pct` flag requires
  `--top-risk` (a named single risk) — a judgment input, never computed in prose.
  DEVIATION FROM DESIGN-SPEC §5.3 (documented in SKILL.md Important Notes and a
  code comment): consensus-PT upside is scored ONLY in sentiment-positioning, not
  here — the spec listed it in both modules, violating its own single-mapping rule;
  the ~10 points are reallocated into the asymmetry (18) + dist-from-ATH (12)
  components. `INPUT_FIELDS` declares the nine scored snapshot fields; `price.last`
  and the ladder are shared reference infrastructure, deliberately excluded.
  `skills/risk-analytics/SKILL.md` runs the script and writes prose only (score
  headline → ≤120-word paragraph → downside-map mini-table → SPY correlation note →
  one-line signal → rubric-version footer).
- **59 unit tests** (`tests/test_score_risk.py`): every scoring branch pinned to a
  hand-computed value (vol percentile/beta bands, max-dd/episode/spread bands,
  dist-from-ATH bands, asymmetry ratios incl. blue-sky convention and
  no-proven-floor, ADV/net-cash bands, valuation-floor arithmetic, stress-row
  arithmetic + top-risk guard, renormalization, determinism), plus an end-to-end
  CLI run against a real fabricated bundle including the missing-module-technical
  exit-2 guard. Full suite: 213 tests green.
- **`technical-analysis` evidence skill** (rubric v1.0.0): `scripts/score_technical.py`
  scores four dimensions off an existing snapshot bundle — trend structure (30),
  momentum (25, RSI band + optional cited divergence adjustment + MACD state),
  structure & levels (25, proven-support proximity + resistance headroom +
  confluence, all read off the shared `levels.py` S/R ladder), and
  volume & extension (20, distance above MA200 + volume regime − vertical-rally
  penalty). Writes `<bundle>/module_technical.json` with per-subscore arithmetic
  strings (the actual numbers), a mechanical `trend_claim`, the ladder, a
  divergence flag, and `signal: null` (the LLM writes the one-line signal in the
  brief, never numbers). Whole-dimension null inputs renormalize the 0-100 score
  over the remaining max and flag `renormalized: true`. `INPUT_FIELDS` declares the
  nine scored snapshot fields (Task-13 cross-skill disjointness will import it);
  `price.last` and the ladder are shared reference infrastructure, deliberately
  excluded. `skills/technical-analysis/SKILL.md` runs the script and writes prose
  only (score headline → ≤120-word paragraph → S/R ladder mini-table → one-line
  signal → rubric-version footer). This is the FIRST scored evidence module, so
  its arithmetic is the rubric of record.
- **53 unit tests** (`tests/test_score_technical.py`): every scoring branch pinned
  to a hand-computed value, plus an end-to-end CLI run against a real fabricated
  bundle (module contract, determinism, divergence-requires-justification guard).
  Full suite: 154 tests green.

## 0.1.0 — 2026-07-16 · Phase 1: Data Engine

First shipped phase. Whole-repo review verdict: SHIP.

### Added
- **`market-snapshot` skill** (L1 data engine): one Alpha-Vantage-first fetch pass →
  `snapshot_<TICKER>_<date>.json` + options chain file, behind a blocking QC gate.
  Schema v0.2.0. Web gap-fill for short interest, spot cross-check, and earnings-calendar
  fallback. IV-history sampling (~26 biweekly EOD chains) for 1-yr IV percentile.
- **Shared scripts** (stdlib-only, Python ≥ 3.10, 74 unit tests):
  `indicators.py` (SMA/EMA/RSI/MACD/returns/RV/beta/drawdowns/percentile),
  `chain.py` (offloaded-chain parser: ATM IV, expected moves, max pain, OI walls,
  OI- and volume-based P/C, 25Δ skew), `ev_kelly.py` (scenario EV, Kelly sizing),
  `qc.py` + `qc_gate.py` (9 blocking checks with waiver/skip disclosure),
  `build_snapshot.py` (manifest-driven builder — the only path from raw API data to
  snapshot numbers; LLM edits qualitative text slots only).
- Plugin scaffold: `.mcp.json` (key via `${ALPHAVANTAGE_API_KEY}` only), marketplace
  manifest, MIT license.

### Validation (Gate 1)
Standalone runs on AAPL (mega-cap), MU (high-volatility, −8% session), ETSY (mid-cap):
3/3 QC gate pass, zero waivers in final state, zero hand-edited numbers.
Findings fixed during the gate:
- `check_mktcap` made staleness-aware: vendor market cap is computed from the prior
  close and false-positived on big-move days; the check now reconciles share count
  against last **or** previous close and discloses vendor staleness.
- P/C cross-check made like-for-like: chain P/C was OI-based while vendor realtime P/C
  is volume-based (MU capitulation day: 1.29 vs 0.93, both correct). Snapshot now
  carries both bases; QC compares volume-vs-volume only.
- SKILL.md mandates `return_full_data=true` (MCP preview truncation silently corrupts
  TTM sums) and `datatype=json` (CSV defaults are unparseable), and copies offloaded
  results into the bundle (temp paths get reaped; bundles must be self-contained).
