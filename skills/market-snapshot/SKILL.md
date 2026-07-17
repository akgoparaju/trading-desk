---
name: market-snapshot
description: Build a verified, QC-gated market data snapshot for a ticker from Alpha Vantage (single source of truth for all downstream trading-desk skills). Use when the user says "snapshot [ticker]", "market snapshot", "build data snapshot", or when any trading-desk skill needs market data. Schema v0.2.1.
---

# Market Snapshot (L1 Data Engine)

Build one verified `snapshot.json` for a ticker: detect the data mode you can actually run in, fetch raw Alpha Vantage responses (or cited web sources when AV is unavailable), gap-fill from the web, then let tested in-repo Python compute every number. This snapshot is the **single source of truth** — all downstream trading-desk skills read it and never re-fetch market data.

**Non-negotiables:**
- **Never do arithmetic in text.** Every price, ratio, return, drawdown, and multiple comes from `scripts/build_snapshot.py`. Your only edits to the snapshot are qualitative TEXT slots. In web-fallback mode you TRANSCRIBE cited figures verbatim (units checked) into the raw files — you still never *compute* a number in text; the QC gate's arithmetic cross-checks (P/E, mktcap, net-cash) are the transcription audit.
- **Never read the options chain into context.** The full chain is ~2M tokens. `scripts/chain.py` is the only reader; you record its file path and move on.
- **All outputs stay under the invoker's current working directory (CWD).** `${CLAUDE_PLUGIN_ROOT}` is the plugin install dir (where `scripts/` lives); it is NOT where outputs go.

Trigger phrases: "snapshot AAPL", "market snapshot for MU", "build data snapshot", or any downstream skill requesting market data for a ticker `<TICKER>`.

---

## Step 0 — Data-mode preflight (BEFORE bundle setup)

The bundled `.mcp.json` connects to Alpha Vantage even with **no key exported** — AV serves empty-key traffic at an anonymous **~25-call/day** free quota. Do NOT assume a connected MCP means a usable key. Detect the tier explicitly, up front, and disclose it — before burning calls a rerun cannot afford. (The env-var check and the classify/ask happen first; the one probe call reuses the AV tools once Step 1 loads them, so it costs nothing extra.)

1. **Check the env var:**
   ```bash
   [ -n "$ALPHAVANTAGE_API_KEY" ] && echo set || echo unset
   ```
