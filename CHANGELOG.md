# Changelog

## Unreleased — Phase 2: Evidence Skills

### Added
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
