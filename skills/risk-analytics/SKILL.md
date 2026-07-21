---
name: risk-analytics
description: Score a ticker's risk-reward conditions (volatility state, drawdown profile, margin of safety, event risk, tail risk, liquidity & solvency) against a versioned rubric, off an existing market-snapshot bundle, and build a downside map from the shared S/R ladder. Use when the user says "risk profile [ticker]", "downside map", "how risky is this entry", or when a report needs the risk evidence module. Consumes an existing snapshot bundle AND the technical-analysis module (runs technical-analysis first if its module is missing). Higher score = better risk-reward conditions. Rubric v1.1.0 (PROVISIONAL — event/tail weights unratified pending B9 calibration).
---

# Risk Analytics (Evidence Module)

Score the risk-reward dimension for one ticker from an already-built snapshot bundle. **All arithmetic is done by `scripts/score_risk.py`** — you run the script, read its small JSON, and write prose. You never compute a score, a percentage, a ratio, or a level in text.

**Higher score = BETTER conditions.** A high risk-analytics score means the *setup* is favorable (low volatility, shallow historical drawdowns, a real discount, tight downside vs. upside, deep liquidity, a strong balance sheet) — it does NOT mean the stock is dangerous. State this convention as a short note in the score HEADLINE line (not inside the ≤120-word paragraph, where it eats the word budget).

**Non-negotiables:**
- **Never do arithmetic in prose.** Every number you cite must already appear in `module_risk.json` or the snapshot. A number you would have to compute is a script change, not a prose change.
- **Single-snapshot rule.** This module never fetches market data. A figure missing from the snapshot is a *snapshot extension request*, not a fetch here.
- **The ladder is the only legal source of levels.** Every level in your downside map comes from `module_technical.json`'s `ladder` (built by `scripts/levels.py`), plus the two script-computed anchors: the valuation floor and the stress row. You never invent a level.
- **Stress scenario is a JUDGMENT input, never a computed one.** You pick the single top risk and the stress percentage; the *script* turns the percentage into a level. Never compute the stress level in prose.

Trigger phrases: "risk profile MU", "downside map for AAPL", "how risky is this entry on NVDA".

---

## Step 1 — Locate the snapshot bundle and require the technical module

In the invoker's CWD, find the newest bundle for the ticker:

```bash
ls -dt ./trading_desk_<TICKER>/detail_reports_* ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

Newest first across both layouts: the new `./trading_desk_<TICKER>/detail_reports_<date>/` bundles and the legacy `./td_bundle_<TICKER>_<date>/` bundles (fallback for old runs).

- **If a bundle exists**, confirm it holds a `snapshot_<TICKER>_*.json`.
- **If NO bundle exists**, invoke the `market-snapshot` skill for `<TICKER>` first, then continue with the bundle it produces. Do not attempt to fetch data here yourself.
- **Require `module_technical.json`.** This module reads the S/R ladder from the technical module. If `<bundle>/module_technical.json` is absent, invoke the `technical-analysis` skill for `<TICKER>` first (it mints the ladder). The scorer exits 2 with `run technical-analysis first (module_technical.json missing)` if you skip this.

---

## Step 2 — Decide the stress scenario (judgment)

Before running the script, decide whether to add a stress row to the downside map. This is a judgment call, made from the snapshot's catalyst/news evidence — never computed in prose:

1. **Name the single top risk.** Read the snapshot's `events` and `sentiment` (news summary, upcoming catalysts). Pick the ONE risk most likely to drive a sharp drawdown (e.g. an earnings miss, a demand air-pocket, a guidance cut). One risk, named plainly.
2. **Pick a stress percentage anchored to a downside-map level.** Choose a signed fraction (e.g. `-0.30`) that lands near a real ladder level below `last` — so the stress scenario reads against structure the snapshot already contains, not a round guess.

Both are fed to the script as flags; the script computes the stress *level*. If you have no concrete risk to name, run without `--stress-pct` — an unfounded stress number is worse than none.

---

## Step 3 — Run the scorer

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_risk.py \
  --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>
```

With a stress scenario (both flags required together):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/score_risk.py \
  --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD> \
  --stress-pct -0.30 \
  --top-risk "HBM demand air-pocket into the next print"
```

The script loads the newest snapshot, reads the ladder from `module_technical.json`, scores the six dimensions (volatility state, drawdown profile, margin of safety, liquidity & solvency, event risk, tail risk), builds the downside map (ladder entries below `last` + a valuation-floor row when computable + the stress row when flagged) and the volatility-profile context block, and writes `<bundle>/module_risk.json` (its path is printed to stdout). `--stress-pct` without `--top-risk` is a hard error (exit 2). Exit 2 also means the bundle/snapshot could not be read or the technical module is missing — fix that and re-run.

**When coverage anchors exist** (`./trading_desk_<TICKER>/coverage/valuation_anchors.json`), add:

```bash
  --anchors ./trading_desk_<TICKER>/coverage/valuation_anchors.json
