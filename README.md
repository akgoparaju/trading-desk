# trade-decision

## What it is

A Claude Code plugin that produces short (≈3-page) trade decision reports built on institutional-depth evidence. It fetches market data from Alpha Vantage, verifies it, and computes every figure in tested in-repo Python — the LLM writes prose, never arithmetic. A single verified `snapshot.json` is the source of truth for every downstream skill, so numbers stay consistent across technicals, sentiment, valuation, and the final trade plan. A blocking QC gate must pass before any snapshot is used, and every number carries its endpoint and retrieval timestamp for provenance.

## Status

**Phase 1 — data engine.** The `market-snapshot` skill (L1 data engine) is shipped: it builds and QC-gates the snapshot that everything else depends on. The evidence, decision, and report skills listed below are planned.

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
| `technical-analysis` | planned |
| `sentiment-positioning` | planned |
| `risk-analytics` | planned |
| `composite-score` | planned |
| `trade-plan` | planned |
| `options-strategy` | planned |
| `report-renderer` | planned |
| `full-trade-analysis` | planned |

## Data & provenance

Every number in a snapshot traces to an Alpha Vantage endpoint or a public web source, each recorded with its retrieval timestamp under `meta.sources`. The blocking QC gate reconciles internal consistency (market cap, P/E, net cash, MA ordering, ranges, spot-check tolerance, options freshness, staleness) and stamps an attestation into `meta.qc`. Snapshot schema version: **v0.2.0**.

## Development

```
python3 -m unittest discover -s tests
```

Python 3.10+, standard library only — no pip installs.

## Disclaimer

For educational and research purposes only. Not financial advice. Nothing here is a recommendation to buy or sell any security. Verify all figures independently before acting.
