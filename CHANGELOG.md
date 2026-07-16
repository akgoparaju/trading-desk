# Changelog

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