```

The valuation floor then becomes the coverage DCF bear case (`dcf_bear`, basis `dcf_bear (coverage anchors)`), replacing the `pe_5yr_median × eps_ntm` floor entirely — the module records `downside_floor_mode: "dcf_bear"`. Without `--anchors`, the pe-median floor and its suspect-flag machinery are unchanged (`downside_floor_mode: "pe_median"`). A malformed anchors file is exit 2 naming the issue — fix the file, never fall back silently.

---

## Step 4 — Read the module JSON and write the brief

The module JSON is small — read it directly. Then write `<bundle>/brief_risk.md` with exactly these parts, in order:

1. **Score headline** — `## Risk Score: <score>/100 — <one-line conditions read>`. Copy `score` verbatim. If `renormalized` is true, add a one-line note quoting `renormalization_note`. State once, plainly, that higher = better conditions.
2. **A single paragraph, ≤120 words.** Cite ONLY numbers present in `module_risk.json` (the `subscores[].arithmetic` strings and `inputs`) or the snapshot. Zero computed-in-prose numbers. Walk the six dimensions (volatility state, drawdown profile, margin of safety, liquidity & solvency, event risk, tail risk), naming the points each earned and why, using the `arithmetic` strings as your source of truth. When `event_risk` or `tail_risk` is cited, note that these are risk-v1.1.0 PROVISIONAL factors (unratified pending B9 calibration).
3. **Downside-map mini-table** — the FIRST 5 rows of `tables.downside_map` — the map is emitted nearest-first, so rows 1-5 are the nearest anchors below `last` in the order price would fall through them, each with its `level`, `type`, and `basis`; include `pct_from_last` if you show a distance (quote it, never recompute), and the `risk` text on the stress row:

   | Level | Type | Basis | % from last |
   |-------|------|-------|-------------|
   | … | … | … | … |

4. **Correlation note vs SPY** — one line from `tables.vol_profile`: quote `beta`, `corr`, and `beta_n_days`. Correlation is context (it is NOT scored) — say so, so no reader treats it as a scored factor.
5. **One-line signal** — your single-sentence read of the risk-reward setup (e.g. "Volatility is calm and the balance sheet is net-cash, but the entry sits near the highs with thin downside asymmetry — size small until it pulls back to structure."). This is the ONLY place a signal appears; it is prose, never a number, and the JSON's `signal` field stays `null`.
6. **Footer** — `_Rubric v<rubric_version> · as of <as_of>_` using the JSON's fields.

---

## Step 5 — Output contract

Report to the user (and to any calling skill):
- **Module path** — `<bundle>/module_risk.json`
- **Brief path** — `<bundle>/brief_risk.md`
- **Score** — `<score>/100` (higher = better conditions)
- **Top downside anchor** — the nearest downside-map level below `last` and its type
- **Signal line** — the one-liner from the brief

---

## Governance Doctrine

**Risk is a gate/governor, never a reward input — conviction never loosens a risk parameter.**

A strong thesis on a name, a high sentiment score, or a compelling catalyst does NOT change what the risk rubric scores. The six dimensions (volatility state, drawdown profile, margin of safety, event risk, tail risk, liquidity & solvency) are computed from evidence that exists independently of conviction. A trader who "deserves" more size because they are very confident has misread what risk scores. Conviction is for position sizing *after* risk gates are satisfied, not a reason to move them.

---

## risk-v1.1.0 — PROVISIONAL, event-aware scoring (unratified)

**risk-v1.1.0 is PROVISIONAL and loudly disclosed.** Rubric v1.0.0 scored four factors (volatility state, drawdown profile, margin of safety, liquidity & solvency, at maxes 25/25/30/20). v1.1.0 trims each of those four by a symmetric −5 (to 20/20/25/15) to free 20 points for two NEW event/tail factors that make the near-term binary REAL scored evidence rather than prose-only context:

- **`event_risk` (max 12)** — from `events.days_to_event` × `events.implied_move_vs_own_history_pctile`. No dated near-term event (or > 30 days out) earns the full 12; a name days from earnings the market is pricing an above-its-own-history move earns as little as 2.
- **`tail_risk` (max 8)** — from `technicals.overnight_gap` (`excess_kurtosis` + `p95_abs`). Calm tails → 8, moderate → 5, violent → 2. When kurtosis is unmeasurable (n < 4, or no overnight-gap block) the factor is NOT evaluable and the score renormalizes over the remaining max — it is never silently zeroed.

The four re-weighted factors keep their v1.0.0 band **shapes** (thresholds) exactly; only the point **ceilings** scale down proportionally, so the ordering a name earns within each factor is unchanged. The re-weight, the two new factors' bands, and the sub-component splits all live in `scripts/score_risk.py` (the rubric of record).

