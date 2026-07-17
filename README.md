# trading-desk

## What it is

A Claude Code plugin that produces short (≈3-page) trade decision reports built on institutional-depth evidence. It fetches market data from Alpha Vantage, verifies it, and computes every figure in tested in-repo Python — the LLM writes prose, never arithmetic. A single verified `snapshot.json` is the source of truth for every downstream skill, so numbers stay consistent across technicals, sentiment, valuation, and the final trade plan. A blocking QC gate must pass before any snapshot is used, and every number carries its endpoint and retrieval timestamp for provenance.

## Status

**Phases 1–4 shipped.** The full pipeline is wired end to end: the `market-snapshot` data engine (L1), the four evidence modules (technical, sentiment, risk, and a compressed fundamental pass), the `composite-score` decision layer (L3), the `trade-plan` + `options-strategy` execution layer, the `report-renderer` 3-page output with its blocking QC gate (L4), and the `full-trade-analysis` orchestrator (L5) that runs them all through phase gates. Acceptance validation V1–V6 (end-to-end runs across a handful of names to review grade distribution and calibrate the provisional weights/bands) is in progress.

## Install

```
/plugin marketplace add akgoparaju/trading-desk
/plugin install trading-desk
```

**The plugin installs skills + scripts only — NO MCP servers, no auto-dependencies.**
Data sources are yours to connect (or none: the built-in `stooq+web` mode needs no key):

```bash
# optional — Alpha Vantage (recommended for full depth incl. options chains):
claude mcp add --transport http alphavantage "https://mcp.alphavantage.co/mcp?apikey=YOUR_KEY"
# or connect any market-data MCP (FMP, Polygon, ...) — see docs/CANONICAL_CONTRACT.md
```
The first run asks which connected source to use and remembers it (`trading_desk_config.json`). After install, the first session shows a ONE-TIME notice if the optional FSI plugins are absent; say `set up FSI` any time to get the 2-command install.

Optional: get an Alpha Vantage key from [alphavantage.co](https://www.alphavantage.co/support/#api-key) and connect the MCP yourself (command above). There is no bundled server — a source is only present if you added it.

## Data modes

The `market-snapshot` skill runs a **data-mode preflight** before fetching anything. Note that with **no key exported the bundled AV MCP still answers** — Alpha Vantage serves empty-key traffic at an anonymous free quota, so a connected MCP does NOT mean you have a usable key. The preflight detects which of three modes you are actually in, announces it, and (interactively) asks before proceeding on a degraded one:

- **`alpha_vantage` (premium, full).** Premium 75 req/min is recommended: the snapshot fetch pass is ~15 calls and runs without pacing; the optional IV-history sampling adds ~26 calls (~21 s). `REALTIME_OPTIONS` needs a 600+/min tier — below that the EOD `HISTORICAL_OPTIONS` chain is used and disclosed in `meta.api_tier_notes`.
- **`av_free_degraded` (free key OR no key exported).** The anonymous / free tier is a **~25-call/day** budget — so **at most one run per day**. Premium endpoints are entitlement-blocked, so you **lose adjusted multi-year history, the options chain, and IV history** (`iv_pctile_1yr` → `null`); the pass is ~13–15 calls. Blocked field groups fall back to the web path below. Every degradation is disclosed in `meta.api_tier_notes` — never silently.
- **`web_fallback` (no AV MCP at all).** FSI-style **cited web research** funneled through the same QC'd pipeline: OHLCV from stooq CSV (split-adjusted; multi-year stats work), fundamentals/overview transcribed **verbatim** from SEC filings / IR / reputable aggregators with per-figure citations, then the same in-repo Python computes every number and the QC gate's arithmetic cross-checks (P/E, mktcap, net-cash) audit the transcription. **Options stand aside** (no chain available) — disclosed. Web-filled fields are listed in `snapshot.fundamentals.web_transcribed_fields`; stooq provenance in `technicals.series_source`; the mode in `meta.data_mode`.

## Bring your own data source

Alpha Vantage is the default, not a requirement. The `market-snapshot` skill runs a **source preflight** that sweeps your connected MCP servers for market-data-shaped tools and offers them alongside the built-ins (`alphavantage`, `stooq+web`). It **asks once**, then persists your choice in `./trading_desk_config.json` (`{"primary_source", "fallbacks", "asked": true}`) so later runs never re-ask — say "change data source" or pass `--reconfigure` to re-open it. The chosen source is recorded as `meta.data_source` in every snapshot.

The builder accepts a fixed, source-neutral set of raw file shapes documented in [`docs/CANONICAL_CONTRACT.md`](docs/CANONICAL_CONTRACT.md). Adapting a foreign source splits by field group: **scalar groups** are transcribed verbatim with per-figure citations (the same cited-web pattern the fallback path uses), and the three **bulk artifacts** (ticker daily series, SPY daily series, options chain) get a small **structural transform** — field mapping only, never arithmetic — that emits an accepted shape. Those transforms are **client-generated and live in your workspace** at `trading_desk_config/adapters/<source>_<group>.py`; they are re-run verbatim on later fetches (so a refresh delta can't misread parsing drift as market movement) and are never plugin code. The QC gate audits the result the same way regardless of source.

## Output layout

A run writes under a per-ticker parent in the invoker's CWD:

```
trading_desk_<TICKER>/
├── <TICKER>_Trade_Report_<date>.md      ← the 3-page report (delta reports too)
├── iv_history_<TICKER>.json             ← IV-history cache (persists across dates)
└── detail_reports_<date>/               ← the dated bundle
    ├── snapshot_<TICKER>_<date>.json     ← the verified single source of truth
    ├── manifest.json                     ← sources + data_mode + retrieval timestamps
    ├── module_{technical,risk,sentiment,fundamental,composite,tradeplan,options}.json
    ├── brief_<dim>.md                    ← per-dimension evidence briefs
    └── raw/                              ← raw AV / web responses (incl. the options chain, never read into context)
```

The report lands in the **parent** `trading_desk_<TICKER>/` (a sibling of the dated data folder), so it is easy to find next to prior dates. (Legacy `td_bundle_<TICKER>_<date>/` bundles still work — discovery globs both; those keep the report inside the bundle.)

## FSI integration (optional)

Deep fundamental and valuation work can reuse the `equity-research` and `financial-analysis` skills from the [claude-for-financial-services](https://github.com/anthropics/claude-for-financial-services) marketplace. When those plugins are installed, trading-desk hands off to them for richer modeling. When they are absent, it runs a compressed fundamental pass instead and discloses the reduced depth — and at runtime, `full-trade-analysis` (and standalone `composite-score`) will **offer once, interactively, to install them** before falling back to the compressed pass. It never auto-installs. The soft dependency is declared in `.claude-plugin/marketplace.json` (`allowCrossMarketplaceDependenciesOn`); nothing about the FSI plugins is required for Phase 1.

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

Every number in a snapshot traces to a data-source endpoint or a public web source, each recorded with its retrieval timestamp under `meta.sources`; `meta.data_source` records the primary source (Alpha Vantage by default) and `meta.data_mode` records which of the three AV data modes produced it. The blocking QC gate reconciles internal consistency (market cap, P/E, net cash, MA ordering, ranges, spot-check tolerance, options freshness, staleness) and stamps an attestation into `meta.qc` — the same arithmetic checks double as the transcription audit for web-fallback runs. Snapshot schema version: **v0.2.1**.

## Development

```
python3 -m unittest discover -s tests
```

Python 3.10+, standard library only — no pip installs.

## Disclaimer

For educational and research purposes only. Not financial advice. Nothing here is a recommendation to buy or sell any security. Verify all figures independently before acting.
