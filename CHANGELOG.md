# Changelog

## [Unreleased]

### Added
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
