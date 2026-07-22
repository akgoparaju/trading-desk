# trading-desk

## What it is

A Claude Code plugin that produces short (≈3-page) trade decision reports built on institutional-depth evidence. It fetches market data from Alpha Vantage, verifies it, and computes every figure in tested in-repo Python — the LLM writes prose, never arithmetic. A single verified `snapshot.json` is the source of truth for every downstream skill, so numbers stay consistent across technicals, sentiment, valuation, and the final trade plan. A blocking QC gate must pass before any snapshot is used, and every number carries its endpoint and retrieval timestamp for provenance.

## Status

**v0.19.0 — analysis depth plus a machine-consumable decision contract.** The full pipeline is wired end to end: the `market-snapshot` data engine (L1), the four evidence modules (technical, sentiment, risk, fundamental), the `composite-score` decision layer (L3), the `trade-plan` + `options-strategy` execution layer, the `report-renderer` 3-page output + docket with its blocking QC gate (L4), and the `full-trade-analysis` orchestrator (L5) that runs them all through phase gates.

The 0.14.0 depth roadmap deepened every evidence module to institutional-practice rubrics and added a scored **confidence layer** (see [Analysis depth & confidence](#analysis-depth--confidence)): event-aware risk, sentiment positioning dynamics, regime-conditional technicals, event-vol-aware options, and base-rate-anchored composite scenarios. **Every score-moving rubric is PROVISIONAL** — shipped with a versioned default and a *pre-registered falsifier*, to be ratified after a calibration set of 5–10 anchored names runs. Reports disclose this in the footer and confidence badge; nothing reads as settled that isn't.

Since then: a capital-authorization decision gate (0.15.0), an issuer/security master + snapshot schema 0.4.0 (0.17.0), and — for downstream LLMs and agents — a **machine-consumable decision contract** (0.18.0–0.19.0): a single versioned `decision.json`, a headless JSON-only mode, and a documented governed-source adapter seam. See [Machine-consumable output](#machine-consumable-output-for-downstream-agents-and-orchestrators).

1.0.0 remains gated on the clean-environment install probe (V6 Part 3) and a non-Alpha-Vantage source validation (FMP).

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
The first run asks which connected source to use and remembers it (`trading_desk_config.json`). After install, the first session shows a ONE-TIME notice if the optional FSI plugins are absent; To install FSI: `/plugin marketplace add anthropics/financial-services`, then `/plugin install equity-research` and `/plugin install financial-analysis`.

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
├── <TICKER>_Trade_Report_<date>.pdf     ← docket: exec (2pp)      ┐ optional, when the
├── <TICKER>_Detail_<date>.pdf           ← docket: detail (~10-15pp)├ render venv is built
├── <TICKER>_Delta_Note_<date>.pdf       ← docket: delta (refresh)  ┘
├── iv_history_<TICKER>.json             ← IV-history cache (persists across dates)
└── detail_reports_<date>/               ← the dated bundle
    ├── snapshot_<TICKER>_<date>.json     ← the verified single source of truth
    ├── manifest.json                     ← sources + data_mode + retrieval timestamps
    ├── module_{technical,risk,sentiment,context,fundamental,composite,tradeplan,options}.json
    ├── module_decision.json             ← consolidated, versioned decision object (contract_version 2.0.0)
    ├── brief_<dim>.md                    ← per-dimension evidence briefs
    ├── pdf_slots.json                    ← docket prose slots (provenance-gated)
    ├── charts/                           ← deterministic chart pack (script-minted PNGs)
    └── raw/                              ← raw AV / web responses (incl. the options chain, never read into context)
```

The report lands in the **parent** `trading_desk_<TICKER>/` (a sibling of the dated data folder), so it is easy to find next to prior dates. (Legacy `td_bundle_<TICKER>_<date>/` bundles still work — discovery globs both; those keep the report inside the bundle.)

## Machine-consumable output (for downstream agents and orchestrators)

The 3-page report and docket are the human-facing output. For a **downstream LLM or agent** that needs to load the call into its own context and act on it, every run also emits a single versioned **decision object** — `detail_reports_<date>/module_decision.json` — so a consumer reads one file and pins one contract, instead of scraping the rendered report or joining five module schemas that each drift independently.

It consolidates the whole call: the capital-authorization gate (`action_owned` / `action_unowned`, `capital_eligible`, `capital_blockers[]`, `ev_band`, hurdles, `entry_state`), the composite score + dimensions + confidence, the executable plan (entries / exits / sizing / both invalidation legs), the options expression, valuation anchors, a structured catalyst calendar, and a stable thesis id.

- **Pin one version.** The object carries a semver `contract_version` (currently **2.0.0**); a consumer asserts it on read and gets a clean, explicit mismatch at upgrade time rather than a silently vanished field. Every emitted artifact also carries a top-level `schema_version` for drift detection on the other module JSONs, and the input side is versioned in [`docs/CANONICAL_CONTRACT.md`](docs/CANONICAL_CONTRACT.md). A published JSON Schema ships at [`docs/decision.schema.json`](docs/decision.schema.json).
- **Provenance-consistent by construction.** A blocking gate (`report_qc.py --decision-gates`, run on every full run and refresh) enforces that every non-derived numeric leaf in the decision object equals a value already in the bundle — it can never carry a number the pipeline didn't compute — and validates the object against the schema.
- **Structured, not prose.** Catalysts are ISO-dated with `days_out` and type (earnings / dividend); the technical-invalidation trigger is an enumerated `operator` a monitor can evaluate mechanically; the thesis carries a deterministic, refresh-stable `id` (`<TICKER>-<inception_date>`) so a tracker can join score / EV / invalidation deltas across refreshes.
- **Headless JSON-only mode.** For an orchestrator that needs only the decision object — a scheduled re-score, a monitoring tick — `scripts/run_pipeline.py --emit json` runs the deterministic scorer chain plus the blocking gates and emits the decision JSON with **no report, no charts, no PDF, and no render venv**. It carries the prior run's judgments forward for a no-event re-score, and refuses (a distinct exit code) to carry them across an earnings/dividend event so the caller can route event-runs back through the full model path; it exits non-zero on any gate failure.
- **Governed / foreign data sources.** The snapshot builder is source-neutral, so a locked-down consumer can feed a governed market-data MCP through the documented adapter seam — see [Bring your own data source](#bring-your-own-data-source) and the worked "Step 2-MCP" example in the `market-snapshot` skill, with a copy-pasteable stub at [`docs/adapter_template.py`](docs/adapter_template.py).

## The docket

After the markdown report passes its QC gate, trading-desk can render a **docket** — the institutional PDF render of the same QC'd bundle in a **bank-note aesthetic** (fine hairlines, restrained accent, weight-ticked score bars):

- **exec** — a 2-page trade sheet (grade box, thesis, price/scenario/valuation charts, trade plan, desk read).
- **detail** — the full ~10-15-page dossier (exec pages + per-dimension evidence, options & vol, downside map, appendix + integrity), closing with a **METHODOLOGY page**: the rubric versions, weight table (with a standard-vs-custom comparison when a custom weight set is active), valuation formula set, active sector scale (parameters, evidence, falsifiers), and governance rules that produced every number in the report — fully script-generated, so the system's transparency is itself provenance-clean.
- **delta** — a 1-page What-Changed note produced by a **refresh** vs the prior bundle (score deltas, level moves, invalidation status).

The docket carries **zero LLM arithmetic**: every number on the page is script-minted from the module JSONs, the deterministic chart pack, or the What-Changed diff. The only authored content is the prose in `pdf_slots.json`, which passes the same number-provenance gate as the report before it can be embedded.

The renderers (matplotlib + reportlab) live in a **one-time ~30s venv bootstrap**, kept out of the stdlib-only core so a bare machine still runs the whole pipeline. Check/build it with `python3 scripts/render_env.py --check` (exit 3 = not built; run `python3 scripts/render_env.py` once). When the venv is absent the report ships **md-only** and the degradation is disclosed — the docket never blocks the run.

## Coverage-first analysis

Deep coverage is the **default** read, not an upgrade. Before scoring a ticker, `full-trade-analysis` checks for `./trading_desk_<TICKER>/coverage/` — the FSI initiation artifacts (company research, financial model, valuation):

- **No coverage yet, FSI installed → it initiates, automatically.** The first run on a name announces it and runs FSI `initiating-coverage` (Tasks 1-3 only — research, model, valuation; the docket renders charts and the report itself, so FSI's Tasks 4-5 are skipped). Initiation is **token-heavy and slow (~30-60+ min)**, but **coverage is permanent**: every later run reuses it and is cheap. That is the trade the default makes — pay once, reuse forever. (Say "skip initiation" to override for a single run.)
- **Coverage exists → it is kept fresh.** If a new quarter has reported since the coverage model was built, the model is `model-update`d before scoring (never scored as if current). A refresh does the same freshness check.
- **No FSI → the compressed floor.** With the FSI plugins absent (and declined), the run drops to the **compressed floor**, loudly disclosed: the fundamental pass is snapshot-only and the company-context module runs `web_compressed` (cited web research) instead of distilling a model. This is a genuine floor, not a peer of the default.

What coverage buys: a `company-context` module (business / competitive / cases / risks + a dated live tape of *what is moving the stock now*), every claim traced through a `findings[]` citation registry. That registry is **load-bearing for scoring** — the fundamental moat flag and the composite's conviction justifications cite its finding IDs (`C3`), and the moat CLI *exits* if a justification names none. Context is unscored itself; it grounds the dimensions that are scored.

## Analysis depth & confidence

Each evidence module scores against a versioned rubric with visible point arithmetic — every subscore's inputs and band are printed, and the LLM does zero scoring arithmetic. As of 0.14.0 the rubrics carry institutional-practice depth:

- **Risk (`risk-v1.1.0`)** — event proximity and implied-move vs the ticker's *own* earnings-move history, plus a tail factor (overnight-gap kurtosis / p95), on top of the base volatility / drawdown / margin-of-safety / liquidity factors. Doctrine: risk is a gate/governor, never a reward input.
- **Sentiment (`sentiment-v1.1.0`)** — 25Δ risk-reversal skew, days-to-cover, decay-weighted **news-heat** (scoring the news *dynamics*, not the vendor number), and insider **routine-vs-opportunistic** classification (Cohen/Malloy/Pomorski) when ≥24mo of history is available, falling back gracefully otherwise.
- **Technical (`technical-v1.1.0`)** — regime guards (Wilder ADX, Weinstein stage) conditioning the momentum read, anchored VWAP levels in the S/R ladder, and Chaikin A/D + up/down-volume quality.
- **Options (`options-v1.1.0`)** — event-vol extraction (variance additivity across the bracketing expiries), ex-earnings realized vol wired into the IV-vs-realized gate, an IV-crush simulation priced with an in-repo Black-Scholes model (verified against reference values), and skew-informed structure choice.
- **Composite / trade-plan (`composite-v1.1.0`)** — scenario probabilities cross-checked against the ticker's empirical earnings base rate (a deviation flag when the view departs >25pp), an auto-tension line when the evidence dimensions disagree, and bull-target triangulation against coverage anchors.

**Confidence layer (`confidence-v1.0.0`).** Every module and the composite carry a scored `confidence` badge — **HIGH / MEDIUM / LOW** — computed deterministically (never LLM-judged) as the *weakest link* of three axes: **source** (premium MCP vs degraded vs web-fallback), **depth** (is the rubric on its deep pass or a shallower one), and **staleness** (is the print fresh, or a weekend/stale/reused quote). A deep rubric on web-sourced data still reads MEDIUM; a fresh premium run on a not-yet-calibrated rubric reads MEDIUM until ratification. The badge is a first-class artifact, versioned and carried in the report footer — so "no usable data source" or "not yet calibrated" surfaces as a level, not buried in prose.

**Provisional by design.** The 0.14.0 rubric weights and bands are *provisional defaults* — each ships with a pre-registered falsifier and is disclosed as unratified in the module note, the report footer, and the confidence depth axis, pending a calibration set. This is deliberate: the system tells you what it hasn't yet earned the right to assert.

## FSI integration (now load-bearing for full depth)

The `equity-research` and `financial-analysis` skills from the claude-for-financial-services marketplace (`/plugin marketplace add anthropics/financial-services`) are **functionally load-bearing for the default deep read** — they produce the coverage the coverage-first pipeline initiates and reuses. When they are installed, trading-desk hands off to them for the initiation model, valuation, and quarterly `model-update`. When they are **absent**, the pipeline still runs — it drops to the compressed floor (snapshot-only fundamentals, `web_compressed` context) and discloses the reduced depth — and at runtime `full-trade-analysis` (and standalone `composite-score`) will **offer once, interactively, to install them**. It never auto-installs. The soft dependency is declared in `.claude-plugin/marketplace.json` (`allowCrossMarketplaceDependenciesOn`); nothing about the FSI plugins is required to run, but full depth needs them.

## Skills

| Skill | Status |
|-------|--------|
| `market-snapshot` | available |
| `technical-analysis` | available |
| `sentiment-positioning` | available |
| `risk-analytics` | available |
| `company-context` | available |
| `composite-score` | available |
| `trade-plan` | available |
| `options-strategy` | available |
| `report-renderer` | available |
| `refresh-analysis` | available |
| `scale-review` | available |
| `full-trade-analysis` | available |

Run the whole pipeline in one shot: **`full trade analysis NVDA`** — snapshot → evidence → composite → plan + options → 3-page report → thesis + re-score offer.

## Data & provenance

Every number in a snapshot traces to a data-source endpoint or a public web source, each recorded with its retrieval timestamp under `meta.sources`; `meta.data_source` records the primary source (Alpha Vantage by default), `meta.data_mode` records which of the three AV data modes produced it, and `meta.latest_trading_day` records the quote's own trading date (so a weekend/stale print is surfaced, not hidden). The blocking QC gate reconciles internal consistency (market cap, P/E, net cash, MA ordering, ranges, spot-check tolerance, options freshness, staleness) and stamps an attestation into `meta.qc` — the same arithmetic checks double as the transcription audit for web-fallback runs. The report's own QC gate additionally enforces that every judgment-flag justification cites a real coverage finding ID (grounding + referential integrity), so a set flag can never rest on an unfounded or fabricated citation. Snapshot schema version: **v0.4.0**.

## Development

```
python3 -m unittest discover -s tests
```

Python 3.10+, standard library only — no pip installs.

## Disclaimer

For educational and research purposes only. Not financial advice. Nothing here is a recommendation to buy or sell any security. Verify all figures independently before acting.
