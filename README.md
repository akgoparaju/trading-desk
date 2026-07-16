# trade-decision

## What it is

A Claude Code plugin that produces short (≈3-page) trade decision reports built on institutional-depth evidence. It fetches market data from Alpha Vantage, verifies it, and computes every figure in tested in-repo Python — the LLM writes prose, never arithmetic. A single verified `snapshot.json` is the source of truth for every downstream skill, so numbers stay consistent across technicals, sentiment, valuation, and the final trade plan. A blocking QC gate must pass before any snapshot is used, and every number carries its endpoint and retrieval timestamp for provenance.

## Status

**Phases 1–4 shipped.** The full pipeline is wired end to end: the `market-snapshot` data engine (L1), the four evidence modules (technical, sentiment, risk, and a compressed fundamental pass), the `composite-score` decision layer (L3), the `trade-plan` + `options-strategy` execution layer, the `report-renderer` 3-page output with its blocking QC gate (L4), and the `full-trade-analysis` orchestrator (L5) that runs them all through phase gates. Acceptance validation V1–V6 (end-to-end runs across a handful of names to review grade distribution and calibrate the provisional weights/bands) is in progress.

## Install

```
/plugin marketplace add <owner>/trade-decision
/plugin install trade-decision
export ALPHAVANTAGE_API_KEY=your_key
```

Get an API key from [alphavantage.co](https://www.alphavantage.co/support/#api-key). Set `ALPHAVANTAGE_API_KEY` in your environment before running any skill — the bundled MCP server reads it from there.

## API tier notes

- **Premium 75 req/min is recommended.** The snapshot fetch pass is ~15 calls and runs without pacing at this tier; the optional IV-history sampling adds ~26 calls (~21 s).
- **`REALTIME_OPTIONS` requires a 600+/min tier.** Below that, the plugin uses the EOD `HISTORICAL_OPTIONS` chain and discloses it in `meta.api_tier_notes`.
- **Free tier (500 req/day) works, degraded.** IV-history sampling is skipped, `iv_pctile_1yr` comes back `null`, and the degradation is disclosed in `meta.api_tier_notes` — never silently.

## FSI integration (optional)

Deep fundamental and valuation work can reuse the `equity-research` and `financial-analysis` skills from the [claude-for-financial-services](https://github.com/anthropics/claude-for-financial-services) marketplace. When those plugins are installed, trade-decision hands off to them for richer modeling. When they are absent, it runs a compressed fundamental pass instead and discloses the reduced depth. The soft dependency is declared in `.claude-plugin/marketplace.json` (`allowCrossMarketplaceDependenciesOn`); nothing about the FSI plugins is required for Phase 1.

## Skills

| Skill | Status |
|-------|--------|
| `market-snapshot` | available |
| `technical-analysis` | available |
| `sentiment-positioning` | available |
| `risk-analytics` | available |
| `composite-score` | available |
| `trade-plan` | available |
| `options-strategy` | available |
| `report-renderer` | available |
| `full-trade-analysis` | available |

Run the whole pipeline in one shot: **`full trade analysis NVDA`** — snapshot → evidence → composite → plan + options → 3-page report → thesis + re-score offer.

## Data & provenance

Every number in a snapshot traces to an Alpha Vantage endpoint or a public web source, each recorded with its retrieval timestamp under `meta.sources`. The blocking QC gate reconciles internal consistency (market cap, P/E, net cash, MA ordering, ranges, spot-check tolerance, options freshness, staleness) and stamps an attestation into `meta.qc`. Snapshot schema version: **v0.2.1**.

## Development

```
python3 -m unittest discover -s tests
```

Python 3.10+, standard library only — no pip installs.

## Disclaimer

For educational and research purposes only. Not financial advice. Nothing here is a recommendation to buy or sell any security. Verify all figures independently before acting.
