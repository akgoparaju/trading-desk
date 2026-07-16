# Changelog

## Unreleased — Phase 4: Assembly

### Added
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