2. **Probe the tier with ONE cheap call.** After loading the AV tools (Step 1's `ToolSearch`), call `GLOBAL_QUOTE` on the ticker once and read the response:
   - A normal quote with **no** rate-limit / free-tier note (and no premium-endpoint block on the subsequent full-data calls) → **likely premium**.
   - A response carrying a free-tier / rate-limit **Note** or **Information** field, OR a premium-endpoint entitlement block later (TIME_SERIES_DAILY_ADJUSTED with `outputsize=full`, HISTORICAL_OPTIONS) → **free**.
   - AV MCP tools **absent / unreachable** → **no AV at all**.
3. **Classify + ANNOUNCE** one of these to the user:
   - **`data_mode=alpha_vantage`** — premium; the full fetch path (Steps 1–6) runs unchanged.
   - **`data_mode=av_free_degraded`** — env unset OR a free-tier key. State explicitly WHICH:
     - env unset → "no `ALPHAVANTAGE_API_KEY` exported — running on Alpha Vantage's anonymous ~25-call/day quota"
     - free-tier key → "free-tier key detected"
     - Warn what degrades: **no adjusted multi-year history** (premium `TIME_SERIES_DAILY_ADJUSTED outputsize=full` is entitlement-blocked), **no options chain** (HISTORICAL_OPTIONS blocked), **no IV history**, and a **~25-call daily budget** — so at most **ONE run/day**. Fall back to Step 2-ALT per field-group as blocks are hit.
   - **`data_mode=web_fallback`** — no AV MCP at all; the entire fetch pass is the Step 2-ALT web-research path.
4. **If interactive, ASK before proceeding** on `av_free_degraded` or `web_fallback`:
   > "(1) stop — set up a key (recommended; a free key takes ~1 min at [alphavantage.co](https://www.alphavantage.co/support/#api-key), premium for full depth), or (2) proceed in `{mode}` with the disclosures above."

   **Unattended → proceed and disclose** (never silently). Record the mode as a **top-level** manifest key `data_mode` (Step 0.5 skeleton) — the builder copies it into `meta.data_mode`.

---

## Step 0.5 — Bundle setup

1. Derive the as-of UTC timestamp (`date -u +%Y-%m-%dT%H:%M:%SZ`). Call the date part `<YYYY-MM-DD>`.
2. In the invoker's CWD, create the parent + dated bundle:
   - Parent dir (NO date): `./trading_desk_<TICKER>/`
   - Bundle dir (dated): `./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>/` with `raw/` inside it.
3. Start `./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>/manifest.json` with this skeleton (note the `data_mode` key from Step 0):
   ```json
   {"ticker": "<TICKER>", "as_of_utc": "<as_of_utc>", "data_mode": "<alpha_vantage|av_free_degraded|web_fallback>", "api_tier_notes": [], "files": {}}
   ```
4. As you complete each fetch, record it into `manifest.files`:
   ```json
   "files": {"<key>": {"path": "raw/<key>.json", "endpoint_or_url": "<endpoint or URL>", "retrieved_utc": "<utc>"}}
   ```
   `path` is bundle-relative. If the harness offloads a tool result to a file elsewhere, record THAT absolute path in `path` instead (the builder passes absolute paths through unchanged).

The manifest keys below are exact — the builder maps them to snapshot blocks. Do not rename them.

---

## Step 1 — Load Alpha Vantage tools (deferred)

Make ONE batched `ToolSearch` call so all AV tools load in a single round-trip:

```
select:mcp__alphavantage__GLOBAL_QUOTE,mcp__alphavantage__COMPANY_OVERVIEW,mcp__alphavantage__TIME_SERIES_DAILY_ADJUSTED,mcp__alphavantage__INCOME_STATEMENT,mcp__alphavantage__BALANCE_SHEET,mcp__alphavantage__CASH_FLOW,mcp__alphavantage__EARNINGS,mcp__alphavantage__EARNINGS_ESTIMATES,mcp__alphavantage__NEWS_SENTIMENT,mcp__alphavantage__INSIDER_TRANSACTIONS,mcp__alphavantage__HISTORICAL_OPTIONS,mcp__alphavantage__REALTIME_PUT_CALL_RATIO,mcp__alphavantage__EARNINGS_CALENDAR,mcp__alphavantage__TREASURY_YIELD
```

---

## Step 2 — Fetch pass (~15 calls) — data_mode=alpha_vantage

**This is the premium (`alpha_vantage`) path.** In `av_free_degraded`, run the calls that clear the free tier (GLOBAL_QUOTE, COMPANY_OVERVIEW, the statement/earnings endpoints, NEWS_SENTIMENT, TREASURY_YIELD) and route each entitlement-blocked group (adjusted multi-year history, the options chain, IV history) to **Step 2-ALT** — do not retry a blocked premium endpoint. In `web_fallback`, skip this step entirely and use **Step 2-ALT** for the whole pass.

Pass **`return_full_data=true` on EVERY Alpha Vantage call** — the AV MCP server otherwise silently truncates any mid-size response into a `{"preview": true, ...}` stub (validation caught BALANCE_SHEET reduced to 2 quarters, which corrupts TTM sums). With it set, small results arrive whole and large ones are offloaded to files by the harness.

Also pass **`datatype=json` on every call that accepts it** — some endpoints (GLOBAL_QUOTE, HISTORICAL_OPTIONS, TREASURY_YIELD) default to CSV, which the builder and `scripts/chain.py` do not parse (validation caught `load_contracts` failing on a CSV chain).

For results that arrive in context: save the payload **VERBATIM** to `raw/<key>.json` — do not reformat, do not transcribe any number by hand. For results the harness offloads to a file: **`cp` the offloaded file into `raw/<key>.json`** (a Bash copy — the content still never enters context) and record the bundle-relative path. Offloaded paths are temp files the harness may reap mid-session (validation caught one disappearing), and a self-contained bundle is required for later delta re-scores. Manifest keys are in parentheses.

1. `GLOBAL_QUOTE` symbol=`<T>` → (`global_quote`)
2. `COMPANY_OVERVIEW` symbol=`<T>` → (`overview`)
3. `TIME_SERIES_DAILY_ADJUSTED` symbol=`<T>`, outputsize=full → (`daily_adjusted`). NEVER use raw `TIME_SERIES_DAILY` — split-adjusted series is mandatory for multi-year stats.
4. `TIME_SERIES_DAILY_ADJUSTED` symbol=SPY, outputsize=full → (`spy_daily_adjusted`)
5. `INCOME_STATEMENT` symbol=`<T>` → (`income_statement`)
6. `BALANCE_SHEET` symbol=`<T>` → (`balance_sheet`)
7. `CASH_FLOW` symbol=`<T>` → (`cash_flow`)
8. `EARNINGS` symbol=`<T>` → (`earnings`)
9. `EARNINGS_ESTIMATES` symbol=`<T>` → (`earnings_estimates`)
10. `NEWS_SENTIMENT` tickers=`<T>`, limit=50 → (`news_sentiment`)
11. `INSIDER_TRANSACTIONS` symbol=`<T>`, **from_date = as-of − 90 days** → (`insider_transactions`). The from_date keeps the response inside the 90-day window the builder uses (without it the default response may omit most of the window).
12. `HISTORICAL_OPTIONS` symbol=`<T>` → (`options_chain`). The full chain is ~2M tokens; the harness offloads it to a file. Per the general rule above, `cp` it into `raw/options_chain.json` (Bash copy only). **NEVER Read this file, never paste any part of it — `scripts/chain.py` is the only reader.**
13. `REALTIME_PUT_CALL_RATIO` symbol=`<T>` → (`pc_ratio_realtime`)
14. `EARNINGS_CALENDAR` symbol=`<T>`, horizon=6month → (`earnings_calendar`). Returns CSV text inside `{"result": "..."}`; save as-is. ⚠ Known to return header-only/empty for valid tickers → use the Step 3c fallback.
15. `TREASURY_YIELD` interval=daily, maturity=10year, datatype=json → (`treasury_yield`). Full history is ~350k tokens, offloaded — `cp` it into `raw/treasury_yield.json`; the builder takes the latest row.

**Rate limit:** premium tier = 75 req/min. This pass is ~15 calls — no pacing needed. (On `av_free_degraded` the daily budget is ~25 calls total — see Important Notes.)

---

## Step 2-ALT — Web-fallback fetch pass (FSI-style, cited web research)

Used when **`data_mode=web_fallback`** (no AV MCP at all), and **per-field-group** in `av_free_degraded` for the groups the free tier blocks. This is the FSI-style path: web research with **per-figure citations**, funneled through the SAME snapshot pipeline and QC gate — the builder's math and the QC arithmetic cross-checks are your transcription audit. Every raw file goes to the same `raw/<key>.json` bundle keys the AV path uses, so the builder needs no changes.

**TRANSCRIPTION RULE (applies to every file below):** copy figures **verbatim** with units checked — **never compute**. Record the source URL in the manifest entry (and, for fundamentals, per-field). The QC gate re-derives P/E, market cap, and net-cash from your transcribed leaves; a transcription slip surfaces there.

1. **OHLCV — ticker + SPY (`daily_adjusted`, `spy_daily_adjusted`):** stooq CSV daily export. The builder auto-detects the CSV shape and labels the series `series_source: stooq_csv_close_as_adjusted` (stooq closes are already split-adjusted, so multi-year MA200 / beta / drawdowns all work).
   ```bash
   curl -fsSL "https://stooq.com/q/d/l/?s=<sym>.us&i=d" -o raw/daily_adjusted.json      # ticker, e.g. s=mu.us
   curl -fsSL "https://stooq.com/q/d/l/?s=spy.us&i=d"   -o raw/spy_daily_adjusted.json   # SPY benchmark
   ```
   Record each with its stooq URL. (The file holds bare CSV; keeping the `.json` name matches the manifest key — the builder's CSV detector handles it.)
2. **Quote / previous close (`global_quote`):** from a public quote page OR the last row of the stooq series (cite the URL). Write `raw/global_quote.json` as a **labeled transcription** in the AV `GLOBAL_QUOTE` shape (`{"Global Quote": {"05. price": "...", "08. previous close": "...", "03. high": "...", "04. low": "..."}}`), source URL in the manifest entry.
3. **Fundamentals (`web_fundamentals`):** TRANSCRIBE from cited sources (SEC filings / company IR / reputable aggregators) into `raw/web_fundamentals.json` per the builder's documented shape — the fields it reads are `rev_ttm`, `rev_growth_latest_q`, `gm_ttm`, `om_ttm`, `nm_ttm`, `eps_ttm`, `eps_ntm_consensus`, `fcf_ttm`, `net_cash_defined`, `roe` — plus a per-field `sources` map (disclosure; the builder reads the value keys, the map is your citation trail):
   ```json
   {
     "rev_ttm": <number>, "rev_growth_latest_q": <fraction>,
     "gm_ttm": <fraction>, "om_ttm": <fraction>, "nm_ttm": <fraction>,
     "eps_ttm": <number>, "eps_ntm_consensus": <number>, "fcf_ttm": <number>,
     "net_cash_defined": {"cash_st": <n>, "lt_inv": <n>, "total_debt": <n>, "net": <n>},
     "roe": <fraction>,
     "sources": {"rev_ttm": "<url>", "eps_ttm": "<url>", "net_cash_defined": "<url>", "...": "..."}
   }
   ```
   **Statement files win when present** — the builder gap-fills a field from `web_fundamentals` ONLY where the statement path left it null; every filled field is disclosed in `fundamentals.web_transcribed_fields`. In `av_free_degraded` the statement endpoints usually cleared the free tier, so this file just fills gaps; in `web_fallback` it supplies the whole fundamentals block. Copy figures verbatim; do not compute `net_cash_defined.net` yourself beyond transcribing the four components as printed.
4. **Overview substitute (`overview`):** same transcription pattern into `raw/overview.json` using AV `COMPANY_OVERVIEW` field names — `SharesOutstanding`, `52WeekHigh`, `52WeekLow`, `AnalystTargetPrice`, `AnalystRatingStrongBuy`/`Buy`/`Hold`/`Sell`/`StrongSell`, `MarketCapitalization`, `PERatio`, `EVToEBITDA`, `PEGRatio`, `DividendPerShare`, `ExDividendDate`, `DividendDate`, `ReturnOnEquityTTM` — from cited sources. Source URL in the manifest entry.
5. **Short interest / earnings date:** the existing Step 3 web paths are unchanged (they were already web sources).
6. **Options chain / P/C / IV — UNAVAILABLE.** Leave `options_chain` and `pc_ratio_realtime` absent → the builder nulls the options block and lists it in `meta.missing`; options-strategy emits its disclosed stand-aside module and the expression falls back to stock. Do NOT transcribe a chain by hand.

Proceed to Step 3 (web gap-fill, unchanged), then skip Step 4 (no IV history without a chain) and go to Step 5.

---

## Step 3 — Web gap-fill (AV has no endpoint for these)

**(a)** ONE independent spot-price check from any public quote page. Write `raw/web_spot.json` (key `web_spot_check`):
```json
{"price": <number>, "source_url": "<url>"}
```

**(b)** Short interest from a public aggregator. Write `raw/short_interest.json` (key `short_interest`):
```json
{"short_interest_pct": <number>, "si_trend": "rising"|"falling"|"flat"|null, "as_of": "YYYY-MM-DD", "source_url": "<url>"}
```
Exchange short-interest data is bi-monthly — staleness is expected and disclosed (window 14 days).

**(c) ONLY IF `EARNINGS_CALENDAR` came back empty** (header-only `result`): find the next earnings date from company IR / a financial site and OVERWRITE `raw/earnings_calendar.json` with:
```json
{"date": "YYYY-MM-DD", "time": "<bmo|amc|...>", "consensus_eps": <number|null>, "consensus_rev": <number|null>, "source_url": "<url>"}
```
Add a note to `manifest.api_tier_notes` recording the fallback.

---

## Step 4 — IV history cache (input for `iv_pctile_1yr`)

Cache file `iv_history_<TICKER>.json` lives in the bundle's **PARENT** dir — now the ticker parent `./trading_desk_<TICKER>/iv_history_<TICKER>.json`, a sibling of every dated `detail_reports_<date>/` bundle so it persists across dates. (`av_free_degraded` and `web_fallback` skip this step — no options chain to sample.) Shape:
```json
{"ticker": "<TICKER>", "samples": [{"date": "YYYY-MM-DD", "atm_iv": <number>}]}
```

If the cache is absent OR its newest sample is more than 14 days old, refresh it: sample `HISTORICAL_OPTIONS` at ~26 biweekly dates across the trailing year (param `date=YYYY-MM-DD`, `return_full_data=true` → each response offloaded to a file). Pick trading days — bi-weekly **Fridays** work; if a sampled date returns an empty/error response (holiday), step back one day at a time (up to 3) and retry, else skip that sample and note it. For each sample date, compute the ATM IV at the ~30-DTE expiry **via the script** (never in text). Use this one-liner, substituting the offloaded file path, the spot at that date, and the sample date:

```bash
python3 - "<offloaded_chain_file>" "<spot>" "<sample_date>" <<'PY'
import os, sys, datetime
sys.path.insert(0, os.environ["CLAUDE_PLUGIN_ROOT"])  # plugin install dir (env var set at skill runtime)
from scripts import chain
path, spot, sample = sys.argv[1], float(sys.argv[2]), sys.argv[3]
cs = chain.load_contracts(path)
sd = datetime.date.fromisoformat(sample)
# expiry closest to ~30 days after the sample date
exp = min(chain.expiries(cs), key=lambda e: abs((datetime.date.fromisoformat(e) - sd).days - 30))
print(chain.atm_iv(cs, spot, exp))
PY
```

Append `{"date": "<sample_date>", "atm_iv": <printed value>}` to the cache. **Then DELETE that temp historical chain file immediately** — 26 chains ≈ hundreds of MB. `rm "<offloaded_chain_file>"`.

~26 calls ≈ 21 s at 75/min — pace only if rate-limit errors appear. When done, record the cache path in the manifest as a **top-level** key (not under `files`). The relative form is unchanged — `..` now resolves from `detail_reports_<date>/` up to the ticker parent, where the cache lives:
```json
"iv_history_path": "../iv_history_<TICKER>.json"
```

---

## Step 5 — Build the snapshot (all numbers scripted)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/build_snapshot.py \
  --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD> --ticker <TICKER>
```
The builder prints the snapshot path to stdout (default `<bundle>/snapshot_<TICKER>_<YYYY-MM-DD>.json`). Exit 2 = a REQUIRED file (`global_quote`, `overview`, `daily_adjusted`, `spy_daily_adjusted`) is missing or unparseable — fix the raw file / manifest and re-run. The builder copies the manifest's `data_mode` into `meta.data_mode`; in a web-fallback / gap-filled run it also discloses filled fields in `fundamentals.web_transcribed_fields` and stooq provenance in `technicals.series_source`.

Then fill ONLY the qualitative TEXT slots by editing the snapshot JSON:
- `sentiment.news_sentiment_summary` — ≤60 words distilled from `raw/news_sentiment.json`.
- `sentiment.inst_flow_notes` — brief institutional-flow read.
- `events.catalysts` — dated entries `{"date": "YYYY-MM-DD", "event": "<text>", "impact": "<text>"}` from news/calendar context.
- `meta.api_tier_notes` — ALWAYS include `"REALTIME_OPTIONS unavailable at 75 req/min tier — HISTORICAL_OPTIONS EOD chain used"` when the EOD chain was used; append any fallbacks (e.g. earnings-calendar web fallback, free-tier degradation, and — in `av_free_degraded` / `web_fallback` — a note naming the data mode and which groups came from the web-fallback path).

**Never edit a numeric field by hand.** If a number looks wrong, fix the raw input or the script and re-run the builder — do not patch the output.

---

## Step 6 — QC gate (BLOCKING)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/qc_gate.py <snapshot path>
```
Exit 0 is REQUIRED to proceed. The gate writes its verdict into `meta.qc` in place and prints a check table plus an attestation paragraph. Checks include mktcap reconciliation, MA ordering, range sanity, price-vs-web-spot tolerance, P/E arithmetic, net-cash reconciliation, options freshness, provenance, and staleness.

On failure (exit 1): root-cause it — bad raw file? script bug? genuinely inconsistent data? — fix and re-run. A check may be waived ONLY with a real justification:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/qc_gate.py <snapshot path> --waive "check_name:reason"
```
Print the attestation paragraph to the user.

---

## Output contract

Report to the user (and to any calling skill):
- **Data mode** — `alpha_vantage | av_free_degraded | web_fallback` (from Step 0), with the one-line disclosure
- **Bundle dir** — `./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>/` (under the ticker parent `./trading_desk_<TICKER>/`)
- **Snapshot path** — QC-stamped `snapshot_<TICKER>_<YYYY-MM-DD>.json`
- **Chain file path** — the on-disk options chain (referenced, never read into context)
- **Manifest** — `manifest.json`
- **QC attestation** — the paragraph printed by the gate
- **Summary table** — one line per block, 2–3 headline values each:

  | Block | Headline values |
  |-------|-----------------|
  | price | last, 52wk range, mktcap |
  | technicals | RSI14, MA50/MA200 ordering, RV30 |
  | benchmark | beta, 12m vs SPY |
  | fundamentals | rev growth, margins, FCF TTM |
  | valuation | P/E ttm, P/E fwd, FCF yield |
  | sentiment | ratings, P/C, IV30, IV pctile |
  | options | expected move (30d), max pain, OI walls |
  | events | next earnings, dividend |
  | macro | 10Y treasury |

Downstream skills read ONLY `snapshot.json` (plus the chain file via `scripts/chain.py`).

---

## Important Notes

- **Single-snapshot rule.** Downstream modules never fetch market data. A figure missing from the snapshot is a *snapshot extension request*, not a downstream fetch.
- **2M-token chain rule.** The options chain is never loaded into context, never Read, never pasted. Only `scripts/chain.py` reads it.
- **`TIME_SERIES_DAILY_ADJUSTED`-only rule.** Never raw `TIME_SERIES_DAILY`; split/dividend-adjusted closes are mandatory for multi-year returns, drawdowns, and vol.
- **Data-mode preflight is mandatory (Step 0).** A connected AV MCP does NOT imply a usable key — the empty-key anonymous quota answers too. Detect the tier, announce the mode, and (interactive) ask before proceeding on `av_free_degraded` / `web_fallback`.
- **Anonymous / free-tier budget (`av_free_degraded`).** The empty-key anonymous quota is **~25 calls/day** (a free-tier key is similar for this pipeline's purposes). A full AV fetch pass here is only **~13–15 calls** because the premium-only groups drop out (no adjusted multi-year history, no chain, no IV history). **Never** attempt Step 4 IV-history sampling on this tier (~26 calls would blow the budget). At most **one run/day**. If a fetch dies mid-run from quota exhaustion, either resume the next day or switch the remaining groups to the Step 2-ALT `web_fallback` path.
- **Free-tier IV degradation.** In any degraded/fallback mode Step 4 is skipped; `iv_pctile_1yr` comes back `null` and is disclosed in `meta.api_tier_notes` (never silently omitted).
- **`EARNINGS_CALENDAR` empty gotcha.** Header-only/empty for valid tickers is common → use the Step 3c web fallback.
- **`REALTIME_OPTIONS` tier limitation.** Requires a 600+/min tier. At 75/min the EOD `HISTORICAL_OPTIONS` chain is used and disclosed in `meta.api_tier_notes`.
- **Missing-field policy.** A missing figure becomes `null`, is listed in `meta.missing`, and downstream renormalizes around it — never silently dropped.
- **Web-fallback transcription rule.** In `web_fallback` / gap-filled runs, every raw figure is copied **verbatim** with units checked — never computed. The builder's arithmetic and the QC gate's cross-checks (P/E, mktcap, net-cash) are the audit. Cite every figure's source URL (per-field for `web_fundamentals` via its `sources` map). Options are unavailable in this mode and stand aside (disclosed).
- **New bundle layout.** Parent `./trading_desk_<TICKER>/` (no date) holds the persistent `iv_history_<TICKER>.json` and each dated `detail_reports_<YYYY-MM-DD>/` bundle. Legacy `./td_bundle_<TICKER>_<date>/` bundles still work downstream (discovery globs both), but new snapshots use the new layout.
- **Outputs always under the invoker CWD.** `${CLAUDE_PLUGIN_ROOT}` locates the scripts; it is never a write target.
