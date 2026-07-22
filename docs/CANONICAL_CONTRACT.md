# Canonical raw-file contract

`contract_version: 1.0.0` — see the [CHANGELOG](#changelog) at the foot of this
document. This version pins the *input* raw-file contract; the pipeline's *output*
artifacts carry a separate `schema_version` (see `scripts/_artifact.py`,
`OUTPUT_SCHEMA_VERSION`).

The source-neutral interface between a *data source* and the trading-desk pipeline.

`scripts/build_snapshot.py` and `scripts/chain.py` are the ONLY readers of raw files.
They accept a small, fixed set of raw shapes per manifest key and compute every
number from them. This document specifies exactly those shapes so a NEW data source
can be adapted without touching plugin code. The shapes here are historically Alpha
Vantage's; that is a naming convention, not a coupling — any source that emits these
shapes flows through unchanged.

**The pipeline is snapshot-centric and fetch-agnostic.** Fetching is always the
client agent's job. This contract governs only the *files on disk* the builder reads.

---

## Envelope handling (applied to EVERY file)

Before shape detection, every raw file is passed through two unwrappers, in order
(`build_snapshot.load_raw` → `_unwrap_all` → `chain.unwrap` then `unpreview`):

1. **MCP content envelope** (`chain.unwrap`): if the object is
   `{"content": [{"text": "<json-string>"}, ...]}`, the inner `text` is parsed as
   JSON and the unwrap recurses. This is the standard MCP tool-result envelope.
2. **Alpha Vantage preview envelope** (`unpreview`): if the object is
   `{"preview": true, "sample_data": "<json-string>", ...}`, `sample_data` is parsed
   as JSON and used (it may itself be truncated — latest-first entries are always
   taken, so truncation is safe).

A foreign source whose tool results arrive already-unwrapped needs no envelope work;
the unwrappers are pass-through on shapes they don't recognize.

The two daily-series keys use a CSV-aware loader (`load_daily_raw`) that also accepts
a **bare non-JSON CSV** file (see below).

---

## Manifest keys → accepted raw shapes

Manifest keys are exact and map 1:1 to snapshot blocks. `REQUIRED` keys
(`global_quote`, `overview`, `daily_adjusted`, `spy_daily_adjusted`) abort the build
if absent or unparseable; all others leave their fields null and are disclosed in
`meta.missing`.

### `daily_adjusted`, `spy_daily_adjusted` (REQUIRED)

The daily OHLCV series (ticker and SPY benchmark). Parsed to ascending
(oldest-first) rows with keys `date/open/high/low/close/adjusted_close/volume` by
`parse_daily_rows`. Two accepted shapes (auto-detected):

- **AV JSON** — an object carrying `"Time Series (Daily)"`, a map of
  `"YYYY-MM-DD"` → bar, each bar with columns:
  `"1. open"`, `"2. high"`, `"3. low"`, `"4. close"`, `"5. adjusted close"`,
  `"6. volume"`. The `"5. adjusted close"` column is mandatory — split/dividend
  adjustment is required for multi-year stats. Keys may be newest-first; the
  builder sorts ascending.
- **stooq-style CSV** — a header line `Date,Open,High,Low,Close,Volume`
  (case-insensitive; a leading BOM is tolerated), one row per day. Detected by the
  first non-empty line beginning `date,open`. Accepted either **bare** (a raw `.csv`
  text file — the `.json` filename is fine, detection is by content) OR wrapped as
  `{"result": "<csv text>"}`. For this shape `adjusted_close == close` (stooq closes
  are already split-adjusted), disclosed in the snapshot as
  `technicals.series_source = "stooq_csv_close_as_adjusted"`.

### `global_quote` (REQUIRED)

AV `Global Quote` shape: `{"Global Quote": {...}}` with the consumed keys:
`"05. price"`, `"08. previous close"`, `"03. high"`, `"04. low"`.

### `overview` (REQUIRED)

AV `COMPANY_OVERVIEW` — a FLAT object. Consumed keys:
`MarketCapitalization`, `SharesOutstanding`, `52WeekHigh`, `52WeekLow`,
`EPS`, `PERatio`, `EVToEBITDA`, `PEGRatio`, `ReturnOnEquityTTM`,
`AnalystTargetPrice`,
`AnalystRatingStrongBuy`, `AnalystRatingBuy`, `AnalystRatingHold`,
`AnalystRatingSell`, `AnalystRatingStrongSell`,
`DividendPerShare`, `ExDividendDate`, `DividendDate`.
(`ForwardPE` is documented for completeness of a company overview but the builder
derives forward P/E from consensus EPS, not this key.)

### `income_statement`, `balance_sheet`, `cash_flow`

Each an object with a `quarterlyReports` list (newest-first). Consumed per-report
field names:

- **income_statement:** `totalRevenue`, `grossProfit`, `operatingIncome`,
  `netIncome`. (Revenue growth = latest quarter vs. the same quarter one year ago,
  i.e. index 0 vs index 4 — at least 5 quarters needed.)
- **balance_sheet** (latest quarter only, for net-cash):
  `cashAndShortTermInvestments` (else `cashAndCashEquivalentsAtCarryingValue` +
  `shortTermInvestments`), `longTermInvestments`, `shortLongTermDebtTotal` (else
  `shortTermDebt` + `longTermDebt`).
- **cash_flow:** `operatingCashflow`, `capitalExpenditures` (FCF = OCF − capex,
  summed over the 4 newest quarters).

### `earnings`

Object with a `quarterlyEarnings` list (newest-first); consumed field
`reportedEPS` (summed over the 4 newest quarters for a computed TTM EPS).

### `earnings_estimates`

Object with a top-level `estimates` list. Each row is an estimate dict; consumed
fields: `date` (`YYYY-MM-DD`), `horizon` (matched case-insensitively for the
substrings `"quarter"` / `"year"`), `eps_estimate_average`,
`eps_estimate_average_90_days_ago`, `eps_estimate_revision_up_trailing_30_days`,
`eps_estimate_revision_down_trailing_30_days`, `revenue_estimate_average`. Only rows
dated strictly after the manifest `as_of_utc` date are used (future quarters/years).

### `options_chain`

Read by `scripts/chain.py` `load_contracts`, never by the builder directly, and
NEVER loaded into LLM context. Accepted, in order:

1. a raw JSON **list** of contracts;
2. an object with a `"data"` OR `"options"` list;
3. an MCP content envelope wrapping (1) or (2);
4. **JSONL** — one contract JSON object per line.

Per-contract fields (aliases → normalized key): `expiration` (`YYYY-MM-DD`,
required), `type` (`call`/`put`, required), `strike` (required, must coerce to a
number), `mark`, `bid`, `ask`, `iv` (alias `implied_volatility`), `delta`,
`oi` (alias `open_interest`), `volume`, `last`. Missing
`strike`/`expiration`/`type` skips that contract. `mark` falls back to the bid/ask
midpoint, then to `last`.

### `web_fundamentals`

Fallback-only transcription (gap-fills fundamentals a statement file could not
supply). A FLAT object; the builder reads value keys, fills a fundamentals field ONLY
where the statement path left it null, and discloses filled fields in
`fundamentals.web_transcribed_fields`. Read keys:
`rev_ttm`, `rev_growth_latest_q`, `gm_ttm`, `om_ttm`, `nm_ttm`, `eps_ttm`,
`eps_ntm_consensus`, `fcf_ttm`, `net_cash_defined`, `roe`. All are numeric except
`net_cash_defined`, which is a dict `{"cash_st", "lt_inv", "total_debt", "net"}`
copied through as-is. An optional `sources` map (`{field: url}`) is a citation trail
the builder ignores.

### `pc_ratio_realtime`

Object with `put_call_ratio_full_chain` (number) and
`put_call_ratio_by_expiration`, a list (first 6 used) of `{date, value}`.

### `earnings_calendar`

Two accepted shapes:
- **CSV-in-JSON:** `{"result": "<csv text>"}` with header columns `reportDate`,
  `timeOfTheDay`, `estimate` (first data row used). May legitimately be header-only.
- **web-fallback dict:** an object carrying a top-level `date` (and no `result`),
  with optional `time`, `consensus_eps`.

### `treasury_yield`

Object with a `data` list, newest-first; the latest row's `value` and `date` are
used (10-year yield).

### `web_spot_check`

`{"price": <number>, "source_url": "<url>"}` — an independent spot-price cross-check.

### `short_interest`

`{"short_interest_pct", "si_trend", "as_of", "source_url"}` — consumed keys
`short_interest_pct`, `si_trend`, `as_of`.

### `insider_transactions`

Object with a `data` list; per-row consumed fields: `transaction_date`
(`YYYY-MM-DD`), `share_price`, `shares`, `acquisition_or_disposal` (`A`/`D`). Rows
with blank/zero `share_price` (RSU grants/vests) are excluded from the dollar math.

### `news_sentiment`

Not parsed for numbers — the LLM distils
`sentiment.news_sentiment_summary` from it in text. Any shape the source emits is
fine; the builder never computes from it.

### `iv_history` (top-level manifest key `iv_history_path`, not under `files`)

`{"ticker", "samples": [{"date": "YYYY-MM-DD", "atm_iv": <number>}]}` — used for
`iv_pctile_1yr`.

---

## THE ADAPTER RULE (foreign sources)

A NEW (foreign) source is adapted per field-group, and the group's *kind* decides how:

- **Scalar groups** — everything except the three bulk artifacts. **TRANSCRIBE** the
  source's values verbatim into the accepted shape above, with the source URL / tool
  name recorded in the manifest entry (and, for `web_fundamentals`, per-field via its
  `sources` map). This is the cited-transcription pattern the web-fallback path
  already uses. Units are checked; nothing is computed. The QC gate's arithmetic
  cross-checks (P/E, mktcap, net-cash) audit the transcription.

- **Bulk groups** — the THREE artifacts too large to transcribe by hand:
  **ticker daily series** (`daily_adjusted`), **SPY daily series**
  (`spy_daily_adjusted`), and the **options chain** (`options_chain`). For these, the
  client agent writes a small **STRUCTURAL transform** that maps the source's fields
  onto one of the accepted shapes above — **field mapping and renaming ONLY, never
  arithmetic** (no unit conversions, no re-scaling, no derived columns; if the source
  reports splits differently, disclose it, don't compute around it). The transform
  emits one of the shapes this contract accepts and the builder does the rest.

### Adapters are user-workspace artifacts, never plugin code

A bulk-group transform is saved to the user's workspace at
`trading_desk_config/adapters/<source>_<group>.py` (e.g.
`trading_desk_config/adapters/polygon_daily_adjusted.py`). On a later fetch from the
same source, the SAME file is re-run **verbatim** — never regenerated. This is a
reproducibility requirement: a regenerated mapping could drift, and a refresh delta
would then misread parsing drift as real market movement. Adapters live with the
user's data, outside the plugin; the plugin ships none.

The QC gate audits the RESULT regardless of how the raw file was produced — a
correct adapter passes the same reconciliation checks (mktcap, P/E, net-cash, MA
ordering, ranges, spot-check tolerance, options freshness, staleness) as a native
Alpha Vantage bundle.

---

## CHANGELOG

Semantic versioning of the *input* raw-file contract. Bump on any change to the
raw shapes `build_snapshot.py` / `chain.py` accept (a new required field, a changed
shape, a removed key). PATCH = editorial/clarification; MINOR = backward-compatible
additive shape; MAJOR = breaking change to an existing shape.

### 1.0.0 — 2026-07-22

First versioned baseline. Captures the raw-file contract as shipped: the MCP + Alpha
Vantage preview envelope handling, the per-manifest-key raw shapes (OVERVIEW, GLOBAL
QUOTE, daily/weekly/monthly series, options chain, earnings/dividends, news/sentiment),
and the adapter guidance. No shape change relative to the prior unversioned document —
this entry only stamps the existing contract as `1.0.0` so future changes are tracked
against a named baseline. The pipeline's output artifacts version independently via
`OUTPUT_SCHEMA_VERSION` in `scripts/_artifact.py` (FR-3).
