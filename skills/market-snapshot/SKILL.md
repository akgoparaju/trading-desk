---
name: market-snapshot
description: Build a verified, QC-gated market data snapshot for a ticker from Alpha Vantage (single source of truth for all downstream trade-decision skills). Use when the user says "snapshot [ticker]", "market snapshot", "build data snapshot", or when any trade-decision skill needs market data. Schema v0.2.0.
---

# Market Snapshot (L1 Data Engine)

Build one verified `snapshot.json` for a ticker: fetch raw Alpha Vantage responses, gap-fill from the web, then let tested in-repo Python compute every number. This snapshot is the **single source of truth** — all downstream trade-decision skills read it and never re-fetch market data.

**Non-negotiables:**
- **Never do arithmetic in text.** Every price, ratio, return, drawdown, and multiple comes from `scripts/build_snapshot.py`. Your only edits to the snapshot are qualitative TEXT slots.
- **Never read the options chain into context.** The full chain is ~2M tokens. `scripts/chain.py` is the only reader; you record its file path and move on.
- **All outputs stay under the invoker's current working directory (CWD).** `${CLAUDE_PLUGIN_ROOT}` is the plugin install dir (where `scripts/` lives); it is NOT where outputs go.

Trigger phrases: "snapshot AAPL", "market snapshot for MU", "build data snapshot", or any downstream skill requesting market data for a ticker `<TICKER>`.

---

## Step 0 — Bundle setup

1. Derive the as-of UTC timestamp (`date -u +%Y-%m-%dT%H:%M:%SZ`). Call the date part `<YYYY-MM-DD>`.
2. In the invoker's CWD, create `./td_bundle_<TICKER>_<YYYY-MM-DD>/raw/`.
3. Start `./td_bundle_<TICKER>_<YYYY-MM-DD>/manifest.json` with this skeleton:
   ```json
   {"ticker": "<TICKER>", "as_of_utc": "<as_of_utc>", "api_tier_notes": [], "files": {}}
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

## Step 2 — Fetch pass (~15 calls)

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
12. `HISTORICAL_OPTIONS` symbol=`<T>` → (`options_chain`). The full chain is ~2M tokens; the harness offloads it to a file. Record that file path. **NEVER Read this file, never paste any part of it — `scripts/chain.py` is the only reader.**
13. `REALTIME_PUT_CALL_RATIO` symbol=`<T>` → (`pc_ratio_realtime`)
14. `EARNINGS_CALENDAR` symbol=`<T>`, horizon=6month → (`earnings_calendar`). Returns CSV text inside `{"result": "..."}`; save as-is. ⚠ Known to return header-only/empty for valid tickers → use the Step 3c fallback.
15. `TREASURY_YIELD` interval=daily, maturity=10year, datatype=json → (`treasury_yield`). Full history is ~350k tokens, offloaded; the builder takes the latest row.

**Rate limit:** premium tier = 75 req/min. This pass is ~15 calls — no pacing needed.

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

Cache file `iv_history_<TICKER>.json` lives in the bundle's **PARENT** dir (i.e. the invoker CWD, alongside the bundle, so it persists across snapshots). Shape:
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

~26 calls ≈ 21 s at 75/min — pace only if rate-limit errors appear. When done, record the cache path in the manifest as a **top-level** key (not under `files`):
```json
"iv_history_path": "../iv_history_<TICKER>.json"
```

---

## Step 5 — Build the snapshot (all numbers scripted)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/build_snapshot.py \
  --bundle ./td_bundle_<TICKER>_<YYYY-MM-DD> --ticker <TICKER>
```
The builder prints the snapshot path to stdout (default `<bundle>/snapshot_<TICKER>_<YYYY-MM-DD>.json`). Exit 2 = a REQUIRED file (`global_quote`, `overview`, `daily_adjusted`, `spy_daily_adjusted`) is missing or unparseable — fix the raw file / manifest and re-run.

Then fill ONLY the qualitative TEXT slots by editing the snapshot JSON:
- `sentiment.news_sentiment_summary` — ≤60 words distilled from `raw/news_sentiment.json`.
- `sentiment.inst_flow_notes` — brief institutional-flow read.
- `events.catalysts` — dated entries `{"date": "YYYY-MM-DD", "event": "<text>", "impact": "<text>"}` from news/calendar context.
- `meta.api_tier_notes` — ALWAYS include `"REALTIME_OPTIONS unavailable at 75 req/min tier — HISTORICAL_OPTIONS EOD chain used"` when the EOD chain was used; append any fallbacks (e.g. earnings-calendar web fallback, free-tier degradation).

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
- **Bundle dir** — `./td_bundle_<TICKER>_<YYYY-MM-DD>/`
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
- **Free-tier degradation (500 req/day).** Skip Step 4 IV-history sampling; note it in `meta.api_tier_notes`. `iv_pctile_1yr` will be `null` and is disclosed (never silently omitted).
- **`EARNINGS_CALENDAR` empty gotcha.** Header-only/empty for valid tickers is common → use the Step 3c web fallback.
- **`REALTIME_OPTIONS` tier limitation.** Requires a 600+/min tier. At 75/min the EOD `HISTORICAL_OPTIONS` chain is used and disclosed in `meta.api_tier_notes`.
- **Missing-field policy.** A missing figure becomes `null`, is listed in `meta.missing`, and downstream renormalizes around it — never silently dropped.
- **Outputs always under the invoker CWD.** `${CLAUDE_PLUGIN_ROOT}` locates the scripts; it is never a write target.