**Why provisional (user philosophy "A"):** the event/tail weights + bands are a versioned, cited, falsifiable DEFAULT — shipped now, ratified only after the B9 calibration set (5–10 anchored names) runs. Until then `rubric_version` is `1.1.0` and the module note reads `risk-v1.1.0 PROVISIONAL — event/tail weights unratified pending B9 calibration; falsifier pre-registered`. **Confidence DEPTH for risk stays MEDIUM while provisional** (event-aware but unratified); it promotes to HIGH only on B9 ratification. An event-aware risk score that read HIGH before its calibration set confirmed it would be the exact dishonesty the confidence layer exists to prevent.

### Pre-registered falsifier (risk-v1.1.0)

> **If across the B9 calibration set event_risk does not separate historically-gappy names from calm ones, OR event_risk+tail_risk swing the composite grade by >1 letter where the other four factors agree it should not → refuted, re-set.**

If the calibration set refutes the weights/bands under this test, `risk-v1.1.0` is re-set (weights/bands revised, version re-bumped) — the provisional default is discarded, not quietly kept. The methodology page prints this falsifier alongside the provisional flag.

---

## Important Notes

- **Higher score means better conditions.** This module scores risk-*reward*, not danger. A near-100 score is a calm, discounted, asymmetric, liquid, cash-rich setup. State the convention in the brief so it can never be read backwards.
- **Single-snapshot rule.** No fetching. A missing figure (e.g. `null` beta) contributes 0 to its component and is named "n/a" in the arithmetic; if an entire dimension is null, the script renormalizes the score over the remaining max and sets `renormalized: true` — disclose that, never hide it.
- **PT-upside is scored elsewhere (single-mapping).** Consensus analyst-target upside is scored in the sentiment-positioning module's street view, NOT here — even though the design spec (§5.3) listed it in both. Listing one fact in two modules violates the spec's own single-mapping rule ("each snapshot fact scores in exactly one module"). We resolve it by scoring PT-upside only in sentiment; the ~10 points it would have carried here are reallocated into the asymmetry component (15 at v1.1.0, was 18) and distance-from-ATH (10 at v1.1.0, was 12), which already express margin of safety without double-counting the analyst target. Do not narrate consensus-target upside as a risk-analytics factor.
- **Ladder is the only legal source of levels.** The downside map's structural rows all come from `module_technical.json`'s `ladder`. The valuation floor (`dcf_bear` from coverage anchors when `--anchors` is passed; otherwise `pe_5yr_median × eps_ntm`) and the stress row (`last × (1 + stress_pct)`) are the only two script-computed anchors, and both carry explicit `type`/`basis` provenance. No other level may appear in the brief.
- **Correlation is context, not a score.** `corr` (and `beta_n_days`) sit in `vol_profile` for the SPY correlation note; only `beta` feeds the volatility-state score. Do not treat correlation as a scored factor.
- **Rubric version travels with the numbers.** The rubric version (`1.1.0`, PROVISIONAL) is printed in the module JSON and MUST appear in the brief footer, so any reader can tell which scoring rule produced the score. When the module note carries the `risk-v1.1.0 PROVISIONAL …` disclosure, surface it — a provisional rubric must never read as settled.
- **Snapshot is read-only.** This module never edits `snapshot.json`.
- **`top_risk` judgment and `event_context`:** The module JSON includes `tables.event_context` — `{days_to_event, implied_move, implied_move_vs_own_history_pctile, earnings_move_history_summary}` — surfaced verbatim from the snapshot. As of **risk-v1.1.0** two of these (`days_to_event` and `implied_move_vs_own_history_pctile`) are now SCORED by the `event_risk` factor; `implied_move` and the `earnings_move_history` list remain pure disclosure context. When writing the `top_risk` judgment (the single named risk for `--top-risk`), you MUST cite this context when an event is imminent: **if `events.days_to_event ≤ 30`, name the event explicitly** (e.g. "earnings 2026-07-24") **and cite the `event_context` figures** — specifically `implied_move` (the market's priced move) and `implied_move_vs_own_history_pctile` (how that compares to this ticker's own history). Example: "earnings binary 12d out; market pricing ±8.2% move (74th pctile vs own history) — size for the binary." An event within 30 days that goes unnamed in `top_risk` is an omission error. The data is now in the module; use it.
- **`downside_floor_mode` and long-horizon anchors.** When `downside_floor_mode` is `"dcf_bear"` or the floor row carries `basis: "long-horizon anchor (not a swing level)"`, note in the brief that this anchor is a multi-year DCF floor — it is a valuation reference, NOT an actionable swing level. A trader should not size or stop around it as if it were near-term structure.
